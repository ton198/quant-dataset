"""Offline tests for immutable filings archive storage transactions."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import date
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from filings import (  # noqa: E402
    DOCUMENTS_SCHEMA,
    FILINGS_SCHEMA,
    ArchiveConflictError,
    ArchiveCorruptionError,
    ArchiveError,
    RawObjectRef,
    RunSpec,
    document_id,
    filing_id,
    open_archive,
    read_snapshot,
)
from filings import archive as archive_module  # noqa: E402
from filings.archive import read_snapshot_descriptors  # noqa: E402

CIK = "0000000123"
ACCESSION = "0000999999-24-000001"
FILING_ID = f"{CIK}:{ACCESSION}"


def _spec(**kwargs: Any) -> RunSpec:
    values = {
        "input_fingerprint": "a" * 64,
        "calendar_provenance": {"calendar": "XNYS", "range": ["2024-01-01", "2024-12-31"]},
    }
    values.update(kwargs)
    return RunSpec(**values)


def _protected(tmp_path: Path) -> tuple[Path, ...]:
    protected = tmp_path / "protected-input"
    protected.mkdir(exist_ok=True)
    return (protected,)


def _open(
    root: Path,
    *,
    spec: RunSpec | None = None,
    resume: bool = False,
    recover_unpublished: bool = False,
):
    return open_archive(
        root,
        spec or _spec(),
        protected_paths=_protected(root.parent),
        resume=resume,
        recover_unpublished=recover_unpublished,
    )


def _file_inventory(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def _filing_row(ref: RawObjectRef, **overrides: Any) -> dict[str, Any]:
    row = {
        "filing_id": filing_id(CIK, ACCESSION),
        "cik10": CIK,
        "accession_number": ACCESSION,
        "form": "10-K",
        "filed_date": date(2024, 1, 12),
        "report_period_end": date(2023, 12, 31),
        "acceptance_datetime_raw": "2024-01-12T20:00:00Z",
        "acceptance_datetime_utc": None,
        "effective_visible_session": date(2024, 1, 16),
        "is_amendment": False,
        "parent_filing_id": None,
        "parent_link_source": None,
        "primary_document_name": "annual.htm",
        "scope_status": "included",
        "scope_evidence_json": None,
        "inventory_status": "known",
        "raw_coverage_status": "partial",
        "source_submission_logical_key": "submissions:0000000123",
        "source_submission_sha256": ref.sha256,
        "source_submission_path": ref.path,
        "source_submission_locator": "filings.recent[0]",
    }
    row.update(overrides)
    return row


def _document_row(inventory: RawObjectRef, raw: RawObjectRef | None = None, **overrides: Any):
    row = {
        "document_id": document_id(FILING_ID, "https://www.sec.gov/Archives/annual.htm"),
        "filing_id": FILING_ID,
        "original_filename": "annual.htm",
        "source_url": "https://www.sec.gov/Archives/annual.htm",
        "role": "primary",
        "selection_status": "required",
        "fetch_status": "present" if raw else "unavailable",
        "source_inventory_sha256": inventory.sha256,
        "source_inventory_locator": "directory[0]",
        "raw_sha256": raw.sha256 if raw else None,
        "raw_path": raw.path if raw else None,
        "byte_size": raw.byte_size if raw else None,
        "media_type": "text/html" if raw else None,
        "fetched_at_utc": None,
        "http_status": 200 if raw else 404,
        "diagnostic_code": None if raw else "not_cached",
        "fact_extraction_status": "not_attempted",
        "text_extraction_status": "not_attempted",
    }
    row.update(overrides)
    return row


def _table(schema: pa.Schema, *rows: dict[str, Any]) -> pa.Table:
    return pa.Table.from_pylist(list(rows), schema=schema)


def _seed_raw(writer) -> tuple[RawObjectRef, RawObjectRef]:
    return (
        writer.put_raw_bytes(b'{"submissions":"cached"}'),
        writer.put_raw_bytes(b'{"directory":"cached"}'),
    )


def _scope_evidence(
    inventory_sha256: str,
    selected_document_ids: list[str],
    *,
    decision: str = "included",
    reasons: list[str] | None = None,
) -> str:
    return json.dumps(
        {
            "policy": "sec-metadata-financial-v1",
            "decision": decision,
            "inventory_sha256": inventory_sha256,
            "inventory_locator": "directory[0]",
            "selected_financial_document_ids": selected_document_ids,
            "reasons": reasons or [],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


_FINANCIAL_DOC_URL = "https://www.sec.gov/Archives/financial-exhibit.htm"


def _financial_document_row(
    inventory: RawObjectRef, raw: RawObjectRef | None = None, **overrides: Any
) -> dict[str, Any]:
    row = _document_row(
        inventory,
        raw,
        document_id=document_id(FILING_ID, _FINANCIAL_DOC_URL),
        original_filename="financial-exhibit.htm",
        source_url=_FINANCIAL_DOC_URL,
        role="exhibit",
        selection_status="required",
    )
    row.update(overrides)
    return row


def test_existing_empty_directory_is_a_valid_explicit_fresh_root(tmp_path: Path) -> None:
    root = tmp_path / "archive"
    root.mkdir()
    with _open(root) as writer:
        snapshot = writer.commit(tables={}, raw_objects=(), expected_manifest_version=0)
    assert snapshot.manifest_version == 1


def test_empty_exact_core_tables_publish_snapshot_and_resume(tmp_path: Path) -> None:
    root = tmp_path / "archive"
    with _open(root) as writer:
        snapshot = writer.commit(
            tables={
                "filings": pa.Table.from_pylist([], schema=FILINGS_SCHEMA),
                "documents": pa.Table.from_pylist([], schema=DOCUMENTS_SCHEMA),
            },
            raw_objects=(),
            expected_manifest_version=0,
        )
        assert snapshot.manifest_version == 1
        assert snapshot.tables["filings"].schema.equals(FILINGS_SCHEMA, check_metadata=True)
        assert snapshot.tables["documents"].num_rows == 0

    reread = read_snapshot(root)
    assert reread.snapshot_id == snapshot.snapshot_id
    assert reread.manifest["manifest_version"] == 1
    assert reread.manifest["parent_snapshot_id"] is None
    assert reread.manifest["input_fingerprint"] == "a" * 64
    with pytest.raises(TypeError):
        reread.manifest["manifest_version"] = 0
    with pytest.raises(TypeError):
        reread.tables["extra"] = pa.table({"a": [1]})

    with _open(root, resume=True) as writer:
        second = writer.commit(tables={}, raw_objects=(), expected_manifest_version=1)
    assert second.manifest_version == 2
    assert second.manifest["parent_snapshot_id"] == snapshot.snapshot_id
    assert set(second.tables) == {"filings", "documents"}


def test_raw_bytes_file_copy_and_expected_hash(tmp_path: Path) -> None:
    root = tmp_path / "archive"
    source = tmp_path / "input.json"
    source.write_bytes(b"source bytes")
    with _open(root) as writer:
        from_bytes = writer.put_raw_bytes(b"source bytes")
        from_file = writer.put_raw_file(
            source, expected_sha256=hashlib.sha256(b"source bytes").hexdigest()
        )
        assert from_bytes == from_file
        assert source.read_bytes() == b"source bytes"
        assert not os.path.samefile(source, root / from_file.path)
        with pytest.raises(ArchiveError, match="expected_sha256"):
            writer.put_raw_file(source, expected_sha256="0" * 64)
        with pytest.raises(ArchiveError, match="expected_sha256"):
            writer.put_raw_bytes(b"wrong", expected_sha256="0" * 64)


def test_core_rows_cross_reference_active_raw_and_validate_enums(tmp_path: Path) -> None:
    root = tmp_path / "archive"
    with _open(root) as writer:
        submission, inventory = _seed_raw(writer)
        filing = _filing_row(submission)
        doc = _document_row(inventory)
        tables = {
            "filings": _table(FILINGS_SCHEMA, filing),
            "documents": _table(DOCUMENTS_SCHEMA, doc),
        }
        snapshot = writer.commit(
            tables=tables, raw_objects=(submission, inventory), expected_manifest_version=0
        )
        assert snapshot.tables["filings"].num_rows == 1

    saved_document = read_snapshot(root).tables["documents"].to_pylist()[0]
    assert saved_document["fetch_status"] == "unavailable"
    assert saved_document["fact_extraction_status"] == "not_attempted"
    assert saved_document["text_extraction_status"] == "not_attempted"

    root2 = tmp_path / "bad-enum"
    with _open(root2) as writer:
        submission, inventory = _seed_raw(writer)
        filing = _filing_row(submission, scope_status="maybe")
        with pytest.raises(ArchiveError, match="scope_status"):
            writer.commit(
                tables={"filings": _table(FILINGS_SCHEMA, filing)},
                raw_objects=(submission, inventory),
                expected_manifest_version=0,
            )


def test_initial_6k_candidate_without_evidence_remains_incomplete(tmp_path: Path) -> None:
    root = tmp_path / "candidate-6k"
    with _open(root) as writer:
        submission, inventory = _seed_raw(writer)
        filing = _filing_row(
            submission,
            form="6-K",
            scope_status="candidate",
            scope_evidence_json=None,
            inventory_status="not_inspected",
            raw_coverage_status="not_attempted",
        )
        snapshot = writer.commit(
            tables={"filings": _table(FILINGS_SCHEMA, filing)},
            raw_objects=(submission, inventory),
            expected_manifest_version=0,
        )
    result = snapshot.tables["filings"].to_pylist()[0]
    assert result["scope_status"] == "candidate"
    assert result["scope_evidence_json"] is None
    assert result["raw_coverage_status"] == "not_attempted"


@pytest.mark.parametrize("scope_status", ["included", "excluded"])
def test_non_candidate_6k_scope_requires_evidence(tmp_path: Path, scope_status: str) -> None:
    root = tmp_path / scope_status
    with _open(root) as writer:
        submission, inventory = _seed_raw(writer)
        filing = _filing_row(
            submission,
            form="6-K",
            scope_status=scope_status,
            scope_evidence_json=None,
            inventory_status="known",
        )
        with pytest.raises(ArchiveError, match="requires scope_evidence_json"):
            writer.commit(
                tables={"filings": _table(FILINGS_SCHEMA, filing)},
                raw_objects=(submission, inventory),
                expected_manifest_version=0,
            )


def test_6k_scope_evidence_rejects_noncanonical_json_and_wrong_decision(
    tmp_path: Path,
) -> None:
    root = tmp_path / "invalid-json"
    with _open(root) as writer:
        submission, inventory = _seed_raw(writer)
        filing = _filing_row(
            submission,
            form="6-K",
            scope_status="included",
            scope_evidence_json="{not-json",
            inventory_status="known",
        )
        with pytest.raises(ArchiveError, match="canonical JSON object"):
            writer.commit(
                tables={"filings": _table(FILINGS_SCHEMA, filing)},
                raw_objects=(submission, inventory),
                expected_manifest_version=0,
            )

    root2 = tmp_path / "wrong-decision"
    with _open(root2) as writer:
        submission, inventory = _seed_raw(writer)
        evidence = _scope_evidence(
            inventory.sha256,
            [],
            decision="excluded",
            reasons=["metadata indicates nonfinancial content"],
        )
        filing = _filing_row(
            submission,
            form="6-K",
            scope_status="included",
            scope_evidence_json=evidence,
            inventory_status="known",
        )
        with pytest.raises(ArchiveError, match="decision does not match"):
            writer.commit(
                tables={"filings": _table(FILINGS_SCHEMA, filing)},
                raw_objects=(submission, inventory),
                expected_manifest_version=0,
            )


def test_6k_scope_evidence_requires_active_inventory_and_owned_document_ids(
    tmp_path: Path,
) -> None:
    root = tmp_path / "non-raw-inventory"
    with _open(root) as writer:
        submission, inventory = _seed_raw(writer)
        evidence = _scope_evidence("f" * 64, [document_id(FILING_ID, _FINANCIAL_DOC_URL)])
        filing = _filing_row(
            submission,
            form="6-K",
            scope_status="included",
            scope_evidence_json=evidence,
            inventory_status="known",
        )
        with pytest.raises(ArchiveError, match="inventory hash is not an active raw object"):
            writer.commit(
                tables={"filings": _table(FILINGS_SCHEMA, filing)},
                raw_objects=(submission, inventory),
                expected_manifest_version=0,
            )

    root2 = tmp_path / "foreign-document"
    with _open(root2) as writer:
        submission, inventory = _seed_raw(writer)
        foreign_filing_id = filing_id("0000000124", ACCESSION)
        foreign_document_url = "https://www.sec.gov/Archives/foreign.htm"
        foreign_document_id = document_id(foreign_filing_id, foreign_document_url)
        evidence = _scope_evidence(inventory.sha256, [foreign_document_id])
        filing = _filing_row(
            submission,
            form="6-K",
            scope_status="included",
            scope_evidence_json=evidence,
            inventory_status="known",
        )
        foreign_filing = _filing_row(
            submission,
            filing_id=foreign_filing_id,
            cik10="0000000124",
            scope_status="included",
        )
        foreign_document = _document_row(
            inventory,
            document_id=foreign_document_id,
            filing_id=foreign_filing_id,
            source_url=foreign_document_url,
            original_filename="foreign.htm",
        )
        with pytest.raises(ArchiveError, match="unknown or belongs to another filing"):
            writer.commit(
                tables={
                    "filings": _table(FILINGS_SCHEMA, filing, foreign_filing),
                    "documents": _table(DOCUMENTS_SCHEMA, foreign_document),
                },
                raw_objects=(submission, inventory),
                expected_manifest_version=0,
            )


def test_6k_financial_document_must_match_evidence_inventory_hash(tmp_path: Path) -> None:
    root = tmp_path / "wrong-document-inventory"
    with _open(root) as writer:
        submission, inventory = _seed_raw(writer)
        other_inventory = writer.put_raw_bytes(b'{"detail":"different inventory"}')
        financial_doc = _financial_document_row(
            other_inventory,
            fetch_status="error",
            http_status=500,
            diagnostic_code="server_error",
        )
        evidence = _scope_evidence(inventory.sha256, [financial_doc["document_id"]])
        filing = _filing_row(
            submission,
            form="6-K",
            scope_status="included",
            scope_evidence_json=evidence,
            inventory_status="known",
            raw_coverage_status="partial",
        )
        with pytest.raises(ArchiveError, match="inventory hash does not match"):
            writer.commit(
                tables={
                    "filings": _table(FILINGS_SCHEMA, filing),
                    "documents": _table(DOCUMENTS_SCHEMA, financial_doc),
                },
                raw_objects=(submission, inventory, other_inventory),
                expected_manifest_version=0,
            )


def test_included_6k_requires_present_financial_document_and_primary(
    tmp_path: Path,
) -> None:
    root = tmp_path / "included-6k-complete"
    with _open(root) as writer:
        submission, inventory = _seed_raw(writer)
        primary_raw = writer.put_raw_bytes(b"primary 6-K payload")
        financial_raw = writer.put_raw_bytes(b"financial exhibit payload")
        financial_doc = _financial_document_row(inventory, financial_raw)
        evidence = _scope_evidence(inventory.sha256, [financial_doc["document_id"]])
        filing = _filing_row(
            submission,
            form="6-K",
            scope_status="included",
            scope_evidence_json=evidence,
            inventory_status="known",
            raw_coverage_status="scoped_complete",
        )
        primary = _document_row(inventory, primary_raw)
        snapshot = writer.commit(
            tables={
                "filings": _table(FILINGS_SCHEMA, filing),
                "documents": _table(DOCUMENTS_SCHEMA, primary, financial_doc),
            },
            raw_objects=(submission, inventory, primary_raw, financial_raw),
            expected_manifest_version=0,
        )
        assert inventory in snapshot.raw_objects
        assert hashlib.sha256((root / inventory.path).read_bytes()).hexdigest() == inventory.sha256
        assert {
            row["role"]
            for row in snapshot.tables["documents"].to_pylist()
            if row["fetch_status"] == "present"
        } == {"primary", "exhibit"}

    partial_root = tmp_path / "included-6k-partial"
    with _open(partial_root) as writer:
        submission, inventory = _seed_raw(writer)
        primary_raw = writer.put_raw_bytes(b"primary 6-K payload")
        financial_doc = _financial_document_row(
            inventory,
            fetch_status="error",
            http_status=500,
            diagnostic_code="server_error",
        )
        evidence = _scope_evidence(inventory.sha256, [financial_doc["document_id"]])
        filing = _filing_row(
            submission,
            form="6-K",
            scope_status="included",
            scope_evidence_json=evidence,
            inventory_status="known",
            raw_coverage_status="partial",
        )
        primary = _document_row(inventory, primary_raw)
        snapshot = writer.commit(
            tables={
                "filings": _table(FILINGS_SCHEMA, filing),
                "documents": _table(DOCUMENTS_SCHEMA, primary, financial_doc),
            },
            raw_objects=(submission, inventory, primary_raw),
            expected_manifest_version=0,
        )
    assert snapshot.tables["filings"].to_pylist()[0]["raw_coverage_status"] == "partial"


def test_excluded_6k_requires_reasons_known_inventory_and_noncomplete_coverage(
    tmp_path: Path,
) -> None:
    root = tmp_path / "excluded-6k"
    with _open(root) as writer:
        submission, inventory = _seed_raw(writer)
        evidence = _scope_evidence(
            inventory.sha256,
            [],
            decision="excluded",
            reasons=["no financial payload identified"],
        )
        filing = _filing_row(
            submission,
            form="6-K",
            scope_status="excluded",
            scope_evidence_json=evidence,
            inventory_status="known",
            raw_coverage_status="blocked",
        )
        snapshot = writer.commit(
            tables={"filings": _table(FILINGS_SCHEMA, filing)},
            raw_objects=(submission, inventory),
            expected_manifest_version=0,
        )
        assert snapshot.tables["filings"].to_pylist()[0]["scope_status"] == "excluded"

    complete_root = tmp_path / "excluded-6k-complete"
    with _open(complete_root) as writer:
        submission, inventory = _seed_raw(writer)
        evidence = _scope_evidence(
            inventory.sha256,
            [],
            decision="excluded",
            reasons=["no financial payload identified"],
        )
        filing = _filing_row(
            submission,
            form="6-K",
            scope_status="excluded",
            scope_evidence_json=evidence,
            inventory_status="known",
            raw_coverage_status="scoped_complete",
        )
        with pytest.raises(ArchiveError, match="excluded 6-K requires"):
            writer.commit(
                tables={"filings": _table(FILINGS_SCHEMA, filing)},
                raw_objects=(submission, inventory),
                expected_manifest_version=0,
            )


@pytest.mark.parametrize(
    "extraction_status",
    ["not_attempted", "full", "partial", "unsupported", "failed"],
)
@pytest.mark.parametrize("field_name", ["fact_extraction_status", "text_extraction_status"])
def test_document_extraction_statuses_accept_verified_present_payload(
    tmp_path: Path, extraction_status: str, field_name: str
) -> None:
    root = tmp_path / f"{field_name}-{extraction_status}"
    with _open(root) as writer:
        submission, inventory = _seed_raw(writer)
        raw = writer.put_raw_bytes(b"verified parser input")
        filing = _filing_row(submission)
        document = _document_row(inventory, raw, **{field_name: extraction_status})
        snapshot = writer.commit(
            tables={
                "filings": _table(FILINGS_SCHEMA, filing),
                "documents": _table(DOCUMENTS_SCHEMA, document),
            },
            raw_objects=(submission, inventory, raw),
            expected_manifest_version=0,
        )
    assert snapshot.tables["documents"].to_pylist()[0][field_name] == extraction_status


@pytest.mark.parametrize("invalid_status", ["unknown", None])
def test_document_extraction_status_rejects_unknown_or_null(
    tmp_path: Path, invalid_status: str | None
) -> None:
    root = tmp_path / f"invalid-{invalid_status}"
    with _open(root) as writer:
        submission, inventory = _seed_raw(writer)
        raw = writer.put_raw_bytes(b"verified parser input")
        filing = _filing_row(submission)
        document = _document_row(inventory, raw, fact_extraction_status=invalid_status)
        with pytest.raises(ArchiveError):
            writer.commit(
                tables={
                    "filings": _table(FILINGS_SCHEMA, filing),
                    "documents": _table(DOCUMENTS_SCHEMA, document),
                },
                raw_objects=(submission, inventory, raw),
                expected_manifest_version=0,
            )


@pytest.mark.parametrize("fetch_status", ["not_requested", "unavailable", "error"])
def test_attempted_extraction_requires_present_document_payload(
    tmp_path: Path, fetch_status: str
) -> None:
    root = tmp_path / f"attempted-without-payload-{fetch_status}"
    with _open(root) as writer:
        submission, inventory = _seed_raw(writer)
        filing = _filing_row(submission)
        document = _document_row(
            inventory,
            fetch_status=fetch_status,
            fact_extraction_status="partial",
        )
        with pytest.raises(ArchiveError, match="requires a present verified document payload"):
            writer.commit(
                tables={
                    "filings": _table(FILINGS_SCHEMA, filing),
                    "documents": _table(DOCUMENTS_SCHEMA, document),
                },
                raw_objects=(submission, inventory),
                expected_manifest_version=0,
            )


def test_attempted_extraction_rejects_present_status_without_verified_raw_triple(
    tmp_path: Path,
) -> None:
    root = tmp_path / "attempted-without-raw-triple"
    with _open(root) as writer:
        submission, inventory = _seed_raw(writer)
        filing = _filing_row(submission)
        document = _document_row(
            inventory,
            fetch_status="present",
            fact_extraction_status="full",
        )
        with pytest.raises(ArchiveError, match="document raw_sha256"):
            writer.commit(
                tables={
                    "filings": _table(FILINGS_SCHEMA, filing),
                    "documents": _table(DOCUMENTS_SCHEMA, document),
                },
                raw_objects=(submission, inventory),
                expected_manifest_version=0,
            )


def test_document_present_triple_and_scoped_complete_rules(tmp_path: Path) -> None:
    root = tmp_path / "archive"
    with _open(root) as writer:
        submission, inventory = _seed_raw(writer)
        document = writer.put_raw_bytes(b"annual filing")
        filing = _filing_row(submission, raw_coverage_status="scoped_complete")
        doc = _document_row(inventory, document)
        snapshot = writer.commit(
            tables={
                "filings": _table(FILINGS_SCHEMA, filing),
                "documents": _table(DOCUMENTS_SCHEMA, doc),
            },
            raw_objects=(submission, inventory, document),
            expected_manifest_version=0,
        )
        assert snapshot.tables["documents"].to_pylist()[0]["raw_sha256"] == document.sha256

    bad_root = tmp_path / "bad-success-triple"
    with _open(bad_root) as writer:
        submission, inventory = _seed_raw(writer)
        filing = _filing_row(submission)
        doc = _document_row(inventory, fetch_status="present")
        with pytest.raises(ArchiveError, match="raw_sha256"):
            writer.commit(
                tables={
                    "filings": _table(FILINGS_SCHEMA, filing),
                    "documents": _table(DOCUMENTS_SCHEMA, doc),
                },
                raw_objects=(submission, inventory),
                expected_manifest_version=0,
            )

    incomplete_root = tmp_path / "incomplete"
    with _open(incomplete_root) as writer:
        submission, inventory = _seed_raw(writer)
        filing = _filing_row(submission, raw_coverage_status="scoped_complete")
        doc = _document_row(inventory, fetch_status="unavailable")
        with pytest.raises(ArchiveError, match="required document not present"):
            writer.commit(
                tables={
                    "filings": _table(FILINGS_SCHEMA, filing),
                    "documents": _table(DOCUMENTS_SCHEMA, doc),
                },
                raw_objects=(submission, inventory),
                expected_manifest_version=0,
            )


@pytest.mark.parametrize(
    "document_case",
    [
        "missing_table",
        "empty_table",
        "candidate_primary",
        "out_of_scope_primary",
        "required_exhibit",
        "candidate_scope",
    ],
)
def test_scoped_complete_requires_a_present_required_primary(
    tmp_path: Path, document_case: str
) -> None:
    root = tmp_path / document_case
    with _open(root) as writer:
        submission, inventory = _seed_raw(writer)
        filing_overrides = {"raw_coverage_status": "scoped_complete"}
        if document_case == "candidate_scope":
            filing_overrides["scope_status"] = "candidate"
        filing = _filing_row(submission, **filing_overrides)
        tables = {"filings": _table(FILINGS_SCHEMA, filing)}
        refs = [submission, inventory]
        if document_case == "empty_table":
            tables["documents"] = pa.Table.from_pylist([], schema=DOCUMENTS_SCHEMA)
        elif document_case != "missing_table":
            present = writer.put_raw_bytes(b"verified primary bytes")
            refs.append(present)
            overrides: dict[str, Any] = {}
            if document_case == "candidate_primary":
                overrides["selection_status"] = "candidate"
            elif document_case == "out_of_scope_primary":
                overrides["selection_status"] = "out_of_scope"
            elif document_case == "required_exhibit":
                overrides["role"] = "exhibit"
            tables["documents"] = _table(
                DOCUMENTS_SCHEMA, _document_row(inventory, present, **overrides)
            )
        expected_message = (
            "included filing scope"
            if document_case == "candidate_scope"
            else "present required primary"
        )
        with pytest.raises(ArchiveError, match=expected_message):
            writer.commit(tables=tables, raw_objects=refs, expected_manifest_version=0)


def test_identity_nullability_and_foreign_key_validation(tmp_path: Path) -> None:
    root = tmp_path / "bad-identity"
    with _open(root) as writer:
        submission, inventory = _seed_raw(writer)
        filing = _filing_row(submission, filing_id="wrong")
        with pytest.raises(ArchiveError, match="filing_id"):
            writer.commit(
                tables={"filings": _table(FILINGS_SCHEMA, filing)},
                raw_objects=(submission, inventory),
                expected_manifest_version=0,
            )

    root2 = tmp_path / "bad-null"
    with _open(root2) as writer:
        submission, inventory = _seed_raw(writer)
        filing = _filing_row(submission, form=None)
        table = _table(FILINGS_SCHEMA, filing)
        with pytest.raises(ArchiveError, match="nulls"):
            writer.commit(
                tables={"filings": table},
                raw_objects=(submission, inventory),
                expected_manifest_version=0,
            )

    root3 = tmp_path / "bad-fk"
    with _open(root3) as writer:
        submission, inventory = _seed_raw(writer)
        filing = _filing_row(submission)
        foreign_filing_id = "0000000001:0000000001-24-000001"
        doc = _document_row(
            inventory,
            filing_id=foreign_filing_id,
            document_id=document_id(foreign_filing_id, "https://www.sec.gov/Archives/annual.htm"),
        )
        with pytest.raises(ArchiveError, match="unknown filing_id"):
            writer.commit(
                tables={
                    "filings": _table(FILINGS_SCHEMA, filing),
                    "documents": _table(DOCUMENTS_SCHEMA, doc),
                },
                raw_objects=(submission, inventory),
                expected_manifest_version=0,
            )


def test_generic_arrow_table_and_absent_tables_are_retained(tmp_path: Path) -> None:
    root = tmp_path / "archive"
    generic_schema = pa.schema(
        [pa.field("metric", pa.string(), nullable=False), pa.field("value", pa.int64())]
    )
    generic = pa.Table.from_pylist([{"metric": "assets", "value": 7}], schema=generic_schema)
    with _open(root) as writer:
        first = writer.commit(
            tables={"facts": generic}, raw_objects=(), expected_manifest_version=0
        )
    with _open(root, resume=True) as writer:
        second = writer.commit(tables={}, raw_objects=(), expected_manifest_version=1)
    assert second.tables["facts"].schema.equals(generic_schema, check_metadata=True)
    assert second.tables["facts"].to_pylist() == [{"metric": "assets", "value": 7}]
    assert first.snapshot_id != second.snapshot_id


def test_root_protection_and_foreign_root_checked_before_writes(tmp_path: Path) -> None:
    protected = tmp_path / "protected"
    protected.mkdir()
    exact_root = protected
    with pytest.raises(ArchiveError, match="overlaps"):
        open_archive(exact_root, _spec(), protected_paths=(protected,))

    child = protected / "archive"
    with pytest.raises(ArchiveError, match="overlaps"):
        open_archive(child, _spec(), protected_paths=(protected,))
    assert not child.exists()

    ancestor = tmp_path / "candidate"
    with pytest.raises(ArchiveError, match="overlaps"):
        open_archive(ancestor, _spec(), protected_paths=(ancestor / "protected-input",))
    assert not ancestor.exists()

    alias = tmp_path / "protected-alias"
    alias.symlink_to(protected, target_is_directory=True)
    symlink_child = alias / "archive"
    with pytest.raises(ArchiveError, match="overlaps"):
        open_archive(symlink_child, _spec(), protected_paths=(protected,))
    assert not (protected / "archive").exists()

    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "keep.txt").write_text("do not touch", encoding="utf-8")
    before = (foreign / "keep.txt").read_bytes()
    with pytest.raises(ArchiveConflictError, match="resume=True"):
        open_archive(foreign, _spec(), protected_paths=_protected(tmp_path))
    assert (foreign / "keep.txt").read_bytes() == before
    assert not (foreign / ".writer.lock").exists()


def test_resume_is_explicit_and_requires_same_runspec(tmp_path: Path) -> None:
    root = tmp_path / "archive"
    with _open(root) as writer:
        writer.commit(tables={}, raw_objects=(), expected_manifest_version=0)

    with pytest.raises(ArchiveConflictError, match="resume=True"):
        open_archive(root, _spec(), protected_paths=_protected(tmp_path))
    changed = _spec(input_fingerprint="b" * 64)
    with pytest.raises(ArchiveConflictError, match="RunSpec"):
        open_archive(root, changed, protected_paths=_protected(tmp_path), resume=True)


def test_second_writer_is_refused_and_lock_released_on_context_exit(tmp_path: Path) -> None:
    root = tmp_path / "archive"
    with _open(root):
        with pytest.raises(ArchiveConflictError, match="single-writer"):
            open_archive(root, _spec(), protected_paths=_protected(tmp_path), resume=True)
    with _open(root, resume=True, recover_unpublished=True) as writer:
        assert writer is not None


def test_raw_and_parquet_corruption_fail_closed(tmp_path: Path) -> None:
    root = tmp_path / "raw-corrupt"
    with _open(root) as writer:
        raw = writer.put_raw_bytes(b"raw")
        _snapshot = writer.commit(tables={}, raw_objects=(raw,), expected_manifest_version=0)
    (root / raw.path).write_bytes(b"tampered")
    with pytest.raises(ArchiveCorruptionError, match="raw object"):
        read_snapshot(root)

    root2 = tmp_path / "parquet-corrupt"
    with _open(root2) as writer:
        snapshot2 = writer.commit(
            tables={"filings": pa.Table.from_pylist([], schema=FILINGS_SCHEMA)},
            raw_objects=(),
            expected_manifest_version=0,
        )
    batch = snapshot2.manifest["tables"]["filings"]["batches"][0]
    (root2 / batch["path"]).write_bytes(b"tampered")
    with pytest.raises(ArchiveCorruptionError, match="hash or byte_size"):
        read_snapshot(root2)


def test_schema_manifest_corruption_fails(tmp_path: Path) -> None:
    root = tmp_path / "archive"
    with _open(root) as writer:
        writer.commit(
            tables={"filings": pa.Table.from_pylist([], schema=FILINGS_SCHEMA)},
            raw_objects=(),
            expected_manifest_version=0,
        )
    head = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    head["tables"]["filings"]["schema_sha256"] = "0" * 64
    content = json.dumps(head, sort_keys=True, separators=(",", ":")).encode()
    (root / "manifest.json").write_bytes(content)
    (root / "snapshots" / head["snapshot_id"] / "manifest.json").write_bytes(content)
    with pytest.raises(ArchiveCorruptionError, match="schema hash"):
        read_snapshot(root)


def test_read_ignores_orphan_unlisted_objects(tmp_path: Path) -> None:
    root = tmp_path / "archive"
    with _open(root) as writer:
        writer.commit(tables={}, raw_objects=(), expected_manifest_version=0)
    # Deliberately malformed unlisted file is not discovered by globbing.
    (root / "raw" / "sha256" / "ff").mkdir(parents=True)
    (root / "raw" / "sha256" / "ff" / ("f" * 64)).write_bytes(b"wrong")
    (root / "tables" / "facts" / "orphan").mkdir(parents=True)
    (root / "tables" / "facts" / "orphan" / "part-00000.parquet").write_bytes(b"wrong")
    assert read_snapshot(root).tables == {}


def test_descriptor_reader_verifies_footer_and_exposes_paths_without_reading_tables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "archive"
    generic = pa.table({"metric": ["assets", "revenue"], "value": [7, 9]})
    with _open(root) as writer:
        snapshot = writer.commit(
            tables={"facts": generic}, raw_objects=(), expected_manifest_version=0
        )

    def forbidden_materialization(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("descriptor reader must not materialize Parquet tables")

    monkeypatch.setattr(archive_module.pq, "read_table", forbidden_materialization)
    descriptors = read_snapshot_descriptors(root)
    facts = descriptors["tables"]["facts"]
    assert facts["row_count"] == 2
    assert facts["schema"].equals(generic.schema, check_metadata=True)
    assert (
        facts["batches"][0]["path"]
        == root / snapshot.manifest["tables"]["facts"]["batches"][0]["path"]
    )
    assert facts["batches"][0]["relative_path"].startswith("tables/facts/")


def test_fault_before_head_is_only_explicitly_recoverable_with_stable_run_id(
    tmp_path: Path,
) -> None:
    root = tmp_path / "archive"
    writer = _open(root)
    run_id = writer.run_id
    writer._fault_hook = lambda step: (
        (_ for _ in ()).throw(RuntimeError(step)) if step == "before_head_replace" else None
    )
    with writer:
        with pytest.raises(RuntimeError, match="before_head_replace"):
            writer.commit(tables={}, raw_objects=(), expected_manifest_version=0)
    assert not (root / "manifest.json").exists()
    assert json.loads((root / "run.json").read_text(encoding="utf-8"))["run_id"] == run_id
    with pytest.raises(ArchiveCorruptionError, match="recover_unpublished"):
        _open(root, resume=True)
    with pytest.raises(ArchiveCorruptionError, match="head manifest"):
        read_snapshot(root)

    with _open(root, resume=True, recover_unpublished=True) as recovered:
        assert recovered.run_id == run_id
        snapshot = recovered.commit(tables={}, raw_objects=(), expected_manifest_version=0)
    assert snapshot.manifest_version == 1
    assert snapshot.manifest["run_id"] == run_id


def test_lost_published_head_fails_closed_even_with_explicit_recovery(tmp_path: Path) -> None:
    root = tmp_path / "archive"
    with _open(root) as writer:
        snapshot = writer.commit(tables={}, raw_objects=(), expected_manifest_version=0)
    (root / "manifest.json").unlink()

    with pytest.raises(ArchiveCorruptionError, match="recover_unpublished"):
        _open(root, resume=True)
    with pytest.raises(ArchiveCorruptionError, match="publication may have occurred"):
        _open(root, resume=True, recover_unpublished=True)
    assert (
        json.loads((root / "run.json").read_text(encoding="utf-8"))["run_id"]
        == snapshot.manifest["run_id"]
    )


def test_fault_after_head_is_committed_even_without_ledger_event(tmp_path: Path) -> None:
    root = tmp_path / "archive"
    writer = _open(root)
    writer._fault_hook = lambda step: (
        (_ for _ in ()).throw(RuntimeError(step)) if step == "after_head_replace" else None
    )
    with writer:
        with pytest.raises(RuntimeError, match="after_head_replace"):
            writer.commit(tables={}, raw_objects=(), expected_manifest_version=0)
    committed = read_snapshot(root)
    assert committed.manifest_version == 1
    ledger_path = root / "ledger.jsonl"
    assert not ledger_path.exists() or "snapshot_published" not in ledger_path.read_text(
        encoding="utf-8"
    )


def test_marker_without_head_blocks_same_writer_republication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "archive"
    writer = _open(root)

    def fail_head_replace(path: Path, content: bytes) -> None:
        raise RuntimeError("injected failure after publication marker")

    monkeypatch.setattr(archive_module, "_atomic_replace", fail_head_replace)
    try:
        with pytest.raises(RuntimeError, match="after publication marker"):
            writer.commit(tables={}, raw_objects=(), expected_manifest_version=0)
        assert (root / archive_module._PUBLICATION_MARKER).exists()
        assert not (root / "manifest.json").exists()
        before_retry = _file_inventory(root)

        with pytest.raises(ArchiveCorruptionError, match="publication may have occurred"):
            writer.commit(tables={}, raw_objects=(), expected_manifest_version=0)
        assert _file_inventory(root) == before_retry
    finally:
        writer.close()


def test_lost_head_blocks_same_writer_republication_without_file_changes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "archive"
    writer = _open(root)
    try:
        snapshot = writer.commit(tables={}, raw_objects=(), expected_manifest_version=0)
        (root / "manifest.json").unlink()
        assert writer._snapshot is snapshot
        before_retry = _file_inventory(root)

        with pytest.raises(ArchiveCorruptionError, match="publication may have occurred"):
            writer.commit(tables={}, raw_objects=(), expected_manifest_version=0)
        assert _file_inventory(root) == before_retry
    finally:
        writer.close()


def test_parent_hash_uses_exact_immutable_parent_bytes(tmp_path: Path) -> None:
    root = tmp_path / "archive"
    with _open(root) as writer:
        first = writer.commit(tables={}, raw_objects=(), expected_manifest_version=0)
    head_path = root / "manifest.json"
    immutable_parent = root / "snapshots" / first.snapshot_id / "manifest.json"
    reformatted = json.dumps(
        json.loads(head_path.read_text(encoding="utf-8")), indent=2, sort_keys=False
    ).encode("utf-8")
    head_path.write_bytes(reformatted)
    immutable_parent.write_bytes(reformatted)
    read_snapshot(root)

    with _open(root, resume=True) as writer:
        second = writer.commit(tables={}, raw_objects=(), expected_manifest_version=1)
    assert second.manifest["parent_manifest_sha256"] == hashlib.sha256(reformatted).hexdigest()
    assert read_snapshot(root).snapshot_id == second.snapshot_id


def test_manifest_lineage_parent_boundary_is_enforced(tmp_path: Path) -> None:
    for version in (1, 2):
        root = tmp_path / f"version-{version}"
        with _open(root) as writer:
            first = writer.commit(tables={}, raw_objects=(), expected_manifest_version=0)
            if version == 1:
                snapshot = first
            else:
                snapshot = writer.commit(tables={}, raw_objects=(), expected_manifest_version=1)
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        if version == 1:
            manifest["parent_snapshot_id"] = "a" * 32
            manifest["parent_manifest_sha256"] = "b" * 64
            expected_error = "version-1"
        else:
            manifest["parent_snapshot_id"] = None
            manifest["parent_manifest_sha256"] = None
            expected_error = "require a parent"
        encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        (root / "manifest.json").write_bytes(encoded)
        (root / "snapshots" / snapshot.snapshot_id / "manifest.json").write_bytes(encoded)
        with pytest.raises(ArchiveCorruptionError, match=expected_error):
            read_snapshot(root)


def test_each_created_archive_directory_parent_is_fsynced_before_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    protected_paths = _protected(tmp_path)
    root = tmp_path / "archive"
    events: list[tuple[str, Path]] = []
    original_mkdir = Path.mkdir
    original_fsync = archive_module._fsync_directory

    def tracked_mkdir(path: Path, *args: Any, **kwargs: Any) -> None:
        existed = path.exists()
        original_mkdir(path, *args, **kwargs)
        if not existed:
            events.append(("mkdir", path))

    def tracked_fsync(path: Path) -> None:
        original_fsync(path)
        events.append(("fsync", path))

    monkeypatch.setattr(Path, "mkdir", tracked_mkdir)
    monkeypatch.setattr(archive_module, "_fsync_directory", tracked_fsync)
    writer = open_archive(root, _spec(), protected_paths=protected_paths)

    def verify_before_publish(step: str) -> None:
        if step != "before_head_replace":
            return
        made = [
            index
            for index, event in enumerate(events)
            if event[0] == "mkdir" and event[1].is_relative_to(root)
        ]
        assert made
        for index in made:
            _, directory = events[index]
            assert events[index + 1] == ("fsync", directory.parent)

    writer._fault_hook = verify_before_publish
    with writer:
        writer.put_raw_bytes(b"archive raw bytes")
        writer.commit(
            tables={"facts": pa.table({"value": [1]})},
            raw_objects=(),
            expected_manifest_version=0,
        )


def test_record_attempt_only_allows_safe_identifiers_and_diagnostics(tmp_path: Path) -> None:
    root = tmp_path / "archive"
    with _open(root) as writer:
        writer.record_attempt(FILING_ID, "unavailable", "not_in_cache")
        ledger = json.loads((root / "ledger.jsonl").read_text(encoding="utf-8"))
        assert ledger["item_id"] == FILING_ID
        assert ledger["diagnostic_code"] == "not_in_cache"
        with pytest.raises(ArchiveError, match="item_id"):
            writer.record_attempt("https://example.test/?token=secret", "error")
        with pytest.raises(ArchiveError, match="diagnostic_code"):
            writer.record_attempt(FILING_ID, "error", "Authorization: secret")
        with pytest.raises(ArchiveError, match="status"):
            writer.record_attempt(FILING_ID, "Bearer secret")


def test_runspec_configuration_is_immutable_and_limits_approved_forms() -> None:
    policy = {"nested": {"values": ["stable"]}}
    spec = _spec(policy=policy)
    digest = spec.sha256
    policy["nested"]["values"].append("changed")
    assert spec.sha256 == digest
    assert spec.canonical_dict()["policy"] == {"nested": {"values": ["stable"]}}
    with pytest.raises(TypeError):
        spec.policy["new"] = "forbidden"
    with pytest.raises(ValueError, match="approved SEC forms"):
        _spec(approved_forms=("8-K",))


def test_run_spec_and_manifest_provenance_are_persisted(tmp_path: Path) -> None:
    root = tmp_path / "archive"
    spec = _spec(
        policy={"coverage": "bounded"},
        coverage_provenance={"status": "partial"},
        scope_provenance={"forms": ["10-K"]},
    )
    with open_archive(root, spec, protected_paths=_protected(tmp_path)) as writer:
        snapshot = writer.commit(tables={}, raw_objects=(), expected_manifest_version=0)
    assert snapshot.manifest["run_spec_sha256"] == spec.sha256
    assert snapshot.manifest["run_spec"]["policy"] == {"coverage": "bounded"}
    assert (
        json.loads((root / "run.json").read_text(encoding="utf-8"))["run_spec_sha256"]
        == spec.sha256
    )
