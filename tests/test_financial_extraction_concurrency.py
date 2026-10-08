from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from financial_extraction.domain import ExtractionLimits, ExtractionTask, NumericPolicy, VerifiedDocument
from financial_extraction.evidence import build_evidence, iter_windows
from financial_extraction.runtime import ModelDescriptor, ModelResponse, UnknownRequestError
from financial_extraction.workflow import run_extraction, run_extraction_async
from financial_extraction.workflow.runner import _measure


def documents(count):
    payload = b"<html><body><p>Revenue is 12.</p></body></html>"
    base = VerifiedDocument(
        "source", "doc", "synthetic_html", True, payload, "utf-8",
        hashlib.sha256(payload).hexdigest(), len(payload),
        hashlib.sha256(payload).hexdigest(), "test-v1",
        limits={"maxpayloadbytes": 10000, "maxnodes": 100, "maxcells": 100, "maxgridspan": 100},
    )
    return [replace(base, source_id=f"source-{i}", document_id=f"doc-{i}") for i in range(count)]


def test_32_calls_overlap_and_results_journal_replay_keep_source_order(tmp_path):
    class Client:
        descriptor = ModelDescriptor("synthetic", "concurrent")

        def __init__(self):
            self.barrier = threading.Barrier(32, timeout=10)
            self.lock = threading.Lock()
            self.active = 0
            self.peak = 0
            self.calls = 0

        def complete(self, request):
            with self.lock:
                self.active += 1
                self.calls += 1
                self.peak = max(self.peak, self.active)
            self.barrier.wait()
            with self.lock:
                self.active -= 1
            return ModelResponse('{"records": []}')

    client = Client()
    docs = documents(64)
    args = dict(documents=docs, client=client, task=ExtractionTask(()),
                limits=ExtractionLimits(), work_dir=tmp_path, workers=32,
                model_workers=32, prefetch_windows=32)
    result = run_extraction(**args)
    assert result.status == "complete"
    assert [w.document_id for w in result.windows] == [d.document_id for d in docs]
    assert client.peak == 32
    assert client.calls == 64
    rows = [json.loads(line) for line in (tmp_path / "journal.jsonl").read_text().splitlines()]
    states = {row["identity"]: row["state"] for row in rows}
    assert len(states) == 64 and set(states.values()) == {"complete"}
    replay = run_extraction(**args)
    assert all(w.replayed for w in replay.windows)
    assert client.calls == 64


def test_completed_later_window_refills_slot_before_slow_first_finishes(tmp_path):
    docs = documents(3)
    task = ExtractionTask(())
    limits = ExtractionLimits()
    window_ids = []
    record_payloads = {}
    for doc in docs:
        from financial_extraction.workflow.references import build_reference_map

        bundle = build_evidence(doc, limits=doc.limits)
        window = next(iter_windows(
            bundle, max_request_bytes=limits.max_request_bytes,
            measure_request=lambda w: _measure(w, client=Client(), task=task, limits=limits),
        ))
        mapping = build_reference_map(window)
        short_of = {long: short for short, long in mapping.items()}
        window_ids.append(window.window_id)
        ref = short_of[window.core[0].ref]
        record_payloads[window.window_id] = json.dumps({"records": [{
            "source_label": "Revenue", "name": doc.document_id, "category": "financial",
            "value": "12", "value_type": "number",
            "value_source": {"ref": ref, "text": "12"},
            "label_source": {"ref": ref, "text": "Revenue"},
            "numeric_policy_id": "us", "unit": None, "currency": None, "scale": None,
            "unit_sources": [], "currency_sources": [], "scale_sources": [],
            "period": {"kind": None, "start": None, "end": None, "sources": []},
            "dimensions": [],
        }]})
    released = threading.Event()

    class RefillClient:
        descriptor = ModelDescriptor("synthetic", "concurrent")

        def complete(self, request):
            window_id = json.loads(request.messages[-1]["content"].split("Evidence window:\n")[1])["window_id"]
            if window_id == window_ids[0]:
                if not released.wait(10):
                    raise RuntimeError("first blocked; coordinator failed to refill")
            elif window_id == window_ids[2]:
                released.set()
            return ModelResponse(record_payloads[window_id])

    result = run_extraction(
        docs, client=RefillClient(), task=ExtractionTask((NumericPolicy("us", ".", ","),)),
        limits=limits, work_dir=tmp_path, workers=2, model_workers=2, prefetch_windows=2,
    )
    assert result.status == "complete"
    assert released.is_set()
    assert [w.document_id for w in result.windows] == [d.document_id for d in docs]
    assert [r.extracted.name for r in result.records] == [d.document_id for d in docs]
    assert all(r.status == "valid" and r.normalized_amount == "12" for r in result.records)


class Client:
    descriptor = ModelDescriptor("synthetic", "concurrent")


def test_provider_and_schema_failures_are_isolated_and_never_retried(tmp_path):
    class FailureClient:
        descriptor = Client.descriptor

        def __init__(self):
            self.lock = threading.Lock()
            self.calls = 0

        def complete(self, request):
            with self.lock:
                index = self.calls
                self.calls += 1
            if index == 0:
                raise RuntimeError("simulated provider limit")
            if index == 1:
                return ModelResponse('{"records": [{"value": "12"}]}')
            return ModelResponse('{"records": []}')

    client = FailureClient()
    args = dict(documents=documents(3), client=client, task=ExtractionTask(()),
                limits=ExtractionLimits(), work_dir=tmp_path, workers=3)
    first = run_extraction(**args)
    assert first.status == "partial_failure"
    assert sum(w.status == "failed" for w in first.windows) == 2
    assert sum(w.status == "validated" for w in first.windows) == 1
    assert client.calls == 3
    second = run_extraction(**args)
    assert second.status == "partial_failure"
    assert client.calls == 3
    assert any("unknown" in (w.error or "") for w in second.windows)
    assert any("missing required keys" in (w.error or "") for w in second.windows)


@pytest.mark.parametrize("workers", [0, -1, True, 1.5, "32"])
def test_invalid_worker_count_rejected_before_creating_journal(tmp_path, workers):
    with pytest.raises(ValueError, match="workers must be a positive integer"):
        run_extraction(documents(1), client=Client(), task=ExtractionTask(()),
                       limits=ExtractionLimits(), work_dir=tmp_path / "work", workers=workers)
    assert not (tmp_path / "work").exists()


def test_default_model_capacity_is_one(tmp_path):
    class SerialClient:
        descriptor = Client.descriptor

        def __init__(self):
            self.active = 0
            self.peak = 0
            self.lock = threading.Lock()

        def complete(self, request):
            with self.lock:
                self.active += 1
                self.peak = max(self.peak, self.active)
            time.sleep(0.01)
            with self.lock:
                self.active -= 1
            return ModelResponse('{"records": []}')

    client = SerialClient()
    result = run_extraction(documents(8), client=client, task=ExtractionTask(()),
                            limits=ExtractionLimits(), work_dir=tmp_path)
    assert result.status == "complete"


def test_document_iterator_failure_marks_run_partial(tmp_path):
    def inputs():
        yield documents(1)[0]
        raise RuntimeError("document source failed")

    class EmptyClient:
        descriptor = Client.descriptor

        def complete(self, request):
            return ModelResponse('{"records": []}')

    result = run_extraction(
        inputs(), client=EmptyClient(), task=ExtractionTask(()), limits=ExtractionLimits(),
        work_dir=tmp_path,
    )

    assert result.status == "partial_failure"
    assert any(problem.code == "WINDOW_PREPARATION_FAILED" for problem in result.problems)

def test_unknown_outcome_stops_queued_sends_and_calls_back_once(tmp_path):
    class UnknownClient:
        descriptor = ModelDescriptor("synthetic", "unknown-stop")

        def __init__(self):
            self.calls = 0

        def complete(self, request):
            self.calls += 1
            raise UnknownRequestError("outcome unknown")

    async def run():
        client = UnknownClient()
        callbacks = []

        async def on_complete(outcome):
            callbacks.append(outcome.window_id)

        result = await asyncio.wait_for(
            run_extraction_async(
                documents(8), client=client, task=ExtractionTask(()),
                limits=ExtractionLimits(), work_dir=tmp_path, workers=1,
                model_workers=1, prefetch_windows=4, on_window_complete=on_complete,
            ),
            timeout=10,
        )
        assert result.status == "partial_failure"
        assert client.calls == 1
        assert len(result.windows) > 1
        assert len(callbacks) == len(result.windows)
        assert len(set(callbacks)) == len(callbacks)
        assert set(callbacks) == {window.window_id for window in result.windows}

    asyncio.run(run())

def test_duplicate_inflight_request_never_calls_provider_twice(tmp_path):
    release = threading.Event()

    class DuplicateClient:
        descriptor = Client.descriptor
        calls = 0

        def complete(self, request):
            self.calls += 1
            if not release.wait(10):
                raise RuntimeError("document iterator failed to release call")
            return ModelResponse('{"records": []}')

    document = documents(1)[0]

    def inputs():
        yield document
        yield document
        release.set()

    client = DuplicateClient()
    callback_ids = []
    result = run_extraction(
        inputs(), client=client, task=ExtractionTask(()), limits=ExtractionLimits(),
        work_dir=tmp_path, workers=2,
        on_window_complete=lambda outcome: callback_ids.append(outcome.window_id),
    )
    assert client.calls == 1
    assert [w.status for w in result.windows] == ["validated", "validated"]
    assert result.windows[1].replayed is True
    assert callback_ids == [w.window_id for w in result.windows]
