"""Bounded, local-cache-only SEC submissions catalog workflow."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import re
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import exchange_calendars
import pyarrow as pa

from .archive import open_archive, read_snapshot
from .catalog import APPROVED_FORMS, CatalogError, read_cached_catalog
from .models import DOCUMENTS_SCHEMA, FILINGS_SCHEMA, CatalogResult, RawObjectRef, RunSpec, Snapshot

_CIK_RE = re.compile(r"[0-9]{1,10}\Z", re.ASCII)
_CALENDAR_START = date(1990, 1, 1)
_PROTECTED_REPO_PATHS = (
    "data/raw",
    "data/organized",
    "data/output",
    "data/baselines",
)


class FilingWorkflowError(ValueError):
    """Raised when a bounded local filings catalog run cannot be completed."""


@dataclass(frozen=True)
class CatalogRunResult:
    """Outcome of a catalog-only run, including the verified archive snapshot."""

    snapshot: Snapshot
    requested_ciks: tuple[str, ...]
    forms: tuple[str, ...]
    start_date: date
    end_date: date
    filing_count: int
    verified_resource_count: int
    active_raw_object_count: int
    input_status: str
    diagnostics: tuple[str, ...]
    idempotent: bool


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _normalize_cik(value: str | int) -> str:
    if isinstance(value, bool):
        raise FilingWorkflowError("CIK values must contain 1 to 10 ASCII digits")
    if isinstance(value, int):
        value = str(value)
    if not isinstance(value, str):
        raise FilingWorkflowError("CIK values must contain 1 to 10 ASCII digits")
    token = value.strip()
    if token[:3].upper() == "CIK":
        token = token[3:]
    if _CIK_RE.fullmatch(token) is None:
        raise FilingWorkflowError("CIK values must contain 1 to 10 ASCII digits")
    return token.zfill(10)


def _calendar_sessions(start_date: date, end_date: date) -> tuple[tuple[date, ...], dict[str, Any]]:
    if (
        not isinstance(start_date, date)
        or isinstance(start_date, datetime)
        or not isinstance(end_date, date)
        or isinstance(end_date, datetime)
    ):
        raise FilingWorkflowError("start_date and end_date must be dates")
    if end_date < start_date:
        raise FilingWorkflowError("end_date must be on or after start_date")
    if start_date < _CALENDAR_START:
        raise FilingWorkflowError(
            f"XNYS calendar input coverage begins at {_CALENDAR_START.isoformat()}"
        )
    try:
        availability_horizon = end_date + timedelta(days=10)
    except OverflowError as exc:
        raise FilingWorkflowError(
            "end_date is too late to validate the 10-day XNYS horizon"
        ) from exc
    try:
        installed_calendar = exchange_calendars.get_calendar("XNYS")
        library_last = installed_calendar.last_session.date()
    except Exception as exc:
        raise FilingWorkflowError("could not load the installed XNYS calendar") from exc
    if availability_horizon > library_last:
        raise FilingWorkflowError(
            "XNYS calendar does not cover end_date plus the required 10-day horizon"
        )
    try:
        calendar = exchange_calendars.get_calendar(
            "XNYS",
            start=_CALENDAR_START.isoformat(),
            end=library_last.isoformat(),
        )
        sessions = tuple(session.date() for session in calendar.sessions)
        calendar_version = importlib.metadata.version("exchange-calendars")
    except Exception as exc:
        raise FilingWorkflowError("could not construct the installed XNYS session range") from exc
    if not sessions or sessions[0] < _CALENDAR_START or sessions[-1] < availability_horizon:
        raise FilingWorkflowError("installed XNYS calendar does not cover the requested range")
    if not any(end_date < session <= availability_horizon for session in sessions):
        raise FilingWorkflowError("XNYS calendar has no session after end_date within 10 days")
    session_digest = hashlib.sha256(
        "\n".join(day.isoformat() for day in sessions).encode()
    ).hexdigest()
    provenance = {
        "calendar": "XNYS",
        "provider": "exchange_calendars",
        "provider_version": calendar_version,
        "requested_start": _CALENDAR_START.isoformat(),
        "requested_end": library_last.isoformat(),
        "availability_horizon": availability_horizon.isoformat(),
        "session_count": len(sessions),
        "sessions_sha256": session_digest,
    }
    return sessions, provenance


def protected_archive_paths(cache_root: Path) -> tuple[Path, ...]:
    """Return known immutable inputs and outputs that archive roots may not overlap."""
    repo_root = Path(__file__).resolve().parents[2]
    paths = [repo_root / relative for relative in _PROTECTED_REPO_PATHS]
    paths.extend(
        (
            Path("/tmp/opencode/candidate-v3-financial-free"),
            Path(cache_root),
        )
    )
    return tuple(path.expanduser().resolve(strict=False) for path in paths)


def _normalized_forms(forms: Collection[str] | None) -> tuple[str, ...]:
    if isinstance(forms, (str, bytes)):
        raise FilingWorkflowError("forms must contain approved SEC form strings")
    try:
        selected = APPROVED_FORMS if forms is None else frozenset(forms)
    except TypeError as exc:
        raise FilingWorkflowError("forms must contain approved SEC form strings") from exc
    if any(not isinstance(form, str) for form in selected):
        raise FilingWorkflowError("forms must contain approved SEC form strings")
    if not selected <= APPROVED_FORMS:
        raise FilingWorkflowError(
            f"forms may only select approved forms: {sorted(selected - APPROVED_FORMS)}"
        )
    return tuple(sorted(selected))


def _catalog_input_fingerprint(
    *,
    per_cik: Sequence[tuple[str, CatalogResult]],
    forms: tuple[str, ...],
    start_date: date,
    end_date: date,
    allow_partial: bool,
    calendar_provenance: dict[str, Any],
) -> str:
    payload = {
        "workflow": "filings-catalog-v1",
        "requested_ciks": [cik for cik, _ in per_cik],
        "forms": list(forms),
        "filed_date_start_inclusive": start_date.isoformat(),
        "filed_date_end_inclusive": end_date.isoformat(),
        "allow_partial": allow_partial,
        "calendar_provenance": calendar_provenance,
        "catalog_inputs": [
            {
                "cik10": cik,
                "input_fingerprint": result.input_fingerprint,
                "input_status": result.input_status,
                "resources": [
                    {
                        "logical_key": resource.logical_key,
                        "sha256": resource.sha256,
                        "byte_size": resource.byte_size,
                    }
                    for resource in sorted(result.resources, key=lambda value: value.logical_key)
                ],
            }
            for cik, result in per_cik
        ],
    }
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


def _selected_rows(
    per_cik: Sequence[tuple[str, CatalogResult]],
    *,
    forms: tuple[str, ...],
    start_date: date,
    end_date: date,
    calendar_start: date,
) -> list[dict[str, Any]]:
    selected_forms = set(forms)
    rows: list[dict[str, Any]] = []
    for _cik, result in per_cik:
        for row in result.filings:
            filed_date = row["filed_date"]
            if filed_date < calendar_start:
                raise FilingWorkflowError(
                    f"cached filing {row['filing_id']} predates the supported XNYS calendar range"
                )
            if start_date <= filed_date <= end_date and row["form"] in selected_forms:
                rows.append(dict(row))
    rows.sort(key=lambda row: (row["cik10"], row["filed_date"], row["accession_number"]))
    return rows


def _same_table(left: pa.Table | None, right: pa.Table) -> bool:
    return (
        left is not None
        and left.schema.equals(right.schema, check_metadata=True)
        and left.equals(right, check_metadata=True)
    )


def run_catalog(
    *,
    archive: Path,
    cache_root: Path,
    ciks: Sequence[str | int],
    start_date: date,
    end_date: date,
    forms: Collection[str] | None = None,
    resume: bool = False,
    allow_partial: bool = False,
) -> CatalogRunResult:
    """Catalog selected filings from local cached SEC JSON and commit the core ledger.

    Filing-date selection is inclusive. Every exact-CIK-owned main submission
    resource and all historical pages it references are verified before the
    requested forms and date bounds are applied. No network requests are made.
    """
    if isinstance(ciks, (str, bytes)) or not ciks:
        raise FilingWorkflowError("at least one --cik is required as a sequence of CIK values")
    if not isinstance(start_date, date) or isinstance(start_date, datetime):
        raise FilingWorkflowError("start_date must be a date, not a datetime")
    if not isinstance(end_date, date) or isinstance(end_date, datetime):
        raise FilingWorkflowError("end_date must be a date, not a datetime")
    if end_date < start_date:
        raise FilingWorkflowError("end_date must be on or after start_date")
    if not isinstance(allow_partial, bool) or not isinstance(resume, bool):
        raise FilingWorkflowError("resume and allow_partial must be boolean values")
    selected_ciks = tuple(sorted({_normalize_cik(value) for value in ciks}))
    selected_forms = _normalized_forms(forms)
    try:
        resolved_cache = Path(cache_root).expanduser().resolve(strict=True)
    except (OSError, TypeError) as exc:
        raise FilingWorkflowError("cache_root must be an existing directory") from exc
    if not resolved_cache.is_dir():
        raise FilingWorkflowError("cache_root must be an existing directory")
    sessions, calendar_provenance = _calendar_sessions(start_date, end_date)

    catalog_results: list[tuple[str, CatalogResult]] = []
    for cik in selected_ciks:
        try:
            result = read_cached_catalog(
                resolved_cache,
                cik,
                xnys_sessions=sessions,
                forms=selected_forms,
                allow_partial=allow_partial,
            )
        except CatalogError as exc:
            raise FilingWorkflowError(f"cached catalog failed for CIK {cik}: {exc}") from exc
        catalog_results.append((cik, result))

    selected_rows = _selected_rows(
        catalog_results,
        forms=selected_forms,
        start_date=start_date,
        end_date=end_date,
        calendar_start=_CALENDAR_START,
    )
    input_status = (
        "partial"
        if any(result.input_status == "partial" for _, result in catalog_results)
        else "complete"
    )
    diagnostics = tuple(
        f"{cik}:{diagnostic}"
        for cik, result in catalog_results
        for diagnostic in result.diagnostics
    )
    status_label = "partial_catalog_only" if input_status == "partial" else "catalog_only"
    input_fingerprint = _catalog_input_fingerprint(
        per_cik=catalog_results,
        forms=selected_forms,
        start_date=start_date,
        end_date=end_date,
        allow_partial=allow_partial,
        calendar_provenance=calendar_provenance,
    )
    spec = RunSpec(
        approved_forms=selected_forms,
        policy={
            "workflow": "filings-catalog-v1",
            "requested_ciks": list(selected_ciks),
            "forms": list(selected_forms),
            "filed_date_start_inclusive": start_date.isoformat(),
            "filed_date_end_inclusive": end_date.isoformat(),
            "allow_partial": allow_partial,
        },
        input_fingerprint=input_fingerprint,
        calendar_provenance=calendar_provenance,
        coverage_provenance={
            "status": status_label,
            "input_status": input_status,
            "diagnostics": list(diagnostics),
            "claim": "catalog metadata only; no filing-document inventory or bytes verified",
        },
        scope_provenance={
            "requested_ciks": list(selected_ciks),
            "forms": list(selected_forms),
            "filed_date_start_inclusive": start_date.isoformat(),
            "filed_date_end_inclusive": end_date.isoformat(),
            "filing_count": len(selected_rows),
            "empty_selection": not selected_rows,
        },
    )

    protected_paths = protected_archive_paths(resolved_cache)
    resource_refs: dict[tuple[str, str], RawObjectRef] = {}
    unique_refs: dict[str, RawObjectRef] = {}
    filing_table: pa.Table
    documents_table = pa.Table.from_pylist([], schema=DOCUMENTS_SCHEMA)

    with open_archive(
        archive,
        spec,
        protected_paths=protected_paths,
        resume=resume,
    ) as writer:
        current_snapshot = (
            read_snapshot(writer.root) if (writer.root / "manifest.json").is_file() else None
        )
        for _cik, result in catalog_results:
            for resource in result.resources:
                ref = writer.put_raw_file(resource.source_path, expected_sha256=resource.sha256)
                key = (resource.logical_key, resource.sha256)
                resource_refs[key] = ref
                unique_refs[ref.path] = ref
        for row in selected_rows:
            key = (row["source_submission_logical_key"], row["source_submission_sha256"])
            ref = resource_refs.get(key)
            if ref is None:
                raise FilingWorkflowError(
                    f"no copied archive resource for filing {row['filing_id']} provenance"
                )
            row["source_submission_path"] = ref.path
        filing_table = pa.Table.from_pylist(selected_rows, schema=FILINGS_SCHEMA)
        replacement_tables = {"filings": filing_table, "documents": documents_table}
        expected_refs = tuple(sorted(unique_refs.values(), key=lambda ref: ref.path))

        if current_snapshot is not None:
            current_raw = {ref.path: ref for ref in current_snapshot.raw_objects}
            same_active_inputs = all(current_raw.get(ref.path) == ref for ref in expected_refs)
            same_core_tables = _same_table(
                current_snapshot.tables.get("filings"), filing_table
            ) and _same_table(current_snapshot.tables.get("documents"), documents_table)
            if same_active_inputs and same_core_tables:
                return CatalogRunResult(
                    snapshot=current_snapshot,
                    requested_ciks=selected_ciks,
                    forms=selected_forms,
                    start_date=start_date,
                    end_date=end_date,
                    filing_count=filing_table.num_rows,
                    verified_resource_count=sum(
                        len(result.resources) for _, result in catalog_results
                    ),
                    active_raw_object_count=len(current_snapshot.raw_objects),
                    input_status=input_status,
                    diagnostics=diagnostics,
                    idempotent=True,
                )

        snapshot = writer.commit(
            tables=replacement_tables,
            raw_objects=expected_refs,
            expected_manifest_version=current_snapshot.manifest_version if current_snapshot else 0,
        )
    return CatalogRunResult(
        snapshot=snapshot,
        requested_ciks=selected_ciks,
        forms=selected_forms,
        start_date=start_date,
        end_date=end_date,
        filing_count=filing_table.num_rows,
        verified_resource_count=sum(len(result.resources) for _, result in catalog_results),
        active_raw_object_count=len(snapshot.raw_objects),
        input_status=input_status,
        diagnostics=diagnostics,
        idempotent=False,
    )
