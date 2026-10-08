from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from financial_extraction.domain import ExtractionLimits, ExtractionTask, VerifiedDocument
from financial_extraction.runtime import ModelDescriptor, ModelRequest, ModelResponse, ReplayStore, StoreError
from financial_extraction.workflow import run_extraction


class _Client:
    descriptor = ModelDescriptor("synthetic", "offline")

    def __init__(self, raw_json: str):
        self.raw_json = raw_json
        self.calls = 0

    def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        return ModelResponse(self.raw_json, input_tokens=10, output_tokens=10)


def _document() -> VerifiedDocument:
    payload = b"<html><body><table><tr><td>Revenue</td><td>12</td></tr></table></body></html>"
    return VerifiedDocument(
        "source", "doc", "synthetic_html", True, payload, "utf-8",
        hashlib.sha256(b"host raw bytes").hexdigest(), len(b"host raw bytes"),
        hashlib.sha256(payload).hexdigest(), "test-v1",
        limits={"maxpayloadbytes": 10_000, "maxnodes": 100, "maxcells": 100, "maxgridspan": 100},
    )


def _response(document: VerifiedDocument) -> str:
    from financial_extraction.evidence import build_evidence, iter_windows
    from financial_extraction.workflow.references import build_reference_map

    bundle = build_evidence(document, limits=document.limits)
    window = next(iter_windows(bundle, max_request_bytes=10_000_000))
    short_of = {long: short for short, long in build_reference_map(window).items()}
    label = next(cell for cell in bundle.cells if cell.text == "Revenue")
    value = next(cell for cell in bundle.cells if cell.text == "12")
    record = {
        "source_label": "Revenue", "name": "open revenue", "category": "financial",
        "value": "12", "value_type": "number",
        "value_source": {"ref": short_of[value.cell_id], "text": "12"},
        "label_source": {"ref": short_of[label.cell_id], "text": "Revenue"},
        "numeric_policy_id": None, "unit": None, "currency": None, "scale": None,
        "unit_sources": [], "currency_sources": [], "scale_sources": [],
        "period": {"kind": None, "start": None, "end": None, "sources": []}, "dimensions": [],
    }
    return json.dumps({"records": [record]})


def test_runner_persists_exact_response_and_replays_without_second_call(tmp_path: Path):
    document = _document()
    client = _Client(_response(document))
    work_dir = tmp_path / "private-run"
    args = dict(
        documents=[document], client=client, task=ExtractionTask(()),
        limits=ExtractionLimits(), work_dir=work_dir,
    )
    first = run_extraction(**args)
    assert first.status == "complete"
    assert len(first.windows) == 1 and len(first.records) == 1
    assert first.records[0].status == "unresolved"  # no policy was configured
    assert first.publishable is False
    assert client.calls == 1

    second = run_extraction(**args)
    assert second.status == "complete"
    assert second.records == first.records
    assert second.windows[0].replayed is True
    assert client.calls == 1


def test_pending_request_is_never_automatically_resubmitted(tmp_path: Path):
    request = ModelRequest(
        descriptor=ModelDescriptor("synthetic", "offline"),
        messages=({"role": "user", "content": "fixture"},),
        schema={"type": "object"}, max_output_tokens=100,
    )
    store = ReplayStore(tmp_path / "journal")
    store.begin(request)
    with pytest.raises(StoreError, match="unknown"):
        store.begin(request)
