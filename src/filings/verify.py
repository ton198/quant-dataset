"""Small archive-integrity report for immutable filing snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .archive import read_snapshot
from .models import _thaw_json


@dataclass(frozen=True)
class ArchiveVerification:
    """Integrity-only summary; it does not assess financial completeness."""

    archive_path: str
    run_id: str
    snapshot_id: str
    manifest_version: int
    input_fingerprint: str
    integrity_status: str
    coverage_claim: str
    table_row_counts: dict[str, int]
    raw_object_count: int
    scope_provenance: dict[str, Any]
    coverage_provenance: dict[str, Any]


def verify_archive(archive: Path) -> ArchiveVerification:
    """Fully verify a published snapshot and summarize its listed objects."""
    snapshot = read_snapshot(archive)
    manifest = snapshot.manifest
    return ArchiveVerification(
        archive_path=str(snapshot.root),
        run_id=str(manifest["run_id"]),
        snapshot_id=snapshot.snapshot_id,
        manifest_version=snapshot.manifest_version,
        input_fingerprint=str(manifest["input_fingerprint"]),
        integrity_status="verified",
        coverage_claim="not_assessed",
        table_row_counts={name: table.num_rows for name, table in sorted(snapshot.tables.items())},
        raw_object_count=len(snapshot.raw_objects),
        scope_provenance=_thaw_json(manifest["scope_provenance"]),
        coverage_provenance=_thaw_json(manifest["coverage_provenance"]),
    )
