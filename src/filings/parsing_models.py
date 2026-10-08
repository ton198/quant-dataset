"""Stable value models and Arrow schemas for pure filing parsers.

This module deliberately has no dependency on archive/storage/catalog code.  Parser
rows use strings for values that could otherwise lose precision (notably decimals and
XBRL numeric values) and JSON strings for nested source metadata.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pyarrow as pa

FACT_SCHEMA = pa.schema(
    [
        pa.field("filing_id", pa.string()),
        pa.field("document_id", pa.string()),
        pa.field("parse_id", pa.string()),
        pa.field("parser_name", pa.string()),
        pa.field("parser_version", pa.string()),
        pa.field("status", pa.string()),
        pa.field("source_hash", pa.string()),
        pa.field("occurrence_id", pa.string()),
        pa.field("source_uri", pa.string()),
        pa.field("source_relpath", pa.string()),
        pa.field("document_hash", pa.string()),
        pa.field("source_xml", pa.string()),
        pa.field("source_line", pa.int64()),
        pa.field("object_index", pa.int64()),
        pa.field("provenance", pa.string()),
        pa.field("fact_qname", pa.string()),
        pa.field("namespace_uri", pa.string()),
        pa.field("concept_local_name", pa.string()),
        pa.field("concept_type", pa.string()),
        pa.field("is_numeric", pa.bool_()),
        pa.field("is_tuple", pa.bool_()),
        pa.field("parent_occurrence_id", pa.string()),
        pa.field("context_id", pa.string()),
        pa.field("context_json", pa.string()),
        pa.field("unit_id", pa.string()),
        pa.field("unit_json", pa.string()),
        pa.field("raw_value", pa.string()),
        pa.field("transformed_value", pa.string()),
        pa.field("normalized_numeric", pa.string()),
        pa.field("x_value_json", pa.string()),
        pa.field("decimals_raw", pa.string()),
        pa.field("decimals", pa.string()),
        pa.field("precision_raw", pa.string()),
        pa.field("precision", pa.string()),
        pa.field("is_nil", pa.bool_()),
        pa.field("x_valid", pa.int64()),
        pa.field("is_valid", pa.bool_()),
        pa.field("validity_scope", pa.string()),
        pa.field("validation_scope", pa.string()),
        pa.field("validated_fraction_numerator", pa.string()),
        pa.field("validated_fraction_denominator", pa.string()),
        pa.field("fraction_numerator_format", pa.string()),
        pa.field("fraction_numerator_sign", pa.string()),
        pa.field("fraction_numerator_scale", pa.string()),
        pa.field("fraction_denominator_format", pa.string()),
        pa.field("fraction_denominator_sign", pa.string()),
        pa.field("fraction_denominator_scale", pa.string()),
        pa.field("inline_format", pa.string()),
        pa.field("inline_sign", pa.string()),
        pa.field("inline_scale", pa.string()),
        pa.field("inline_hidden", pa.bool_()),
        pa.field("continued_at", pa.string()),
        pa.field("fraction_numerator", pa.string()),
        pa.field("fraction_denominator", pa.string()),
        pa.field("error_count", pa.int64()),
        pa.field("error_codes_json", pa.string()),
    ]
)

SECTION_SCHEMA = pa.schema(
    [
        pa.field("filing_id", pa.string()),
        pa.field("document_id", pa.string()),
        pa.field("parse_id", pa.string()),
        pa.field("parser_name", pa.string()),
        pa.field("parser_version", pa.string()),
        pa.field("status", pa.string()),
        pa.field("source_hash", pa.string()),
        pa.field("document_hash", pa.string()),
        pa.field("source_relpath", pa.string()),
        pa.field("section_kind", pa.string()),
        pa.field("heading", pa.string()),
        pa.field("content_text", pa.string()),
        pa.field("source_xpath", pa.string()),
        pa.field("source_ordinal", pa.int64()),
        pa.field("heading_level", pa.int64()),
        pa.field("confidence", pa.string()),
        pa.field("raw_encoding", pa.string()),
        pa.field("provenance", pa.string()),
        pa.field("validation_scope", pa.string()),
    ]
)


@dataclass(slots=True)
class ParseResult:
    """One parser invocation and its deterministic fact/section occurrences."""

    filing_id: str
    document_id: str
    parse_id: str
    parser_name: str
    parser_version: str
    status: str
    source_hash: str | None
    validation_scope: str = "not_performed"
    facts: list[dict[str, Any]] = field(default_factory=list)
    sections: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)

    def facts_to_arrow(self) -> pa.Table:
        """Return fact occurrences with the fixed public schema, including when empty."""
        return pa.Table.from_pylist(self.facts, schema=FACT_SCHEMA)

    def sections_to_arrow(self) -> pa.Table:
        """Return text sections with the fixed public schema, including when empty."""
        return pa.Table.from_pylist(self.sections, schema=SECTION_SCHEMA)

    def to_arrow_tables(self) -> tuple[pa.Table, pa.Table]:
        """Return ``(facts, sections)`` tables in their fixed column order."""
        return self.facts_to_arrow(), self.sections_to_arrow()
