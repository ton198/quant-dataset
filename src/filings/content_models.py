"""Arrow schemas for normalized SEC statement content evidence."""

from __future__ import annotations

import hashlib

import pyarrow as pa

CONTENT_CONTRACT = "filings-content-v2"


def _schema(fields: list[tuple[str, pa.DataType, bool]]) -> pa.Schema:
    return pa.schema([pa.field(name, dtype, nullable=nullable) for name, dtype, nullable in fields])


IDENTITY = [("filing_id", pa.string(), False), ("document_id", pa.string(), False)]
SEGMENT = [("segment_index", pa.int32(), True)]

CONTENT_DOCUMENTS_SCHEMA = _schema(
    IDENTITY
    + SEGMENT
    + [
        ("cik10", pa.string(), False),
        ("accession_number", pa.string(), False),
        ("form", pa.string(), False),
        ("filed_date", pa.date32(), False),
        ("report_date", pa.date32(), True),
        ("document_role", pa.string(), False),
        ("segment_type", pa.string(), True),
        ("segment_description", pa.string(), True),
        ("original_filename", pa.string(), True),
        ("raw_sha256", pa.string(), True),
        ("raw_byte_size", pa.int64(), True),
        ("payload_kind", pa.string(), False),
        ("encoding", pa.string(), True),
        ("parser_name", pa.string(), False),
        ("parser_version", pa.string(), False),
        ("content_contract", pa.string(), False),
        ("status", pa.string(), False),
        ("diagnostic_codes_json", pa.string(), False),
        ("limitations_json", pa.string(), False),
        ("block_count", pa.int32(), False),
        ("table_count", pa.int32(), False),
        ("visible_text_chars", pa.int64(), True),
        ("covered_text_chars", pa.int64(), True),
        ("hidden_excluded_count", pa.int32(), False),
        ("css_hidden_rules_applied", pa.int32(), False),
        ("css_rules_unsupported", pa.int32(), False),
    ]
)

CONTENT_BLOCKS_SCHEMA = _schema(
    IDENTITY
    + SEGMENT
    + [
        ("block_id", pa.string(), False),
        ("ordinal", pa.int32(), False),
        ("block_kind", pa.string(), False),
        ("heading_path_json", pa.string(), False),
        ("item_code", pa.string(), True),
        ("text", pa.string(), False),
        ("text_sha256", pa.string(), False),
        ("source_xpath", pa.string(), True),
        ("line_start", pa.int32(), True),
        ("line_end", pa.int32(), True),
    ]
)


def content_id(
    document_id: str, raw_sha256: str, segment_index: int | None, kind: str, locator: str
) -> str:
    segment_token = "" if segment_index is None else str(segment_index)
    payload = "\0".join(
        (document_id, raw_sha256, CONTENT_CONTRACT, segment_token, kind, locator)
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:32]
