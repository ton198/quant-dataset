"""Stable Arrow schemas and identities for persisted parser processing rows."""

from __future__ import annotations

import hashlib
import json

import pyarrow as pa

PROCESSING_CONTRACT = "filings-processing-v2"

PARSES_SCHEMA = pa.schema(
    [
        pa.field("parse_id", pa.string(), nullable=False),
        pa.field("filing_id", pa.string(), nullable=False),
        pa.field("document_id", pa.string(), nullable=False),
        pa.field("parser_name", pa.string(), nullable=False),
        pa.field("parser_version", pa.string(), nullable=False),
        pa.field("status", pa.string(), nullable=False),
        pa.field("validation_scope", pa.string(), nullable=False),
        pa.field("source_sha256", pa.string(), nullable=False),
        pa.field("dependencies_fingerprint", pa.string(), nullable=False),
        pa.field("fact_count", pa.int64(), nullable=False),
        pa.field("section_count", pa.int64(), nullable=False),
        pa.field("errors_json", pa.string(), nullable=False),
        pa.field("notes_json", pa.string(), nullable=False),
    ]
)

DEPENDENCIES_SCHEMA = pa.schema(
    [
        pa.field("filing_id", pa.string(), nullable=False),
        pa.field("dependency_fingerprint", pa.string(), nullable=False),
        pa.field("requested_url", pa.string()),
        pa.field("final_url", pa.string()),
        pa.field("transport_url", pa.string()),
        pa.field("sha256", pa.string()),
        pa.field("byte_size", pa.int64()),
        pa.field("raw_path", pa.string()),
        pa.field("status", pa.string(), nullable=False),
        pa.field("diagnostic_code", pa.string()),
        pa.field("provenance_json", pa.string(), nullable=False),
    ]
)


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def parse_id(
    filing_id_value: str,
    document_id_value: str,
    source_hash: str,
    parser_name: str,
    parser_version: str,
    dependencies_fingerprint: str,
    source_group_fingerprint: str | None = None,
    *,
    contract: str = PROCESSING_CONTRACT,
) -> str:
    """Return the stable parser identity under the requested contract version."""
    identity = {
        "contract": contract,
        "filing_id": filing_id_value,
        "document_id": document_id_value,
        "source_sha256": source_hash,
        "parser_name": parser_name,
        "parser_version": parser_version,
        "dependencies_fingerprint": dependencies_fingerprint,
    }
    if source_group_fingerprint is not None:
        identity["source_group_fingerprint"] = source_group_fingerprint
    return hashlib.sha256(_canonical_json(identity)).hexdigest()
