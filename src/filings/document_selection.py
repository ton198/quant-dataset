"""Deterministic SEC filing inventory and conservative document selection.

This module inventories an accession directory without writing it to disk. It retains
all indexed file metadata, chooses the primary document and recognized XBRL resources,
and only selects an exhibit as financial content when SEC description/type metadata is
strong evidence. Unknown exhibit material remains a candidate for later review.
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from .sec_client import (
    FetchResponse,
    SecClient,
    SecClientError,
    SecNotFoundError,
    validate_sec_url,
)

_APPROVED_FORMS = frozenset(
    {
        "10-K",
        "10-K/A",
        "10-Q",
        "10-Q/A",
        "20-F",
        "20-F/A",
        "40-F",
        "40-F/A",
        "6-K",
    }
)
_ACCESSION_RE = re.compile(r"^[0-9]{10}-[0-9]{2}-[0-9]{6}$", re.ASCII)
_CIK_RE = re.compile(r"^[0-9]{1,10}$", re.ASCII)
_EXHIBIT_RE = re.compile(r"^EX(?:HIBIT)?[- ]?\d", re.IGNORECASE)
_STRONG_FINANCIAL_PHRASES = (
    "financial statements",
    "consolidated financial statements",
    "consolidated statements",
    "interim financial statements",
    "interim condensed financial statements",
    "annual report",
    "quarterly report",
    "interim report",
    "financial results",
    "results of operations",
    "financial review",
    "earnings release",
    "earnings news release",
    "quarterly earnings",
    "return on equity and assets ratios",
    "annual financial report",
    "financial information",
)
_MDA_PHRASES = (
    "management's discussion and analysis",
    "management’s discussion and analysis",
    "management discussion and analysis",
    "md&a",
)
_MDA_PERIOD_CONTEXT = re.compile(
    r"\b(?:q[1-4]|quarter(?:ly)?|annual|interim|fiscal(?:\s+year)?|year\s+ended|20[0-9]{2})\b",
    re.IGNORECASE,
)
_NONFINANCIAL_PHRASES = (
    "material agreement",
    "credit agreement",
    "purchase agreement",
    "merger agreement",
    "employment agreement",
    "consulting agreement",
    "license agreement",
    "underwriting agreement",
    "debt agreement",
    "change in directors",
    "change in control",
    "change in auditors",
    "dividend declaration",
    "notice of meeting",
    "shareholder meeting",
    "amendment to agreement",
    "ceo and cfo certificates",
    "cfo certification",
    "ceo certification",
    "consent of independent registered public accounting firm",
    "code of conduct",
    "clawback policy",
    "compensation recovery policy",
)
_XBRL_TYPE_ROLES = {
    "INS": "xbrl_instance",
    "SCH": "schema",
    "PRE": "linkbase",
    "CAL": "linkbase",
    "DEF": "linkbase",
    "LAB": "linkbase",
}
_XBRL_FILENAME_SUFFIXES = (
    "_ins.xml",
    "_cal.xml",
    "_def.xml",
    "_lab.xml",
    "_pre.xml",
    "_tag.xml",
)


class FilingInventoryError(ValueError):
    """The SEC inventory does not safely describe the requested accession."""


class FilingInventoryMismatchError(FilingInventoryError):
    """The returned accession directory or detail page conflicts with its request."""


class FilingIndexParseError(FilingInventoryError):
    """The SEC filing detail page did not contain a usable document table."""


class UnsupportedFilingFormError(ValueError):
    """The form is outside the bounded supported filing scope."""


@dataclass(frozen=True, slots=True)
class DocumentCandidate:
    """One attachment listed by the SEC, selected or retained for later review."""

    filename: str
    url: str
    document_type: str | None
    description: str | None
    sequence: str | None
    role: str
    selected: bool
    selection_reason: str
    filing_id: str
    document_id: str
    selection_status: str
    inventory_metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DocumentPlan:
    """Document selection result plus the exact inventory responses it used."""

    candidates: tuple[DocumentCandidate, ...]
    selection_status: str
    reasons: tuple[str, ...]
    inventory_responses: tuple[FetchResponse, ...]
    form: str | None = None


@dataclass(slots=True)
class _DetailRow:
    cells: list[str]
    links: list[tuple[str, int]]


class _FilingTableParser(HTMLParser):
    """Extract SEC filing-table rows without loading an optional HTML library."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._table_depth = 0
        self._table_seen = False
        self._row: _DetailRow | None = None
        self._cell_index: int | None = None
        self._cell_parts: list[str] = []
        self._anchor_href: str | None = None
        self.rows: list[_DetailRow] = []

    @property
    def table_seen(self) -> bool:
        return self._table_seen

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        attrs_map = {key.casefold(): value for key, value in attrs}
        if tag == "table":
            if self._table_depth:
                self._table_depth += 1
            else:
                classes = (attrs_map.get("class") or "").casefold().split()
                if "tablefile" in classes:
                    self._table_seen = True
                    self._table_depth = 1
            return
        if not self._table_depth:
            return
        if tag == "tr":
            if self._row is not None:
                self._finish_row()
            self._row = _DetailRow(cells=[], links=[])
            self._cell_index = None
            self._cell_parts = []
        elif tag in {"td", "th"} and self._row is not None:
            self._finish_cell()
            self._cell_index = len(self._row.cells)
            self._cell_parts = []
        elif tag == "a" and self._row is not None:
            self._anchor_href = attrs_map.get("href")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if not self._table_depth:
            return
        if tag in {"td", "th"}:
            self._finish_cell()
        elif tag == "tr":
            self._finish_row()
        elif tag == "a":
            if self._row is not None and self._anchor_href and self._cell_index is not None:
                self._row.links.append((self._anchor_href, self._cell_index))
            self._anchor_href = None
        elif tag == "table":
            self._table_depth -= 1
            if not self._table_depth:
                self._finish_row()

    def handle_data(self, data: str) -> None:
        if self._table_depth and self._row is not None and self._cell_index is not None:
            self._cell_parts.append(data)

    def _finish_cell(self) -> None:
        if self._row is not None and self._cell_index is not None:
            text = " ".join(" ".join(self._cell_parts).split())
            self._row.cells.append(text)
            self._cell_index = None
            self._cell_parts = []

    def _finish_row(self) -> None:
        if self._row is None:
            return
        self._finish_cell()
        if self._row.cells or self._row.links:
            self.rows.append(self._row)
        self._row = None
        self._cell_index = None
        self._cell_parts = []
        self._anchor_href = None


@dataclass(slots=True)
class _PrimaryExhibitRow:
    ordinal: int
    line: int
    column: int
    cells: list[str] = field(default_factory=list)
    links: list[tuple[str, int, str]] = field(default_factory=list)


class _PrimaryExhibitHTMLParser(HTMLParser):
    """Collect table-row citation text and hrefs without resolving DTDs/entities."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[_PrimaryExhibitRow] = []
        self._row: _PrimaryExhibitRow | None = None
        self._row_ordinal = 0
        self._cell_index: int | None = None
        self._cell_parts: list[str] = []
        self._anchor_href: str | None = None
        self._anchor_cell: int | None = None
        self._anchor_parts: list[str] = []
        self._excluded_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        if tag in {"script", "style", "noscript", "template"}:
            self._excluded_depth += 1
            return
        if self._excluded_depth:
            return
        attributes = {key.casefold(): value for key, value in attrs}
        if tag == "tr":
            if self._row is not None:
                self._finish_row()
            self._row_ordinal += 1
            line, column = self.getpos()
            self._row = _PrimaryExhibitRow(self._row_ordinal, line, column)
            self._cell_index = None
            self._cell_parts = []
        elif tag in {"td", "th"} and self._row is not None:
            self._finish_cell()
            self._cell_index = len(self._row.cells)
            self._cell_parts = []
        elif tag == "a" and self._row is not None and self._cell_index is not None:
            href = attributes.get("href")
            if href is not None:
                self._anchor_href = href
                self._anchor_cell = self._cell_index
                self._anchor_parts = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag in {"script", "style", "noscript", "template"} and self._excluded_depth:
            self._excluded_depth -= 1
            return
        if self._excluded_depth:
            return
        if tag in {"td", "th"}:
            self._finish_cell()
        elif tag == "a":
            if (
                self._row is not None
                and self._anchor_href is not None
                and self._anchor_cell is not None
            ):
                self._row.links.append(
                    (
                        self._anchor_href,
                        self._anchor_cell,
                        " ".join(" ".join(self._anchor_parts).split()),
                    )
                )
            self._anchor_href = None
            self._anchor_cell = None
            self._anchor_parts = []
        elif tag == "tr":
            self._finish_row()

    def handle_data(self, data: str) -> None:
        if self._excluded_depth or self._row is None or self._cell_index is None:
            return
        self._cell_parts.append(data)
        if self._anchor_href is not None:
            self._anchor_parts.append(data)

    def _finish_cell(self) -> None:
        if self._row is not None and self._cell_index is not None:
            self._row.cells.append(" ".join(" ".join(self._cell_parts).split()))
            self._cell_index = None
            self._cell_parts = []

    def _finish_row(self) -> None:
        if self._row is None:
            return
        self._finish_cell()
        if self._row.cells or self._row.links:
            self.rows.append(self._row)
        self._row = None
        self._cell_index = None
        self._cell_parts = []
        self._anchor_href = None
        self._anchor_cell = None
        self._anchor_parts = []


def _canonical_cik(cik: str) -> str:
    if not isinstance(cik, str) or not _CIK_RE.fullmatch(cik):
        raise ValueError("CIK must contain one to ten ASCII digits")
    return cik.zfill(10)


def _accession_digits(accession_number: str) -> str:
    if not isinstance(accession_number, str) or not _ACCESSION_RE.fullmatch(accession_number):
        raise ValueError("accession_number must match NNNNNNNNNN-NN-NNNNNN")
    return accession_number.replace("-", "")


def _safe_filename(filename: Any) -> str:
    if not isinstance(filename, str) or not filename:
        raise FilingInventoryError("SEC inventory contains an empty or non-string filename")
    if filename in {".", ".."} or ".." in filename:
        raise FilingInventoryError("SEC inventory contains a traversal-like filename")
    if filename.startswith(("/", "\\")) or "/" in filename or "\\" in filename:
        raise FilingInventoryError("SEC inventory filename is not a basename")
    if "\x00" in filename or any(ord(char) < 0x20 or ord(char) == 0x7F for char in filename):
        raise FilingInventoryError("SEC inventory filename contains control characters")
    if re.search(r"%(?![0-9a-fA-F]{2})", filename):
        raise FilingInventoryError("SEC inventory filename has malformed percent encoding")

    decoded = filename
    for _ in range(5):
        next_name = urllib.parse.unquote(decoded)
        if next_name == decoded:
            break
        decoded = next_name
    else:
        raise FilingInventoryError("SEC inventory filename has excessive nested encoding")
    if (
        decoded in {".", ".."}
        or ".." in decoded
        or "/" in decoded
        or "\\" in decoded
        or "\x00" in decoded
        or decoded.startswith(("/", "\\"))
    ):
        raise FilingInventoryError("SEC inventory filename contains encoded path traversal")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in decoded):
        raise FilingInventoryError("SEC inventory filename contains encoded control characters")
    return filename


def _safe_index_directory(value: Any, expected_cik: str, accession_digits: str) -> None:
    if not isinstance(value, str) or not value:
        raise FilingInventoryMismatchError("SEC index is missing its accession directory name")
    if "?" in value or "#" in value or "\\" in value:
        raise FilingInventoryMismatchError("SEC index directory name is not a safe path")
    decoded = urllib.parse.unquote(value)
    if decoded != value and (".." in decoded or "\\" in decoded or "\x00" in decoded):
        raise FilingInventoryMismatchError("SEC index directory name contains encoded traversal")
    parts = [part for part in decoded.split("/") if part]
    expected = ["Archives", "edgar", "data", str(int(expected_cik)), accession_digits]
    if parts != expected:
        raise FilingInventoryMismatchError(
            "SEC index directory does not match the requested CIK and accession"
        )


def _encoded_filename(filename: str) -> str:
    # Encode a raw basename exactly once. Apostrophes and non-ASCII characters are escaped.
    return urllib.parse.quote(filename, safe="-._~")


def _document_url(base_url: str, filename: str) -> str:
    return validate_sec_url(f"{base_url}{_encoded_filename(filename)}")


def _metadata_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        text = str(value).strip()
        return text or None
    return None


def _parse_index(payload: bytes, cik10: str, accession_digits: str) -> list[dict[str, Any]]:
    try:
        document = json.loads(payload.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise FilingInventoryError("SEC accession index is not valid UTF-8 JSON") from None
    if not isinstance(document, dict):
        raise FilingInventoryError("SEC accession index has an unexpected document shape")
    directory = document.get("directory")
    if not isinstance(directory, dict):
        raise FilingInventoryError("SEC accession index is missing its directory record")
    _safe_index_directory(directory.get("name"), cik10, accession_digits)
    items = directory.get("item")
    if not isinstance(items, list):
        raise FilingInventoryError("SEC accession index is missing its item list")

    unique: dict[str, dict[str, Any]] = {}
    ordered: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            raise FilingInventoryError("SEC accession index contains a malformed item")
        filename = _safe_filename(item.get("name"))
        item_type = item.get("type")
        if item_type is not None and not isinstance(item_type, (str, int, float)):
            raise FilingInventoryError("SEC accession index contains malformed item metadata")
        if filename.casefold() in {"index.json", "index.htm", "index.html"}:
            continue
        normalized = dict(item)
        normalized["name"] = filename
        prior = unique.get(filename)
        if prior is not None:
            if prior != normalized:
                raise FilingInventoryMismatchError(
                    "SEC accession index repeats a filename with conflicting metadata"
                )
            continue
        unique[filename] = normalized
        ordered.append(filename)
    return [unique[name] for name in ordered]


def _detail_filename_from_href(href: str, expected_directory_path: str) -> str:
    if not href or "\\" in href or "\x00" in href:
        raise FilingIndexParseError("SEC filing detail table contains an unsafe document link")
    try:
        parsed = urllib.parse.urlsplit(href)
        port = parsed.port
    except ValueError:
        raise FilingIndexParseError(
            "SEC filing detail table contains an invalid document link"
        ) from None

    if parsed.scheme and parsed.scheme.casefold() != "https":
        raise FilingIndexParseError("SEC filing detail table contains a non-HTTPS document link")
    if parsed.scheme and not parsed.netloc:
        raise FilingIndexParseError("SEC filing detail table contains an invalid document link")
    if parsed.username is not None or parsed.password is not None:
        raise FilingIndexParseError("SEC filing detail table link credentials are not allowed")
    if parsed.netloc and (parsed.hostname or "").casefold() not in {"www.sec.gov", "data.sec.gov"}:
        raise FilingIndexParseError("SEC filing detail table links outside SEC hosts")
    if port not in (None, 443):
        raise FilingIndexParseError("SEC filing detail table link uses a nonstandard HTTPS port")
    if "#" in href or parsed.fragment:
        raise FilingIndexParseError(
            "SEC filing detail table links with query/fragment are not accepted"
        )

    if parsed.path in {"/ix", "/ixviewer/doc/action"}:
        if not parsed.query or "&" in parsed.query or parsed.query.partition("=")[0] != "doc":
            raise FilingIndexParseError(
                "SEC filing detail viewer link must contain only one doc parameter"
            )
        try:
            pairs = urllib.parse.parse_qsl(
                parsed.query,
                keep_blank_values=True,
                strict_parsing=True,
                max_num_fields=2,
                errors="strict",
            )
        except ValueError:
            raise FilingIndexParseError(
                "SEC filing detail table contains an invalid viewer link"
            ) from None
        if len(pairs) != 1 or pairs[0][0] != "doc":
            raise FilingIndexParseError(
                "SEC filing detail viewer link must contain only one doc parameter"
            )

        decoded_path = pairs[0][1]
        if (
            not decoded_path.startswith("/")
            or decoded_path.startswith("//")
            or not expected_directory_path.endswith("/")
            or not decoded_path.startswith(expected_directory_path)
            or decoded_path.endswith("/")
            or "\\" in decoded_path
            or "\x00" in decoded_path
            or "%" in decoded_path
            or "?" in decoded_path
            or "#" in decoded_path
        ):
            raise FilingIndexParseError(
                "SEC filing detail document link leaves its accession directory"
            )
        raw_name = _safe_filename(decoded_path.rsplit("/", 1)[-1])
        if raw_name in {".", ".."} or decoded_path != f"{expected_directory_path}{raw_name}":
            raise FilingIndexParseError(
                "SEC filing detail document link leaves its accession directory"
            )
        return raw_name

    if parsed.query:
        raise FilingIndexParseError(
            "SEC filing detail table links with query/fragment are not accepted"
        )
    try:
        decoded_path = urllib.parse.unquote(parsed.path, errors="strict")
    except UnicodeDecodeError:
        raise FilingIndexParseError(
            "SEC filing detail table contains an invalid document link"
        ) from None
    if not decoded_path or decoded_path.endswith("/"):
        raise FilingIndexParseError("SEC filing detail table link has no filename")
    if "\x00" in decoded_path or "\\" in decoded_path or "%" in decoded_path:
        raise FilingIndexParseError("SEC filing detail table contains an unsafe document link")

    raw_name = _safe_filename(decoded_path.rsplit("/", 1)[-1])
    if "/" in decoded_path:
        if (
            not decoded_path.startswith("/")
            or decoded_path != f"{expected_directory_path}{raw_name}"
        ):
            raise FilingIndexParseError(
                "SEC filing detail document link leaves its accession directory"
            )
    return raw_name


def _parse_detail(
    payload: bytes,
    known_files: set[str],
    expected_directory_path: str,
) -> dict[str, tuple[str | None, str | None, str | None]]:
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = payload.decode("latin-1")
    parser = _FilingTableParser()
    try:
        parser.feed(text)
        parser.close()
    except Exception:
        raise FilingIndexParseError("SEC filing detail page could not be parsed safely") from None
    if not parser.table_seen:
        raise FilingIndexParseError("SEC filing detail page has no SEC document table")

    details: dict[str, tuple[str | None, str | None, str | None]] = {}
    for row in parser.rows:
        for href, link_cell in row.links:
            filename = _detail_filename_from_href(href, expected_directory_path)
            if filename not in known_files:
                # The index page may link to scripts or navigation controls; only names
                # listed in the independently validated directory inventory are documents.
                continue
            sequence = row.cells[0].strip() if row.cells else ""
            description = (
                row.cells[link_cell - 1].strip()
                if link_cell > 0 and link_cell - 1 < len(row.cells)
                else ""
            )
            document_type = (
                row.cells[link_cell + 1].strip() if link_cell + 1 < len(row.cells) else ""
            )
            info = (
                _metadata_text(sequence),
                _metadata_text(document_type),
                _metadata_text(description),
            )
            prior = details.get(filename)
            if prior is not None and prior != info:
                raise FilingInventoryMismatchError(
                    "SEC filing detail page repeats a filename with conflicting metadata"
                )
            details[filename] = info
    if not details:
        raise FilingIndexParseError(
            "SEC filing detail page has no document links matching its index"
        )
    return details


def _xbrl_role(
    filename: str, document_type: str | None, description: str | None = None
) -> str | None:
    type_text = (document_type or "").strip().upper().replace(" ", "")
    match = re.match(r"^EX-?101[.]([A-Z]+)(?:\b|$)", type_text)
    if match:
        return _XBRL_TYPE_ROLES.get(match.group(1))
    if (
        type_text == "XML"
        and " ".join((description or "").casefold().split()) == "extracted xbrl instance document"
    ):
        return "xbrl_instance"

    lower = filename.casefold()
    if lower.endswith(".xsd"):
        return "schema"
    if lower.endswith(_XBRL_FILENAME_SUFFIXES):
        suffix = next(suffix for suffix in _XBRL_FILENAME_SUFFIXES if lower.endswith(suffix))
        return "xbrl_instance" if suffix == "_ins.xml" else "linkbase"
    return None


def _strong_financial_evidence(description: str | None, *, form: str | None = None) -> str | None:
    desc = " ".join((description or "").casefold().split())
    if not desc or _clearly_nonfinancial(desc):
        return None
    phrase = next((phrase for phrase in _STRONG_FINANCIAL_PHRASES if phrase in desc), None)
    if phrase:
        return f"SEC exhibit description explicitly identifies {phrase}"
    if any(phrase in desc for phrase in _MDA_PHRASES):
        annual_or_quarterly_form = (form or "").upper() in {
            "10-K",
            "10-K/A",
            "10-Q",
            "10-Q/A",
            "20-F",
            "20-F/A",
            "40-F",
            "40-F/A",
        }
        if annual_or_quarterly_form or _MDA_PERIOD_CONTEXT.search(desc):
            return (
                "SEC exhibit description explicitly identifies management's discussion and "
                "analysis in annual/quarterly context"
            )
    return None


def _clearly_nonfinancial(description: str | None) -> bool:
    desc = (description or "").strip().casefold()
    return bool(desc) and any(phrase in desc for phrase in _NONFINANCIAL_PHRASES)


def _generic_exhibit(document_type: str | None, filename: str | None = None) -> bool:
    dtype = (document_type or "").strip().upper()
    if _EXHIBIT_RE.match(dtype):
        return True
    name = (filename or "").casefold()
    return bool(re.match(r"^(?:ex|exhibit)[-_. ]?99(?:[._-]|$)", name))


def _document_id(filing_id: str, source_url: str) -> str:
    payload = json.dumps(
        ["document-v1", filing_id, source_url],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _candidate_status(selected: bool, role: str, reason: str) -> str:
    if selected:
        return "required"
    if role == "other" and (
        reason.startswith("Outside selected document scope")
        or reason.startswith("SEC description identifies non-financial exhibit")
    ):
        return "out_of_scope"
    return "candidate"


def enumerate_documents(
    cik10: str,
    accession_number: str,
    *,
    primary_document: str,
    form: str,
    client: SecClient,
) -> DocumentPlan:
    """Fetch SEC directory/detail metadata and conservatively select useful documents.

    All directory file entries are represented by ``DocumentCandidate`` records. Only
    the primary document, recognized XBRL taxonomy files, and exhibits with explicit
    financial-description evidence are marked selected. This plan does not recursively
    retrieve external taxonomy dependencies or claim that every attachment is financial.
    """
    canonical_cik = _canonical_cik(cik10)
    accession_digits = _accession_digits(accession_number)
    normalized_form = form.strip().upper() if isinstance(form, str) else ""
    if normalized_form not in _APPROVED_FORMS:
        raise UnsupportedFilingFormError("form is outside the supported SEC filing scope")
    primary_document = _safe_filename(primary_document)

    directory_url = (
        f"https://www.sec.gov/Archives/edgar/data/{int(canonical_cik)}/{accession_digits}/"
    )
    index_url = validate_sec_url(directory_url + "index.json")
    index_response = client.get(index_url)
    items = _parse_index(index_response.body, canonical_cik, accession_digits)
    by_name = {item["name"]: item for item in items}
    if primary_document not in by_name:
        raise FilingInventoryMismatchError(
            "primary document is missing from the SEC accession index"
        )
    if not items:
        raise FilingInventoryError("SEC accession index contains no document files")

    detail_url = validate_sec_url(directory_url + f"{accession_number}-index.html")
    responses: list[FetchResponse] = [index_response]
    detail_available = True
    detail_reason: str | None = None
    try:
        detail_response = client.get(detail_url)
    except SecNotFoundError:
        detail_available = False
        detail_reason = "SEC filing detail page returned 404; exhibit descriptions are unavailable"
        details: dict[str, tuple[str | None, str | None, str | None]] = {}
    except SecClientError:
        raise
    else:
        responses.append(detail_response)
        details = _parse_detail(
            detail_response.body,
            set(by_name),
            urllib.parse.urlsplit(directory_url).path,
        )

    filing_id_value = f"{canonical_cik}:{accession_number}"
    selected: dict[str, tuple[str, str]] = {}
    review_reasons: list[str] = []
    informational_reasons: list[str] = []
    _, _, primary_description = details.get(primary_document, (None, None, None))
    primary_financial_evidence = _strong_financial_evidence(
        primary_description, form=normalized_form
    )

    # The primary filing document is always acquired, but is not by itself proof that
    # a 40-F or 6-K contains the complete financial statement content unless the SEC
    # document table explicitly describes that primary as financial.
    primary_reason = "Declared primary filing document"
    if primary_financial_evidence:
        primary_reason += f"; {primary_financial_evidence}"
    selected[primary_document] = ("primary", primary_reason)

    for filename in by_name:
        if filename == primary_document:
            continue
        _, document_type, description = details.get(filename, (None, None, None))
        if _strong_financial_evidence(description, form=normalized_form):
            continue
        role = _xbrl_role(filename, document_type, description)
        if role:
            selected[filename] = (
                role,
                "Recognized local XBRL instance/taxonomy attachment; no remote dependency fetched",
            )

    strong_financial: list[tuple[str, str]] = (
        [(primary_document, primary_financial_evidence)] if primary_financial_evidence else []
    )
    unknown_exhibits: list[str] = []
    nonfinancial_exhibits: list[str] = []
    weak_financial_names: list[str] = []
    uncertain_attachments: list[str] = []
    for filename in by_name:
        if filename == primary_document or filename in selected:
            continue
        _, document_type, description = details.get(filename, (None, None, None))
        evidence = _strong_financial_evidence(description, form=normalized_form)
        if evidence:
            strong_financial.append((filename, evidence))
            selected[filename] = ("exhibit", evidence)
            continue
        if _clearly_nonfinancial(description):
            nonfinancial_exhibits.append(filename)
            informational_reasons.append(f"{filename} is described as non-financial material")
            continue
        if _generic_exhibit(document_type, filename):
            unknown_exhibits.append(filename)
            continue
        # Filename tokens alone are deliberately not strong enough to select exhibits.
        lower_name = filename.casefold()
        if any(
            token in lower_name
            for token in ("financial", "annual-report", "quarterly-report", "earnings")
        ):
            weak_financial_names.append(filename)
            continue
        suffix = Path(filename).suffix.casefold()
        if suffix in {".htm", ".html", ".pdf", ".doc", ".docx", ".rtf", ".xml"}:
            uncertain_attachments.append(filename)

    for filename in unknown_exhibits:
        review_reasons.append(
            f"{filename} has an exhibit type but no explicit financial or non-financial description"
        )
    for filename in weak_financial_names:
        review_reasons.append(
            f"{filename} filename suggests financial content but SEC description/type "
            "is not conclusive"
        )
    for filename in uncertain_attachments:
        review_reasons.append(
            f"{filename} lacks a conclusive SEC description/type and remains an "
            "unselected attachment candidate"
        )

    if normalized_form in {"40-F", "40-F/A"} and not strong_financial:
        review_reasons.append(
            "40-F primary document may be a cover; no explicitly described financial "
            "exhibit was identified"
        )
    if normalized_form in {"6-K"}:
        if (
            not strong_financial
            and not unknown_exhibits
            and not weak_financial_names
            and not uncertain_attachments
        ):
            if nonfinancial_exhibits:
                informational_reasons.append(
                    "6-K exhibits are described as non-financial; no financial report was selected"
                )
            else:
                review_reasons.append(
                    "6-K financial content is not established by the primary document "
                    "or exhibit metadata"
                )
        elif not strong_financial:
            review_reasons.append(
                "6-K has no explicitly described financial exhibit; primary document "
                "alone is not treated as complete"
            )

    if not detail_available:
        review_reasons.append(detail_reason or "SEC filing detail metadata is unavailable")
    elif primary_document not in details:
        review_reasons.append("primary document has no matching row in the SEC filing detail table")
    for filename in by_name:
        if filename not in details and filename != "index.json":
            # The detail page's independent document table may omit data attachments;
            # their index.json metadata remains preserved and selection stays conservative.
            continue

    if normalized_form in {"10-K", "10-K/A", "10-Q", "10-Q/A", "20-F", "20-F/A"}:
        # These forms' primary filing is the bounded financial body scope. Unknown
        # optional attachments remain recorded candidates but do not downgrade it.
        plan_status = "selected_financial"
    elif review_reasons:
        plan_status = "needs_review"
    elif normalized_form == "6-K":
        plan_status = "selected_financial" if strong_financial else "non_financial"
    elif normalized_form in {"40-F", "40-F/A"}:
        plan_status = "selected_financial" if strong_financial else "needs_review"
    else:
        plan_status = "selected_financial"

    candidates: list[DocumentCandidate] = []
    for item in items:
        filename = item["name"]
        sequence, document_type, description = details.get(filename, (None, None, None))
        if filename in selected:
            role, selection_reason = selected[filename]
            is_selected = True
        else:
            role = _xbrl_role(filename, document_type, description) or "other"
            is_selected = False
            if _generic_exhibit(document_type, filename):
                selection_reason = (
                    "Unknown exhibit retained for review; not selected without clear "
                    "financial description"
                    if filename in unknown_exhibits
                    else (
                        "SEC description identifies non-financial exhibit; not selected "
                        "as financial content"
                    )
                )
            elif filename in weak_financial_names:
                selection_reason = (
                    "Filename-only financial hint is weak evidence; retained for review"
                )
            elif filename in uncertain_attachments:
                selection_reason = (
                    "Unclassified filing attachment retained as a candidate for review"
                )
            else:
                selection_reason = "Outside selected document scope; inventory metadata retained"

        source_url = _document_url(directory_url, filename)
        # Validate the client's public-API URL policy here too; candidate URLs are never
        # handed off with untrusted directory/item path material.
        validate_sec_url(source_url)
        candidates.append(
            DocumentCandidate(
                filename=filename,
                url=source_url,
                document_type=document_type,
                description=description,
                sequence=sequence,
                role=role,
                selected=is_selected,
                selection_reason=selection_reason,
                filing_id=filing_id_value,
                document_id=_document_id(filing_id_value, source_url),
                selection_status=_candidate_status(is_selected, role, selection_reason),
                inventory_metadata=dict(item),
            )
        )

    if not any(
        candidate.filename == primary_document and candidate.selected for candidate in candidates
    ):
        raise FilingInventoryMismatchError("primary filing document was not selected")

    reasons = tuple(dict.fromkeys(review_reasons + informational_reasons))
    if plan_status == "selected_financial" and not reasons:
        reasons = (
            "Financial filing body selected from its primary document"
            if normalized_form not in {"6-K", "40-F", "40-F/A"}
            else "Financial filing document explicitly identified by SEC metadata",
        )
    return DocumentPlan(
        candidates=tuple(candidates),
        selection_status=plan_status,
        reasons=reasons,
        inventory_responses=tuple(responses),
        form=normalized_form,
    )


_PRIMARY_EXHIBIT_NUMBER_RE = re.compile(
    r"^\s*(?:EX(?:HIBIT)?[\s-]*)?(99\.\d+)(?:\s+.*)?$", re.IGNORECASE
)
_PRIMARY_FINANCIAL_PHRASES = (
    "financial statements",
    "annual financial report",
    "interim financial report",
    "quarterly financial report",
    "financial review",
    "earnings release",
    "earnings news release",
    "financial results",
)
_PRIMARY_REPORT_PHRASES = ("annual report", "interim report", "quarterly report")
_PRIMARY_AUDIT_PHRASES = ("audit", "auditor", "review")
_PRIMARY_EVIDENCE_MAX_BYTES = 8 * 1024 * 1024
_PRIMARY_EVIDENCE_RULE = "primary-exhibit-title-v1"


def _primary_exhibit_number(row: _PrimaryExhibitRow) -> tuple[int, str] | None:
    for index, cell in enumerate(row.cells):
        match = _PRIMARY_EXHIBIT_NUMBER_RE.fullmatch(cell)
        if match:
            return index, match.group(1)
    return None


def _primary_financial_title(
    title: str, *, form: str | None = None, candidate_description: str | None = None
) -> str | None:
    folded = " ".join(title.casefold().split())
    context = " ".join(f"{folded} {candidate_description or ''}".casefold().split())
    if not folded or _clearly_nonfinancial(context):
        return None
    phrase = next((item for item in _PRIMARY_FINANCIAL_PHRASES if item in folded), None)
    if phrase:
        return phrase
    has_mda_title = any(item in folded for item in _MDA_PHRASES)
    annual_or_quarterly_form = (form or "").upper() in {
        "10-K",
        "10-K/A",
        "10-Q",
        "10-Q/A",
        "20-F",
        "20-F/A",
        "40-F",
        "40-F/A",
    }
    if has_mda_title and (annual_or_quarterly_form or _MDA_PERIOD_CONTEXT.search(context)):
        return "management's discussion and analysis"
    report = next((item for item in _PRIMARY_REPORT_PHRASES if item in folded), None)
    if report and any(item in folded for item in _PRIMARY_AUDIT_PHRASES):
        return report
    return None


def _index_item_proof(plan: DocumentPlan, filename: str) -> tuple[str, str, str]:
    index_responses = [
        response
        for response in plan.inventory_responses
        if urllib.parse.urlsplit(response.request_url).path.rsplit("/", 1)[-1].casefold()
        == "index.json"
    ]
    if len(index_responses) != 1:
        raise FilingInventoryMismatchError(
            "primary financial exhibit proof requires exactly one accession index.json source"
        )
    response = index_responses[0]
    try:
        index_document = json.loads(response.body.decode("utf-8-sig"))
        items = index_document["directory"]["item"]
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError):
        raise FilingInventoryMismatchError(
            "primary financial exhibit proof has an unreadable accession index"
        ) from None
    matches = (
        [
            position
            for position, item in enumerate(items)
            if isinstance(item, Mapping) and item.get("name") == filename
        ]
        if isinstance(items, list)
        else []
    )
    if len(matches) != 1:
        raise FilingInventoryMismatchError(
            "primary financial exhibit does not map to exactly one accession index item"
        )
    return (
        hashlib.sha256(response.body).hexdigest(),
        response.request_url,
        f"$.directory.item[{matches[0]}]",
    )


def refine_document_plan(
    plan: DocumentPlan,
    *,
    primary_response: FetchResponse,
) -> DocumentPlan:
    """Select only explicitly titled financial exhibits linked by a verified cover.

    The base inventory selector intentionally remains metadata-only and conservative.
    This second, pure step is limited to 6-K and 40-F cover-style filings and requires
    the already-verified primary response; it never fetches bytes itself.
    """
    if (plan.form or "").upper() not in {"6-K", "40-F", "40-F/A"}:
        return plan
    if not isinstance(primary_response.body, bytes):
        raise FilingInventoryMismatchError("primary filing response body is not bytes")
    primaries = [
        candidate
        for candidate in plan.candidates
        if candidate.role == "primary" and candidate.selected
    ]
    if len(primaries) != 1:
        raise FilingInventoryMismatchError(
            "primary exhibit refinement requires exactly one selected primary document"
        )
    primary = primaries[0]
    if primary_response.request_url != primary.url or primary_response.status != 200:
        raise FilingInventoryMismatchError(
            "primary exhibit proof does not match the selected primary response"
        )
    try:
        cik_token, accession_number = primary.filing_id.split(":", 1)
        canonical_cik = _canonical_cik(cik_token)
        accession_digits = _accession_digits(accession_number)
        directory_url = (
            f"https://www.sec.gov/Archives/edgar/data/{int(canonical_cik)}/{accession_digits}/"
        )
        expected_primary_url = _document_url(directory_url, primary.filename)
        requested = urllib.parse.urlsplit(validate_sec_url(primary.url))
        final = urllib.parse.urlsplit(validate_sec_url(primary_response.final_url))
        port = final.port
    except (TypeError, ValueError):
        raise FilingInventoryMismatchError(
            "primary exhibit response URL or filing identity is not a validated SEC "
            "accession source"
        ) from None
    expected_directory = urllib.parse.urlsplit(directory_url).path
    if (
        primary.filing_id != f"{canonical_cik}:{accession_number}"
        or primary.url != expected_primary_url
        or primary.document_id != _document_id(primary.filing_id, primary.url)
        or requested.scheme != "https"
        or requested.path.rsplit("/", 1)[0] + "/" != expected_directory
        or final.scheme != "https"
        or final.path != requested.path
        or final.query
        or final.fragment
        or port not in (None, 443)
    ):
        raise FilingInventoryMismatchError(
            "primary exhibit response left its exact SEC accession document URL"
        )
    index_responses = [
        response
        for response in plan.inventory_responses
        if urllib.parse.urlsplit(response.request_url).path.rsplit("/", 1)[-1].casefold()
        == "index.json"
    ]
    expected_inventory_urls = {
        directory_url + "index.json",
        directory_url + f"{accession_number}-index.html",
    }
    inventory_urls_valid = True
    for response in plan.inventory_responses:
        try:
            response_request = urllib.parse.urlsplit(validate_sec_url(response.request_url))
            response_final = urllib.parse.urlsplit(validate_sec_url(response.final_url))
        except (TypeError, ValueError):
            inventory_urls_valid = False
            break
        if (
            response.status != 200
            or response.request_url not in expected_inventory_urls
            or response_final.path != response_request.path
            or response_final.query
            or response_final.fragment
        ):
            inventory_urls_valid = False
            break
    if (
        len(index_responses) != 1
        or index_responses[0].request_url != directory_url + "index.json"
        or not inventory_urls_valid
        or sum(
            1
            for response in plan.inventory_responses
            if response.request_url == directory_url + f"{accession_number}-index.html"
        )
        > 1
    ):
        raise FilingInventoryMismatchError(
            "primary exhibit plan does not retain only this filing's exact accession inventory"
        )
    try:
        indexed_items = _parse_index(index_responses[0].body, canonical_cik, accession_digits)
    except FilingInventoryError as exc:
        raise FilingInventoryMismatchError(
            "primary exhibit plan accession index failed identity validation"
        ) from exc
    if any(
        candidate.filing_id != primary.filing_id
        or candidate.document_id != _document_id(candidate.filing_id, candidate.url)
        or candidate.filename not in {item["name"] for item in indexed_items}
        for candidate in plan.candidates
    ):
        raise FilingInventoryMismatchError(
            "primary exhibit plan candidate is not bound to the verified accession index"
        )
    source_hash = hashlib.sha256(primary_response.body).hexdigest()
    if len(primary_response.body) > _PRIMARY_EVIDENCE_MAX_BYTES:
        reason = (
            "Primary document exceeds the bounded primary-exhibit evidence parse limit; "
            "financial exhibit remains unverified"
        )
        return replace(
            plan,
            selection_status="needs_review",
            reasons=tuple(dict.fromkeys((*plan.reasons, reason))),
        )
    if not re.search(
        rb"<\s*(?:!doctype\s+html|html|body|table|tr)\b", primary_response.body[:8192], re.I
    ):
        reason = "Primary document is not recognizable HTML for exhibit evidence review"
        return replace(
            plan,
            selection_status="needs_review",
            reasons=tuple(dict.fromkeys((*plan.reasons, reason))),
        )

    try:
        text = primary_response.body.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = primary_response.body.decode("cp1252")
    parser = _PrimaryExhibitHTMLParser()
    try:
        parser.feed(text)
        parser.close()
    except Exception:
        reason = (
            "Primary HTML exhibit table could not be parsed safely; financial exhibit "
            "remains unverified"
        )
        return replace(
            plan,
            selection_status="needs_review",
            reasons=tuple(dict.fromkeys((*plan.reasons, reason))),
        )

    accession_directory = expected_directory
    candidate_by_filename: dict[str, DocumentCandidate] = {}
    for candidate in plan.candidates:
        try:
            candidate_url = validate_sec_url(candidate.url)
            parsed_candidate = urllib.parse.urlsplit(candidate_url)
            linked_name = _detail_filename_from_href(candidate.url, accession_directory)
        except (ValueError, FilingIndexParseError):
            raise FilingInventoryMismatchError(
                "primary exhibit refinement found a candidate outside the verified SEC accession"
            ) from None
        if (
            candidate.url
            != _document_url(f"https://www.sec.gov{accession_directory}", candidate.filename)
            or parsed_candidate.path.rsplit("/", 1)[0] + "/" != accession_directory
            or linked_name != candidate.filename
        ):
            raise FilingInventoryMismatchError(
                "primary exhibit candidate URL/filename does not match its accession inventory"
            )
        if candidate.filename in candidate_by_filename:
            raise FilingInventoryMismatchError(
                "primary exhibit refinement found duplicate accession candidates"
            )
        candidate_by_filename[candidate.filename] = candidate

    evidence_by_filename: dict[str, dict[str, Any]] = {}
    unresolved_primary_rows: list[str] = []
    for row in parser.rows:
        numbered = _primary_exhibit_number(row)
        if numbered is None:
            continue
        number_cell, exhibit_number = numbered
        title = " ".join(
            cell for index, cell in enumerate(row.cells) if index != number_cell and cell
        ).strip()
        linked_targets: set[str] = set()
        for href, _cell_index, _anchor_text in row.links:
            try:
                filename = _detail_filename_from_href(href, accession_directory)
            except FilingIndexParseError:
                raise FilingInventoryMismatchError(
                    "primary exhibit row contains a foreign, unsafe, or query-bearing href"
                ) from None
            if filename in candidate_by_filename:
                linked_targets.add(filename)
        title_candidate = (
            candidate_by_filename[next(iter(linked_targets))] if len(linked_targets) == 1 else None
        )
        phrase = _primary_financial_title(
            title,
            form=plan.form,
            candidate_description=(title_candidate.description if title_candidate else None),
        )
        if not phrase:
            if linked_targets and plan.selection_status == "non_financial":
                unresolved_primary_rows.append(
                    f"Primary Exhibit {exhibit_number} links to an indexed document but its title "
                    "does not establish financial statements or an "
                    "audited/reviewed annual/interim report"
                )
            continue
        if len(linked_targets) != 1:
            unresolved_primary_rows.append(
                f"Primary Exhibit {exhibit_number} explicitly names {phrase} but does not uniquely "
                "match one indexed attachment"
            )
            continue
        filename = next(iter(linked_targets))
        candidate = candidate_by_filename[filename]
        candidate_financial_evidence = _strong_financial_evidence(
            candidate.description, form=plan.form
        )
        already_selected_financial = (
            candidate.role == "exhibit"
            and candidate.selected
            and candidate.selection_status == "required"
            and candidate_financial_evidence is not None
        )
        promotable_candidate = (
            candidate.role == "other"
            and candidate.selection_status not in {"out_of_scope", "required"}
            and not candidate.selected
        )
        if (
            not (already_selected_financial or promotable_candidate)
            or Path(filename).suffix.casefold()
            not in {".htm", ".html", ".pdf", ".doc", ".docx", ".rtf"}
            or _clearly_nonfinancial(candidate.description)
        ):
            unresolved_primary_rows.append(
                f"Primary Exhibit {exhibit_number} financial title conflicts with the indexed "
                "attachment classification"
            )
            continue
        index_sha256, index_url, index_locator = _index_item_proof(plan, filename)
        source_locator = f"primary_html:tr[{row.ordinal}].cell[{number_cell + 1}]"
        proof = {
            "rule_version": _PRIMARY_EVIDENCE_RULE,
            "exhibit_number": exhibit_number,
            "description": title,
            "phrase": phrase,
            "source_locator": source_locator,
            "primary_source": {
                "source_url": primary_response.request_url,
                "source_sha256": source_hash,
                "line": row.line,
                "column": row.column,
            },
            "linked_href": next(
                href
                for href, _cell_index, _anchor_text in row.links
                if _detail_filename_from_href(href, accession_directory) == filename
            ),
            "matched_document": {"filename": filename, "source_url": candidate.url},
            "accession_index_source": {
                "source_url": index_url,
                "source_sha256": index_sha256,
                "locator": index_locator,
            },
        }
        previous = evidence_by_filename.get(filename)
        if previous is not None and previous != proof:
            raise FilingInventoryMismatchError(
                "primary HTML contains duplicate conflicting financial-exhibit citations"
            )
        evidence_by_filename[filename] = proof

    artifact_names = {
        f"{primary.filing_id.split(':', 1)[1]}-index.html",
        f"{primary.filing_id.split(':', 1)[1]}-index-headers.html",
    }
    new_candidates: list[DocumentCandidate] = []
    for candidate in plan.candidates:
        if candidate.filename in artifact_names and candidate.role == "other":
            new_candidates.append(
                replace(
                    candidate,
                    selection_status="out_of_scope",
                    selection_reason=(
                        "SEC accession index artifact; retained as inventory, not filing "
                        "financial content"
                    ),
                )
            )
            continue
        proof = evidence_by_filename.get(candidate.filename)
        if proof is None:
            new_candidates.append(candidate)
            continue
        metadata = dict(candidate.inventory_metadata)
        existing_proof = metadata.get("primary_exhibit_evidence")
        if existing_proof is not None and existing_proof != proof:
            raise FilingInventoryMismatchError(
                "primary exhibit evidence conflicts with an existing provenance record"
            )
        metadata["primary_exhibit_evidence"] = proof
        reason = (
            f"Selected by {_PRIMARY_EVIDENCE_RULE}: primary document description explicitly "
            f"identifies {proof['phrase']} in Exhibit {proof['exhibit_number']} "
            f"({proof['source_locator']})"
        )
        new_candidates.append(
            replace(
                candidate,
                role="exhibit",
                selected=True,
                selection_status="required",
                selection_reason=reason,
                inventory_metadata=metadata,
            )
        )

    resolved_filenames = set(evidence_by_filename)
    resolved_reasons: list[str] = []
    for reason in plan.reasons:
        if reason.startswith("SEC accession index artifact"):
            continue
        if any(
            reason.startswith(f"{filename} has an exhibit type but no explicit financial")
            or reason.startswith(f"{filename} lacks a conclusive SEC description/type")
            for filename in resolved_filenames | artifact_names
        ):
            continue
        if evidence_by_filename and (
            reason.startswith("6-K has no explicitly described financial exhibit;")
            or reason.startswith("40-F primary document may be a cover;")
        ):
            continue
        if reason not in resolved_reasons:
            resolved_reasons.append(reason)

    resolved_financial = bool(evidence_by_filename)
    remaining_review = [
        reason
        for reason in resolved_reasons
        if not (
            reason.endswith("is described as non-financial material")
            or reason.startswith("6-K exhibits are described as non-financial;")
        )
    ]
    if unresolved_primary_rows:
        resolved_reasons.extend(
            reason for reason in unresolved_primary_rows if reason not in resolved_reasons
        )
        remaining_review.extend(unresolved_primary_rows)
    if resolved_financial and not remaining_review:
        status = "selected_financial"
    elif resolved_financial or unresolved_primary_rows:
        status = "needs_review"
    else:
        status = plan.selection_status

    return replace(
        plan,
        candidates=tuple(new_candidates),
        selection_status=status,
        reasons=tuple(resolved_reasons),
    )
