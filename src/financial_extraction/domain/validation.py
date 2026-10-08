"""Strict JSON contracts and source-grounded validation for open extraction records."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any

from .contracts import Dimension, EvidenceBundle, ExtractedRecord, ExtractionWindow, Period, Quote, ResolvedSource, ValidatedRecord, ValidationProblem, WindowItem, WindowValidation
from .metrics import NumericParseError, NumericPolicy, parse_numeric


def _quote_schema() -> dict[str, Any]:
    return {"type": "object", "additionalProperties": False, "required": ["ref", "text"], "properties": {"ref": {"type": "string"}, "text": {"type": "string"}}}


def _quotes_schema() -> dict[str, Any]:
    return {"type": "array", "items": _quote_schema()}


def _nullable_string() -> dict[str, Any]:
    return {"type": ["string", "null"]}


_PERIOD_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["kind", "start", "end", "sources"], "properties": {"kind": _nullable_string(), "start": _nullable_string(), "end": _nullable_string(), "sources": _quotes_schema()}}
_DIMENSION_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["name", "value", "sources"], "properties": {"name": {"type": "string"}, "value": {"type": "string"}, "sources": _quotes_schema()}}
_RECORD_PROPERTIES = {
    "source_label": {"type": "string"}, "name": {"type": "string"}, "category": {"type": "string"},
    "value": {"type": "string"}, "value_type": {"type": "string", "enum": ["number", "percentage", "ratio", "text"]},
    "value_source": _quote_schema(), "label_source": _quote_schema(), "numeric_policy_id": _nullable_string(),
    "unit": _nullable_string(), "currency": _nullable_string(), "scale": _nullable_string(),
    "unit_sources": _quotes_schema(), "currency_sources": _quotes_schema(), "scale_sources": _quotes_schema(),
    "period": _PERIOD_SCHEMA, "dimensions": {"type": "array", "items": _DIMENSION_SCHEMA},
}
OUTPUT_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["records"], "properties": {"records": {"type": "array", "items": {"type": "object", "additionalProperties": False, "required": list(_RECORD_PROPERTIES), "properties": _RECORD_PROPERTIES}}}}


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        raise TypeError("floating-point values are forbidden in canonical JSON")
    if isinstance(value, Decimal):
        if not value.is_finite(): raise ValueError("non-finite Decimal values are forbidden")
        return format(value, "f")
    if hasattr(value, "__dataclass_fields__"):
        return _json_value(asdict(value))
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value): raise TypeError("canonical JSON keys must be strings")
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    raise TypeError(f"unsupported canonical JSON value: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    return json.dumps(_json_value(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def evidence_bundle_hash(bundle: EvidenceBundle) -> str:
    """Hash structural evidence and verified document digests, not payload bytes."""
    documents = [
        {
            "source_id": document.source_id,
            "document_id": document.document_id,
            "source_type": document.source_type,
            "fixture_only": document.fixture_only,
            "raw_sha256": document.raw_sha256,
            "raw_bytes": document.raw_bytes,
            "payload_sha256": document.payload_sha256,
            "preprocessing_revision": document.preprocessing_revision,
            "origin": document.origin,
            "limits": document.limits,
        }
        for document in bundle.documents
    ]
    return canonical_hash(
        {
            "documents": documents,
            "blocks": bundle.blocks,
            "cells": bundle.cells,
            "canonical_text_hash": bundle.canonical_text_hash,
            "preprocessing_config_hash": bundle.preprocessing_config_hash,
            "limits": bundle.limits,
            "limitations": bundle.limitations,
        }
    )


def parse_json_strict(payload: str | bytes, *, max_bytes: int = 1_000_000) -> Any:
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes <= 0: raise ValueError("max_bytes must be a positive integer")
    if isinstance(payload, bytes):
        if len(payload) > max_bytes: raise ValueError("JSON payload exceeds max_bytes")
        try: text = payload.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc: raise ValueError("JSON bytes must be UTF-8") from exc
    elif isinstance(payload, str): text = payload
    else: raise TypeError("JSON payload must be str or bytes")
    if len(text.encode("utf-8")) > max_bytes: raise ValueError("JSON payload exceeds max_bytes")
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result = {}
        for key, value in items:
            if key in result: raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result
    def constant(value: str) -> None: raise ValueError(f"non-standard JSON constant: {value}")
    try: decoded = json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
    except (json.JSONDecodeError, RecursionError) as exc: raise ValueError("invalid or excessively nested JSON") from exc
    _check_json_shape(decoded)
    return decoded


def _check_json_shape(value: Any, depth: int = 0) -> None:
    if depth > 64: raise ValueError("JSON nesting exceeds 64 levels")
    if isinstance(value, dict):
        if len(value) > 10_000: raise ValueError("JSON object has too many keys")
        for key, item in value.items():
            if len(key) > 16_384: raise ValueError("JSON key is too long")
            _check_json_shape(item, depth + 1)
    elif isinstance(value, list):
        if len(value) > 100_000: raise ValueError("JSON array is too large")
        for item in value: _check_json_shape(item, depth + 1)
    elif isinstance(value, str) and len(value) > 1_000_000: raise ValueError("JSON string is too long")
    elif isinstance(value, float): raise ValueError("JSON floating-point numbers are forbidden")


def _object(value: Any, required: set[str], where: str) -> dict[str, Any]:
    if not isinstance(value, dict): raise ValueError(f"{where} must be an object")
    missing, unknown = required - value.keys(), value.keys() - required
    if missing: raise ValueError(f"{where} missing required keys: {', '.join(sorted(missing))}")
    if unknown: raise ValueError(f"{where} has unknown keys: {', '.join(sorted(unknown))}")
    return value


def _string(value: Any, where: str, nullable: bool = False) -> str | None:
    if nullable and value is None: return None
    if not isinstance(value, str): raise ValueError(f"{where} must be a string")
    return value


def _quote(value: Any, where: str) -> Quote:
    row = _object(value, {"ref", "text"}, where)
    return Quote(_string(row["ref"], where + ".ref") or "", _string(row["text"], where + ".text") or "")


def _quotes(value: Any, where: str) -> tuple[Quote, ...]:
    if not isinstance(value, list): raise ValueError(f"{where} must be an array")
    return tuple(_quote(item, f"{where}[{i}]") for i, item in enumerate(value))


def _record(value: Any, where: str) -> ExtractedRecord:
    row = _object(value, set(_RECORD_PROPERTIES), where)
    value_type = _string(row["value_type"], where + ".value_type")
    if value_type not in {"number", "percentage", "ratio", "text"}: raise ValueError("unsupported value_type")
    p = _object(row["period"], {"kind", "start", "end", "sources"}, where + ".period")
    period = Period(_string(p["kind"], "period.kind", True), _string(p["start"], "period.start", True), _string(p["end"], "period.end", True), _quotes(p["sources"], "period.sources"))
    if not isinstance(row["dimensions"], list): raise ValueError("dimensions must be an array")
    dimensions = []
    for i, item in enumerate(row["dimensions"]):
        d = _object(item, {"name", "value", "sources"}, f"{where}.dimensions[{i}]")
        dimensions.append(Dimension(_string(d["name"], "dimension.name") or "", _string(d["value"], "dimension.value") or "", _quotes(d["sources"], "dimension.sources")))
    return ExtractedRecord(
        _string(row["source_label"], "source_label") or "", _string(row["name"], "name") or "", _string(row["category"], "category") or "", _string(row["value"], "value") or "", value_type,
        _quote(row["value_source"], "value_source"), _quote(row["label_source"], "label_source"),
        _string(row["numeric_policy_id"], "numeric_policy_id", True), _string(row["unit"], "unit", True), _string(row["currency"], "currency", True), _string(row["scale"], "scale", True),
        _quotes(row["unit_sources"], "unit_sources"), _quotes(row["currency_sources"], "currency_sources"), _quotes(row["scale_sources"], "scale_sources"), period, tuple(dimensions))


def records_from_json(raw_json: str) -> tuple[ExtractedRecord, ...]:
    response = _object(parse_json_strict(raw_json), {"records"}, "response")
    if not isinstance(response["records"], list): raise ValueError("response.records must be an array")
    return tuple(_record(item, f"records[{i}]") for i, item in enumerate(response["records"]))


def _window_items(window: ExtractionWindow) -> dict[str, WindowItem]:
    result = {}
    for item in (*window.core, *window.context):
        if item.source.document_id != window.document_id: raise ValueError("window item references a different document")
        if item.ref in result and result[item.ref] != item: raise ValueError("ambiguous evidence ref")
        result[item.ref] = item
    return result


def _resolve(field: str, quote: Quote, items: dict[str, WindowItem], problems: list[ValidationProblem]) -> ResolvedSource | None:
    item = items.get(quote.ref)
    if item is None:
        problems.append(ValidationProblem("SOURCE_OUTSIDE_WINDOW", field, "reference is not in this window")); return None
    if not quote.text or quote.text not in item.text:
        problems.append(ValidationProblem("SOURCE_QUOTE_MISMATCH", field, "quote is not an exact source substring")); return None
    return ResolvedSource(field, item.source, quote.text)


def _decimal_token_in_text(value: str, text: str) -> bool:
    escaped = re.escape(value)
    return re.search(rf"(?<![\w.]){escaped}(?![\w.])", text) is not None


def _normalize(record: ExtractedRecord, policies: dict[str, NumericPolicy]) -> tuple[str | None, str | None]:
    if record.value_type == "text": return None, None
    policy = policies.get(record.numeric_policy_id or "")
    if policy is None: return None, "NUMERIC_POLICY_MISSING"
    try: amount = parse_numeric(record.value, policy)
    except (NumericParseError, ValueError, TypeError): return None, "NUMERIC_VALUE_INVALID"
    if record.value_type == "percentage" and record.scale is None: return format(amount, "f"), None
    if record.scale is None: return format(amount, "f"), None
    if len(record.scale) > 128 or re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", record.scale) is None: return None, "SCALE_INVALID"
    try: factor = Decimal(record.scale)
    except InvalidOperation: return None, "SCALE_INVALID"
    if not factor.is_finite() or factor <= 0: return None, "SCALE_INVALID"
    with localcontext() as ctx:
        ctx.prec = max(28, len(amount.as_tuple().digits) + len(factor.as_tuple().digits) + 4)
        amount *= factor
    return format(amount, "f"), None


def validate_window(records: tuple[ExtractedRecord, ...], *, window: ExtractionWindow, task: Any) -> WindowValidation:
    if not isinstance(window, ExtractionWindow): raise TypeError("window must be an ExtractionWindow")
    policies: dict[str, NumericPolicy] = {}
    for policy in task.numeric_policies:
        if not isinstance(policy, NumericPolicy): raise TypeError("numeric policies must be typed")
        if policy.policy_id in policies: raise ValueError("numeric policy ids must be unique")
        policies[policy.policy_id] = policy
    items = _window_items(window); validated = []; all_problems = []
    for i, record in enumerate(records):
        problems: list[ValidationProblem] = []; sources: list[ResolvedSource] = []
        refs = [("value", record.value_source), ("label", record.label_source)]
        refs.extend((field, quote) for field, quotes in (("period", record.period.sources), ("unit", record.unit_sources), ("currency", record.currency_sources), ("scale", record.scale_sources)) for quote in quotes)
        refs.extend(("dimension", quote) for dimension in record.dimensions for quote in dimension.sources)
        for field, quote in refs:
            resolved = _resolve(field, quote, items, problems)
            if resolved: sources.append(resolved)
        amount, problem = _normalize(record, policies)
        if problem: problems.append(ValidationProblem(problem, "value", "numeric value or scale is unresolved"))
        if record.value_type != "text" and not _decimal_token_in_text(record.value, record.value_source.text):
            problems.append(ValidationProblem("VALUE_NOT_PRESENT", "value", "numeric lexical token is absent from cited source"))
        status = "valid" if not problems else "unresolved"
        validated.append(ValidatedRecord(f"{window.window_id}:{i}", record, amount, tuple(sources), tuple(problems), status)); all_problems.extend(problems)
    return WindowValidation(tuple(validated), tuple(all_problems))
