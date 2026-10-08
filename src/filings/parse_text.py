"""Offline full-text extraction for filing HTML and plain-text documents."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from filings.parsing_models import ParseResult
from filings.source.sec_envelope import (
    _sec_envelope_provenance,
    extract_sec_envelope,
    is_complete_sec_pdf_envelope,
    sec_envelope_encoding,
)

# Preserve the former private import paths used by existing callers.
_extract_sec_envelope = extract_sec_envelope
_is_complete_sec_pdf_envelope = is_complete_sec_pdf_envelope
_sec_envelope_encoding = sec_envelope_encoding

PARSER_NAME = "filings.text"
PARSER_VERSION = "1.0.3"

_EXCLUDED_TAGS = {"script", "style", "noscript", "nav"}
_INLINE_XBRL_NAMESPACES = frozenset(
    {
        "http://www.xbrl.org/2013/inlineXBRL",
        "http://www.xbrl.org/2008/inlineXBRL",
    }
)
_INLINE_XBRL_METADATA_TAGS = frozenset({"header", "resources", "references", "hidden"})
_BLOCK_TAGS = {
    "address",
    "article",
    "aside",
    "blockquote",
    "br",
    "div",
    "dl",
    "dt",
    "dd",
    "fieldset",
    "figcaption",
    "figure",
    "footer",
    "form",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "header",
    "hr",
    "li",
    "main",
    "ol",
    "p",
    "section",
    "table",
    "tbody",
    "td",
    "th",
    "tr",
    "ul",
}
_HEADING_RE = re.compile(r"^h([1-6])$")
_META_CHARSET_RE = re.compile(rb"<meta[^>]+charset\s*=\s*['\"]?([A-Za-z0-9._-]+)", re.IGNORECASE)
_XML_ENCODING_RE = re.compile(rb"<\?xml[^>]+encoding\s*=\s*['\"]([^'\"]+)", re.IGNORECASE)


def _normalized(value: str) -> str:
    return " ".join(value.split())


def _inline_xbrl_metadata_tag(element: Any, tag: str) -> bool:
    if tag.startswith("{") and "}" in tag:
        namespace, local_name = tag[1:].split("}", 1)
        return (
            namespace in _INLINE_XBRL_NAMESPACES
            and local_name.casefold() in _INLINE_XBRL_METADATA_TAGS
        )
    if ":" not in tag:
        return False
    prefix, local_name = tag.split(":", 1)
    if local_name.casefold() not in _INLINE_XBRL_METADATA_TAGS:
        return False
    namespace_attribute = f"xmlns:{prefix}".casefold()
    current = element
    while current is not None:
        namespace = next(
            (
                value
                for name, value in current.attrib.items()
                if name.casefold() == namespace_attribute
            ),
            None,
        )
        if namespace is not None:
            return namespace in _INLINE_XBRL_NAMESPACES
        current = current.getparent()
    return False


def _hidden_by_inline_style(style: str) -> bool:
    last_display_value: str | None = None
    for declaration in style.split(";"):
        property_name, separator, value = declaration.partition(":")
        if separator and property_name.strip().casefold() == "display":
            last_display_value = value.strip().casefold()
    return last_display_value == "none"


def _excluded(element: Any) -> bool:
    raw_tag = str(element.tag) if isinstance(element.tag, str) else ""
    tag = raw_tag.casefold()
    if tag in _EXCLUDED_TAGS:
        return True
    if _inline_xbrl_metadata_tag(element, raw_tag):
        return True
    if any(name.casefold() == "hidden" for name in element.attrib):
        return True
    if _hidden_by_inline_style(element.get("style", "")):
        return True
    if element.get("role", "").strip().casefold() == "navigation":
        return True
    nav_tokens = re.compile(r"(?:^|[-_\s])(nav|navigation)(?:$|[-_\s])", re.IGNORECASE)
    return any(nav_tokens.search(element.get(attr, "")) for attr in ("id", "class"))


def _html_text(root: Any) -> str:
    """Extract text in document order, retaining readable table cell boundaries."""
    pieces: list[str] = []

    def visit(element: Any) -> None:
        if _excluded(element):
            return
        tag = str(element.tag).lower() if isinstance(element.tag, str) else ""
        if tag in _BLOCK_TAGS:
            pieces.append(" ")
        if element.text:
            pieces.append(element.text)
        for child in element:
            visit(child)
            if child.tail:
                pieces.append(child.tail)
        if tag in _BLOCK_TAGS:
            pieces.append(" ")

    visit(root)
    return _normalized("".join(pieces))


def _decode_text(raw: bytes) -> tuple[str, str, bool]:
    """Decode text with explicit BOM/UTF-8/windows-1252 behavior."""
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig"), "utf-8-sig", False
    if raw.startswith(b"\xff\xfe\x00\x00"):
        return raw.decode("utf-32"), "utf-32", False
    if raw.startswith(b"\x00\x00\xfe\xff"):
        return raw.decode("utf-32"), "utf-32", False
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16"), "utf-16", False
    try:
        return raw.decode("utf-8"), "utf-8", False
    except UnicodeDecodeError:
        return raw.decode("cp1252"), "windows-1252", True


def _html_encoding(raw: bytes) -> str:
    if raw.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    if raw.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        return "utf-32"
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "utf-16"
    if raw.startswith(b"\x00<\x00?"):
        return "utf-16-be"
    if raw.startswith(b"<\x00?\x00"):
        return "utf-16-le"
    if raw.startswith(b"\x00\x00\x00<"):
        return "utf-32-be"
    if raw.startswith(b"<\x00\x00\x00"):
        return "utf-32-le"
    for match in (_XML_ENCODING_RE.search(raw[:8192]), _META_CHARSET_RE.search(raw[:8192])):
        if match is not None:
            try:
                return match.group(1).decode("ascii")
            except UnicodeDecodeError:
                break
    return "utf-8"


def _sec_payload_html_encoding(payload: bytes, envelope_encoding: str) -> str:
    """Respect payload declarations, otherwise parse with its source encoding."""
    if payload.startswith((b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff", b"\x00\x00\xfe\xff")):
        return _html_encoding(payload)
    if _XML_ENCODING_RE.search(payload[:8192]) or _META_CHARSET_RE.search(payload[:8192]):
        return _html_encoding(payload)
    if envelope_encoding == "cp1252":
        return "windows-1252"
    return {
        "utf-16-le": "UTF-16LE",
        "utf-16-be": "UTF-16BE",
        "utf-32-le": "UTF-32LE",
        "utf-32-be": "UTF-32BE",
    }.get(envelope_encoding, envelope_encoding)


def _assert_no_symlink_components(path: Path) -> None:
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"symlink paths are not allowed: {current}")


def _safe_text_source(document_path: Path, allowed_root: Path | None) -> tuple[Path, Path]:
    path = Path(document_path).absolute()
    root_input = Path(allowed_root).absolute() if allowed_root is not None else path.parent
    _assert_no_symlink_components(path)
    _assert_no_symlink_components(root_input)
    root = root_input.resolve(strict=True)
    resolved = path.resolve(strict=True)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"source is outside allowed_root: {document_path}") from exc
    if not resolved.is_file():
        raise ValueError(f"source is not a regular file: {document_path}")
    return resolved, root


def _common(
    *,
    filing_id: str,
    document_id: str,
    parse_id: str,
    source_hash: str | None,
    status: str,
    document_hash: str | None,
    source_relpath: str | None,
    validation_scope: str = "not_applicable_text_extraction",
) -> dict[str, Any]:
    return {
        "filing_id": filing_id,
        "document_id": document_id,
        "parse_id": parse_id,
        "parser_name": PARSER_NAME,
        "parser_version": PARSER_VERSION,
        "status": status,
        "validation_scope": validation_scope,
        "source_hash": source_hash,
        "document_hash": document_hash,
        "source_relpath": source_relpath,
    }


def _failed_result(
    *,
    filing_id: str,
    document_id: str,
    parse_id: str,
    error_code: str,
    message: str,
    source_hash: str | None = None,
) -> ParseResult:
    return ParseResult(
        filing_id=filing_id,
        document_id=document_id,
        parse_id=parse_id,
        parser_name=PARSER_NAME,
        parser_version=PARSER_VERSION,
        status="failed",
        source_hash=source_hash,
        validation_scope="not_applicable_text_extraction",
        errors=[{"code": error_code, "message": message, "severity": "error"}],
    )


def parse_text(
    document_path: Path,
    *,
    filing_id: str,
    document_id: str,
    parse_id: str,
    allowed_root: Path | None = None,
    expected_hash: str | None = None,
) -> ParseResult:
    """Parse a local filing HTML or text document without network access.

    HTML output contains a full normalized document section plus best-effort
    heading sections.  The heading XPath and ordinal are structural anchors, not
    character offsets.  PDF and non-text binary documents are reported as
    unsupported rather than as empty filings.
    """
    try:
        path, source_root = _safe_text_source(document_path, allowed_root)
        raw = path.read_bytes()
    except (OSError, ValueError) as exc:
        return _failed_result(
            filing_id=filing_id,
            document_id=document_id,
            parse_id=parse_id,
            error_code="source_path_rejected"
            if isinstance(exc, ValueError)
            else "source_unreadable",
            message=str(exc),
        )

    source_hash = hashlib.sha256(raw).hexdigest()
    if expected_hash is not None and source_hash.lower() != expected_hash.lower():
        return _failed_result(
            filing_id=filing_id,
            document_id=document_id,
            parse_id=parse_id,
            error_code="source_hash_mismatch",
            message="Text source bytes do not match the caller-provided expected hash.",
            source_hash=source_hash,
        )
    source_relpath = path.relative_to(source_root).as_posix()
    if (
        path.suffix.lower() == ".pdf"
        or raw.startswith(b"%PDF-")
        or _is_complete_sec_pdf_envelope(raw)
    ):
        return ParseResult(
            filing_id=filing_id,
            document_id=document_id,
            parse_id=parse_id,
            parser_name=PARSER_NAME,
            parser_version=PARSER_VERSION,
            status="unsupported",
            source_hash=source_hash,
            validation_scope="not_applicable_text_extraction",
            errors=[
                {
                    "code": "unsupported_pdf",
                    "message": (
                        "PDF text payload is unsupported; original source bytes are retained "
                        "and no full text was claimed."
                    ),
                    "severity": "warning",
                }
            ],
        )
    if b"\x00" in raw and not raw.startswith((b"\xff\xfe", b"\xfe\xff", b"\x00\x00")):
        return ParseResult(
            filing_id=filing_id,
            document_id=document_id,
            parse_id=parse_id,
            parser_name=PARSER_NAME,
            parser_version=PARSER_VERSION,
            status="unsupported",
            source_hash=source_hash,
            validation_scope="not_applicable_text_extraction",
            errors=[
                {
                    "code": "unsupported_binary",
                    "message": "Binary input is not a supported text or HTML document.",
                    "severity": "warning",
                }
            ],
        )

    try:
        envelope = _extract_sec_envelope(raw)
    except ValueError as exc:
        return _failed_result(
            filing_id=filing_id,
            document_id=document_id,
            parse_id=parse_id,
            error_code="unsupported_envelope",
            message=str(exc),
            source_hash=source_hash,
        )
    html_source = envelope["payload"] if envelope is not None else raw

    common = _common(
        filing_id=filing_id,
        document_id=document_id,
        parse_id=parse_id,
        source_hash=source_hash,
        status="full",
        document_hash=source_hash,
        source_relpath=source_relpath,
    )
    errors: list[dict[str, Any]] = []
    sections: list[dict[str, Any]] = []
    envelope_provenance = _sec_envelope_provenance(envelope) if envelope is not None else ""
    if envelope is not None:
        looks_html = envelope["payload_kind"] == "html"
    else:
        looks_html = path.suffix.lower() in {".htm", ".html", ".xhtml"} or bool(
            re.search(
                rb"<\s*(?:!doctype\s+html|html|body|div|p|table|h[1-6])\b",
                raw[:4096],
                re.I,
            )
        )

    if envelope is not None and envelope["payload_kind"] == "legacy_text":
        try:
            legacy_text = html_source.decode(envelope["encoding"])
        except (UnicodeDecodeError, LookupError) as exc:
            return _failed_result(
                filing_id=filing_id,
                document_id=document_id,
                parse_id=parse_id,
                error_code="text_decode_failed",
                message=str(exc),
                source_hash=source_hash,
            )
        sections.append(
            {
                **common,
                "section_kind": "fulltext",
                "heading": None,
                "content_text": legacy_text.strip(),
                "source_xpath": None,
                "source_ordinal": 0,
                "heading_level": None,
                "confidence": "document",
                "raw_encoding": envelope["encoding"],
                "provenance": (
                    "raw_sec_sgml_presentation_text; enclosing payload boundary whitespace "
                    "trimmed only; presentation markers and line order retained; no HTML DOM, "
                    "heading XPath, or table-column semantics inferred; not financial-coverage "
                    "validation" + envelope_provenance
                ),
            }
        )
        if envelope["used_fallback"]:
            errors.append(
                {
                    "code": "encoding_fallback",
                    "message": (
                        "Invalid UTF-8 in SEC envelope decoded with the explicit "
                        "windows-1252 fallback."
                    ),
                    "severity": "warning",
                }
            )
    elif looks_html:
        try:
            from lxml import etree

            parser_source = html_source
            if envelope is not None and envelope["encoding"].startswith("utf-32"):
                # libxml2's HTML parser does not reliably build a tree from
                # UTF-32 byte input. Decode the validated payload view in memory;
                # recorded SEC byte offsets remain against the original source.
                parser_source = html_source.decode(envelope["encoding"]).encode("utf-8")
                html_encoding = "utf-8"
            else:
                html_encoding = (
                    _sec_payload_html_encoding(html_source, envelope["encoding"])
                    if envelope is not None
                    else _html_encoding(raw)
                )
            parser = etree.HTMLParser(
                encoding=html_encoding,
                recover=True,
                no_network=True,
                remove_comments=True,
            )
            root = etree.fromstring(parser_source, parser)
            tree = root.getroottree()
            full_text = _html_text(root)
            if envelope is not None:
                encoding = envelope["encoding"]
            else:
                encoding = tree.docinfo.encoding if tree.docinfo is not None else None
            sections.append(
                {
                    **common,
                    "section_kind": "fulltext",
                    "heading": None,
                    "content_text": full_text,
                    "source_xpath": tree.getpath(root),
                    "source_ordinal": 0,
                    "heading_level": None,
                    "confidence": "document",
                    "raw_encoding": encoding,
                    "provenance": (
                        "normalized_html_document_text; tables and visible inline XBRL "
                        "values retained; navigation/script/style, inline XBRL metadata, and "
                        "explicitly hidden elements "
                        "excluded; not browser-rendered or economic-coverage validation"
                        + (
                            "; source_xpath refers to the HTML TEXT payload DOM, not the original "
                            "SEC SGML envelope DOM" + envelope_provenance
                            if envelope is not None
                            else ""
                        )
                    ),
                }
            )

            active: list[dict[str, Any]] = []
            heading_records: list[dict[str, Any]] = []
            heading_ordinal = 0
            root_tree = root.getroottree()

            def visit_sections(element: Any) -> None:
                nonlocal heading_ordinal, active
                if _excluded(element):
                    return
                tag = str(element.tag).lower() if isinstance(element.tag, str) else ""
                if tag in _BLOCK_TAGS:
                    for item in active:
                        item["parts"].append(" ")
                heading_match = _HEADING_RE.match(tag)
                if heading_match:
                    level = int(heading_match.group(1))
                    active = [item for item in active if item["level"] < level]
                    heading_ordinal += 1
                    heading = _html_text(element)
                    item = {
                        "level": level,
                        "parts": [],
                        "heading": heading,
                        "xpath": root_tree.getpath(element),
                        "ordinal": heading_ordinal,
                    }
                    active.append(item)
                    heading_records.append(item)
                if element.text:
                    for item in active:
                        item["parts"].append(element.text)
                for child in element:
                    visit_sections(child)
                    if child.tail:
                        for item in active:
                            item["parts"].append(child.tail)
                if tag in _BLOCK_TAGS:
                    for item in active:
                        item["parts"].append(" ")

            visit_sections(root)
            for item in heading_records:
                sections.append(
                    {
                        **common,
                        "section_kind": "heading",
                        "heading": item["heading"],
                        "content_text": _normalized("".join(item["parts"])),
                        "source_xpath": item["xpath"],
                        "source_ordinal": item["ordinal"],
                        "heading_level": item["level"],
                        "confidence": "heuristic",
                        "raw_encoding": encoding,
                        "provenance": (
                            "heading-based heuristic section; XPath and ordinal are structural "
                            "anchors, not character offsets"
                            + (
                                "; source_xpath refers to the HTML TEXT payload DOM, "
                                "not the original SEC SGML envelope DOM" + envelope_provenance
                                if envelope is not None
                                else ""
                            )
                        ),
                    }
                )
            if parser.error_log:
                errors.extend(
                    {
                        "code": "html_recovery",
                        "message": str(entry),
                        "severity": "warning",
                        "line": getattr(entry, "line", None),
                    }
                    for entry in parser.error_log
                )
            if envelope is not None and envelope["used_fallback"]:
                errors.append(
                    {
                        "code": "encoding_fallback",
                        "message": (
                            "Invalid UTF-8 in SEC envelope decoded with the explicit "
                            "windows-1252 fallback."
                        ),
                        "severity": "warning",
                    }
                )
        except Exception as exc:
            return _failed_result(
                filing_id=filing_id,
                document_id=document_id,
                parse_id=parse_id,
                error_code="html_parse_failed",
                message=str(exc),
                source_hash=source_hash,
            )
    else:
        try:
            text, encoding, used_fallback = _decode_text(raw)
        except (UnicodeDecodeError, LookupError) as exc:
            return _failed_result(
                filing_id=filing_id,
                document_id=document_id,
                parse_id=parse_id,
                error_code="text_decode_failed",
                message=str(exc),
                source_hash=source_hash,
            )
        full_text = _normalized(text)
        sections.append(
            {
                **common,
                "section_kind": "fulltext",
                "heading": None,
                "content_text": full_text,
                "source_xpath": None,
                "source_ordinal": 0,
                "heading_level": None,
                "confidence": "document",
                "raw_encoding": encoding,
                "provenance": "normalized_plain_text; source bytes retained unchanged",
            }
        )
        if used_fallback:
            errors.append(
                {
                    "code": "encoding_fallback",
                    "message": "Invalid UTF-8 decoded with the explicit windows-1252 fallback.",
                    "severity": "warning",
                }
            )

    try:
        post_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        return _failed_result(
            filing_id=filing_id,
            document_id=document_id,
            parse_id=parse_id,
            error_code="source_integrity_changed",
            message=f"Text source became unreadable during parsing: {exc}",
            source_hash=source_hash,
        )
    if post_hash != source_hash or (
        expected_hash is not None and post_hash.lower() != expected_hash.lower()
    ):
        return _failed_result(
            filing_id=filing_id,
            document_id=document_id,
            parse_id=parse_id,
            error_code="source_integrity_changed",
            message="Text source bytes changed during parsing.",
            source_hash=source_hash,
        )

    status = "partial" if errors else "full"
    for row in sections:
        row["status"] = status
        row["provenance"] = str(row["provenance"])
    return ParseResult(
        filing_id=filing_id,
        document_id=document_id,
        parse_id=parse_id,
        parser_name=PARSER_NAME,
        parser_version=PARSER_VERSION,
        status=status,
        source_hash=source_hash,
        validation_scope="not_applicable_text_extraction",
        sections=sections,
        errors=errors,
    )
