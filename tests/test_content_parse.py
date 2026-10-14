"""Offline behavior checks for normalized SEC content samples."""

from __future__ import annotations

import hashlib
import json

import pytest

from filings.content_parse import _fields, parse_content

FILING = {
    "cik10": "0000000001",
    "accession_number": "0000000001-24-000001",
    "form": "10-K",
    "filed_date": "2024-01-01",
    "report_date": None,
}


def _document(raw: bytes, role: str = "primary") -> dict[str, str]:
    return {
        "document_id": "doc-1",
        "role": role,
        "filename": "report.htm",
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def _table_blocks(result):
    return [b for b in result.blocks if b["block_kind"] == "table"]


def test_content_parser_handles_visible_paragraph_hidden_css_and_item_heading() -> None:
    raw = b"""<!DOCTYPE html><html><head><style>.clear{display:none}</style></head><body>
    <p class='clear'>hidden text</p><div>Visible body</div>
    <p>Item 7. Management discussion</p></body></html>"""
    result = parse_content(raw, filing=FILING, document=_document(raw))
    texts = [block["text"] for block in result.blocks]
    assert "hidden text" not in " ".join(texts)
    assert any("Visible body" in text for text in texts)
    assert any(block["item_code"] == "7" for block in result.blocks)
    assert result.documents[0]["hidden_excluded_count"] >= 1


def test_content_parser_rejects_pdf_and_entity_declaration() -> None:
    pdf = parse_content(b"%PDF-1.7\n", filing=FILING, document=_document(b"%PDF-1.7\n"))
    assert pdf.documents[0]["status"] == "unsupported"
    raw = b"<!DOCTYPE html [<!ENTITY x 'x'>]><html><body>x</body></html>"
    entity = parse_content(raw, filing=FILING, document=_document(raw))
    assert entity.documents[0]["diagnostic_codes_json"] == '["entity_declaration"]'


def test_root_table_is_emitted_with_cell_labels_and_raw_byte_size() -> None:
    raw = b"<table><tr><td>Cash</td><td>1</td><td>2</td></tr></table>"
    result = parse_content(raw, filing=FILING, document=_document(raw))
    assert result.documents[0]["table_count"] == 1
    assert result.documents[0]["raw_byte_size"] == len(raw)
    tables = _table_blocks(result)
    assert len(tables) == 1
    assert tables[0]["text"] == ("1（行：Cash；列：unknown）\n2（行：Cash；列：unknown）")


def test_content_ids_distinguish_segment_zero_from_no_segment() -> None:
    from filings.content_models import content_id

    assert content_id("doc", "hash", None, "block", "x") != content_id(
        "doc", "hash", 0, "block", "x"
    )


def test_html_whitespace_before_table_does_not_change_html_detection() -> None:
    raw = (
        b"<!doctype html>\n<html>\n<body>\n<table>\n<tr><td>17</td><td>18</td></tr>\n"
        b"</table>\n</body></html>"
    )
    result = parse_content(raw, filing=FILING, document=_document(raw))
    assert result.documents[0]["payload_kind"] == "html"
    assert result.documents[0]["raw_byte_size"] == len(raw)


def test_legacy_sgml_page_and_table_markers_stay_line_text() -> None:
    raw = b"<PAGE>\n< TABLE >\n<C>Revenue<S>1,234\n"
    result = parse_content(raw, filing=FILING, document=_document(raw))
    assert result.documents[0]["payload_kind"] == "legacy_text"
    assert result.documents[0]["status"] == "full"
    assert _table_blocks(result) == []


def test_full_submission_parses_three_segments_and_keeps_raw_sizes() -> None:
    parts = [
        b"<DOCUMENT>\n<TYPE>10-K\n<FILENAME>report.htm\n<TEXT>\n"
        b"<html><body><table><tr><td>$ 42</td></tr></table></body></html>\n</TEXT>\n</DOCUMENT>\n",
        b"<DOCUMENT>\n<TYPE>EX-99\n<FILENAME>press.htm\n<TEXT>\n"
        b"<html><body>not selected</body></html>\n</TEXT>\n</DOCUMENT>\n",
        b"<DOCUMENT>\n<TYPE>EX-13\n<FILENAME>annual.htm\n<TEXT>\n"
        b"<html><body>selected exhibit</body></html>\n</TEXT>\n</DOCUMENT>\n",
    ]
    raw = b"\n".join(parts)
    document = _document(raw, "full_submission")
    result = parse_content(raw, filing=FILING, document=document)
    assert [row["segment_index"] for row in result.documents] == [0, 1, 2]
    assert [row["status"] for row in result.documents] == ["full", "out_of_scope", "full"]
    assert result.documents[0]["raw_byte_size"] == len(parts[0])
    assert result.documents[1]["raw_byte_size"] == len(parts[1])


def test_legacy_sgml_data_table_is_one_table_block() -> None:
    raw = b"<PAGE>\nTitle\n<TABLE>\n<S>       <C>        <C>\nCash      1,200      2\n</TABLE>\n"
    result = parse_content(raw, filing=FILING, document=_document(raw))
    assert result.documents[0]["payload_kind"] == "legacy_text"
    assert result.documents[0]["status"] == "full"
    tables = _table_blocks(result)
    assert len(tables) == 1
    assert tables[0]["line_start"] == 3
    assert tables[0]["line_end"] == 6
    assert "Cash      1,200      2" in tables[0]["text"]


def test_page_before_real_html_routes_to_html() -> None:
    raw = b"<PAGE>\n<html><body><table><tr><td>Cash</td><td>$1,200</td></tr></table></body></html>"
    result = parse_content(raw, filing=FILING, document=_document(raw))
    assert result.documents[0]["payload_kind"] == "html"
    assert len(_table_blocks(result)) == 1


def test_pre_wrapped_sgml_table_preserves_alignment_and_values() -> None:
    raw = (
        b"<HTML><BODY><PRE><PAGE>\n<TABLE>\n<CAPTION>\n                 2003      2002\n"
        b"<S>              <C>       <C>\nCash ........    $ 1,200   (300)\n"
        b"Deposits           641      --\n</TABLE></PRE></BODY></HTML>"
    )
    result = parse_content(raw, filing=FILING, document=_document(raw))
    assert result.documents[0]["payload_kind"] == "html_pre"
    assert result.documents[0]["status"] == "full"
    tables = _table_blocks(result)
    assert len(tables) == 1
    assert "Cash ........    $ 1,200   (300)" in tables[0]["text"]


def test_fixed_width_table_emits_four_value_columns() -> None:
    rows = [
        ("OPERATING REVENUE", ["$49,334", "46,153", "$115,093", "112,567"]),
        ("Purchased water", ["$1,000", "2,000", "$3,000", "4,000"]),
        ("Interest on long-term debt", ["(2,178)", "(1,953)", "(6,370)", "(6,113)"]),
    ]
    raw = "\n".join(
        label.ljust(28)
        + "  ".join(value.ljust(width) for value, width in zip(values, [7, 7, 8, 8], strict=True))
        for label, values in rows
    ).encode()
    result = parse_content(raw, filing=FILING, document=_document(raw))
    tables = _table_blocks(result)
    assert len(tables) == 1
    assert tables[0]["line_start"] == 1
    assert tables[0]["line_end"] == 3
    assert not [b for b in result.blocks if b["block_kind"] == "text"]


def test_plain_table_columns_preserve_sparse_positions_without_duplicate_blocks() -> None:
    rows = [("Revenue", ["1", "2", "3"]), ("Expenses", ["4", "", "6"]), ("Income", ["7", "8", "9"])]
    raw = "\n".join(
        label.ljust(18) + "  ".join(value.ljust(5) for value in values) for label, values in rows
    ).encode()
    result = parse_content(raw, filing=FILING, document=_document(raw))
    assert len(_table_blocks(result)) == 1
    assert not any(b["block_kind"] == "text" and "Revenue" in b["text"] for b in result.blocks)


def test_legacy_value_tokenizer_splits_packed_currency_fields() -> None:
    fields = _fields("Net income   $109,603    $119,996 $219,363   $238,515")
    assert [(i, f["raw"]) for i, f in enumerate(fields) if f["value"]] == [
        (1, "$109,603"),
        (2, "$119,996"),
        (3, "$219,363"),
        (4, "$238,515"),
    ]
    fields = _fields("TOTAL ...  $ (124,772)  $ 76,250  $ (145,232) $    4,782,278")
    assert [f["raw"] for f in fields if f["value"]][-1] == "$ 4,782,278"


def test_legacy_layout_grid_becomes_text_only() -> None:
    raw = b"<TABLE>\n<S><C>\nKOSS CORPORATION    Commission file number 0-3295\n</TABLE>"
    result = parse_content(raw, filing=FILING, document=_document(raw))
    assert result.documents[0]["status"] == "full"
    assert _table_blocks(result) == []
    assert any(b["block_kind"] == "text" and "KOSS CORPORATION" in b["text"] for b in result.blocks)


def test_plain_year_header_is_not_a_value_cluster() -> None:
    raw = (
        b"                         2007         2006\n"
        b"Revenue           $ 7,449,592  $ 7,204,250\n"
        b"Cost              $ 1,000,000  $ 900,000  \n"
        b"Profit            $ 6,449,592  $ 6,304,250"
    )
    result = parse_content(raw, filing=FILING, document=_document(raw))
    tables = _table_blocks(result)
    assert len(tables) == 1
    assert tables[0]["line_start"] == 1
    assert tables[0]["line_end"] == 4


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        (b"<TABLE>\n<S>       <C>       <C>\nCash      $1  $2\n</TABLE>", "column_collision"),
        (b"<TABLE>\n<C>       <S>\nCash      $1,200\n</TABLE>", "marker_order"),
        (
            b"<TABLE>\n<S>       <C>\nCash      $1,200\n<PAGE>\n"
            b"<S>         <C>\nDebt       $2,000\n</TABLE>",
            "marker_mismatch",
        ),
        (
            b"<TABLE>\n<S>       <C>       <C> <S>       <C>       <C>\n"
            b"Cash      $ 1       $ 2\n</TABLE>",
            "multiple_stub_markers",
        ),
        (
            b"<TABLE>\n<S>          <C>\nHeader       Amount\n"
            b"Canyon Shadows  Real estate  10%  $ 1,131,961\n</TABLE>",
            "text_in_value_column",
        ),
    ],
)
def test_former_ambiguous_legacy_tables_are_table_blocks(raw: bytes, reason: str) -> None:
    raw = b"Intro text\n\n" + raw
    result = parse_content(raw, filing=FILING, document=_document(raw))
    assert result.documents[0]["status"] == "full"
    assert result.documents[0]["diagnostic_codes_json"] == "[]"
    tables = _table_blocks(result)
    assert len(tables) == 1 and tables[0]["text"].startswith("<TABLE>")
    assert any(
        block["block_kind"] == "text" and block["text"] == "Intro text" for block in result.blocks
    )


def test_plain_table_after_heading_emits_label_only_row_without_overlap() -> None:
    raw = (
        "Statement heading " + "x" * 120 + "\n\n"
        "Revenue             1       2       3\n"
        "Supplemental detail\n"
        "Expenses            4       5       6\n"
        "Income              7       8       9\n"
    ).encode()
    result = parse_content(raw, filing=FILING, document=_document(raw))
    table_block = next(b for b in result.blocks if b["block_kind"] == "table")
    assert table_block["line_start"] == 3
    assert all(
        not (b["block_kind"] == "text" and b["line_start"] >= 3 and b["line_end"] <= 6)
        for b in result.blocks
    )


def test_unclosed_legacy_table_is_retained_as_text() -> None:
    raw = b"<TABLE>\n<S>       <C>\nCash      $1,200"
    result = parse_content(raw, filing=FILING, document=_document(raw))
    assert result.documents[0]["status"] == "partial"
    assert _table_blocks(result) == []
    assert any(
        block["block_kind"] == "text" and "<TABLE>" in block["text"] for block in result.blocks
    )


def test_html_pre_nested_markup_remains_document_failure() -> None:
    raw = b"<HTML><BODY><PRE><table><tr><td>x</td></tr></table></PRE></BODY></HTML>"
    result = parse_content(raw, filing=FILING, document=_document(raw))
    assert result.documents[0]["status"] == "failed"
    assert result.documents[0]["diagnostic_codes_json"] == '["html_pre_nested_markup"]'
    assert result.blocks == []


def test_html_blocks_preserve_source_order_and_sections() -> None:
    raw = (
        b"<p>Part I</p><table><tr><td><p>Item 2. MD&amp;A</p></td></tr></table>"
        b"<p>Body</p><table><tr><td><p>Cash</p></td><td><p>$ 100</p></td>"
        b"<td>200</td></tr></table><p>After</p>"
    )
    result = parse_content(raw, filing=FILING, document=_document(raw))
    assert [block["block_kind"] for block in result.blocks] == [
        "text",
        "text",
        "text",
        "table",
        "text",
    ]
    assert not any(
        block["block_kind"] == "text" and block["text"] == "Cash" for block in result.blocks
    )
    assert result.documents[0]["table_count"] == 1
    for block in (result.blocks[2], result.blocks[3], result.blocks[4]):
        assert block["item_code"] == "2"
        assert [part["text"] for part in json.loads(block["heading_path_json"])] == [
            "Part I",
            "Item 2. MD&A",
        ]


def test_legacy_sections_apply_to_wrapped_prose_and_tables() -> None:
    raw = (
        b"PART I\n\nITEM 7. MANAGEMENT'S DISCUSSION\nRevenue grew.\n"
        b"Item 5 of the plan is wrapped prose.\n\n<TABLE>\n"
        b"<S>       <C>        <C>\nCash      1,200      2\n</TABLE>"
    )
    result = parse_content(raw, filing=FILING, document=_document(raw))
    part = next(
        block
        for block in result.blocks
        if block["block_kind"] == "text" and block["text"].strip() == "PART I"
    )
    item = next(
        block
        for block in result.blocks
        if block["block_kind"] == "text"
        and block["text"].strip() == "ITEM 7. MANAGEMENT'S DISCUSSION"
    )
    assert part["text"].strip().splitlines() == ["PART I"]
    assert item["text"].strip().splitlines() == ["ITEM 7. MANAGEMENT'S DISCUSSION"]
    prose = next(
        block
        for block in result.blocks
        if block["block_kind"] == "text" and "Revenue grew." in block["text"]
    )
    table = next(block for block in result.blocks if block["block_kind"] == "table")
    expected = ["PART I", "ITEM 7. MANAGEMENT'S DISCUSSION"]
    assert "Item 5 of the plan" in prose["text"]
    assert prose["item_code"] == table["item_code"] == "7"
    assert [part["text"] for part in json.loads(prose["heading_path_json"])] == expected
    assert [part["text"] for part in json.loads(table["heading_path_json"])] == expected


@pytest.mark.parametrize(
    ("rows_html", "is_table"),
    [
        (
            "<tr><td>Revenue</td><td>$</td><td>1,200</td></tr><tr><td>Cost</td><td>(300</td><td>)</td></tr>",
            True,
        ),
        (
            "<tr><td>10.1</td><td>Offer Letter for Jane Doe dated March 27, 2025</td></tr>"
            "<tr><td>31.1</td><td>Certification of Chief Executive Officer</td></tr>"
            "<tr><td>32.1</td><td>Certification pursuant to Section 906</td></tr>",
            False,
        ),
        (
            "<tr><td>Item 1.</td><td>Business</td><td>3</td></tr>"
            "<tr><td>Item 1A.</td><td>Risk Factors</td><td>7</td></tr>"
            "<tr><td>Item 2.</td><td>Properties</td><td>20</td></tr>",
            False,
        ),
        ("<tr><td>Goldman Sachs June 2024 Form 10-Q</td><td>62</td></tr>", False),
        ("<tr><td>Customer A</td><td>48%</td></tr>", True),
    ],
)
def test_html_table_classification(rows_html: str, is_table: bool) -> None:
    raw = f"<html><body><table>{rows_html}</table></body></html>".encode()
    result = parse_content(raw, filing=FILING, document=_document(raw))
    assert (len(_table_blocks(result)) == 1) is is_table
