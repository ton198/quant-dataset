"""Safe, read-only materialization of archived filing entrypoints for parsers."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any
from urllib.parse import unquote

import pyarrow as pa

from .archive import ArchiveCorruptionError, read_snapshot
from .models import DOCUMENTS_SCHEMA, FILINGS_SCHEMA, Snapshot
from .models import filing_id as make_filing_id

_PACKAGE_MARKER = ".filing-package.json"
_PACKAGE_FORMAT = "filing-parser-workspace"
_PACKAGE_VERSION = 1
_PACKAGE_ROLES = frozenset({"primary", "xbrl_instance", "schema", "linkbase", "exhibit"})
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)


class PackagePreparationError(ValueError):
    """A selected archive filing cannot be safely materialized for parsing."""


class PackageConflictError(PackagePreparationError):
    """The requested workspace is foreign, modified, or conflicts with the package."""


@dataclass(frozen=True, slots=True)
class PackageEntry:
    """A copied filing document and its immutable archive identity."""

    local_path: Path
    document_id: str
    raw_sha256: str
    byte_size: int
    role: str
    original_filename: str


@dataclass(frozen=True, slots=True)
class PreparedPackage:
    """Parser entrypoints plus the scope/coverage state that constrains claims."""

    filing_id: str
    workspace_root: Path
    entrypoints: Mapping[str, PackageEntry]
    scope_status: str
    raw_coverage_status: str
    snapshot_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "entrypoints", MappingProxyType(dict(self.entrypoints)))


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _safe_filename(value: Any) -> str:
    if not isinstance(value, str) or not value or value in {".", ".."} or ".." in value:
        raise PackagePreparationError("archived document has an unsafe original filename")
    if value.startswith(("/", "\\")) or "/" in value or "\\" in value:
        raise PackagePreparationError("archived document filename is not a basename")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise PackagePreparationError("archived document filename contains control characters")
    if re.search(r"%(?![0-9a-fA-F]{2})", value):
        raise PackagePreparationError("archived document filename has malformed percent encoding")
    decoded = value
    for _ in range(5):
        next_value = unquote(decoded)
        if next_value == decoded:
            break
        decoded = next_value
    else:
        raise PackagePreparationError("archived filename has excessive nested encoding")
    if (
        ".." in decoded
        or "/" in decoded
        or "\\" in decoded
        or "\x00" in decoded
        or decoded.startswith(("/", "\\"))
        or any(ord(char) < 0x20 or ord(char) == 0x7F for char in decoded)
    ):
        raise PackagePreparationError("archived document filename contains encoded traversal")
    return value


def _validate_filing_id(value: str) -> str:
    if not isinstance(value, str) or value.count(":") != 1:
        raise PackagePreparationError("filing_id must be a canonical CIK/accession identity")
    cik10, accession = value.split(":", 1)
    try:
        return make_filing_id(cik10, accession)
    except ValueError as exc:
        raise PackagePreparationError(
            "filing_id must be a canonical CIK/accession identity"
        ) from exc


def _read_rows(snapshot: Snapshot) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    filings_table = snapshot.tables.get("filings")
    documents_table = snapshot.tables.get("documents")
    if not isinstance(filings_table, pa.Table) or not filings_table.schema.equals(
        FILINGS_SCHEMA, check_metadata=True
    ):
        raise PackagePreparationError("archive snapshot has no valid filings table")
    if not isinstance(documents_table, pa.Table) or not documents_table.schema.equals(
        DOCUMENTS_SCHEMA, check_metadata=True
    ):
        raise PackagePreparationError("archive snapshot has no valid documents table")
    return filings_table.to_pylist(), documents_table.to_pylist()


def _safe_source_path(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise ArchiveCorruptionError("document raw_path is not a normalized archive path")
    token = PurePosixPath(relative)
    if token.is_absolute() or any(part in {"", ".", ".."} for part in token.parts):
        raise ArchiveCorruptionError("document raw_path is not a normalized archive path")
    current = root
    for part in token.parts:
        current = current / part
        if current.is_symlink():
            raise ArchiveCorruptionError("document raw_path traverses a symlink")
    try:
        resolved = current.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise ArchiveCorruptionError("document raw_path is missing or outside the archive") from exc
    if not resolved.is_file():
        raise ArchiveCorruptionError("document raw_path is not a regular file")
    return resolved


def _workspace_path(path: Path, archive_root: Path) -> Path:
    try:
        raw_path = Path(path).expanduser()
    except (TypeError, OSError) as exc:
        raise PackagePreparationError("workspace_root must be an explicit filesystem path") from exc
    if not raw_path.is_absolute():
        raw_path = Path.cwd() / raw_path
    for component in (raw_path, *raw_path.parents):
        if component.is_symlink():
            raise PackagePreparationError("workspace path cannot traverse a symlink")
    parent = raw_path.parent
    try:
        resolved_parent = parent.resolve(strict=True)
    except (OSError, ValueError) as exc:
        raise PackagePreparationError("workspace parent must already exist") from exc
    if not resolved_parent.is_dir():
        raise PackagePreparationError("workspace parent must be a directory")
    destination = resolved_parent / raw_path.name
    archive = archive_root.resolve(strict=True)
    if destination == archive or destination in archive.parents or archive in destination.parents:
        raise PackagePreparationError("workspace_root must not overlap the archive root")
    return destination


def _entry_rows(
    filing_id_value: str,
    filings: list[dict[str, Any]],
    documents: list[dict[str, Any]],
    raw_refs: Mapping[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    matching_filings = [row for row in filings if row["filing_id"] == filing_id_value]
    if len(matching_filings) != 1:
        raise PackagePreparationError(
            "filing_id is not present exactly once in the active snapshot"
        )
    filing = matching_filings[0]
    if filing["scope_status"] == "excluded":
        raise PackagePreparationError("excluded filing has no parser package")

    selected = [
        row
        for row in documents
        if row["filing_id"] == filing_id_value
        and row["selection_status"] == "required"
        and row["role"] in _PACKAGE_ROLES
    ]
    if not selected:
        raise PackagePreparationError("filing has no selected parser documents")
    if not any(row["role"] == "primary" for row in selected):
        raise PackagePreparationError("filing has no selected primary document")
    if any(row["fetch_status"] != "present" for row in selected):
        raise PackagePreparationError(
            "all selected parser documents must be present before materialization"
        )

    for row in selected:
        _safe_filename(row["original_filename"])
        digest = row["raw_sha256"]
        relative = row["raw_path"]
        size = row["byte_size"]
        if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
            raise ArchiveCorruptionError("present document has an invalid raw SHA-256")
        if (
            not isinstance(relative, str)
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
        ):
            raise ArchiveCorruptionError("present document has incomplete raw object metadata")
        ref = raw_refs.get(relative)
        if ref is None or ref.sha256 != digest or ref.byte_size != size:
            raise ArchiveCorruptionError(
                "present document raw object is not active in the snapshot"
            )
    return filing, selected


def _copy_and_hash(source: Path, destination: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(destination, flags, 0o644)
    try:
        with source.open("rb") as src, os.fdopen(descriptor, "wb", closefd=False) as dst:
            while chunk := src.read(1024 * 1024):
                dst.write(chunk)
                digest.update(chunk)
                size += len(chunk)
            dst.flush()
            os.fsync(dst.fileno())
    finally:
        os.close(descriptor)
    return digest.hexdigest(), size


def _prepared_entries(rows: list[dict[str, Any]], workspace: Path) -> dict[str, PackageEntry]:
    return {
        row["source_url"]: PackageEntry(
            local_path=workspace / row["original_filename"],
            document_id=row["document_id"],
            raw_sha256=row["raw_sha256"],
            byte_size=row["byte_size"],
            role=row["role"],
            original_filename=row["original_filename"],
        )
        for row in rows
    }


def materialize_package(
    archive_root: Path,
    filing_id: str,
    *,
    workspace_root: Path,
) -> PreparedPackage:
    """Copy only verified selected filing bytes into a controlled parser workspace.

    Existing workspaces are reusable only when their ownership marker and every
    listed byte hash exactly match this package. Foreign contents are never replaced.
    Files are copied, not moved or hard-linked, and remain under their SEC basenames
    so relative XBRL references work without URI rewriting.
    """
    identity = _validate_filing_id(filing_id)
    snapshot = read_snapshot(Path(archive_root))
    filings, documents = _read_rows(snapshot)
    refs = {ref.path: ref for ref in snapshot.raw_objects}
    filing, selected = _entry_rows(identity, filings, documents, refs)

    by_filename: dict[str, dict[str, Any]] = {}
    filename_casefolds: dict[str, str] = {}
    for row in selected:
        filename = _safe_filename(row["original_filename"])
        folded = filename.casefold()
        prior_name = filename_casefolds.get(folded)
        if prior_name is not None and prior_name != filename:
            raise PackageConflictError("selected documents have case-colliding original filenames")
        filename_casefolds[folded] = filename
        prior = by_filename.get(filename)
        if prior is not None:
            if (
                prior["document_id"] != row["document_id"]
                or prior["raw_sha256"] != row["raw_sha256"]
                or prior["source_url"] != row["source_url"]
            ):
                raise PackageConflictError("selected documents conflict on one original filename")
            continue
        if filename.casefold() == _PACKAGE_MARKER.casefold():
            raise PackageConflictError(
                "source filename conflicts with the package ownership marker"
            )
        by_filename[filename] = row

    selected_rows = [by_filename[name] for name in sorted(by_filename)]
    expected_entries = [
        {
            "source_url": row["source_url"],
            "document_id": row["document_id"],
            "original_filename": row["original_filename"],
            "raw_sha256": row["raw_sha256"],
            "byte_size": row["byte_size"],
            "role": row["role"],
        }
        for row in selected_rows
    ]
    expected_marker = {
        "format": _PACKAGE_FORMAT,
        "format_version": _PACKAGE_VERSION,
        "filing_id": identity,
        "entries": expected_entries,
    }
    marker_bytes = _canonical_json(expected_marker)
    workspace = _workspace_path(workspace_root, Path(archive_root))

    if workspace.exists():
        if not workspace.is_dir() or workspace.is_symlink():
            raise PackageConflictError("workspace_root exists but is not a regular directory")
        marker_path = workspace / _PACKAGE_MARKER
        if marker_path.is_symlink():
            raise PackageConflictError("existing package ownership marker cannot be a symlink")
        try:
            actual_marker_bytes = marker_path.read_bytes()
            actual_marker = json.loads(actual_marker_bytes.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            raise PackageConflictError(
                "existing workspace has no valid package ownership marker"
            ) from None
        if actual_marker != expected_marker:
            raise PackageConflictError("existing workspace belongs to a different package identity")
        expected_names = {
            _PACKAGE_MARKER,
            *(entry["original_filename"] for entry in expected_entries),
        }
        actual_names = {child.name for child in workspace.iterdir()}
        if actual_names != expected_names:
            raise PackageConflictError(
                "existing package workspace contains foreign or missing files"
            )
        for entry in expected_entries:
            local = workspace / entry["original_filename"]
            if local.is_symlink() or not local.is_file():
                raise PackageConflictError("existing package entry is not a regular file")
            digest, size = _hash_path(local)
            if digest != entry["raw_sha256"] or size != entry["byte_size"]:
                raise PackageConflictError("existing package entry bytes differ from the archive")
        return PreparedPackage(
            filing_id=identity,
            workspace_root=workspace,
            entrypoints=_prepared_entries(selected_rows, workspace),
            scope_status=filing["scope_status"],
            raw_coverage_status=filing["raw_coverage_status"],
            snapshot_id=snapshot.snapshot_id,
        )

    try:
        workspace.mkdir(mode=0o700)
    except FileExistsError:
        raise PackageConflictError("workspace_root appeared during package creation") from None

    try:
        for row in selected_rows:
            filename = row["original_filename"]
            source = _safe_source_path(snapshot.root, row["raw_path"])
            destination = workspace / filename
            digest, size = _copy_and_hash(source, destination)
            if digest != row["raw_sha256"] or size != row["byte_size"]:
                raise ArchiveCorruptionError(
                    "copied package bytes differ from the committed raw object"
                )
        descriptor = os.open(
            workspace / _PACKAGE_MARKER,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as marker:
                marker.write(marker_bytes)
                marker.flush()
                os.fsync(marker.fileno())
        finally:
            os.close(descriptor)
        workspace_fd = os.open(workspace, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(workspace_fd)
        finally:
            os.close(workspace_fd)
    except Exception:
        shutil.rmtree(workspace, ignore_errors=True)
        raise

    return PreparedPackage(
        filing_id=identity,
        workspace_root=workspace,
        entrypoints=_prepared_entries(selected_rows, workspace),
        scope_status=filing["scope_status"],
        raw_coverage_status=filing["raw_coverage_status"],
        snapshot_id=snapshot.snapshot_id,
    )


def _hash_path(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size
