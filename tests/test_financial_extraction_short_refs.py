"""Short-ref window mapping: exact restore, strict rejection, replay identity."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from financial_extraction.domain import (
    ExtractionLimits,
    ExtractionTask,
    NumericPolicy,
    VerifiedDocument,
)
from financial_extraction.evidence import build_evidence, iter_windows
from financial_extraction.runtime import ModelDescriptor, ModelResponse
from financial_extraction.workflow import (
    build_reference_map,
    encode_short_ref,
    is_wellformed_short_ref,
    run_extraction,
)


def _mutate(value: str, position: int) -> str:
    replacement = "1" if value[position] == "0" else "0"
    return value[:position] + replacement + value[position + 1:]

def _document(payload: bytes | None = None) -> VerifiedDocument:
    body = payload or b"<html><body><table><tr><td>Revenue</td><td>$8.2</td></tr></table></body></html>"
    return VerifiedDocument(
        "source", "doc", "synthetic_html", True, body, "utf-8",
        hashlib.sha256(b"host raw bytes").hexdigest(), len(b"host raw bytes"),
        hashlib.sha256(body).hexdigest(), "test-v1",
        limits={"maxpayloadbytes": 10_000, "maxnodes": 100, "maxcells": 100, "maxgridspan": 100},
    )


def _window(document=None):
    document = document or _document()
    bundle = build_evidence(document, limits=document.limits)
    return document, bundle, next(iter_windows(bundle, max_request_bytes=10_000_000))


def _record(ref: str, text: str = "Revenue") -> dict:
    return {
        "source_label": "Revenue", "name": "open revenue", "category": "financial",
        "value": text, "value_type": "text",
        "value_source": {"ref": ref, "text": text},
        "label_source": {"ref": ref, "text": text},
        "numeric_policy_id": None, "unit": None, "currency": None, "scale": None,
        "unit_sources": [], "currency_sources": [], "scale_sources": [],
        "period": {"kind": None, "start": None, "end": None, "sources": []}, "dimensions": [],
    }


class _Client:
    descriptor = ModelDescriptor("synthetic", "offline")

    def __init__(self, payload: str):
        self.payload = payload
        self.calls = 0
        self.seen_requests = []

    def complete(self, request):
        self.calls += 1
        self.seen_requests.append(request)
        return ModelResponse(self.payload)


def test_short_ids_are_fixed_width_with_two_check_chars_and_stable_mapping():
    _, _, window = _window()
    first = build_reference_map(window)
    assert build_reference_map(window) == first
    for position, (short, long) in enumerate(first.items(), start=1):
        assert short == encode_short_ref(position)
        assert is_wellformed_short_ref(short)
    # core/context duplicates share one ID
    assert len(first) == len({item.ref for item in (*window.core, *window.context)})


def test_provider_window_carries_short_refs_and_hides_long_refs(tmp_path):
    document, _, window = _window()
    mapping = build_reference_map(window)
    client = _Client(json.dumps({"records": [
        _record(next(iter(mapping)), window.core[0].text),
    ]}))
    run_extraction(
        [document], client=client, task=ExtractionTask(()),
        limits=ExtractionLimits(), work_dir=tmp_path,
    )
    user = client.seen_requests[0].messages[-1]["content"]
    long_refs = [long for long in mapping.values()]
    assert all(short in user for short in mapping)
    assert all(long not in user for long in long_refs)
    assert client.seen_requests[0].reference_map == mapping


def test_all_quote_fields_restore_original_refs(tmp_path):
    document, _, window = _window()
    mapping = build_reference_map(window)
    shorts = list(mapping)
    text = window.core[0].text
    row = _record(shorts[0], text)
    row["unit_sources"] = [{"ref": shorts[0], "text": text}]
    row["currency_sources"] = [{"ref": shorts[0], "text": text}]
    row["scale_sources"] = [{"ref": shorts[0], "text": text}]
    row["period"] = {"kind": None, "start": None, "end": None,
                     "sources": [{"ref": shorts[0], "text": text}]}
    row["dimensions"] = [{"name": "seg", "value": "w", "sources": [{"ref": shorts[0], "text": text}]}]
    client = _Client(json.dumps({"records": [row]}))
    result = run_extraction(
        [document], client=client, task=ExtractionTask(()),
        limits=ExtractionLimits(), work_dir=tmp_path,
    )
    assert result.status == "complete"
    record = result.records[0]
    long = mapping[shorts[0]]
    assert record.extracted.value_source.ref == long
    assert record.extracted.label_source.ref == long
    assert {quote.ref for quote in record.extracted.unit_sources} == {long}
    assert {quote.ref for quote in record.extracted.currency_sources} == {long}
    assert {quote.ref for quote in record.extracted.scale_sources} == {long}
    assert {quote.ref for quote in record.extracted.period.sources} == {long}
    assert {quote.ref for dim in record.extracted.dimensions for quote in dim.sources} == {long}
    # raw model response keeps short IDs verbatim
    journal = (tmp_path / "journal.jsonl").read_text(encoding="utf-8")
    assert shorts[0] in journal

def test_unknown_long_and_prefix_and_typo_refs_stay_unresolved(tmp_path):
    document, bundle, window = _window()
    mapping = build_reference_map(window)
    label = next(item for item in window.core if item.text == "Revenue")
    short = next(short for short, long in mapping.items() if long == label.ref)
    long = label.ref
    text = label.text
    typo = _mutate(short, 3)
    assert not is_wellformed_short_ref(typo)
    assert typo not in mapping
    rows = [
        _record("r999999ZZ", text),  # unknown short
        _record(long, text),  # original long ID returned as-is
        _record(short[:5], text),  # prefix
        _record(typo, text),  # one-char typo
        _record(_mutate(short, 7), text),  # wrong check char
        _record(short, text),  # one valid control
    ]
    client = _Client(json.dumps({"records": rows}))
    result = run_extraction(
        [document], client=client, task=ExtractionTask(()),
        limits=ExtractionLimits(), work_dir=tmp_path,
    )
    assert result.status == "complete"
    statuses = [record.status for record in result.records]
    assert statuses[-1] == "valid"
    assert statuses[:-1] == ["unresolved"] * 5
    problems = [problem.code for record in result.records[:-1] for problem in record.problems]
    assert problems and all(code == "SOURCE_OUTSIDE_WINDOW" for code in problems)


def test_repeated_dollar_wrong_id_single_char_errors_rejected(tmp_path):
    document = _document(
        b"<html><body><table><tr><td>Cash</td><td>$</td><td>$</td></tr></table></body></html>"
    )
    _, _, window = _window(document)
    mapping = build_reference_map(window)
    reverse = {long: short for short, long in mapping.items()}
    dollar_items = [item for item in window.core if item.text == "$"]
    assert len(dollar_items) == 2
    short = reverse[dollar_items[0].ref]
    row = _record(short, "$")
    # A copied ID with only its index changed must not silently select the other '$'.
    other = reverse[dollar_items[1].ref]
    changed_body = other[:7] + short[7:]
    assert sum(a != b for a, b in zip(short, changed_body)) == 1
    row["value_source"] = {"ref": changed_body, "text": "$"}
    client = _Client(json.dumps({"records": [row]}))
    result = run_extraction(
        [document], client=client, task=ExtractionTask(()),
        limits=ExtractionLimits(), work_dir=tmp_path,
    )
    assert result.records[0].status == "unresolved"
    assert "SOURCE_OUTSIDE_WINDOW" in {problem.code for problem in result.records[0].problems}


def test_mapping_change_shifts_identity_and_replay_needs_no_second_call(tmp_path):
    document = _document()
    _, _, window = _window(document)
    mapping = build_reference_map(window)
    short = next(iter(mapping))
    client = _Client(json.dumps({"records": [_record(short, window.core[0].text)]}))
    args = dict(
        documents=[document], client=client, task=ExtractionTask(()),
        limits=ExtractionLimits(), work_dir=tmp_path,
    )
    first = run_extraction(**args)
    assert client.calls == 1
    second = run_extraction(**args)
    assert second.records == first.records
    assert second.windows[0].replayed is True
    assert client.calls == 1
    assert client.seen_requests[0].reference_map != {}
    # a mapping change is a different request identity (journal + identity)
    from financial_extraction.runtime import ModelRequest as _MR

    altered = dict(mapping)
    altered[short] = "other-long-ref"
    assert _MR(
        client.seen_requests[0].descriptor, client.seen_requests[0].messages,
        client.seen_requests[0].schema, client.seen_requests[0].max_output_tokens,
        altered,
    ).identity != client.seen_requests[0].identity


def test_core_context_duplicate_dedup_is_deterministic():
    from dataclasses import replace

    _, _, window = _window()
    repeated = replace(window, context=(window.core[0],))
    assert build_reference_map(repeated) == build_reference_map(window)
    assert len(build_reference_map(repeated)) == len(window.core)


def test_every_single_character_substitution_and_adjacent_digit_swap_is_rejected():
    alphabet = "r0123456789ABCDEFG"
    for index in (1, 15, 16, 255, 256, 0x123456, 0xFFFFFF):
        ref = encode_short_ref(index)
        assert is_wellformed_short_ref(ref)
        for position, original in enumerate(ref):
            for char in alphabet:
                if char != original:
                    assert not is_wellformed_short_ref(ref[:position] + char + ref[position + 1:])
        for position in range(1, 6):
            if ref[position] != ref[position + 1]:
                swapped = ref[:position] + ref[position + 1] + ref[position] + ref[position + 2:]
                assert not is_wellformed_short_ref(swapped)
