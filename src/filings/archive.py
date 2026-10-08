"""Transactional, immutable local filing archive storage.

The archive is an explicitly selected run root; this module never chooses a
production path, fetches network data, or imports parser implementations.
"""

from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import re
import shutil
import tempfile
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from pathlib import Path, PurePosixPath
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from .models import (
    DOCUMENTS_SCHEMA,
    FILINGS_SCHEMA,
    RawObjectRef,
    RunSpec,
    Snapshot,
    _thaw_json,
    document_id,
    filing_id,
)

_FORMAT = "filings-core-archive"
_FORMAT_VERSION = 1
_HEX_256 = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_RUN_ID = re.compile(r"[0-9a-f]{32}\Z", re.ASCII)
_PUBLICATION_MARKER = ".publication_started"
_TABLE_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,127}\Z", re.ASCII)
_SAFE_ITEM_ID = re.compile(r"(?:[0-9]{10}:[0-9]{10}-[0-9]{2}-[0-9]{6}|[0-9a-f]{64})\Z", re.ASCII)
_SAFE_DIAGNOSTIC = re.compile(r"[a-z][a-z0-9_]{0,63}\Z", re.ASCII)
_ATTEMPT_STATES = frozenset(
    {
        "pending",
        "present",
        "unavailable",
        "error",
        "skipped",
        "complete",
        "partial",
        "blocked",
        "not_attempted",
    }
)
_CORE_SCHEMAS = {"filings": FILINGS_SCHEMA, "documents": DOCUMENTS_SCHEMA}
_SCOPE_STATES = frozenset({"included", "candidate", "excluded"})
_INVENTORY_STATES = frozenset({"not_inspected", "known", "partial"})
_COVERAGE_STATES = frozenset({"not_attempted", "partial", "scoped_complete", "blocked"})
_ROLES = frozenset(
    {"filing_index", "primary", "xbrl_instance", "schema", "linkbase", "exhibit", "other"}
)
_SELECTION_STATES = frozenset({"required", "candidate", "out_of_scope"})
_FETCH_STATES = frozenset({"not_requested", "present", "unavailable", "error"})
_EXTRACTION_STATES = frozenset({"not_attempted", "full", "partial", "unsupported", "failed"})


class ArchiveError(ValueError):
    """Raised when an archive root or immutable archive object is invalid."""


class ArchiveConflictError(ArchiveError):
    """Raised for stale manifest versions, identity conflicts, or occupied paths."""


class ArchiveCorruptionError(ArchiveError):
    """Raised when a committed object fails immutable integrity validation."""


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ArchiveError("archive metadata must contain canonical JSON values") from exc


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _resolve_inside(root: Path, relative: str, *, must_exist: bool) -> Path:
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise ArchiveCorruptionError("archive reference must be a normalized relative POSIX path")
    token = PurePosixPath(relative)
    if token.is_absolute() or any(part in ("", ".", "..") for part in token.parts):
        raise ArchiveCorruptionError("archive reference must be a normalized relative POSIX path")
    candidate = root.joinpath(*token.parts)
    component = root
    for part in token.parts:
        component = component / part
        if component.is_symlink():
            raise ArchiveCorruptionError("archive reference traverses a symlink")
    try:
        resolved = candidate.resolve(strict=must_exist)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise ArchiveCorruptionError(
            "archive reference is missing or escapes archive root"
        ) from exc
    return resolved


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _durable_mkdir(path: Path) -> None:
    """Create each missing directory and fsync its parent entry immediately."""
    target = Path(os.path.abspath(path))
    missing: list[Path] = []
    current = target
    while not current.exists():
        missing.append(current)
        parent = current.parent
        if parent == current:
            raise ArchiveError(f"cannot create directory path {target}")
        current = parent
    if not current.is_dir():
        raise ArchiveError(f"directory path has a non-directory ancestor: {current}")
    for directory in reversed(missing):
        try:
            directory.mkdir()
        except FileExistsError:
            if not directory.is_dir():
                raise
            continue
        _fsync_directory(directory.parent)


def _write_new_file(path: Path, content: bytes) -> None:
    _durable_mkdir(path.parent)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(descriptor)
    _fsync_directory(path.parent)


def _atomic_replace(path: Path, content: bytes) -> None:
    _durable_mkdir(path.parent)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _check_hash(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or _HEX_256.fullmatch(value) is None:
        raise ArchiveError(f"{label} must be a lowercase SHA-256 hex digest")
    return value


def _ref_map(refs: Sequence[RawObjectRef]) -> dict[str, RawObjectRef]:
    by_path: dict[str, RawObjectRef] = {}
    by_hash: dict[str, RawObjectRef] = {}
    for ref in refs:
        if not isinstance(ref, RawObjectRef):
            raise ArchiveError("raw_objects must contain RawObjectRef values")
        digest = _check_hash(ref.sha256, label="raw object sha256")
        if (
            isinstance(ref.byte_size, bool)
            or not isinstance(ref.byte_size, int)
            or ref.byte_size < 0
        ):
            raise ArchiveError("raw object byte_size must be a non-negative integer")
        expected_path = f"raw/sha256/{digest[:2]}/{digest}"
        if ref.path != expected_path:
            raise ArchiveError("raw object path does not match its content-addressed SHA-256")
        previous_path = by_path.get(ref.path)
        if previous_path is not None and previous_path != ref:
            raise ArchiveConflictError("conflicting raw object references for one path")
        previous_hash = by_hash.get(digest)
        if previous_hash is not None and previous_hash != ref:
            raise ArchiveConflictError("conflicting raw object references for one SHA-256")
        by_path[ref.path] = ref
        by_hash[digest] = ref
    return by_path


def _verify_raw_ref(root: Path, ref: RawObjectRef) -> None:
    refs = _ref_map((ref,))
    del refs
    path = _resolve_inside(root, ref.path, must_exist=True)
    if not path.is_file():
        raise ArchiveCorruptionError(f"raw object {ref.path} is not a regular file")
    digest, size = _hash_file(path)
    if digest != ref.sha256 or size != ref.byte_size:
        raise ArchiveCorruptionError(f"raw object {ref.path} hash or byte_size mismatch")


def _serialized_schema(schema: pa.Schema) -> str:
    return base64.b64encode(schema.serialize().to_pybytes()).decode("ascii")


def _schema_from_descriptor(value: Any) -> pa.Schema:
    if not isinstance(value, str):
        raise ArchiveCorruptionError("table descriptor is missing serialized Arrow schema")
    try:
        encoded = base64.b64decode(value, validate=True)
        return pa.ipc.read_schema(pa.BufferReader(encoded))
    except (ValueError, pa.ArrowException) as exc:
        raise ArchiveCorruptionError("table descriptor has an invalid Arrow schema") from exc


def _schema_matches(table: pa.Table, schema: pa.Schema, *, table_name: str) -> None:
    if not table.schema.equals(schema, check_metadata=True):
        raise ArchiveError(f"table {table_name} does not use its exact Arrow schema")
    for field in schema:
        if not field.nullable and table[field.name].null_count:
            raise ArchiveError(
                f"table {table_name}.{field.name} contains nulls in a required field"
            )


def _read_table_descriptor(root: Path, name: str, descriptor: Mapping[str, Any]) -> pa.Table:
    if not isinstance(descriptor, Mapping):
        raise ArchiveCorruptionError(f"table descriptor {name} is malformed")
    if descriptor.get("version") != 1:
        raise ArchiveCorruptionError(f"table {name} has an unsupported descriptor version")
    schema = _schema_from_descriptor(descriptor.get("schema_arrow_ipc_base64"))
    expected_schema_hash = _check_hash(
        descriptor.get("schema_sha256"), label=f"table {name} schema_sha256"
    )
    if _sha256(schema.serialize().to_pybytes()) != expected_schema_hash:
        raise ArchiveCorruptionError(f"table {name} serialized schema hash mismatch")
    if name in _CORE_SCHEMAS and not schema.equals(_CORE_SCHEMAS[name], check_metadata=True):
        raise ArchiveCorruptionError(f"table {name} serialized schema does not match core schema")
    batches = descriptor.get("batches")
    if not isinstance(batches, list) or not batches:
        raise ArchiveCorruptionError(f"table {name} must list at least one Parquet batch")
    tables: list[pa.Table] = []
    row_total = 0
    for _batch_index, batch in enumerate(batches):
        if not isinstance(batch, Mapping):
            raise ArchiveCorruptionError(f"table {name} batch descriptor is malformed")
        relative = batch.get("path")
        batch_path = PurePosixPath(relative) if isinstance(relative, str) else PurePosixPath(".")
        if (
            len(batch_path.parts) != 4
            or batch_path.parts[0] != "tables"
            or batch_path.parts[1] != name
            or re.fullmatch(r"[0-9a-f]{32}", batch_path.parts[2], re.ASCII) is None
            or re.fullmatch(r"part-[0-9]{5}\.parquet", batch_path.parts[3], re.ASCII) is None
        ):
            raise ArchiveCorruptionError(f"table {name} batch path is not an immutable table path")
        digest = _check_hash(batch.get("sha256"), label=f"table {name} batch sha256")
        size = batch.get("byte_size")
        rows = batch.get("row_count")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ArchiveCorruptionError(f"table {name} batch byte_size is invalid")
        if isinstance(rows, bool) or not isinstance(rows, int) or rows < 0:
            raise ArchiveCorruptionError(f"table {name} batch row_count is invalid")
        path = _resolve_inside(root, relative, must_exist=True)
        actual_digest, actual_size = _hash_file(path)
        if actual_digest != digest or actual_size != size:
            raise ArchiveCorruptionError(f"table {name} batch hash or byte_size mismatch")
        try:
            table = pq.read_table(path)
        except (OSError, pa.ArrowException) as exc:
            raise ArchiveCorruptionError(f"table {name} Parquet batch cannot be read") from exc
        if not table.schema.equals(schema, check_metadata=True):
            raise ArchiveCorruptionError(f"table {name} Parquet schema does not match descriptor")
        if table.num_rows != rows:
            raise ArchiveCorruptionError(f"table {name} Parquet row_count mismatch")
        row_total += rows
        tables.append(table)
    expected_rows = descriptor.get("row_count")
    if (
        isinstance(expected_rows, bool)
        or not isinstance(expected_rows, int)
        or expected_rows != row_total
    ):
        raise ArchiveCorruptionError(f"table {name} total row_count mismatch")
    result = pa.concat_tables(tables) if len(tables) > 1 else tables[0]
    if name in _CORE_SCHEMAS:
        _schema_matches(result, _CORE_SCHEMAS[name], table_name=name)
    return result


def _parse_scope_evidence_json(value: Any) -> Mapping[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ArchiveError("scope_evidence_json must be a canonical JSON string or null")
    try:
        evidence = json.loads(value)
        canonical = _canonical_json(evidence).decode("utf-8")
    except (json.JSONDecodeError, ArchiveError) as exc:
        raise ArchiveError("scope_evidence_json must be a canonical JSON object") from exc
    if not isinstance(evidence, Mapping) or canonical != value:
        raise ArchiveError("scope_evidence_json must be a canonical JSON object")

    if evidence.get("policy") != "sec-metadata-financial-v1":
        raise ArchiveError("scope evidence has an unsupported policy")
    if evidence.get("decision") not in ("included", "excluded"):
        raise ArchiveError("scope evidence decision must be included or excluded")
    _check_hash(evidence.get("inventory_sha256"), label="scope evidence inventory_sha256")
    locator = evidence.get("inventory_locator")
    if not isinstance(locator, str) or not locator.strip():
        raise ArchiveError("scope evidence inventory_locator must be a non-empty string")
    selected_ids = evidence.get("selected_financial_document_ids")
    if not isinstance(selected_ids, list) or any(
        not isinstance(document_id_value, str) or not document_id_value
        for document_id_value in selected_ids
    ):
        raise ArchiveError("scope evidence selected_financial_document_ids must be a string array")
    if len(set(selected_ids)) != len(selected_ids):
        raise ArchiveError("scope evidence selected financial document IDs must be unique")
    reasons = evidence.get("reasons")
    if not isinstance(reasons, list) or any(
        not isinstance(reason, str) or not reason.strip() for reason in reasons
    ):
        raise ArchiveError("scope evidence reasons must be an array of non-empty strings")
    return evidence


def _validate_core_tables(
    tables: Mapping[str, pa.Table], raw_refs: Sequence[RawObjectRef], spec: RunSpec
) -> None:
    for name, schema in _CORE_SCHEMAS.items():
        table = tables.get(name)
        if table is not None:
            _schema_matches(table, schema, table_name=name)

    raw_by_path = _ref_map(raw_refs)
    raw_by_hash = {ref.sha256: ref for ref in raw_by_path.values()}
    filings = tables.get("filings")
    documents = tables.get("documents")
    filing_rows: dict[str, Mapping[str, Any]] = {}
    scope_evidence_by_filing: dict[str, Mapping[str, Any]] = {}
    documents_by_id: dict[str, Mapping[str, Any]] = {}
    present_required_primary: set[str] = set()

    if filings is not None:
        seen: set[str] = set()
        for row in filings.to_pylist():
            identity = filing_id(row["cik10"], row["accession_number"])
            if row["filing_id"] != identity:
                raise ArchiveError("filings row filing_id does not match CIK/accession identity")
            if identity in seen:
                raise ArchiveError(f"duplicate filing_id in filings table: {identity}")
            seen.add(identity)
            filing_rows[identity] = row
            if row["form"] not in spec.approved_forms:
                raise ArchiveError(
                    f"filings row form is outside RunSpec approved_forms: {row['form']}"
                )
            if row["is_amendment"] != row["form"].endswith("/A"):
                raise ArchiveError("is_amendment must match the SEC /A form suffix")
            filed = row["filed_date"]
            visible = row["effective_visible_session"]
            if filed is None or visible is None or visible <= filed:
                raise ArchiveError("effective_visible_session must be strictly after filed_date")
            if row["scope_status"] not in _SCOPE_STATES:
                raise ArchiveError("filings row has invalid scope_status")
            if row["inventory_status"] not in _INVENTORY_STATES:
                raise ArchiveError("filings row has invalid inventory_status")
            if row["raw_coverage_status"] not in _COVERAGE_STATES:
                raise ArchiveError("filings row has invalid raw_coverage_status")
            scope_evidence = _parse_scope_evidence_json(row["scope_evidence_json"])
            if row["form"] == "6-K":
                if scope_evidence is None:
                    if row["scope_status"] != "candidate":
                        raise ArchiveError(
                            "included or excluded 6-K scope requires scope_evidence_json"
                        )
                    if row["raw_coverage_status"] == "scoped_complete":
                        raise ArchiveError("candidate 6-K filings cannot be scoped_complete")
                else:
                    if scope_evidence["decision"] != row["scope_status"]:
                        raise ArchiveError(
                            "6-K scope evidence decision does not match scope_status"
                        )
                    inventory_hash = scope_evidence["inventory_sha256"]
                    if inventory_hash not in raw_by_hash:
                        raise ArchiveError(
                            "6-K scope evidence inventory hash is not an active raw object"
                        )
                    if row["inventory_status"] != "known":
                        raise ArchiveError("6-K scope evidence requires known document inventory")
                    selected_ids = scope_evidence["selected_financial_document_ids"]
                    reasons = scope_evidence["reasons"]
                    if scope_evidence["decision"] == "included":
                        if not selected_ids:
                            raise ArchiveError("included 6-K requires selected financial documents")
                    elif (
                        selected_ids
                        or not reasons
                        or row["raw_coverage_status"] == "scoped_complete"
                    ):
                        raise ArchiveError(
                            "excluded 6-K requires known inventory, no financial documents, "
                            "reasons, and non-complete coverage"
                        )
                    scope_evidence_by_filing[identity] = scope_evidence
            elif scope_evidence is not None:
                raise ArchiveError("scope_evidence_json is only valid for 6-K filings")
            for field_name in (
                "source_submission_logical_key",
                "source_submission_locator",
                "source_submission_path",
            ):
                if not row[field_name]:
                    raise ArchiveError(f"filings row is missing {field_name}")
            digest = _check_hash(row["source_submission_sha256"], label="source_submission_sha256")
            ref = raw_by_path.get(row["source_submission_path"])
            if ref is None or ref.sha256 != digest:
                raise ArchiveError(
                    "filings source submission path/hash is not an active raw object"
                )
            if row["raw_coverage_status"] == "scoped_complete":
                if row["scope_status"] != "included":
                    raise ArchiveError("scoped_complete requires included filing scope")
                if row["inventory_status"] != "known":
                    raise ArchiveError("scoped_complete requires known document inventory")

    if documents is not None:
        if documents.num_rows and filings is None:
            raise ArchiveError("documents table requires an active filings table")
        seen_documents: set[str] = set()
        for row in documents.to_pylist():
            identity = row["document_id"]
            expected_identity = document_id(row["filing_id"], row["source_url"])
            if identity != expected_identity:
                raise ArchiveError("documents row document_id does not match canonical identity")
            if identity in seen_documents:
                raise ArchiveError(f"duplicate document_id in documents table: {identity}")
            seen_documents.add(identity)
            filing = filing_rows.get(row["filing_id"])
            if filing is None:
                raise ArchiveError("documents row references an unknown filing_id")
            if row["role"] not in _ROLES:
                raise ArchiveError("documents row has invalid role")
            if row["selection_status"] not in _SELECTION_STATES:
                raise ArchiveError("documents row has invalid selection_status")
            if row["fetch_status"] not in _FETCH_STATES:
                raise ArchiveError("documents row has invalid fetch_status")
            for field_name in ("fact_extraction_status", "text_extraction_status"):
                extraction_status = row[field_name]
                if extraction_status not in _EXTRACTION_STATES:
                    raise ArchiveError(f"documents row has invalid {field_name}")
                if extraction_status != "not_attempted" and row["fetch_status"] != "present":
                    raise ArchiveError(f"{field_name} requires a present verified document payload")
            inventory_hash = _check_hash(
                row["source_inventory_sha256"], label="source_inventory_sha256"
            )
            if not row["source_inventory_locator"] or inventory_hash not in raw_by_hash:
                raise ArchiveError(
                    "document source inventory provenance is not an active raw object"
                )
            success_fields = (row["raw_sha256"], row["raw_path"], row["byte_size"])
            if row["fetch_status"] == "present":
                digest = _check_hash(row["raw_sha256"], label="document raw_sha256")
                path = row["raw_path"]
                size = row["byte_size"]
                if (
                    not isinstance(path, str)
                    or isinstance(size, bool)
                    or not isinstance(size, int)
                    or size < 0
                ):
                    raise ArchiveError("present document requires raw path and byte size")
                ref = raw_by_path.get(path)
                if ref is None or ref.sha256 != digest or ref.byte_size != size:
                    raise ArchiveError(
                        "present document raw hash/path/size is not an active raw object"
                    )
            elif any(value is not None for value in success_fields):
                raise ArchiveError(
                    "non-present document cannot declare successful raw object fields"
                )
            if (
                filing["raw_coverage_status"] == "scoped_complete"
                and row["selection_status"] == "required"
                and row["fetch_status"] != "present"
            ):
                raise ArchiveError("scoped_complete filing has a required document not present")
            if (
                row["role"] == "primary"
                and row["selection_status"] == "required"
                and row["fetch_status"] == "present"
            ):
                present_required_primary.add(row["filing_id"])
            documents_by_id[identity] = row

    for filing_identity, evidence in scope_evidence_by_filing.items():
        evidence_inventory_hash = evidence["inventory_sha256"]
        for selected_document_id in evidence["selected_financial_document_ids"]:
            document = documents_by_id.get(selected_document_id)
            if document is None or document["filing_id"] != filing_identity:
                raise ArchiveError(
                    "scope evidence selected financial document is unknown or belongs "
                    "to another filing"
                )
            if document["selection_status"] != "required":
                raise ArchiveError("scope evidence financial documents must be required")
            if document["source_inventory_sha256"] != evidence_inventory_hash:
                raise ArchiveError(
                    "scope evidence financial document inventory hash does not match"
                )

    if filings is not None:
        for identity, row in filing_rows.items():
            if row["raw_coverage_status"] != "scoped_complete":
                continue
            if documents is None or identity not in present_required_primary:
                raise ArchiveError(
                    "scoped_complete filing requires a present required primary document"
                )

        for row in filing_rows.values():
            parent = row["parent_filing_id"]
            if parent is not None and parent not in filing_rows:
                raise ArchiveError("filings row references an unknown parent_filing_id")


def _validate_manifest_shape(manifest: Any) -> Mapping[str, Any]:
    if not isinstance(manifest, Mapping):
        raise ArchiveCorruptionError("archive manifest must be a JSON object")
    required = {
        "format",
        "format_version",
        "run_id",
        "manifest_version",
        "snapshot_id",
        "parent_snapshot_id",
        "parent_manifest_sha256",
        "run_spec",
        "run_spec_sha256",
        "input_fingerprint",
        "coverage_provenance",
        "scope_provenance",
        "calendar_provenance",
        "tables",
        "raw_objects",
    }
    if not required <= set(manifest):
        raise ArchiveCorruptionError("archive manifest is missing required fields")
    if manifest["format"] != _FORMAT or manifest["format_version"] != _FORMAT_VERSION:
        raise ArchiveCorruptionError("unsupported archive manifest format/version")
    version = manifest["manifest_version"]
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ArchiveCorruptionError("published archive manifest_version must be positive")
    for name in ("run_id", "snapshot_id", "run_spec_sha256"):
        if not isinstance(manifest[name], str) or not manifest[name]:
            raise ArchiveCorruptionError(f"archive manifest {name} is invalid")
    if _RUN_ID.fullmatch(manifest["run_id"]) is None:
        raise ArchiveCorruptionError("archive manifest run_id is invalid")
    tables = manifest["tables"]
    refs = manifest["raw_objects"]
    if not isinstance(tables, Mapping) or not isinstance(refs, list):
        raise ArchiveCorruptionError("archive manifest tables/raw_objects have invalid shape")
    parent = manifest["parent_snapshot_id"]
    parent_hash = manifest["parent_manifest_sha256"]
    if (parent is None) != (parent_hash is None):
        raise ArchiveCorruptionError("archive manifest parent snapshot reference is incomplete")
    if version == 1 and parent is not None:
        raise ArchiveCorruptionError("version-1 archive manifest cannot have a parent snapshot")
    if version > 1 and parent is None:
        raise ArchiveCorruptionError("archive manifest versions after 1 require a parent snapshot")
    if parent is not None:
        _check_hash(parent_hash, label="parent_manifest_sha256")
        if not isinstance(parent, str) or re.fullmatch(r"[0-9a-f]{32}", parent, re.ASCII) is None:
            raise ArchiveCorruptionError("parent_snapshot_id is invalid")
    snapshot_id = manifest["snapshot_id"]
    if not isinstance(snapshot_id, str) or re.fullmatch(r"[0-9a-f]{32}", snapshot_id) is None:
        raise ArchiveCorruptionError("snapshot_id is invalid")
    expected_spec_hash = _sha256(_canonical_json(manifest["run_spec"]))
    if expected_spec_hash != manifest["run_spec_sha256"]:
        raise ArchiveCorruptionError("run_spec_sha256 does not match manifest run_spec")
    run_spec = manifest["run_spec"]
    if not isinstance(run_spec, Mapping):
        raise ArchiveCorruptionError("manifest run_spec must be an object")
    for manifest_key, spec_key in (
        ("input_fingerprint", "input_fingerprint"),
        ("coverage_provenance", "coverage_provenance"),
        ("scope_provenance", "scope_provenance"),
        ("calendar_provenance", "calendar_provenance"),
    ):
        if manifest[manifest_key] != run_spec.get(spec_key):
            raise ArchiveCorruptionError(f"manifest {manifest_key} differs from RunSpec")
    return manifest


def _read_published_manifest(root: Path) -> tuple[bytes, Mapping[str, Any]]:
    head_path = root / "manifest.json"
    try:
        head_bytes = head_path.read_bytes()
        head = json.loads(head_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArchiveCorruptionError("archive has no readable published head manifest") from exc
    manifest = _validate_manifest_shape(head)
    run_path = root / "run.json"
    try:
        run_document = json.loads(run_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArchiveCorruptionError("archive run.json is unreadable") from exc
    if (
        not isinstance(run_document, Mapping)
        or run_document.get("format") != _FORMAT
        or run_document.get("format_version") != _FORMAT_VERSION
        or run_document.get("run_spec") != manifest["run_spec"]
        or run_document.get("run_spec_sha256") != manifest["run_spec_sha256"]
        or ("run_id" in run_document and run_document.get("run_id") != manifest["run_id"])
    ):
        raise ArchiveCorruptionError("archive run.json differs from published RunSpec or run_id")
    snapshot_relative = f"snapshots/{manifest['snapshot_id']}/manifest.json"
    immutable_path = _resolve_inside(root, snapshot_relative, must_exist=True)
    try:
        immutable_bytes = immutable_path.read_bytes()
    except OSError as exc:
        raise ArchiveCorruptionError("immutable snapshot manifest is missing") from exc
    if immutable_bytes != head_bytes:
        raise ArchiveCorruptionError("head manifest differs from immutable snapshot manifest")

    if manifest["parent_snapshot_id"] is not None:
        parent_relative = f"snapshots/{manifest['parent_snapshot_id']}/manifest.json"
        parent_path = _resolve_inside(root, parent_relative, must_exist=True)
        try:
            parent_bytes = parent_path.read_bytes()
            if _sha256(parent_bytes) != manifest["parent_manifest_sha256"]:
                raise ArchiveCorruptionError("parent immutable snapshot hash mismatch")
            parent_manifest = _validate_manifest_shape(json.loads(parent_bytes.decode("utf-8")))
        except ArchiveCorruptionError:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ArchiveCorruptionError("parent immutable snapshot manifest is invalid") from exc
        if parent_manifest["snapshot_id"] != manifest["parent_snapshot_id"]:
            raise ArchiveCorruptionError("parent immutable snapshot identity mismatch")
        if (
            parent_manifest["manifest_version"] != manifest["manifest_version"] - 1
            or parent_manifest["run_id"] != manifest["run_id"]
            or parent_manifest["run_spec_sha256"] != manifest["run_spec_sha256"]
        ):
            raise ArchiveCorruptionError("parent snapshot lineage is inconsistent")
    return head_bytes, manifest


def _read_snapshot_from_root(root: Path) -> Snapshot:
    _, manifest = _read_published_manifest(root)
    tables: dict[str, pa.Table] = {}
    for name, descriptor in manifest["tables"].items():
        if not isinstance(name, str) or _TABLE_NAME.fullmatch(name) is None:
            raise ArchiveCorruptionError("archive manifest has an invalid table name")
        tables[name] = _read_table_descriptor(root, name, descriptor)

    raw_objects: list[RawObjectRef] = []
    for value in manifest["raw_objects"]:
        if not isinstance(value, Mapping):
            raise ArchiveCorruptionError("raw object descriptor is malformed")
        ref = RawObjectRef(
            path=value.get("path"), sha256=value.get("sha256"), byte_size=value.get("byte_size")
        )
        try:
            _verify_raw_ref(root, ref)
        except ArchiveCorruptionError:
            raise
        except ArchiveError as exc:
            raise ArchiveCorruptionError("raw object descriptor is invalid") from exc
        raw_objects.append(ref)
    if len(_ref_map(raw_objects)) != len(raw_objects):
        raise ArchiveCorruptionError("archive manifest repeats a raw object reference")
    try:
        _validate_core_tables(tables, raw_objects, _runspec_from_canonical(manifest["run_spec"]))
    except ArchiveCorruptionError:
        raise
    except ArchiveError as exc:
        raise ArchiveCorruptionError(f"committed core table validation failed: {exc}") from exc
    return Snapshot(
        root=root, manifest=dict(manifest), tables=tables, raw_objects=tuple(raw_objects)
    )


def _runspec_from_canonical(value: Any) -> RunSpec:
    if not isinstance(value, Mapping):
        raise ArchiveCorruptionError("manifest run_spec must be an object")
    try:
        return RunSpec(
            approved_forms=tuple(value["approved_forms"]),
            policy=value["policy"],
            input_fingerprint=value["input_fingerprint"],
            calendar_provenance=value["calendar_provenance"],
            coverage_provenance=value["coverage_provenance"],
            scope_provenance=value["scope_provenance"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ArchiveCorruptionError("manifest run_spec is malformed") from exc


def read_snapshot(root: Path) -> Snapshot:
    """Read the immutable published head and verify only manifest-listed files."""
    try:
        resolved_root = Path(root).resolve(strict=True)
    except (OSError, TypeError) as exc:
        raise ArchiveError("archive root must be an existing directory") from exc
    if not resolved_root.is_dir():
        raise ArchiveError("archive root must be a directory")
    return _read_snapshot_from_root(resolved_root)


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return value


def _restore_spec(snapshot: Snapshot) -> RunSpec:
    """Reconstruct only the exact RunSpec authenticated by a verified snapshot."""
    document = _plain(snapshot.manifest.get("run_spec"))
    if not isinstance(document, dict):
        raise ArchiveCorruptionError("published archive has no canonical RunSpec")
    try:
        spec = RunSpec(
            approved_forms=tuple(document["approved_forms"]),
            policy=document["policy"],
            input_fingerprint=document["input_fingerprint"],
            calendar_provenance=document["calendar_provenance"],
            coverage_provenance=document["coverage_provenance"],
            scope_provenance=document["scope_provenance"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ArchiveCorruptionError("published archive RunSpec is malformed") from exc
    if spec.canonical_dict() != document or spec.sha256 != snapshot.manifest.get("run_spec_sha256"):
        raise ArchiveCorruptionError("published archive RunSpec failed canonical hash verification")
    return spec


def load_archive_runspec(archive_root: Path) -> RunSpec:
    """Read the verified published head and return its exact persisted RunSpec."""
    return _restore_spec(read_snapshot(Path(archive_root)))


def read_snapshot_descriptors(root: Path) -> Mapping[str, Any]:
    """Verify published files and Parquet footers without materializing table rows.

    This query-oriented view verifies manifest lineage, raw-file hashes, table-file
    hashes, Parquet schemas, and footer row counts. It intentionally does *not*
    perform full core row acceptance, foreign-key validation, or value scans;
    callers requiring archive acceptance must use :func:`read_snapshot`.
    Each returned table batch includes its absolute ``path`` for Arrow dataset
    readers that need to query the immutable files directly.
    """
    try:
        resolved_root = Path(root).resolve(strict=True)
    except (OSError, TypeError) as exc:
        raise ArchiveError("archive root must be an existing directory") from exc
    if not resolved_root.is_dir():
        raise ArchiveError("archive root must be a directory")

    _, manifest = _read_published_manifest(resolved_root)
    verified_tables: dict[str, Any] = {}
    for name, descriptor in manifest["tables"].items():
        if not isinstance(name, str) or _TABLE_NAME.fullmatch(name) is None:
            raise ArchiveCorruptionError("archive manifest has an invalid table name")
        if not isinstance(descriptor, Mapping) or descriptor.get("version") != 1:
            raise ArchiveCorruptionError(f"table descriptor {name} is malformed")
        schema = _schema_from_descriptor(descriptor.get("schema_arrow_ipc_base64"))
        expected_schema_hash = _check_hash(
            descriptor.get("schema_sha256"), label=f"table {name} schema_sha256"
        )
        if _sha256(schema.serialize().to_pybytes()) != expected_schema_hash:
            raise ArchiveCorruptionError(f"table {name} serialized schema hash mismatch")
        if name in _CORE_SCHEMAS and not schema.equals(_CORE_SCHEMAS[name], check_metadata=True):
            raise ArchiveCorruptionError(
                f"table {name} serialized schema does not match core schema"
            )

        batches = descriptor.get("batches")
        if not isinstance(batches, list) or not batches:
            raise ArchiveCorruptionError(f"table {name} must list at least one Parquet batch")
        verified_batches: list[dict[str, Any]] = []
        row_total = 0
        for batch in batches:
            if not isinstance(batch, Mapping):
                raise ArchiveCorruptionError(f"table {name} batch descriptor is malformed")
            relative = batch.get("path")
            batch_path = (
                PurePosixPath(relative) if isinstance(relative, str) else PurePosixPath(".")
            )
            if (
                len(batch_path.parts) != 4
                or batch_path.parts[0] != "tables"
                or batch_path.parts[1] != name
                or re.fullmatch(r"[0-9a-f]{32}", batch_path.parts[2], re.ASCII) is None
                or re.fullmatch(r"part-[0-9]{5}\.parquet", batch_path.parts[3], re.ASCII) is None
            ):
                raise ArchiveCorruptionError(
                    f"table {name} batch path is not an immutable table path"
                )
            digest = _check_hash(batch.get("sha256"), label=f"table {name} batch sha256")
            size = batch.get("byte_size")
            rows = batch.get("row_count")
            if isinstance(size, bool) or not isinstance(size, int) or size < 0:
                raise ArchiveCorruptionError(f"table {name} batch byte_size is invalid")
            if isinstance(rows, bool) or not isinstance(rows, int) or rows < 0:
                raise ArchiveCorruptionError(f"table {name} batch row_count is invalid")
            file_path = _resolve_inside(resolved_root, relative, must_exist=True)
            actual_digest, actual_size = _hash_file(file_path)
            if actual_digest != digest or actual_size != size:
                raise ArchiveCorruptionError(f"table {name} batch hash or byte_size mismatch")
            try:
                parquet_file = pq.ParquetFile(file_path)
                footer_schema = parquet_file.schema_arrow
                footer_rows = parquet_file.metadata.num_rows
            except (OSError, pa.ArrowException) as exc:
                raise ArchiveCorruptionError(f"table {name} Parquet footer cannot be read") from exc
            if not footer_schema.equals(schema, check_metadata=True):
                raise ArchiveCorruptionError(
                    f"table {name} Parquet schema does not match descriptor"
                )
            if footer_rows != rows:
                raise ArchiveCorruptionError(f"table {name} Parquet row_count mismatch")
            row_total += rows
            verified_batches.append(
                {
                    "path": file_path,
                    "relative_path": relative,
                    "sha256": digest,
                    "byte_size": size,
                    "row_count": rows,
                }
            )

        expected_rows = descriptor.get("row_count")
        if (
            isinstance(expected_rows, bool)
            or not isinstance(expected_rows, int)
            or expected_rows != row_total
        ):
            raise ArchiveCorruptionError(f"table {name} total row_count mismatch")
        verified_tables[name] = {
            "schema": schema,
            "row_count": row_total,
            "batches": tuple(verified_batches),
        }

    raw_objects: list[RawObjectRef] = []
    for value in manifest["raw_objects"]:
        if not isinstance(value, Mapping):
            raise ArchiveCorruptionError("raw object descriptor is malformed")
        ref = RawObjectRef(
            path=value.get("path"), sha256=value.get("sha256"), byte_size=value.get("byte_size")
        )
        try:
            _verify_raw_ref(resolved_root, ref)
        except ArchiveCorruptionError:
            raise
        except ArchiveError as exc:
            raise ArchiveCorruptionError("raw object descriptor is invalid") from exc
        raw_objects.append(ref)
    if len(_ref_map(raw_objects)) != len(raw_objects):
        raise ArchiveCorruptionError("archive manifest repeats a raw object reference")

    return {
        "root": resolved_root,
        "manifest": manifest,
        "tables": verified_tables,
        "raw_objects": tuple(raw_objects),
    }


class ArchiveWriter(AbstractContextManager["ArchiveWriter"]):
    """Single-writer archive transaction handle; acquire via :func:`open_archive`."""

    def __init__(
        self,
        root: Path,
        spec: RunSpec,
        lock_fd: int,
        snapshot: Snapshot | None,
        run_id: str,
    ) -> None:
        self.root = root
        self.spec = spec
        self._lock_fd = lock_fd
        self._snapshot = snapshot
        self._closed = False
        self._fault_hook: Callable[[str], None] | None = None
        self._spec_document = json.loads(_canonical_json(spec.canonical_dict()).decode("utf-8"))
        self._spec_sha256 = _sha256(_canonical_json(self._spec_document))
        self.run_id = run_id

    def __enter__(self) -> ArchiveWriter:
        self._ensure_open()
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        self.close()

    def _ensure_open(self) -> None:
        if self._closed:
            raise ArchiveError("archive writer is closed")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(self._lock_fd)

    def put_raw_bytes(self, content: bytes, *, expected_sha256: str | None = None) -> RawObjectRef:
        """Install bytes into immutable SHA-256 CAS without activating them."""
        self._ensure_open()
        if not isinstance(content, bytes):
            raise ArchiveError("put_raw_bytes requires bytes")
        digest = _sha256(content)
        if (
            expected_sha256 is not None
            and _check_hash(expected_sha256, label="expected_sha256") != digest
        ):
            raise ArchiveError("raw bytes do not match expected_sha256")
        ref = RawObjectRef(f"raw/sha256/{digest[:2]}/{digest}", digest, len(content))
        destination = _resolve_inside(self.root, ref.path, must_exist=False)
        _durable_mkdir(destination.parent)
        if destination.exists():
            _verify_raw_ref(self.root, ref)
            return ref
        stage = self.root / ".staging" / f"raw-{uuid.uuid4().hex}.tmp"
        _write_new_file(stage, content)
        try:
            _durable_mkdir(destination.parent)
            try:
                os.link(stage, destination)
                _fsync_directory(destination.parent)
            except FileExistsError:
                _verify_raw_ref(self.root, ref)
        finally:
            stage.unlink(missing_ok=True)
        return ref

    def put_raw_file(self, source: Path, *, expected_sha256: str | None = None) -> RawObjectRef:
        """Copy a source file into CAS, streaming it and never moving/linking it."""
        self._ensure_open()
        if expected_sha256 is not None:
            expected_sha256 = _check_hash(expected_sha256, label="expected_sha256")
        try:
            source_path = Path(source).resolve(strict=True)
            if not source_path.is_file():
                raise ArchiveError("put_raw_file source must be a regular file")
        except (OSError, TypeError) as exc:
            raise ArchiveError("put_raw_file source must be an existing regular file") from exc
        stage = self.root / ".staging" / f"raw-{uuid.uuid4().hex}.tmp"
        _durable_mkdir(stage.parent)
        digest = hashlib.sha256()
        size = 0
        try:
            with source_path.open("rb") as src, stage.open("xb") as dst:
                while chunk := src.read(1024 * 1024):
                    dst.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
                dst.flush()
                os.fsync(dst.fileno())
            hash_value = digest.hexdigest()
            if expected_sha256 is not None and expected_sha256 != hash_value:
                raise ArchiveError("source file does not match expected_sha256")
            ref = RawObjectRef(f"raw/sha256/{hash_value[:2]}/{hash_value}", hash_value, size)
            destination = _resolve_inside(self.root, ref.path, must_exist=False)
            _durable_mkdir(destination.parent)
            try:
                os.link(stage, destination)
                _fsync_directory(destination.parent)
            except FileExistsError:
                _verify_raw_ref(self.root, ref)
            return ref
        finally:
            stage.unlink(missing_ok=True)

    def record_attempt(self, item_id: str, status: str, diagnostic_code: str | None = None) -> None:
        """Append a sanitized event; ledger history is not publication truth."""
        self._ensure_open()
        if not isinstance(item_id, str) or _SAFE_ITEM_ID.fullmatch(item_id) is None:
            raise ArchiveError("item_id must be a safe non-empty identifier")
        if not isinstance(status, str) or status not in _ATTEMPT_STATES:
            raise ArchiveError("status is not an approved attempt state")
        if diagnostic_code is not None and (
            not isinstance(diagnostic_code, str)
            or _SAFE_DIAGNOSTIC.fullmatch(diagnostic_code) is None
        ):
            raise ArchiveError("diagnostic_code must be a sanitized lowercase code")
        event = {
            "event": "attempt",
            "item_id": item_id,
            "status": status,
            "diagnostic_code": diagnostic_code,
        }
        ledger = self.root / "ledger.jsonl"
        with ledger.open("ab") as stream:
            stream.write(_canonical_json(event) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        _fsync_directory(self.root)

    def commit(
        self,
        *,
        tables: Mapping[str, pa.Table],
        raw_objects: Sequence[RawObjectRef],
        expected_manifest_version: int,
    ) -> Snapshot:
        """Publish full table replacements and a verified active raw-object set."""
        self._ensure_open()
        if not isinstance(tables, Mapping):
            raise ArchiveError("tables must be a mapping of table names to Arrow tables")
        if isinstance(expected_manifest_version, bool) or not isinstance(
            expected_manifest_version, int
        ):
            raise ArchiveError("expected_manifest_version must be an integer")
        head_path = self.root / "manifest.json"
        current = _read_snapshot_from_root(self.root) if head_path.exists() else None
        if current is None and (
            self._snapshot is not None or (self.root / _PUBLICATION_MARKER).exists()
        ):
            raise ArchiveCorruptionError(
                "archive publication may have occurred; refusing headless commit"
            )
        current_version = current.manifest_version if current is not None else 0
        if expected_manifest_version != current_version:
            raise ArchiveConflictError(
                f"expected manifest version {expected_manifest_version}, found {current_version}"
            )
        if current is not None and current.manifest["run_spec_sha256"] != self._spec_sha256:
            raise ArchiveConflictError("archive RunSpec changed during writer lifecycle")

        replacement_tables: dict[str, pa.Table] = {}
        for name, table in tables.items():
            if not isinstance(name, str) or _TABLE_NAME.fullmatch(name) is None:
                raise ArchiveError(f"invalid table name: {name!r}")
            if not isinstance(table, pa.Table):
                raise ArchiveError(f"table {name} must be a pyarrow.Table")
            if name in _CORE_SCHEMAS:
                _schema_matches(table, _CORE_SCHEMAS[name], table_name=name)
            replacement_tables[name] = table

        raw_by_path = _ref_map(tuple(raw_objects))
        previous_refs = current.raw_objects if current is not None else ()
        all_refs = _ref_map((*previous_refs, *raw_by_path.values()))
        for ref in all_refs.values():
            _verify_raw_ref(self.root, ref)

        active_tables = dict(current.tables) if current is not None else {}
        active_tables.update(replacement_tables)
        _validate_core_tables(active_tables, tuple(all_refs.values()), self.spec)

        snapshot_id = uuid.uuid4().hex
        staging = self.root / ".staging" / f"commit-{snapshot_id}"
        if staging.exists():
            raise ArchiveConflictError("archive commit staging path already exists")
        _durable_mkdir(staging)
        current_manifest = _thaw_json(current.manifest) if current is not None else None
        table_descriptors: dict[str, Any] = (
            dict(current_manifest["tables"]) if current_manifest is not None else {}
        )
        try:
            for name, table in replacement_tables.items():
                table_bytes = _parquet_bytes(table)
                digest = _sha256(table_bytes)
                relative = f"tables/{name}/{snapshot_id}/part-00000.parquet"
                staged_file = staging / f"{name}.parquet"
                with staged_file.open("xb") as stream:
                    stream.write(table_bytes)
                    stream.flush()
                    os.fsync(stream.fileno())
                descriptor = {
                    "version": 1,
                    "schema_arrow_ipc_base64": _serialized_schema(table.schema),
                    "schema_sha256": _sha256(table.schema.serialize().to_pybytes()),
                    "row_count": table.num_rows,
                    "batches": [
                        {
                            "path": relative,
                            "sha256": digest,
                            "byte_size": len(table_bytes),
                            "row_count": table.num_rows,
                        }
                    ],
                }
                table_descriptors[name] = descriptor

            next_version = current_version + 1
            parent_id = current.snapshot_id if current is not None else None
            parent_hash = None
            if current is not None:
                parent_relative = f"snapshots/{current.snapshot_id}/manifest.json"
                parent_path = _resolve_inside(self.root, parent_relative, must_exist=True)
                try:
                    parent_bytes = parent_path.read_bytes()
                    parent_document = json.loads(parent_bytes.decode("utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ArchiveCorruptionError(
                        "current immutable parent manifest is unreadable"
                    ) from exc
                if parent_document != current_manifest:
                    raise ArchiveCorruptionError(
                        "current immutable parent manifest changed before commit"
                    )
                parent_hash = _sha256(parent_bytes)
            manifest: dict[str, Any] = {
                "format": _FORMAT,
                "format_version": _FORMAT_VERSION,
                "run_id": self.run_id,
                "manifest_version": next_version,
                "snapshot_id": snapshot_id,
                "parent_snapshot_id": parent_id,
                "parent_manifest_sha256": parent_hash,
                "run_spec": self._spec_document,
                "run_spec_sha256": self._spec_sha256,
                "input_fingerprint": self._spec_document["input_fingerprint"],
                "coverage_provenance": self._spec_document["coverage_provenance"],
                "scope_provenance": self._spec_document["scope_provenance"],
                "calendar_provenance": self._spec_document["calendar_provenance"],
                "tables": table_descriptors,
                "raw_objects": [
                    {"path": ref.path, "sha256": ref.sha256, "byte_size": ref.byte_size}
                    for ref in sorted(all_refs.values(), key=lambda item: item.path)
                ],
            }
            for name in replacement_tables:
                descriptor = table_descriptors[name]
                relative = descriptor["batches"][0]["path"]
                destination = _resolve_inside(self.root, relative, must_exist=False)
                _durable_mkdir(destination.parent)
                staged_file = staging / f"{name}.parquet"
                if destination.exists():
                    raise ArchiveCorruptionError("refusing to overwrite an existing table batch")
                os.link(staged_file, destination)
                _fsync_directory(destination.parent)

            manifest_bytes = _canonical_json(manifest)
            _validate_manifest_shape(manifest)
            snapshot_dir = self.root / "snapshots" / snapshot_id
            if snapshot_dir.exists():
                raise ArchiveConflictError("archive snapshot path already exists")
            _durable_mkdir(snapshot_dir)
            immutable_manifest = snapshot_dir / "manifest.json"
            _write_new_file(immutable_manifest, manifest_bytes)
            _fsync_directory(snapshot_dir.parent)

            if self._fault_hook is not None:
                self._fault_hook("before_head_replace")
            marker_path = self.root / _PUBLICATION_MARKER
            if not marker_path.exists():
                marker = _canonical_json({"run_id": self.run_id, "state": "publication_started"})
                _write_new_file(marker_path, marker)
            _atomic_replace(self.root / "manifest.json", manifest_bytes)
            published = _read_snapshot_from_root(self.root)
            self._snapshot = published
            if self._fault_hook is not None:
                self._fault_hook("after_head_replace")
            event = {
                "event": "snapshot_published",
                "snapshot_id": snapshot_id,
                "manifest_version": next_version,
            }
            ledger = self.root / "ledger.jsonl"
            with ledger.open("ab") as stream:
                stream.write(_canonical_json(event) + b"\n")
                stream.flush()
                os.fsync(stream.fileno())
            _fsync_directory(self.root)
            return published
        finally:
            shutil.rmtree(staging, ignore_errors=True)


def _parquet_bytes(table: pa.Table) -> bytes:
    sink = pa.BufferOutputStream()
    try:
        pq.write_table(table, sink, compression="zstd", version="2.6")
        return sink.getvalue().to_pybytes()
    except (OSError, pa.ArrowException) as exc:
        raise ArchiveError("could not serialize Arrow table to Parquet") from exc


def _validate_protected_paths(root: Path, protected_paths: Sequence[Path]) -> Path:
    if isinstance(protected_paths, (str, bytes)) or not isinstance(protected_paths, Sequence):
        raise ArchiveError("protected_paths must be a sequence of explicit paths")
    resolved_root = Path(root).expanduser().resolve(strict=False)
    if resolved_root == Path(resolved_root.anchor):
        raise ArchiveError("filesystem root cannot be used as an archive root")
    for protected in protected_paths:
        try:
            protected_path = Path(protected).expanduser().resolve(strict=False)
        except (TypeError, OSError) as exc:
            raise ArchiveError("protected_paths must contain filesystem paths") from exc
        if (
            resolved_root == protected_path
            or resolved_root in protected_path.parents
            or protected_path in resolved_root.parents
        ):
            raise ArchiveError("archive root overlaps a protected path")
    return resolved_root


def open_archive(
    root: Path,
    spec: RunSpec,
    *,
    protected_paths: Sequence[Path],
    resume: bool = False,
    recover_unpublished: bool = False,
) -> ArchiveWriter:
    """Open an explicitly selected archive root while holding its writer lock.

    A fresh empty root accepts commit version zero; there is no published empty
    head. Existing archive roots require ``resume=True`` and a matching RunSpec.
    A headless non-fresh root is never resumed implicitly. ``recover_unpublished``
    explicitly permits retry only when stable run identity exists and no durable
    publication-intent marker was written; an attempted or lost publication must
    use a new root rather than resetting run identity or choosing an orphan.
    """
    if not isinstance(spec, RunSpec):
        raise ArchiveError("spec must be a RunSpec")
    if not isinstance(resume, bool):
        raise ArchiveError("resume must be a bool")
    if not isinstance(recover_unpublished, bool):
        raise ArchiveError("recover_unpublished must be a bool")
    if recover_unpublished and not resume:
        raise ArchiveError("recover_unpublished requires resume=True")
    resolved_root = _validate_protected_paths(Path(root), protected_paths)
    existed = resolved_root.exists()
    initially_empty = False
    if existed:
        if not resolved_root.is_dir():
            raise ArchiveError("archive root must be a directory")
        entries = list(resolved_root.iterdir())
        initially_empty = not entries
        if entries and not resume:
            raise ArchiveConflictError("non-empty archive root requires explicit resume=True")
        if not entries and resume:
            raise ArchiveError("cannot resume an empty archive root without a committed head")
        allowed_entries = {
            ".writer.lock",
            "run.json",
            "raw",
            "tables",
            "snapshots",
            ".staging",
            "manifest.json",
            "ledger.jsonl",
            _PUBLICATION_MARKER,
        }
        unexpected = [entry.name for entry in entries if entry.name not in allowed_entries]
        if unexpected:
            raise ArchiveConflictError(
                f"archive root contains foreign entries: {sorted(unexpected)}"
            )
        if any(entry.is_symlink() for entry in entries):
            raise ArchiveConflictError("archive root cannot contain symlink entries")
    else:
        _durable_mkdir(resolved_root)
    fresh_root = not existed or initially_empty

    lock_path = resolved_root / ".writer.lock"
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ArchiveConflictError(
                "another archive writer holds the single-writer lock"
            ) from exc
        if fresh_root:
            for directory in ("raw/sha256", "tables", "snapshots", ".staging"):
                _durable_mkdir(resolved_root / directory)
        else:
            allowed = {
                ".writer.lock",
                "run.json",
                "raw",
                "tables",
                "snapshots",
                ".staging",
                "manifest.json",
                "ledger.jsonl",
                _PUBLICATION_MARKER,
            }
            unexpected = [
                entry.name for entry in resolved_root.iterdir() if entry.name not in allowed
            ]
            if unexpected:
                raise ArchiveConflictError(
                    f"archive root contains foreign entries: {sorted(unexpected)}"
                )
            for directory in ("raw", "tables", "snapshots", ".staging"):
                candidate = resolved_root / directory
                try:
                    candidate.resolve(strict=True).relative_to(resolved_root)
                except (OSError, ValueError) as exc:
                    raise ArchiveCorruptionError(
                        f"archive root has an unsafe {directory}/ path"
                    ) from exc
                if not candidate.is_dir():
                    raise ArchiveCorruptionError(f"archive root is missing {directory}/")

        run_path = resolved_root / "run.json"
        run_base = {
            "format": _FORMAT,
            "format_version": _FORMAT_VERSION,
            "run_spec": spec.canonical_dict(),
            "run_spec_sha256": spec.sha256,
        }
        head_path = resolved_root / "manifest.json"
        if run_path.exists():
            try:
                run_document = json.loads(run_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ArchiveCorruptionError("archive run.json is unreadable") from exc
            if (
                not isinstance(run_document, Mapping)
                or set(run_document) not in (set(run_base), set(run_base) | {"run_id"})
                or run_document.get("format") != _FORMAT
                or run_document.get("format_version") != _FORMAT_VERSION
            ):
                raise ArchiveCorruptionError("archive run.json has an invalid shape")
            if (
                run_document.get("run_spec") != run_base["run_spec"]
                or run_document.get("run_spec_sha256") != run_base["run_spec_sha256"]
            ):
                raise ArchiveConflictError(
                    "RunSpec or input fingerprint does not match archive run.json"
                )
            persisted_run_id = run_document.get("run_id")
            if persisted_run_id is not None and (
                not isinstance(persisted_run_id, str) or _RUN_ID.fullmatch(persisted_run_id) is None
            ):
                raise ArchiveCorruptionError("archive run.json has an invalid stable run_id")
        elif fresh_root:
            persisted_run_id = uuid.uuid4().hex
            run_document = {**run_base, "run_id": persisted_run_id}
            _write_new_file(run_path, _canonical_json(run_document))
        else:
            raise ArchiveCorruptionError("archive root is missing immutable run.json")

        if head_path.exists():
            if not resume:
                raise ArchiveConflictError("existing archive head requires explicit resume=True")
            snapshot = _read_snapshot_from_root(resolved_root)
            if snapshot.manifest["run_spec_sha256"] != spec.sha256:
                raise ArchiveConflictError(
                    "RunSpec or input fingerprint does not match existing archive"
                )
            snapshot_run_id = snapshot.manifest["run_id"]
            if persisted_run_id is not None and persisted_run_id != snapshot_run_id:
                raise ArchiveCorruptionError("archive run.json run_id differs from published head")
            persisted_run_id = snapshot_run_id
            marker_path = resolved_root / _PUBLICATION_MARKER
            if not marker_path.exists():
                _write_new_file(
                    marker_path,
                    _canonical_json(
                        {"run_id": persisted_run_id, "state": "observed_published_head"}
                    ),
                )
        elif fresh_root:
            snapshot = None
        else:
            if not resume:
                raise ArchiveConflictError("headless archive root requires explicit resume=True")
            if not recover_unpublished:
                raise ArchiveCorruptionError(
                    "archive has no published head; explicit recover_unpublished=True is required "
                    "only for a pre-publication crash"
                )
            if (resolved_root / _PUBLICATION_MARKER).exists():
                raise ArchiveCorruptionError(
                    "archive publication may have occurred; refusing headless recovery"
                )
            if persisted_run_id is None:
                raise ArchiveCorruptionError(
                    "headless archive has no persisted stable run_id; use a new root"
                )
            snapshot = None

        if persisted_run_id is None:
            raise ArchiveCorruptionError("archive has no stable run_id")
        _fsync_directory(resolved_root)
        return ArchiveWriter(resolved_root, spec, lock_fd, snapshot, persisted_run_id)
    except Exception:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)
        raise
