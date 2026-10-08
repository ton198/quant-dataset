"""Regression coverage for visible text adjacent to excluded HTML subtrees."""

from __future__ import annotations

import hashlib

from financial_extraction.domain import VerifiedDocument
from financial_extraction.evidence import build_evidence

LIMITS = {
    "maxpayloadbytes": 10_000,
    "maxnodes": 1_000,
    "maxcells": 100,
    "maxgridspan": 1_000,
}
_HOST_RECORD = b"synthetic regression fixture host record"


def _document(payload: bytes) -> VerifiedDocument:
    return VerifiedDocument(
        source_id="opaque-regression-source",
        document_id="opaque-regression-document",
        source_type="synthetic_html",
        fixture_only=True,
        payload=payload,
        encoding="utf-8",
        raw_sha256=hashlib.sha256(_HOST_RECORD).hexdigest(),
        raw_bytes=len(_HOST_RECORD),
        payload_sha256=hashlib.sha256(payload).hexdigest(),
        preprocessing_revision="fixture-html-v1",
        origin={"fixture": True},
    )


def test_excluded_subtrees_preserve_visible_cell_text_and_ordered_tails():
    payload = (
        b"<table><tr><th>Metric</th>"
        b"<td>before<script>ignore()</script>after</td>"
        b"<td><script>ignore()</script>1,234</td>"
        b"<td>prefix <span hidden>hidden parent<b>hidden child</b> child-tail"
        b"<script>hidden script content</script></span> 2,345</td>"
        b"<td>first<script>script content</script> middle"
        b"<span hidden>hidden sibling</span> last"
        b"<style>.ignored { color: red; }</style> end</td>"
        b"<td><style>ignored style</style>3,456</td>"
        b"</tr></table>"
    )

    bundle = build_evidence(_document(payload), limits=LIMITS)
    cells = {cell.column_index: cell for cell in bundle.cells}

    assert cells[1].text == "beforeafter"
    assert cells[2].text == "1,234"
    assert cells[3].text == "prefix 2,345"
    assert cells[4].text == "first middle last end"
    assert cells[5].text == "3,456"
    assert all(
        excluded not in bundle.canonical_text
        for excluded in (
            "ignore()",
            "hidden parent",
            "hidden child",
            "child-tail",
            "hidden script content",
            "script content",
            "hidden sibling",
            "ignored style",
            ".ignored",
        )
    )
    assert any("script/style subtree(s) excluded" in item for item in bundle.limitations)
    assert any("explicitly hidden HTML subtree(s) excluded" in item for item in bundle.limitations)

    amount = cells[2]
    assert bundle.canonical_text[amount.canonical_start : amount.canonical_end] == "1,234"
    assert amount.raw_byte_start is None
    assert amount.raw_byte_end is None


def test_cleanup_is_deterministic_and_evidence_ids_remain_bound_to_raw_payload():
    payload = (
        b"<table><tr><td>Revenue</td>"
        b"<td><script>ignore()</script>1,234</td></tr></table>"
    )
    first = build_evidence(_document(payload), limits=LIMITS)
    repeated = build_evidence(_document(payload), limits=LIMITS)

    assert first.canonical_text == repeated.canonical_text
    assert first.canonical_text_hash == repeated.canonical_text_hash
    assert first.bundle_hash == repeated.bundle_hash
    assert [block.block_id for block in first.blocks] == [
        block.block_id for block in repeated.blocks
    ]
    assert [cell.cell_id for cell in first.cells] == [cell.cell_id for cell in repeated.cells]
    assert first.canonical_text_hash == hashlib.sha256(
        first.canonical_text.encode("utf-8")
    ).hexdigest()

    changed_ignored_content = payload.replace(b"ignore()", b"different ignored code")
    changed = build_evidence(_document(changed_ignored_content), limits=LIMITS)
    assert changed.canonical_text == first.canonical_text
    assert changed.canonical_text_hash == first.canonical_text_hash
    assert [cell.cell_id for cell in changed.cells] != [cell.cell_id for cell in first.cells]
    assert changed.bundle_hash != first.bundle_hash

    amount = next(cell for cell in first.cells if cell.text == "1,234")
    assert first.canonical_text[amount.canonical_start : amount.canonical_end] == amount.text
    assert amount.raw_byte_start is None
    assert amount.raw_byte_end is None
