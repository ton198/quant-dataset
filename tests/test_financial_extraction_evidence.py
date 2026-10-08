"""Synthetic, offline evidence extraction and safety-bound tests."""

from __future__ import annotations

import hashlib
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from financial_extraction.domain import VerifiedDocument
from financial_extraction.evidence import (
    EvidenceBuildError,
    EvidenceLimitError,
    build_evidence,
    iter_windows,
    render_window,
)
FIXTURE = (
    Path(__file__).resolve().parent / "fixtures/financial_extraction/two_period_statement.html"
)
LIMITS = {"maxpayloadbytes": 100_000, "maxnodes": 1_000, "maxcells": 100, "maxgridspan": 1_000}


def _document(payload: bytes | None = None, *, encoding: str = "utf-8"):
    data = FIXTURE.read_bytes() if payload is None else payload
    return VerifiedDocument(
        source_id="opaque-synthetic-source",
        document_id="opaque-synthetic-document",
        source_type="synthetic_html",
        fixture_only=True,
        payload=data,
        encoding=encoding,
        raw_sha256=hashlib.sha256(b"synthetic host-side raw record").hexdigest(),
        raw_bytes=len(b"synthetic host-side raw record"),
        payload_sha256=hashlib.sha256(data).hexdigest(),
        preprocessing_revision="fixture-html-v1",
        origin={"fixture": True},
    )


def test_blocks_cells_grid_context_nesting_and_stable_ids():
    first = build_evidence(_document(), limits=LIMITS)
    second = build_evidence(_document(), limits=LIMITS)
    assert first.canonical_text_hash == second.canonical_text_hash
    assert first.bundle_hash == second.bundle_hash
    assert [item.block_id for item in first.blocks] == [item.block_id for item in second.blocks]
    assert [item.cell_id for item in first.cells] == [item.cell_id for item in second.cells]

    metric_header = next(cell for cell in first.cells if cell.text == "Metric / 指标")
    assert metric_header.rowspan == 2
    assert metric_header.covered_positions == ((0, 0), (1, 0))
    span_header = next(cell for cell in first.cells if cell.text == "Years Ended December 31")
    assert span_header.colspan == 2
    assert span_header.covered_positions == ((0, 1), (0, 2))

    nested_table = next(
        block
        for block in first.blocks
        if block.kind == "table" and block.parent_block_id is not None
    )
    nested_cell = next(
        cell
        for cell in first.cells
        if cell.table_id == nested_table.block_id and cell.text == "1,234"
    )
    outer_cell = next(cell for cell in first.cells if cell.text.startswith("Outer cell text"))
    assert "1,234" not in outer_cell.text
    assert outer_cell.text == "Outer cell text tail"
    assert nested_cell.parent_table_id == outer_cell.table_id
    assert len({cell.cell_id for cell in first.cells}) == len(first.cells)

    kinds = {block.kind for block in first.blocks}
    assert {"heading", "paragraph", "list_item", "table", "footnote"} <= kinds
    assert "script" not in first.canonical_text
    assert "untrusted active content" not in first.canonical_text
    assert all(cell.raw_byte_start is None and cell.raw_byte_end is None for cell in first.cells)
    assert any("not original byte coordinates" in limitation for limitation in first.limitations)


def test_windows_cover_every_evidence_item_once_and_render_stably():
    bundle = build_evidence(_document(), limits=LIMITS)
    first = tuple(iter_windows(bundle, max_request_bytes=1_000_000))
    second = tuple(iter_windows(bundle, max_request_bytes=1_000_000))
    expected = {block.block_id for block in bundle.blocks} | {
        cell.cell_id for cell in bundle.cells
    }
    core = [item.ref for window in first for item in window.core]
    assert set(core) == expected
    assert len(core) == len(expected)
    assert [render_window(window) for window in first] == [render_window(window) for window in second]
    assert all(window.document_id == "opaque-synthetic-document" for window in first)
    assert any(window.context for window in first)




def test_strict_decode_payload_hash_limits_and_unsafe_dtd_fail_closed():
    valid = FIXTURE.read_bytes()
    with pytest.raises(EvidenceBuildError, match="decoded strictly"):
        build_evidence(_document(b"<p>\xff</p>"), limits=LIMITS)
    mismatched = replace(_document(valid), payload_sha256="0" * 64)
    with pytest.raises(EvidenceBuildError, match="does not match"):
        build_evidence(mismatched, limits=LIMITS)
    with pytest.raises(EvidenceLimitError, match="maxpayloadbytes"):
        build_evidence(_document(valid), limits={**LIMITS, "maxpayloadbytes": 8})
    with pytest.raises(EvidenceLimitError, match="maxcells"):
        build_evidence(_document(valid), limits={**LIMITS, "maxcells": 2})
    with pytest.raises(EvidenceLimitError, match="maxnodes"):
        build_evidence(_document(valid), limits={**LIMITS, "maxnodes": 3})
    with pytest.raises(EvidenceBuildError, match="DTD"):
        payload = b"<!DOCTYPE html SYSTEM 'https://example.invalid/evil.dtd'><html><body>data</body></html>"
        build_evidence(_document(payload), limits=LIMITS)
    with pytest.raises(EvidenceBuildError, match="entity declarations"):
        payload = b"<!DOCTYPE html [<!ENTITY x SYSTEM 'file:///etc/passwd'>]><html><body>&x;</body></html>"
        build_evidence(_document(payload), limits=LIMITS)


def test_grid_expansion_limits_and_malformed_spans_are_bounded():
    payload = b"<html><body><table><tr><td colspan='3'>X</td></tr></table></body></html>"
    with pytest.raises(EvidenceLimitError, match="maxgridspan"):
        build_evidence(
            _document(payload),
            limits={"maxpayloadbytes": 10_000, "maxnodes": 100, "maxcells": 10, "maxgridspan": 2},
        )
    malformed = b"<html><body><table><tr><td colspan='oops'>X</td></tr></table></body></html>"
    with pytest.raises(EvidenceBuildError, match="integer"):
        build_evidence(
            _document(malformed),
            limits={"maxpayloadbytes": 10_000, "maxnodes": 100, "maxcells": 10, "maxgridspan": 100},
        )
    out_of_bounds = b"<html><body><table><tr><td rowspan='2'>X</td></tr></table></body></html>"
    with pytest.raises(EvidenceBuildError, match="beyond the physical table rows"):
        build_evidence(
            _document(out_of_bounds),
            limits={"maxpayloadbytes": 10_000, "maxnodes": 100, "maxcells": 10, "maxgridspan": 100},
        )


def test_hidden_content_is_explicitly_excluded_and_malformed_html_reports_recovery():
    payload = (
        b"<html><body><h1>Section</h1><p>Visible</p>"
        b"<p hidden>Hidden attribute</p><p style='display:none'>Hidden style</p>"
        b"<p>Recovered content<p></body></html>"
    )
    bundle = build_evidence(_document(payload), limits=LIMITS)
    assert "Visible" in bundle.canonical_text
    assert "Recovered content" in bundle.canonical_text
    assert "Hidden attribute" not in bundle.canonical_text
    assert "Hidden style" not in bundle.canonical_text
    assert any("explicitly hidden" in limitation for limitation in bundle.limitations)
    assert any("recovery-mode parsing" in limitation for limitation in bundle.limitations)


def test_optional_lxml_is_not_imported_by_domain_or_package_import():
    script = f"""
import builtins, hashlib, sys
sys.path.insert(0, {str(SOURCE_ROOT)!r})
original = builtins.__import__
def guard(name, *args, **kwargs):
    if name == 'lxml' or name.startswith('lxml.'):
        raise ModuleNotFoundError('simulated missing optional dependency')
    return original(name, *args, **kwargs)
builtins.__import__ = guard
import financial_extraction.domain
from financial_extraction.domain import VerifiedDocument
from financial_extraction.evidence import build_evidence
payload = b'<html><body><p>fixture</p></body></html>'
document = VerifiedDocument(
    's', 'd', 'synthetic', True, payload, 'utf-8', '0' * 64, 1,
    hashlib.sha256(payload).hexdigest(), 'v1'
)
try:
    build_evidence(
        document,
        limits={{'maxpayloadbytes': 100, 'maxnodes': 100, 'maxcells': 10,
                'maxgridspan': 100}}
    )
except ImportError as exc:
    assert 'extraction' in str(exc) and 'lxml' in str(exc)
else:
    raise AssertionError('missing optional lxml must be explained')
"""
    subprocess.run([sys.executable, "-c", script], check=True, capture_output=True, text=True)
