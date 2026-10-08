"""Parser-neutral SEC single-document envelope and byte-safety helpers.

The public extractor recognizes only one complete SEC SGML DOCUMENT/TEXT
wrapper. Its payload is returned as the original byte span; ``parse_text``
retains the historical private helper names as compatibility aliases.
"""

from __future__ import annotations

import io
import json
import re
from typing import Any

_SEC_HEADER_TAGS = frozenset(
    {"type", "sequence", "filename", "description", "mime", "content-type", "size", "md5"}
)
_SEC_HEADER_LINE_RE = re.compile(r"^\s*<([A-Za-z][A-Za-z0-9_-]*)>(.*)$")
_SEC_HTML_START_RE = re.compile(
    r"<\s*(?:!doctype\s+html|html|head|body|table|p|div|h[1-6])\b", re.IGNORECASE
)
_SEC_HTML_DOCUMENT_RE = re.compile(r"<\s*(?:!doctype\s+html\b|html\b|head\b|body\b)", re.IGNORECASE)
_SEC_SGML_PAGE_RE = re.compile(r"(?im)^\s*<PAGE>\s*$")
_SEC_SGML_TABLE_RE = re.compile(r"(?im)^\s*<TABLE>\s*$")
_SEC_SGML_CELL_RE = re.compile(r"(?im)^\s*<(?:S|C)>\s*$")
_SEC_PDF_TEXT_LINE_RE = re.compile(
    rb"^[ \t]*<TEXT>[ \t]*(?:\r\n|\r|\n)", re.IGNORECASE | re.MULTILINE
)

def sec_envelope_encoding(raw: bytes) -> tuple[str, int] | None:
    """Return a safely decodable candidate encoding for a leading SEC envelope."""
    candidates: list[tuple[str, int]] = []
    if raw.startswith(b"\xef\xbb\xbf"):
        candidates.append(("utf-8", 3))
    elif raw.startswith(b"\xff\xfe\x00\x00"):
        candidates.append(("utf-32-le", 4))
    elif raw.startswith(b"\x00\x00\xfe\xff"):
        candidates.append(("utf-32-be", 4))
    elif raw.startswith(b"\xff\xfe"):
        candidates.append(("utf-16-le", 2))
    elif raw.startswith(b"\xfe\xff"):
        candidates.append(("utf-16-be", 2))
    else:
        prefix = raw.lstrip(b" \t\r\n\v\f")
        if prefix[:9].upper() == b"<DOCUMENT":
            candidates.append(("utf-8", 0))
        else:
            return None

    encoding, bom_length = candidates[0]
    sample = raw[bom_length : bom_length + 1024]
    unit = 4 if encoding.startswith("utf-32") else 2 if encoding.startswith("utf-16") else 1
    sample = sample[: len(sample) - (len(sample) % unit)]
    try:
        decoded_sample = sample.decode(encoding)
    except UnicodeDecodeError:
        # A candidate with a damaged opening marker still needs a fail-closed
        # envelope diagnostic instead of falling through to ordinary HTML.
        marker = {
            "utf-8": b"<DOCUMENT",
            "utf-16-le": b"<\x00D\x00O\x00C\x00U\x00M\x00E\x00N\x00T\x00",
            "utf-16-be": b"\x00<\x00D\x00O\x00C\x00U\x00M\x00E\x00N\x00T",
            "utf-32-le": (
                b"<\x00\x00\x00D\x00\x00\x00O\x00\x00\x00"
                b"C\x00\x00\x00U\x00\x00\x00M\x00\x00\x00"
                b"E\x00\x00\x00N\x00\x00\x00T\x00\x00\x00"
            ),
            "utf-32-be": (
                b"\x00\x00\x00<\x00\x00\x00D\x00\x00\x00O\x00\x00\x00"
                b"C\x00\x00\x00U\x00\x00\x00M\x00\x00\x00"
                b"E\x00\x00\x00N\x00\x00\x00T"
            ),
        }[encoding]
        if sample.lstrip().upper().startswith(marker):
            return encoding, bom_length
        if sample.decode(encoding, errors="ignore").lstrip().casefold().startswith("<document"):
            return encoding, bom_length
        return None
    if decoded_sample.lstrip().casefold().startswith("<document"):
        return encoding, bom_length
    return None


def is_complete_sec_pdf_envelope(raw: bytes) -> bool:
    """Recognize a complete ASCII SEC wrapper around binary PDF bytes only."""
    bom_length = 3 if raw.startswith(b"\xef\xbb\xbf") else 0
    header_start = bom_length
    while header_start < len(raw) and raw[header_start] in b" \t\r\n\v\f":
        header_start += 1
    document_marker = b"<document>"
    if raw[header_start : header_start + len(document_marker)].lower() != document_marker:
        return False
    text_match = _SEC_PDF_TEXT_LINE_RE.search(
        raw, header_start, min(len(raw), header_start + 16384)
    )
    if text_match is None:
        return False
    payload_probe = text_match.end()
    while payload_probe < len(raw) and raw[payload_probe] in b" \t\r\n\v\f":
        payload_probe += 1
    if raw[payload_probe : payload_probe + 5] != b"%PDF-":
        return False

    lines: list[tuple[int, int, bytes]] = []
    cursor = 0
    for line in raw.splitlines(keepends=True):
        end = cursor + len(line)
        marker = line.strip()
        if cursor == 0 and bom_length:
            marker = marker[3:].strip()
        lines.append((cursor, end, marker))
        cursor = end
    if cursor < len(raw):
        marker = raw[cursor:].strip()
        if cursor == 0 and bom_length:
            marker = marker[3:].strip()
        lines.append((cursor, len(raw), marker))
    if not lines:
        return False

    markers = [line[2].lower() for line in lines]
    if markers.count(b"<document>") != 1 or markers.count(b"</document>") != 1:
        return False
    if markers.count(b"<text>") != 1 or markers.count(b"</text>") != 1:
        return False

    first_content = next((index for index, marker in enumerate(markers) if marker), None)
    if first_content is None or markers[first_content] != b"<document>":
        return False

    metadata: set[bytes] = set()
    metadata_bytes = 0
    type_present = False
    text_line_index: int | None = None
    for index in range(first_content + 1, len(lines)):
        marker = markers[index]
        if not marker:
            continue
        if marker == b"<text>":
            text_line_index = index
            break
        match = re.fullmatch(rb"<([A-Za-z][A-Za-z0-9_-]*)>(.*)", marker)
        if match is None:
            return False
        tag, value = match.group(1).lower(), match.group(2).strip()
        if tag not in {item.encode("ascii") for item in _SEC_HEADER_TAGS} or tag in metadata:
            return False
        if len(value) > 4096:
            return False
        metadata.add(tag)
        metadata_bytes += len(tag) + len(value)
        if metadata_bytes > 8192:
            return False
        if tag == b"type" and value:
            type_present = True
    if text_line_index is None or not type_present:
        return False

    payload_start = lines[text_line_index][1]
    if not raw[payload_start:].lstrip(b" \t\r\n\v\f").startswith(b"%PDF-"):
        return False
    text_end_line = markers.index(b"</text>")
    if text_end_line <= text_line_index:
        return False
    document_end_line = markers.index(b"</document>")
    if document_end_line <= text_end_line:
        return False
    if any(markers[text_end_line + 1 : document_end_line]):
        return False
    return all(not marker for marker in markers[document_end_line + 1 :])


def _sec_envelope_payload_kind(probe: str) -> str:
    """Distinguish HTML from preserved legacy text without XML-parsing SGML."""
    if _SEC_HTML_DOCUMENT_RE.search(probe):
        return "html"
    if _SEC_SGML_PAGE_RE.search(probe) or (
        _SEC_SGML_TABLE_RE.search(probe) and _SEC_SGML_CELL_RE.search(probe)
    ):
        return "legacy_text"
    return "html" if _SEC_HTML_START_RE.search(probe) else "legacy_text"


def extract_sec_envelope(raw: bytes) -> dict[str, Any] | None:
    """Validate and expose a single SEC SGML DOCUMENT/TEXT payload in memory.

    This intentionally supports only a narrowly bounded, single-document
    wrapper. It does not extract the first HTML fragment from arbitrary SGML.
    The returned offsets are byte offsets into the unchanged original source.

    Return ``None`` when the bytes do not begin with a recognized envelope;
    raise ``ValueError`` when a detected envelope is malformed or unsafe.
    Otherwise return the legacy ``dict[str, Any]`` shape with exactly these
    fields: ``payload`` (original payload bytes), ``payload_kind`` (``html`` or
    ``legacy_text``), ``encoding`` (decoder name), ``used_fallback`` (bool),
    ``metadata`` (header-name to value mapping), ``start_byte`` and
    ``end_byte`` (the half-open payload span in the unchanged source bytes).
    """
    encoding_info = sec_envelope_encoding(raw)
    if encoding_info is None:
        return None
    encoding, bom_length = encoding_info

    try:
        if encoding == "utf-8":
            decoded = raw[bom_length:].decode("utf-8")
            used_fallback = False
        else:
            decoded = raw[bom_length:].decode(encoding)
            used_fallback = False
    except UnicodeDecodeError as exc:
        if encoding != "utf-8":
            raise ValueError(f"SEC envelope cannot be decoded safely as {encoding}: {exc}") from exc
        try:
            decoded = raw[bom_length:].decode("cp1252")
        except UnicodeDecodeError as fallback_exc:
            raise ValueError(
                f"SEC envelope cannot be decoded safely as UTF-8 or windows-1252: {fallback_exc}"
            ) from fallback_exc
        encoding = "cp1252"
        used_fallback = True

    stream = io.StringIO(decoded)
    byte_cursor = bom_length
    opening_found = False
    for line in stream:
        line_bytes = len(line.encode(encoding))
        byte_cursor += line_bytes
        if not line.strip():
            continue
        if line.strip().casefold() == "<document>":
            opening_found = True
            break
        raise ValueError("SEC envelope has non-whitespace content before <DOCUMENT>.")
    if not opening_found:
        raise ValueError("SEC envelope is missing its opening <DOCUMENT> marker.")

    metadata: dict[str, str] = {}
    text_start_byte: int | None = None
    html_probe: list[str] = []
    probe_char_count = 0
    for line in stream:
        byte_cursor += len(line.encode(encoding))
        marker = line.rstrip("\r\n")
        stripped = marker.strip()
        if not stripped:
            continue
        if stripped.casefold() == "<text>":
            text_start_byte = byte_cursor
            break
        match = _SEC_HEADER_LINE_RE.fullmatch(marker)
        if match is None:
            raise ValueError("SEC envelope contains unrecognized content before <TEXT>.")
        tag = match.group(1).casefold()
        value = match.group(2).strip()
        if tag not in _SEC_HEADER_TAGS:
            raise ValueError(f"SEC envelope contains unsupported header tag <{tag.upper()}>.")
        if tag in metadata:
            raise ValueError(f"SEC envelope repeats the <{tag.upper()}> header field.")
        if len(value) > 4096:
            raise ValueError(f"SEC envelope <{tag.upper()}> metadata exceeds the supported limit.")
        metadata[tag] = value
        if sum(len(key) + len(item) for key, item in metadata.items()) > 8192:
            raise ValueError("SEC envelope metadata exceeds the supported limit.")
    if text_start_byte is None:
        raise ValueError("SEC envelope is missing its opening <TEXT> marker.")
    if not metadata.get("type", "").strip():
        raise ValueError("SEC envelope is missing a non-empty <TYPE> header field.")

    # A closing marker is accepted only if it is followed by the one allowed
    # outer footer and whitespace through EOF. This rejects truncated or
    # multi-document submissions instead of silently taking their first body.
    possible_footers: list[dict[str, int | bool]] = []
    valid_footers: list[dict[str, int | bool]] = []
    text_end_byte: int | None = None
    outer_document_followed_by_content = False
    for line in stream:
        line_start = byte_cursor
        byte_cursor += len(line.encode(encoding))
        marker = line.rstrip("\r\n")
        stripped = marker.strip()

        for footer in possible_footers:
            if footer["state"] == 0:
                if not stripped:
                    continue
                if stripped.casefold() == "</document>":
                    footer["state"] = 1
                else:
                    footer["state"] = -1
            elif stripped:
                outer_document_followed_by_content = True
                footer["state"] = -1
        possible_footers = [footer for footer in possible_footers if footer["state"] != -1]

        if probe_char_count < 8192 and stripped.casefold() != "</text>":
            html_probe.append(line)
            probe_char_count += len(line)

        if stripped.casefold() == "</text>":
            possible_footers.append({"state": 0, "end_byte": line_start})

    if outer_document_followed_by_content:
        raise ValueError(
            "SEC envelope contains non-whitespace content after its </DOCUMENT> footer."
        )
    valid_footers = [footer for footer in possible_footers if footer["state"] == 1]
    if len(valid_footers) != 1:
        raise ValueError(
            "SEC envelope must contain exactly one complete </TEXT></DOCUMENT> footer "
            "and whitespace only through EOF."
        )
    text_end_byte = int(valid_footers[0]["end_byte"])
    if text_end_byte <= text_start_byte:
        raise ValueError("SEC envelope has an empty or invalid <TEXT> payload.")
    payload_kind = _sec_envelope_payload_kind("".join(html_probe))

    if byte_cursor != len(raw):
        raise ValueError("SEC envelope byte offsets could not be verified against the source.")
    payload = raw[text_start_byte:text_end_byte]
    try:
        if not payload.decode(encoding).strip():
            raise ValueError("SEC envelope <TEXT> payload is empty.")
    except UnicodeDecodeError as exc:
        raise ValueError(f"SEC envelope <TEXT> payload is not valid {encoding}: {exc}") from exc

    return {
        "payload": payload,
        "payload_kind": payload_kind,
        "encoding": encoding,
        "used_fallback": used_fallback,
        "metadata": metadata,
        "start_byte": text_start_byte,
        "end_byte": text_end_byte,
    }


def _sec_envelope_provenance(envelope: dict[str, Any]) -> str:
    payload_kind = envelope["payload_kind"]
    record = {
        "format": "sec_sgml_single_document",
        "metadata": envelope["metadata"],
        "text_payload_kind": payload_kind,
        "text_payload_start_byte": envelope["start_byte"],
        "text_payload_end_byte": envelope["end_byte"],
        "source_xpath_scope": (
            "HTML parser tree built from the TEXT payload byte span; not the "
            "original SGML wrapper DOM"
            if payload_kind == "html"
            else None
        ),
        "legacy_text_representation": (
            "Raw TEXT payload bytes decoded as text; line order and SGML presentation "
            "markers are retained without HTML DOM, heading XPath, or table-column inference."
            if payload_kind == "legacy_text"
            else None
        ),
    }
    return "; SEC SGML envelope provenance=" + json.dumps(
        record, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


__all__ = [
    "extract_sec_envelope",
    "is_complete_sec_pdf_envelope",
    "sec_envelope_encoding",
]
