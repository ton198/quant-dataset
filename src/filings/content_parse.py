"""Deterministic, bounded normalization of downloaded SEC filing documents."""

from __future__ import annotations

import hashlib
import html
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

from .content_models import CONTENT_CONTRACT, content_id
from .source import html_text
from .source.sec_envelope import extract_sec_envelope, is_complete_sec_pdf_envelope

PARSER_NAME = "filings.content"
PARSER_VERSION = "3.1.1"
MAX_PAYLOAD_BYTES = 128 * 1024 * 1024
MAX_NODES = 5_000_000
MAX_DEPTH = 512


@dataclass
class ContentResult:
    documents: list[dict[str, Any]]
    blocks: list[dict[str, Any]]


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _identity(
    filing: Mapping[str, Any],
    document: Mapping[str, Any],
    segment_index: int | None,
    raw_byte_size: int = 0,
) -> dict[str, Any]:
    return {
        "filing_id": f"{filing['cik10']}:{filing['accession_number']}",
        "document_id": document["document_id"],
        "segment_index": segment_index,
        "cik10": filing["cik10"],
        "accession_number": filing["accession_number"],
        "form": filing["form"],
        "filed_date": date.fromisoformat(filing["filed_date"]),
        "report_date": date.fromisoformat(filing["report_date"])
        if filing.get("report_date")
        else None,
        "document_role": document["role"],
        "segment_type": None,
        "segment_description": None,
        "original_filename": document.get("filename"),
        "raw_sha256": document.get("sha256"),
        "raw_byte_size": raw_byte_size,
        "payload_kind": "unknown",
        "encoding": None,
        "parser_name": PARSER_NAME,
        "parser_version": PARSER_VERSION,
        "content_contract": CONTENT_CONTRACT,
        "status": "unsupported",
        "diagnostic_codes_json": "[]",
        "limitations_json": "[]",
        "block_count": 0,
        "table_count": 0,
        "visible_text_chars": None,
        "hidden_excluded_count": 0,
        "css_hidden_rules_applied": 0,
        "css_rules_unsupported": 0,
    }


def _document_id_row(
    filing: Mapping[str, Any],
    document: Mapping[str, Any],
    segment_index: int | None,
    status: str,
    payload_kind: str,
    diagnostics: list[str],
    raw_byte_size: int = 0,
) -> dict[str, Any]:
    row = _identity(filing, document, segment_index, raw_byte_size)
    row.update(status=status, payload_kind=payload_kind, diagnostic_codes_json=_json(diagnostics))
    return row


class LegacyLayoutRejected(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _classify_payload(payload: bytes) -> str:
    outside_pre = re.sub(rb"(?is)<pre\b[^>]*>.*?</pre\s*>", b" ", payload)
    if re.search(rb"(?i)<t[rd]\b", outside_pre):
        return "html"
    if re.search(rb"(?i)<pre\b", payload) and re.search(
        rb"(?i)<(?:!doctype\s+html|html\b|body\b)", payload
    ):
        return "html_pre"
    if re.search(rb"(?m)^[ \t]*<(?:PAGE|TABLE|S|C)>[ \t]*\r?$", payload):
        return "legacy_text"
    if re.search(
        rb"(?i)<(?:!doctype\s+html|html\b|head\b|body\b|div\b|p(?=[\s>])|span\b|table\b|ix:[\w.-]+\b)",
        payload,
    ):
        return "html"
    return "legacy_text"


def _extract_pre_text(text: str) -> str:
    parts = []
    for match in re.finditer(r"(?is)<pre\b[^>]*>(.*?)</pre\s*>", text):
        part = re.sub(r"(?i)<br\s*/?>", "\n", match.group(1))
        part = re.sub(r"(?i)</?(?:b|i|u|font|a|span|strong|em|sup|sub)\b[^>]*>", "", part)
        if re.search(r"(?i)</?(?:table|tr|td|th|div|p|pre|html|body)\b", part):
            # Uppercase SGML TABLE delimiters are text, not nested HTML.
            structural = re.sub(r"(?m)^[ \t]*</?TABLE>[ \t]*\r?$", "", part)
            if re.search(r"(?i)</?(?:table|tr|td|th|div|p|pre|html|body)\b", structural):
                raise LegacyLayoutRejected("html_pre_nested_markup")
        parts.append(html.unescape(part))
    return "\n".join(parts)


def _year_header(fields: list[dict[str, Any]]) -> bool:
    return bool(fields) and all(_YEAR.fullmatch(f["raw"].strip()) for f in fields)


def _prose_line(fields: list[dict[str, Any]]) -> bool:
    return bool(fields) and not fields[-1]["value"] and any(f["value"] for f in fields)


_SECTION_HEADING = re.compile(
    r"(?i)(?:part\s+(?P<part>[ivx]+)\b.*|item\s+(?P<item>\d{1,2}[a-c]?)\s*[.:\-–—]?.*)"
)


def _section_match(text: str) -> re.Match[str] | None:
    return _SECTION_HEADING.fullmatch(text) if len(text) <= 200 else None


_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\ufeff"), None)
_FRAGMENTS = {"$", "%", "(", ")"}
_NUMBER_SHAPE = re.compile(r"[$(]*\s*[-+]?\s*\$?\s*(?:\d[\d,]*(?:\.\d+)?|\.\d+)\s*%?\s*\)?\s*%?")
_NOTE_MARK = re.compile(r"\(?\d{1,2}\)|\d{1,2}\.")
_PAGE_REF = re.compile(r"\d{1,3}(?:-\d{1,3})?")
_SECTION_CELL = re.compile(r"(?i)(?:item|part)\s")
_SGML_TAG = re.compile(r"(?i)<(?:S|C|/?TABLE|/?CAPTION|PAGE|/?FN)>")


def _number_shape(text: str) -> bool:
    return (
        bool(_NUMBER_SHAPE.fullmatch(text))
        and not _YEAR.fullmatch(text)
        and not _NOTE_MARK.fullmatch(text)
    )


def _data_table(rows: list[list[str]]) -> bool:
    texts = [[t.translate(_ZERO_WIDTH).strip() for t in row] for row in rows]
    cleaned = [[t for t in row if t and t not in _FRAGMENTS] for row in texts]
    one = two = 0
    numbers: list[str] = []
    for cells in cleaned:
        if not cells:
            continue
        found = [t for t in cells[1:] if _number_shape(t)]
        numbers.extend(found)
        if _number_shape(cells[0]):
            continue
        one += bool(found)
        two += len(found) >= 2
    marked = any(t.startswith(("$", "(")) or t.endswith("%") for t in numbers)
    if not (two or one >= 2 or (one and marked)):
        return False
    flat = [t for row in cleaned for t in row]
    contents = (
        any(_SECTION_CELL.match(t) for t in flat)
        and not any(t.startswith("$") for row in texts for t in row)
        and all(_PAGE_REF.fullmatch(t) for t in flat if _number_shape(t))
    )
    return not contents


class _Sections:
    def __init__(self) -> None:
        self.part: dict[str, str] | None = None
        self.item: dict[str, str] | None = None
        self.subs: list[dict[str, Any]] = []

    def heading(self, block_id: str, text: str, level: int | None) -> None:
        match = _section_match(text)
        if match and match.group("part"):
            self.part = {"block_id": block_id, "text": text}
            self.item = None
            self.subs = []
            return
        if match and match.group("item"):
            self.item = {"block_id": block_id, "text": text, "code": match.group("item").upper()}
            self.subs = []
            return
        if level is not None:
            self.subs = [sub for sub in self.subs if sub["level"] < level]
            self.subs.append({"block_id": block_id, "text": text, "level": level})
        return

    @property
    def item_code(self) -> str | None:
        return self.item["code"] if self.item else None

    def path_json(self) -> str:
        path = [
            {"block_id": section["block_id"], "text": section["text"]}
            for section in (self.part, self.item)
            if section
        ]
        path.extend({"block_id": sub["block_id"], "text": sub["text"]} for sub in self.subs[-4:])
        return _json(path)


_YEAR = re.compile(r"(?:19|20)\d\d")
_NUM = r"(?:\d[\d,]*(?:\.\d+)?|\.\d+)"
_PLACEHOLDER = {"-", "--", "---", "—", "–", "- - -", "$-", "$--", "$ -", "$ --", "n/a", "N/A"}
_VALUE_FIELD = re.compile(
    r"^\$?\s*\(?\$?\s*" + _NUM + r"\s*%?\s*\)?%?(?:/\d{1,2}/|\s*\([a-z1-9]\))?$"
)
_FIELD = re.compile(r"\S+(?: \S+)*")


def _is_value(raw: str) -> bool:
    stripped = raw.strip()
    return bool(_VALUE_FIELD.fullmatch(stripped)) or stripped in _PLACEHOLDER


def _fields(line: str) -> list[dict[str, Any]]:
    expanded = line.expandtabs(8)
    fields = []
    for match in _FIELD.finditer(expanded):
        raw = match.group()
        tokens = list(re.finditer(r"\S+", raw))
        if (
            len(tokens) > 1
            and not _is_value(raw)
            and all(_is_value(t.group()) or t.group() == "$" for t in tokens)
        ):
            fields.extend(
                {
                    "start": match.start() + t.start(),
                    "end": match.start() + t.end(),
                    "raw": t.group(),
                    "value": _is_value(t.group()),
                }
                for t in tokens
            )
        else:
            fields.append(
                {"start": match.start(), "end": match.end(), "raw": raw, "value": _is_value(raw)}
            )
    merged = []
    i = 0
    while i < len(fields):
        f = fields[i]
        if (
            f["raw"] in {"$", "("}
            and i + 1 < len(fields)
            and (
                fields[i + 1]["value"]
                or (f["raw"] == "(" and re.fullmatch(_NUM + r"\)", fields[i + 1]["raw"]))
            )
        ):
            raw = f["raw"] + " " + fields[i + 1]["raw"]
            merged.append(
                {
                    "start": f["start"],
                    "end": fields[i + 1]["end"],
                    "raw": raw,
                    "value": _is_value(raw),
                }
            )
            i += 2
        else:
            merged.append(f)
            i += 1
    return merged


def _parse_text_lines(
    text: str, filing: Mapping[str, Any], document: Mapping[str, Any], segment_index: int | None
) -> tuple[list[dict[str, Any]], list[str]]:
    lines = text.splitlines()
    blocks: list[dict[str, Any]] = []
    paragraph: list[tuple[int, str]] = []
    sections = _Sections()
    diagnostics: list[str] = []

    def _block(locator: str, kind: str, body: str, start: int | None, end: int | None) -> None:
        blocks.append(
            {
                **_identity(filing, document, segment_index),
                "block_id": content_id(
                    document["document_id"], document["sha256"], segment_index, "block", locator
                ),
                "ordinal": len(blocks),
                "block_kind": kind,
                "heading_path_json": sections.path_json(),
                "item_code": sections.item_code,
                "text": body,
                "text_sha256": hashlib.sha256(body.encode()).hexdigest(),
                "source_xpath": None,
                "line_start": start,
                "line_end": end,
            }
        )

    def flush() -> None:
        if not paragraph:
            return
        body = "\n".join(x[1] for x in paragraph)
        locator = f"L{paragraph[0][0]}-{paragraph[-1][0]}"
        _block(locator, "text", body, paragraph[0][0], paragraph[-1][0])
        paragraph.clear()

    def emit_table(start: int, end: int) -> None:
        _block(f"L{start}-{end}", "table", "\n".join(lines[start - 1 : end]), start, end)

    table_re = re.compile(r"(?i)^\s*<TABLE>\s*$")
    has_sgml = any(table_re.fullmatch(line) for line in lines)
    candidates = []
    for _, line in enumerate(lines, 1):
        fields = _fields(line)
        values = [f for f in fields if f["value"]]
        candidates.append(
            not has_sgml
            and not _year_header(fields)
            and not _prose_line(fields)
            and bool((fields and not fields[0]["value"] and values) or len(values) >= 2)
        )
    regions = []
    region = []
    blanks = 0
    for n, line in enumerate(lines, 1):
        if line.strip():
            blanks = 0
            if candidates[n - 1] or len(line.strip()) <= 100:
                region.append(n)
            elif region:
                regions.append(region)
                region = []
        else:
            blanks += 1
            if blanks >= 2:
                if region:
                    region = [n for n in region if lines[n - 1].strip()]
                    if region:
                        regions.append(region)
                    region = []
            elif region:
                region.append(n)
    if region:
        region = [n for n in region if lines[n - 1].strip()]
        if region:
            regions.append(region)
    plain: dict[int, int] = {}
    for region in regions:
        cand = [n for n in region if candidates[n - 1]]
        if (
            len(cand) < 3
            or sum(len([f for f in _fields(lines[n - 1]) if f["value"]]) >= 2 for n in cand) < 2
        ):
            continue
        rows = [[f["raw"] for f in _fields(lines[n - 1])] for n in region]
        if not _data_table(rows):
            continue
        plain[min(region)] = max(region)
    i = 0
    while i < len(lines):
        if i + 1 in plain:
            end = plain[i + 1]
            flush()
            emit_table(i + 1, end)
            i = end
            continue
        if lines[i].strip().upper() == "<PAGE>":
            i += 1
            continue
        if not table_re.fullmatch(lines[i]):
            stripped = lines[i].strip()
            if stripped and not paragraph and _section_match(stripped):
                flush()
                locator = f"L{i + 1}-{i + 1}"
                bid = content_id(
                    document["document_id"], document["sha256"], segment_index, "block", locator
                )
                sections.heading(bid, stripped, None)
                _block(locator, "text", lines[i], i + 1, i + 1)
            elif stripped:
                paragraph.append((i + 1, lines[i]))
            else:
                flush()
            i += 1
            continue
        flush()
        close = next(
            (j for j in range(i + 1, len(lines)) if re.fullmatch(r"(?i)\s*</TABLE>\s*", lines[j])),
            None,
        )
        nested = next((j for j in range(i + 1, len(lines)) if table_re.fullmatch(lines[j])), None)
        if nested is not None and (close is None or nested < close):
            paragraph.append((i + 1, lines[i]))
            diagnostics.append(f"legacy_table_unresolved:nested_table:L{nested + 1}")
            i += 1
            continue
        if close is None:
            paragraph.append((i + 1, lines[i]))
            diagnostics.append(f"legacy_table_unresolved:unclosed_table:L{i + 1}")
            i += 1
            continue
        rows = [
            [f["raw"] for f in _fields(lines[j])]
            for j in range(i, close + 1)
            if not _SGML_TAG.search(lines[j])
        ]
        if _data_table(rows):
            flush()
            emit_table(i + 1, close + 1)
        else:
            paragraph.extend((j + 1, lines[j]) for j in range(i, close + 1))
            flush()
        i = close + 1
    flush()
    return blocks, diagnostics


def _hidden_nodes(root: Any, nodes: list[Any]) -> tuple[set[Any], int, int]:
    hidden = {
        node
        for node in nodes
        if html_text.excluded(node)
        or node.get("aria-hidden", "").lower() == "true"
        or re.search(r"visibility\s*:\s*hidden", node.get("style", ""), re.I)
    }
    styles = "\n".join(node.text or "" for node in root.xpath(".//style"))
    styles = re.sub(r"/\*.*?\*/", "", styles, flags=re.S)
    unsupported = 0
    applied = 0
    for rule in styles.split("}"):
        selector, sep, declaration = rule.partition("{")
        if not sep or not re.search(
            r"(?:display\s*:\s*none|visibility\s*:\s*hidden)", declaration, re.I
        ):
            continue
        selector = selector.strip()
        if selector.startswith("@media"):
            continue
        selectors = [item.strip() for item in selector.split(",")]
        if any(re.search(r"[\s>\[\]:]", item) for item in selectors):
            unsupported += 1
            continue
        for item in selectors:
            match = re.fullmatch(r"(?:(?P<tag>[\w-]+))?(?P<kind>[.#])(?P<value>[\w-]+)", item)
            if not match:
                unsupported += 1
                continue
            tag, kind, value = match.group("tag"), match.group("kind"), match.group("value")
            matched = root.xpath(f".//{tag or '*'}")
            matched = [
                node
                for node in matched
                if (
                    node.get("id") == value
                    if kind == "#"
                    else value in node.get("class", "").split()
                )
            ]
            if matched:
                applied += 1
            for node in matched:
                hidden.update(node.iter())
    return hidden, applied, unsupported


def _table_grid_text(table_node: Any) -> tuple[str, bool]:
    """Render recoverable physical table cells with their resolved row/column headers."""
    rows = table_node.xpath("./tr|./thead/tr|./tbody/tr|./tfoot/tr")
    grid: list[list[tuple[str, int, int] | None]] = []
    degraded = False
    for row_index, row in enumerate(rows):
        if row_index >= len(grid):
            grid.append([])
        column = 0
        for cell in row.xpath("./th|./td"):
            while column < len(grid[row_index]) and grid[row_index][column] is not None:
                column += 1
            text = "".join(cell.itertext())
            rowspan = max(1, min(100, int(cell.get("rowspan", "1"))))
            colspan = max(1, min(100, int(cell.get("colspan", "1"))))
            if rowspan > 1 or colspan > 1:
                degraded = True
            for target_row in range(row_index, min(len(rows), row_index + rowspan)):
                while len(grid) <= target_row:
                    grid.append([])
                for target_column in range(column, column + colspan):
                    while len(grid[target_row]) <= target_column:
                        grid[target_row].append(None)
                    if grid[target_row][target_column] is not None:
                        degraded = True
                    else:
                        grid[target_row][target_column] = (text, rowspan, colspan)
            column += colspan
    width = max((len(row) for row in grid), default=0)
    for row in grid:
        row.extend([None] * (width - len(row)))
    captions = table_node.xpath("./caption")
    caption = " ".join("".join(captions[0].itertext()).split()) if captions else ""
    header_row = next(
        (
            i
            for i, row in enumerate(rows)
            if row.xpath("./th") or row.getparent().tag.casefold() == "thead"
        ),
        None,
    )
    column_headers = [
        (
            grid[header_row][col][0].strip()
            if header_row is not None and grid[header_row][col]
            else ""
        )
        for col in range(width)
    ]
    output: list[str] = []
    if caption:
        output.append(caption)
    for row_index, row in enumerate(grid):
        source_cells = rows[row_index].xpath("./th|./td") if row_index < len(rows) else []
        header_columns = {
            col
            for col, cell in enumerate(row)
            if cell is not None
            and col < len(source_cells)
            and source_cells[col].tag.casefold() == "th"
        }
        row_header_column = next(
            (
                col
                for col, cell in enumerate(row)
                if col not in header_columns and cell and cell[0].strip()
            ),
            None,
        )
        row_header = row[row_header_column][0].strip() if row_header_column is not None else ""
        for col, cell in enumerate(row):
            if (
                cell is None
                or col in header_columns
                or col == row_header_column
                or not cell[0].strip()
            ):
                continue
            column_header = column_headers[col] if col < len(column_headers) else ""
            output.append(
                f"{cell[0]}（行：{row_header or 'unknown'}；列：{column_header or 'unknown'}）"
            )
    return "\n".join(output), degraded


def _html_blocks(
    root: Any,
    hidden: set[Any],
    sections: _Sections,
    blocks: list[dict[str, Any]],
    filing: Mapping[str, Any],
    document: Mapping[str, Any],
    segment_index: int | None,
    locator_prefix: str = "",
) -> None:
    def visible(node: Any) -> bool:
        return node not in hidden and not any(parent in hidden for parent in node.iterancestors())

    data_tables: set[Any] = set()
    table_texts: dict[Any, str] = {}
    for table_node in root.xpath("self::table|.//table"):
        if not visible(table_node):
            continue
        row_nodes = table_node.xpath("./tr|./thead/tr|./tbody/tr|./tfoot/tr")
        texts = [
            [" ".join("".join(cell.itertext()).split()) for cell in row.xpath("./th|./td")]
            for row in row_nodes
        ]
        if _data_table(texts):
            data_tables.add(table_node)
            rendered, _degraded = _table_grid_text(table_node)
            table_texts[table_node] = rendered or "\n".join("\t".join(row) for row in texts)
    traversal = root.xpath(
        "self::table|.//table|.//h1|.//h2|.//h3|.//h4|.//h5|.//h6|"
        ".//p|.//li|.//div|.//blockquote|.//center|.//pre|.//dd|.//dt|.//td|.//th"
    )
    for node in traversal:
        if not visible(node) or any(parent in data_tables for parent in node.iterancestors()):
            continue
        if node in data_tables:
            locator = locator_prefix + node.getroottree().getpath(node)
            table_text = table_texts[node]
            table_block = content_id(
                document["document_id"], document["sha256"], segment_index, "block", locator
            )
            blocks.append(
                {
                    **_identity(filing, document, segment_index),
                    "block_id": table_block,
                    "ordinal": len(blocks),
                    "block_kind": "table",
                    "heading_path_json": sections.path_json(),
                    "item_code": sections.item_code,
                    "text": table_text,
                    "text_sha256": hashlib.sha256(table_text.encode()).hexdigest(),
                    "source_xpath": locator,
                    "line_start": None,
                    "line_end": None,
                }
            )
            continue
        if node.tag.lower() == "table":
            continue
        tag = node.tag.lower()
        chunks: list[str] = []

        def own_text(el: Any, root: Any = node, output: list[str] = chunks) -> None:
            if el is not root and (
                el.tag
                in {
                    "table",
                    "p",
                    "div",
                    "li",
                    "blockquote",
                    "pre",
                    "dd",
                    "dt",
                    "td",
                    "h1",
                    "h2",
                    "h3",
                    "h4",
                    "h5",
                    "h6",
                }
            ):
                return
            if el.text:
                output.append(el.text)
            for child in el:
                if child not in hidden:
                    own_text(child, root, output)
                if child.tail:
                    output.append(child.tail)

        own_text(node)
        text = " ".join("".join(chunks).split())
        if not text:
            continue
        locator = locator_prefix + node.getroottree().getpath(node)
        bid = content_id(
            document["document_id"], document["sha256"], segment_index, "block", locator
        )
        level = int(tag[1]) if re.fullmatch(r"h[1-6]", tag) else None
        sections.heading(bid, text, level)
        blocks.append(
            {
                **_identity(filing, document, segment_index),
                "block_id": bid,
                "ordinal": len(blocks),
                "block_kind": "text",
                "heading_path_json": sections.path_json(),
                "item_code": sections.item_code,
                "text": text,
                "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                "source_xpath": locator,
                "line_start": None,
                "line_end": None,
            }
        )


def _parse_single(
    raw: bytes,
    filing: Mapping[str, Any],
    document: Mapping[str, Any],
    segment_index: int | None = None,
) -> ContentResult:
    row = _identity(filing, document, segment_index, len(raw))
    blocks: list[dict[str, Any]] = []
    if raw.startswith(b"%PDF-") or is_complete_sec_pdf_envelope(raw):
        row.update(
            status="unsupported",
            payload_kind="pdf",
            diagnostic_codes_json=_json(["unsupported_pdf"]),
        )
        return ContentResult([row], blocks)
    if b"\0" in raw:
        row.update(
            status="unsupported",
            payload_kind="binary",
            diagnostic_codes_json=_json(["unsupported_binary"]),
        )
        return ContentResult([row], blocks)
    try:
        envelope = extract_sec_envelope(raw)
    except ValueError:
        row.update(status="failed", diagnostic_codes_json=_json(["sec_envelope_invalid"]))
        return ContentResult([row], blocks)
    payload = raw if envelope is None else envelope["payload"]
    payload_kind = _classify_payload(payload)
    row["payload_kind"] = payload_kind
    if payload_kind in {"legacy_text", "html_pre"}:
        encoding = (envelope.get("encoding") if envelope else None) or "utf-8"
        text = payload.decode(encoding, errors="replace")
        if payload_kind == "html_pre":
            row["limitations_json"] = _json(["line numbers refer to extracted <pre> text"])
            try:
                text = _extract_pre_text(text)
            except LegacyLayoutRejected as exc:
                row.update(status="failed", diagnostic_codes_json=_json([exc.code]))
                return ContentResult([row], [])
        blocks, diagnostics = _parse_text_lines(text, filing, document, segment_index)
        row.update(
            status="partial" if diagnostics else "full",
            diagnostic_codes_json=_json(diagnostics),
            encoding=encoding,
            visible_text_chars=len(text),
            covered_text_chars=sum(len(b["text"]) for b in blocks),
            block_count=len(blocks),
            table_count=sum(1 for b in blocks if b["block_kind"] == "table"),
        )
    else:
        try:
            from lxml import html as lxml_html
        except ImportError as exc:
            raise ImportError("report content parsing requires the 'dev' installation") from exc
        if b"<!ENTITY" in payload.upper():
            row.update(status="failed", diagnostic_codes_json=_json(["entity_declaration"]))
            return ContentResult([row], blocks)
        encoding = (
            html_text.sec_payload_html_encoding(payload, envelope["encoding"])
            if envelope
            else html_text.html_encoding(raw)
        )
        source = payload
        if encoding.casefold().startswith("utf-32"):
            source = payload.decode(encoding).encode("utf-8")
            encoding = "utf-8"
        root = lxml_html.fromstring(
            source,
            parser=lxml_html.HTMLParser(
                encoding=encoding,
                recover=True,
                no_network=True,
                huge_tree=False,
                remove_comments=True,
            ),
        )
        nodes = list(root.iter())
        if len(nodes) > MAX_NODES:
            row.update(status="failed", diagnostic_codes_json=_json(["limit_exceeded"]))
            return ContentResult([row], [])
        hidden, applied, unsupported = _hidden_nodes(root, nodes)
        row["hidden_excluded_count"] = len(hidden)
        row["css_hidden_rules_applied"] = applied
        row["css_rules_unsupported"] = unsupported
        sections = _Sections()
        _html_blocks(root, hidden, sections, blocks, filing, document, segment_index)
        blocks_text_covered = sum(len(block["text"]) for block in blocks)
        row.update(
            status="partial" if unsupported else "full",
            payload_kind="html",
            encoding=encoding,
            visible_text_chars=sum(
                len(" ".join("".join(n.itertext()).split())) for n in nodes if n not in hidden
            ),
            covered_text_chars=blocks_text_covered,
            block_count=len(blocks),
            table_count=sum(1 for b in blocks if b["block_kind"] == "table"),
            css_rules_unsupported=unsupported,
        )
        if unsupported:
            row["limitations_json"] = _json(["unsupported CSS selectors encountered"])
    return ContentResult([row], blocks)


def parse_content(
    raw: bytes, *, filing: Mapping[str, Any], document: Mapping[str, Any]
) -> ContentResult:
    if not isinstance(raw, bytes):
        raise TypeError("raw must be bytes")
    if len(raw) > MAX_PAYLOAD_BYTES:
        row = _document_id_row(
            filing, document, None, "failed", "unknown", ["limit_exceeded"], len(raw)
        )
        return ContentResult([row], [])
    role = document.get("role")
    if role in {"xbrl_instance", "schema", "linkbase"}:
        return ContentResult(
            [_document_id_row(filing, document, None, "not_content", "xbrl", [], len(raw))], []
        )
    if role not in {"primary", "exhibit", "full_submission"}:
        return ContentResult(
            [_document_id_row(filing, document, None, "out_of_scope", "unknown", [], len(raw))], []
        )
    if role != "full_submission":
        return _parse_single(raw, filing, document)
    starts = list(re.finditer(rb"(?im)^[ \t]*<DOCUMENT>[ \t]*\r?$", raw))
    ends = list(re.finditer(rb"(?im)^[ \t]*</DOCUMENT>[ \t]*\r?\n?", raw))
    if len(starts) != len(ends):
        return ContentResult(
            [
                _document_id_row(
                    filing,
                    document,
                    None,
                    "failed",
                    "unknown",
                    ["full_submission_unbalanced"],
                    len(raw),
                )
            ],
            [],
        )
    documents: list[dict[str, Any]] = []
    blocks: list[dict[str, Any]] = []
    for index, (start, end) in enumerate(zip(starts, ends, strict=True)):
        segment = raw[start.start() : end.end()]
        try:
            envelope = extract_sec_envelope(segment)
        except ValueError:
            documents.append(
                _document_id_row(
                    filing,
                    document,
                    index,
                    "failed",
                    "unknown",
                    ["sec_envelope_invalid"],
                    len(segment),
                )
            )
            continue
        dtype = (envelope.get("metadata", {}).get("type") or "").upper() if envelope else ""
        if dtype == filing["form"].upper() or dtype.startswith(("EX-13", "EX-27")):
            segment_doc = dict(document)
            segment_doc.update(
                role="primary",
                document_id=content_id(
                    document["document_id"], document["sha256"], index, "segment", str(index)
                ),
                filename=envelope.get("metadata", {}).get("filename", document.get("filename")),
            )
            part = _parse_single(segment, filing, segment_doc, index)
            documents.extend(part.documents)
            blocks.extend(part.blocks)
        else:
            documents.append(
                _document_id_row(
                    filing, document, index, "out_of_scope", "unknown", [], len(segment)
                )
            )
    return ContentResult(documents, blocks)
