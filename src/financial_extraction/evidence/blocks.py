"""Deterministic, bounded HTML evidence blocks and coverage planning.

The parser treats all document text as untrusted evidence. It never executes
scripts, follows links, loads external resources, or assigns meaning to prompts.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import asdict, replace
from typing import Any

from ..domain.contracts import (
    EvidenceBlock,
    EvidenceBundle,
    EvidenceCell,
    ExtractionWindow,
    SourceLocation,
    VerifiedDocument,
    WindowItem,
)
from ..domain.validation import canonical_hash, evidence_bundle_hash


class EvidenceBuildError(ValueError):
    """The source cannot be decoded or safely represented as bounded evidence."""


class EvidenceLimitError(EvidenceBuildError):
    """An explicit parser, payload, cell, or grid limit was exceeded."""


_PREPROCESSING_REVISION = "financial-extraction-html-v1"
_HIDDEN_STYLE = re.compile(r"(?:display\s*:\s*none|visibility\s*:\s*hidden)", re.IGNORECASE)
_FOOTNOTE = re.compile(r"foot\s*notes?|end\s*notes?", re.IGNORECASE)


def _positive_limits(limits: Mapping[str, int], required: set[str], where: str) -> dict[str, int]:
    if not isinstance(limits, Mapping):
        raise TypeError(f"{where} must be a mapping of positive integer limits")
    missing = required - set(limits)
    if missing:
        raise ValueError(f"{where} missing required limits: {', '.join(sorted(missing))}")
    result: dict[str, int] = {}
    for name, value in limits.items():
        if (
            not isinstance(name, str)
            or not isinstance(value, int)
            or isinstance(value, bool)
            or value <= 0
        ):
            raise ValueError(f"{where}.{name} must be a positive integer")
        result[name] = value
    return result


def _digest_id(*parts: str) -> str:
    raw = "\0".join(parts).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:32]


def _normalize_text(value: str) -> str:
    return " ".join(value.split())


def _text_without(element: Any, *, stop_tags: set[str], root: Any) -> str:
    chunks: list[str] = []

    def walk(node: Any, is_root: bool = False) -> None:
        if not is_root and str(node.tag).lower() in stop_tags:
            if node.tail:
                chunks.append(node.tail)
            return
        if node.text:
            chunks.append(node.text)
        for child in node:
            tag = str(child.tag).lower()
            if tag in {"br", "div", "p", "li", "tr", "td", "th", "table"}:
                chunks.append(" ")
            walk(child)
            if tag in {"div", "p", "li", "tr", "td", "th", "table"}:
                chunks.append(" ")
        if not is_root and node.tail:
            chunks.append(node.tail)

    walk(element, True)
    return _normalize_text("".join(chunks))


def _is_hidden(element: Any) -> bool:
    if "hidden" in element.attrib:
        return True
    if element.attrib.get("aria-hidden", "").strip().lower() == "true":
        return True
    return bool(_HIDDEN_STYLE.search(element.attrib.get("style", "")))


def _safe_encoding(payload: bytes, encoding: str) -> str:
    if not isinstance(encoding, str) or not encoding.strip():
        raise EvidenceBuildError("a verified payload encoding is required")
    try:
        return payload.decode(encoding, errors="strict")
    except (LookupError, UnicodeDecodeError) as exc:
        raise EvidenceBuildError(f"payload cannot be decoded strictly as {encoding!r}") from exc


def _check_declarations(text: str) -> None:
    if re.search(r"<!\s*ENTITY\b", text, flags=re.IGNORECASE):
        raise EvidenceBuildError("HTML entity declarations are not accepted")
    for match in re.finditer(r"<!\s*DOCTYPE\b([^>]*)>", text, flags=re.IGNORECASE):
        declaration = match.group(1).strip()
        if declaration.lower() != "html":
            raise EvidenceBuildError("external or non-HTML DTD declarations are not accepted")


def _cell_span(raw: str | None, *, default: int, remaining_rows: int | None = None) -> int:
    if raw is None:
        return default
    try:
        value = int(raw, 10)
    except (TypeError, ValueError) as exc:
        raise EvidenceBuildError("table rowspan/colspan must be an integer") from exc
    if value == 0 and remaining_rows is not None:
        return remaining_rows
    if value < 1:
        raise EvidenceBuildError("table rowspan/colspan must be positive")
    return value


def build_evidence(document: VerifiedDocument, *, limits: Mapping[str, int]) -> EvidenceBundle:
    """Parse one approved opaque document into stable blocks and physical cells.

    ``limits`` must include ``maxpayloadbytes``, ``maxnodes``, ``maxcells`` and
    ``maxgridspan``. Each cell remains unique; covered grid slots point back to
    that physical cell rather than synthesizing duplicate cells/observations.
    """
    bounds = _positive_limits(
        limits, {"maxpayloadbytes", "maxnodes", "maxcells", "maxgridspan"}, "limits"
    )
    if not isinstance(document, VerifiedDocument):
        raise TypeError("document must be a VerifiedDocument DTO")
    if not all(
        isinstance(value, str) and value.strip()
        for value in (
            document.source_id,
            document.document_id,
            document.source_type,
            document.preprocessing_revision,
        )
    ):
        raise EvidenceBuildError(
            "opaque source/document IDs and revision must be non-empty strings"
        )
    if not isinstance(document.fixture_only, bool):
        raise EvidenceBuildError("fixture_only must be an explicit boolean")
    if not isinstance(document.payload, bytes):
        raise EvidenceBuildError("verified payload must be bytes")
    if (
        not isinstance(document.raw_bytes, int)
        or isinstance(document.raw_bytes, bool)
        or document.raw_bytes < 0
    ):
        raise EvidenceBuildError("host-recorded raw byte count must be a non-negative integer")
    if len(document.payload) > bounds["maxpayloadbytes"]:
        raise EvidenceLimitError("payload exceeds maxpayloadbytes")
    if not re.fullmatch(r"[0-9a-f]{64}", document.raw_sha256):
        raise EvidenceBuildError("raw_sha256 must be a lowercase SHA-256 digest")
    payload_hash = hashlib.sha256(document.payload).hexdigest()
    if payload_hash != document.payload_sha256:
        raise EvidenceBuildError("payload_sha256 does not match received payload bytes")
    text = _safe_encoding(document.payload, document.encoding)
    _check_declarations(text)

    try:
        from lxml import etree, html
    except ImportError as exc:
        raise ImportError(
            "HTML evidence parsing requires the optional 'extraction' extra; "
            "install quant-dataset[extraction] (lxml>=5,<7)."
        ) from exc

    parser = html.HTMLParser(
        no_network=True,
        recover=True,
        huge_tree=False,
        remove_comments=True,
    )
    try:
        root = html.document_fromstring(text, parser=parser)
    except (etree.ParserError, ValueError) as exc:
        raise EvidenceBuildError("payload could not be parsed as bounded HTML") from exc
    if root is None:
        raise EvidenceBuildError("payload has no HTML document root")

    # Dangerous active content and explicit hidden subtrees are excluded before
    # extraction. CSS stylesheets and visual layout are intentionally not run.
    limitations: list[str] = [
        "CSS stylesheets/layout are not interpreted; visually hidden CSS-only content may remain.",
        "DOM XPath and canonical character spans are not original byte coordinates.",
        "HTML recovery-mode parsing may produce an incomplete DOM for malformed source markup.",
    ]
    removed_scripts = 0
    excluded_hidden = 0
    removed_subtrees: set[Any] = set()
    if _is_hidden(root):
        excluded_hidden += 1
        root.text = None
        for child in list(root):
            root.remove(child)
    else:
        for element in list(root.iter()):
            if not isinstance(element.tag, str):
                continue
            if any(ancestor in removed_subtrees for ancestor in element.iterancestors()):
                continue
            tag = element.tag.lower()
            parent = element.getparent()
            if tag in {"script", "style"}:
                if parent is not None:
                    element.drop_tree()
                    removed_subtrees.add(element)
                    removed_scripts += 1
                continue
            if _is_hidden(element) and parent is not None:
                element.drop_tree()
                removed_subtrees.add(element)
                excluded_hidden += 1
    if removed_scripts:
        limitations.append(
            f"{removed_scripts} script/style subtree(s) excluded; contents treated as untrusted."
        )
    if excluded_hidden:
        limitations.append(f"{excluded_hidden} explicitly hidden HTML subtree(s) excluded.")
    if parser.error_log:
        limitations.append(
            "HTML parser recovered malformed markup; DOM structure may be incomplete."
        )

    nodes = [item for item in root.iter() if isinstance(item.tag, str)]
    if len(nodes) > bounds["maxnodes"]:
        raise EvidenceLimitError("DOM exceeds maxnodes")
    depth_stack: list[tuple[Any, int]] = [(root, 1)]
    while depth_stack:
        node, depth = depth_stack.pop()
        if depth > bounds.get("maxdepth", 512):
            raise EvidenceLimitError("DOM exceeds maxdepth")
        depth_stack.extend((child, depth + 1) for child in node if isinstance(child.tag, str))

    table_elements = root.xpath(".//table")
    table_ids: dict[Any, str] = {}
    parent_table_ids: dict[Any, str | None] = {}
    for table in table_elements:
        xpath = root.getroottree().getpath(table)
        table_id = _digest_id(
            document.document_id,
            document.payload_sha256,
            document.preprocessing_revision,
            "table",
            xpath,
        )
        ancestors = [
            ancestor for ancestor in table.iterancestors() if str(ancestor.tag).lower() == "table"
        ]
        parent_table_ids[table] = table_ids.get(ancestors[0]) if ancestors else None
        table_ids[table] = table_id

    # Pending items carry an element-order key. This gives canonical spans a
    # deterministic DOM order while keeping each physical table cell distinct.
    node_order = {
        node: index for index, node in enumerate(root.iter()) if isinstance(node.tag, str)
    }
    pending: list[tuple[int, int, str, dict[str, Any]]] = []
    cell_count = 0

    for table in table_elements:
        table_id = table_ids[table]
        table_xpath = root.getroottree().getpath(table)
        caption_elements = table.xpath("./caption")
        caption_text = (
            _text_without(caption_elements[0], stop_tags={"table"}, root=caption_elements[0])
            if caption_elements
            else ""
        )
        table_block_id = _digest_id(
            document.document_id,
            document.payload_sha256,
            document.preprocessing_revision,
            "table",
            table_xpath,
        )
        pending.append(
            (
                node_order[table],
                0,
                "block",
                {
                    "block_id": table_block_id,
                    "document_id": document.document_id,
                    "kind": "table",
                    "text": caption_text,
                    "xpath": table_xpath,
                    "table_id": table_id,
                    "parent_block_id": parent_table_ids[table],
                },
            )
        )
        rows = table.xpath("./tr | ./thead/tr | ./tbody/tr | ./tfoot/tr")
        slot_rows: list[dict[int, str]] = [dict() for _ in rows]
        direct_cells: list[dict[str, Any]] = []
        expanded_slots = 0
        for row_index, row in enumerate(rows):
            physical_cells = row.xpath("./th | ./td")
            column = 0
            for cell in physical_cells:
                while column in slot_rows[row_index]:
                    column += 1
                remaining = len(rows) - row_index
                rowspan = _cell_span(cell.get("rowspan"), default=1, remaining_rows=remaining)
                colspan = _cell_span(cell.get("colspan"), default=1)
                if rowspan > remaining:
                    raise EvidenceBuildError("rowspan extends beyond the physical table rows")
                if max(rowspan, colspan, rowspan * colspan) > bounds["maxgridspan"]:
                    raise EvidenceLimitError("cell span exceeds maxgridspan")
                cell_text = _text_without(cell, stop_tags={"table"}, root=cell)
                cell_xpath = root.getroottree().getpath(cell)
                cell_id = _digest_id(
                    document.document_id,
                    document.payload_sha256,
                    document.preprocessing_revision,
                    "cell",
                    cell_xpath,
                )
                positions = tuple(
                    (covered_row, covered_col)
                    for covered_row in range(row_index, row_index + rowspan)
                    for covered_col in range(column, column + colspan)
                )
                if any(
                    covered_col in slot_rows[covered_row] for covered_row, covered_col in positions
                ):
                    raise EvidenceBuildError("overlapping table rowspan/colspan cells")
                expanded_slots += len(positions)
                if expanded_slots > bounds["maxgridspan"]:
                    raise EvidenceLimitError("expanded table grid exceeds maxgridspan")
                for covered_row, covered_col in positions:
                    slot_rows[covered_row][covered_col] = cell_id
                direct_cells.append(
                    {
                        "cell_id": cell_id,
                        "document_id": document.document_id,
                        "table_id": table_id,
                        "parent_table_id": parent_table_ids[table],
                        "row_index": row_index,
                        "column_index": column,
                        "text": cell_text,
                        "xpath": cell_xpath,
                        "rowspan": rowspan,
                        "colspan": colspan,
                        "is_header": str(cell.tag).lower() == "th",
                        "covered_positions": positions,
                        "element": cell,
                    }
                )
                column += colspan
                cell_count += 1
                if cell_count > bounds["maxcells"]:
                    raise EvidenceLimitError("physical cells exceed maxcells")
        table_width = max((max(row.keys(), default=-1) + 1 for row in slot_rows), default=0)
        if len(rows) * table_width > bounds["maxgridspan"]:
            raise EvidenceLimitError("expanded table grid exceeds maxgridspan")
        for info in direct_cells:
            pending.append((node_order[info["element"]], 1, "cell", info))

    block_tags = {"h1", "h2", "h3", "h4", "h5", "h6", "p", "li"}
    for element in nodes:
        tag = element.tag.lower()
        if tag not in block_tags:
            continue
        if any(str(parent.tag).lower() == "table" for parent in element.iterancestors()):
            continue
        if tag == "p" and any(
            str(parent.tag).lower() == "li" for parent in element.iterancestors()
        ):
            continue
        stop_tags = {"table"}
        if tag == "li":
            stop_tags.add("li")
        block_text = _text_without(element, stop_tags=stop_tags, root=element)
        if not block_text:
            continue
        xpath = root.getroottree().getpath(element)
        kind = (
            "heading"
            if re.fullmatch(r"h[1-6]", tag)
            else "list_item"
            if tag == "li"
            else "paragraph"
        )
        element_name = " ".join((element.get("id", ""), element.get("class", "")))
        if _FOOTNOTE.search(element_name):
            kind = "footnote"
        block_id = _digest_id(
            document.document_id,
            document.payload_sha256,
            document.preprocessing_revision,
            kind,
            xpath,
        )
        pending.append(
            (
                node_order[element],
                0,
                "block",
                {
                    "block_id": block_id,
                    "document_id": document.document_id,
                    "kind": kind,
                    "text": block_text,
                    "xpath": xpath,
                    "table_id": None,
                    "parent_block_id": None,
                },
            )
        )

    pending.sort(key=lambda item: (item[0], item[1], item[2], item[3].get("xpath", "")))
    text_parts: list[str] = []
    blocks: list[EvidenceBlock] = []
    cells: list[EvidenceCell] = []
    cursor = 0
    heading_context: list[tuple[str, str]] = []
    for _, _, kind, info in pending:
        item_text = info["text"]
        if text_parts:
            text_parts.append("\n")
            cursor += 1
        start = cursor
        text_parts.append(item_text)
        cursor += len(item_text)
        end = cursor
        current_context = tuple(text for _, text in heading_context[-3:])
        context_ids = tuple(item_id for item_id, _ in heading_context[-3:])
        if kind == "block":
            block = EvidenceBlock(
                block_id=info["block_id"],
                document_id=info["document_id"],
                kind=info["kind"],
                text=item_text,
                xpath=info["xpath"],
                canonical_start=start,
                canonical_end=end,
                parent_block_id=info["parent_block_id"],
                table_id=info["table_id"],
                heading_context=current_context,
                context_refs=context_ids,
                limitations=("raw byte span unavailable for DOM-derived text",),
            )
            blocks.append(block)
            if block.kind == "heading":
                heading_context.append((block.block_id, block.text))
                heading_context = heading_context[-3:]
        else:
            cell = EvidenceCell(
                cell_id=info["cell_id"],
                document_id=info["document_id"],
                table_id=info["table_id"],
                parent_table_id=info["parent_table_id"],
                row_index=info["row_index"],
                column_index=info["column_index"],
                text=item_text,
                xpath=info["xpath"],
                rowspan=info["rowspan"],
                colspan=info["colspan"],
                is_header=info["is_header"],
                covered_positions=info["covered_positions"],
                canonical_start=start,
                canonical_end=end,
                heading_context=current_context,
                context_refs=context_ids,
                limitations=("raw byte span unavailable for DOM-derived text",),
            )
            cells.append(cell)

    canonical_text = "".join(text_parts)
    canonical_hash_value = hashlib.sha256(canonical_text.encode("utf-8")).hexdigest()
    config = {
        "revision": _PREPROCESSING_REVISION,
        "document_revision": document.preprocessing_revision,
        "normalization": "unicode-whitespace-collapse-v1",
        "parser": "lxml.html.HTMLParser",
        "lxml_version": ".".join(str(part) for part in etree.LXML_VERSION),
        "network": False,
        "external_entities": False,
        "hidden_attribute_policy": "exclude-explicit-hidden-v1",
        "limits": bounds,
    }
    config_hash = canonical_hash(config)
    bundle = EvidenceBundle(
        documents=(document,),
        blocks=tuple(blocks),
        cells=tuple(cells),
        canonical_text=canonical_text,
        canonical_text_hash=canonical_hash_value,
        preprocessing_config=config,
        preprocessing_config_hash=config_hash,
        limits=bounds,
        limitations=tuple(dict.fromkeys(limitations)),
    )
    return replace(bundle, bundle_hash=evidence_bundle_hash(bundle))


def _window_item(evidence_id: str, kind: str, text: str, source: Any) -> WindowItem:
    return WindowItem(
        ref=evidence_id,
        kind=kind,
        text=text,
        source=SourceLocation(
            document_id=source.document_id,
            evidence_id=evidence_id,
            xpath=source.xpath,
            canonical_start=source.canonical_start,
            canonical_end=source.canonical_end,
        ),
    )


def _measure_default(window: ExtractionWindow) -> int:
    import json

    return len(json.dumps(asdict(window), ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def iter_windows(
    bundle: EvidenceBundle,
    *,
    max_request_bytes: int,
    measure_request: Any = _measure_default,
):
    """Yield source-bounded windows; every block/cell is core exactly once."""
    if not isinstance(max_request_bytes, int) or isinstance(max_request_bytes, bool) or max_request_bytes <= 0:
        raise ValueError("max_request_bytes must be a positive integer")
    evidence: dict[str, WindowItem] = {}
    headers_by_document: dict[str, list[WindowItem]] = {
        document.document_id: [] for document in bundle.documents
    }
    for block in bundle.blocks:
        evidence[block.block_id] = _window_item(block.block_id, block.kind, block.text, block)
    for cell in bundle.cells:
        evidence[cell.cell_id] = _window_item(cell.cell_id, "cell", cell.text, cell)
        if cell.is_header:
            headers_by_document[cell.document_id].append(evidence[cell.cell_id])
    grouped: dict[str, list[WindowItem]] = {
        document.document_id: [] for document in bundle.documents
    }
    for block in bundle.blocks:
        if block.kind == "table":
            cells = sorted(
                (cell for cell in bundle.cells if cell.table_id == block.block_id),
                key=lambda cell: (cell.row_index, cell.column_index),
            )
            grouped[block.document_id].extend(evidence[cell.cell_id] for cell in cells)
            grouped[block.document_id].extend(
                evidence[ref] for ref in block.context_refs if ref in evidence
            )
            grouped[block.document_id].append(evidence[block.block_id])
        else:
            grouped[block.document_id].append(evidence[block.block_id])
    for cell in bundle.cells:
        if not any(block.kind == "table" and block.block_id == cell.table_id for block in bundle.blocks):
            grouped[cell.document_id].append(evidence[cell.cell_id])

    core_ids = set(evidence)
    emitted: set[str] = set()
    for document_id in sorted(grouped):
        pending = grouped[document_id]
        # Deterministic source order; core items are deduplicated by evidence ref.
        unique = {item.ref: item for item in pending}
        pending = sorted(
            unique.values(),
            key=lambda item: (
                item.source.canonical_start,
                item.source.canonical_end,
                item.ref,
            ),
        )
        cores = [item for item in pending if item.ref in core_ids and item.ref not in emitted]
        context = tuple(headers_by_document[document_id])
        offset = 0
        while offset < len(cores):
            chosen: list[WindowItem] = []
            while offset < len(cores):
                candidate = cores[offset]
                window_id = hashlib.sha256(
                    f"{document_id}:{candidate.ref}:{offset}".encode("utf-8")
                ).hexdigest()[:24]
                probe = ExtractionWindow(window_id, document_id, tuple((*chosen, candidate)), context)
                size = measure_request(probe)
                if size > max_request_bytes:
                    if not chosen:
                        raise EvidenceLimitError("indivisible evidence item exceeds max_request_bytes")
                    break
                chosen.append(candidate)
                offset += 1
            window_id = hashlib.sha256(
                f"{document_id}:{chosen[0].ref}:{chosen[-1].ref}".encode("utf-8")
            ).hexdigest()[:24]
            window = ExtractionWindow(window_id, document_id, tuple(chosen), context)
            if measure_request(window) > max_request_bytes:
                # Context may be the oversized component; omit it before rejecting core.
                window = ExtractionWindow(window_id, document_id, tuple(chosen), ())
                if measure_request(window) > max_request_bytes:
                    raise EvidenceLimitError("indivisible evidence item exceeds max_request_bytes")
            emitted.update(item.ref for item in chosen)
            yield window
    if emitted != core_ids:
        raise EvidenceBuildError("windowing failed to include every evidence item exactly once")


def render_window(window: ExtractionWindow) -> str:
    import json

    return json.dumps(
        {
            "window_id": window.window_id,
            "document_id": window.document_id,
            "core": [asdict(item) for item in window.core],
            "context": [asdict(item) for item in window.context],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


