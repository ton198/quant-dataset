from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from financial_extraction.domain import (
    OUTPUT_SCHEMA,
    Dimension,
    ExtractedRecord,
    ExtractionTask,
    ExtractionWindow,
    NumericPolicy,
    Period,
    Quote,
    SourceLocation,
    WindowItem,
    parse_json_strict,
    records_from_json,
    validate_window,
)


def _walk_objects(schema: dict):
    if schema.get("type") == "object":
        assert set(schema["required"]) == set(schema["properties"])
        for child in schema["properties"].values():
            yield from _walk_objects(child)
    if "items" in schema:
        yield from _walk_objects(schema["items"])


def test_output_schema_requires_every_property_at_each_object_level():
    assert tuple(_walk_objects(OUTPUT_SCHEMA)) == ()


def test_open_field_names_parse_and_unknown_keys_fail_closed():
    row = {
        "source_label": "Sales from new segment", "name": "Orbital subscriptions",
        "category": "operational metric", "value": "1,234.50", "value_type": "number",
        "value_source": {"ref": "cell-1", "text": "Orbital subscriptions 1,234.50"},
        "label_source": {"ref": "cell-1", "text": "Orbital subscriptions"},
        "numeric_policy_id": "us", "unit": None, "currency": "USD", "scale": "1000",
        "unit_sources": [], "currency_sources": [], "scale_sources": [],
        "period": {"kind": "duration", "start": None, "end": "2024-12-31", "sources": []},
        "dimensions": [{"name": "region west", "value": "new territory", "sources": []}],
    }
    result = records_from_json(json.dumps({"records": [row]}))
    assert result[0].name == "Orbital subscriptions"
    row["extra"] = "ignored"
    with pytest.raises(ValueError, match="unknown keys"):
        records_from_json(json.dumps({"records": [row]}))
    with pytest.raises(ValueError, match="duplicate JSON key"):
        parse_json_strict('{"records":[],"records":[]}')


def test_window_rejects_cross_document_refs_and_normalizes_exact_decimal():
    source = SourceLocation("doc", "cell-1", "/table/tr/td", 0, 40)
    item = WindowItem("cell-1", "cell", "Revenue 1,234.50 USD", source)
    window = ExtractionWindow("win", "doc", (item,))
    record = ExtractedRecord(
        "Revenue", "Custom revenue", "financial", "1,234.50", "number",
        Quote("cell-1", "1,234.50"), Quote("cell-1", "Revenue"),
        numeric_policy_id="us", currency="USD", scale="1000",
        currency_sources=(Quote("cell-1", "USD"),), period=Period(),
        dimensions=(Dimension("segment", "west"),),
    )
    task = ExtractionTask((NumericPolicy("us", ".", ","),))
    valid = validate_window((record,), window=window, task=task).records[0]
    assert valid.normalized_amount == "1234500.00"
    assert valid.status == "valid"

    outsider = ExtractedRecord(
        "Revenue", "Revenue", "financial", "1,234.50", "number",
        Quote("other-window", "1,234.50"), Quote("cell-1", "Revenue"), numeric_policy_id="us",
    )
    report = validate_window((outsider,), window=window, task=task)
    assert "SOURCE_OUTSIDE_WINDOW" in {problem.code for problem in report.problems}
