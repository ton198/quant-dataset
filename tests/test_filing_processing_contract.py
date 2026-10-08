"""Stable parser contract and archive RunSpec restoration checks."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

import filings.processing as processing  # noqa: E402
from filings.archive import ArchiveCorruptionError, _restore_spec  # noqa: E402
from filings.models import RunSpec, Snapshot  # noqa: E402
from filings.processing_contract import (  # noqa: E402
    DEPENDENCIES_SCHEMA,
    PARSES_SCHEMA,
    PROCESSING_CONTRACT,
    parse_id,
)


def test_parse_id_preserves_contract_v2_canonical_digest_vectors() -> None:
    args = (
        "0000000123:0000999999-24-000001",
        "doc-α",
        "a" * 64,
        "filings.xbrl",
        "7.2",
        "b" * 64,
    )
    assert PROCESSING_CONTRACT == "filings-processing-v2"
    assert parse_id(*args) == "98bc91fab5d79a7e782da0bf84c29dbffe65df63171354d53643be07024f0612"
    assert parse_id(*args, source_group_fingerprint="c" * 64) == (
        "46a2a51dd69f36faad7dc5fda979bdfa39ceec62d33c9f929ee7c59bb06fb279"
    )
    assert processing._parse_id(*args) == parse_id(*args)
    assert processing._parse_id(*args, source_group_fingerprint="c" * 64) == parse_id(
        *args, source_group_fingerprint="c" * 64
    )


def test_legacy_parse_id_wrapper_reads_dynamic_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    args = ("filing", "document", "a" * 64, "parser", "version", "b" * 64)
    monkeypatch.setattr(processing, "_PROCESSING_CONTRACT", "test-contract-v9")
    assert processing._parse_id(*args) == parse_id(*args, contract="test-contract-v9")


def test_processing_schema_aliases_preserve_field_order_and_nullability() -> None:
    assert processing.PARSES_SCHEMA.equals(PARSES_SCHEMA, check_metadata=True)
    assert processing.DEPENDENCIES_SCHEMA.equals(DEPENDENCIES_SCHEMA, check_metadata=True)
    assert PARSES_SCHEMA.names == [
        "parse_id",
        "filing_id",
        "document_id",
        "parser_name",
        "parser_version",
        "status",
        "validation_scope",
        "source_sha256",
        "dependencies_fingerprint",
        "fact_count",
        "section_count",
        "errors_json",
        "notes_json",
    ]
    assert DEPENDENCIES_SCHEMA.names == [
        "filing_id",
        "dependency_fingerprint",
        "requested_url",
        "final_url",
        "transport_url",
        "sha256",
        "byte_size",
        "raw_path",
        "status",
        "diagnostic_code",
        "provenance_json",
    ]


def test_archive_run_spec_restore_requires_canonical_document_and_hash() -> None:
    spec = RunSpec(input_fingerprint="input", policy={"version": 1})
    manifest = {
        "run_spec": spec.canonical_dict(),
        "run_spec_sha256": spec.sha256,
    }
    snapshot = Snapshot(root=Path("/archive"), manifest=manifest, tables={}, raw_objects=())
    assert _restore_spec(snapshot).canonical_dict() == spec.canonical_dict()

    bad_hash = Snapshot(
        root=Path("/archive"),
        manifest={**manifest, "run_spec_sha256": "0" * 64},
        tables={},
        raw_objects=(),
    )
    with pytest.raises(ArchiveCorruptionError, match="canonical hash verification"):
        _restore_spec(bad_hash)

    extra_field = Snapshot(
        root=Path("/archive"),
        manifest={
            **manifest,
            "run_spec": {**spec.canonical_dict(), "unexpected": True},
        },
        tables={},
        raw_objects=(),
    )
    with pytest.raises(ArchiveCorruptionError, match="canonical hash verification"):
        _restore_spec(extra_field)


def test_parse_id_golden_vectors_match_independent_canonical_json() -> None:
    identity = {
        "contract": PROCESSING_CONTRACT,
        "filing_id": "0000000123:0000999999-24-000001",
        "document_id": "doc-α",
        "source_sha256": "a" * 64,
        "parser_name": "filings.xbrl",
        "parser_version": "7.2",
        "dependencies_fingerprint": "b" * 64,
        "source_group_fingerprint": "c" * 64,
    }
    expected = hashlib.sha256(
        json.dumps(
            identity,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    assert parse_id(*tuple(identity[key] for key in (
        "filing_id",
        "document_id",
        "source_sha256",
        "parser_name",
        "parser_version",
        "dependencies_fingerprint",
    )), source_group_fingerprint=identity["source_group_fingerprint"]) == expected
