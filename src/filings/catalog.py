"""Strict readers for cached SEC submissions resources.

Only resources named by exact submissions logical keys are opened.  The module
never fetches from SEC and never inspects Company Facts resources.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Collection, Mapping, Sequence
from datetime import date, datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from .models import CatalogResource, CatalogResult, filing_id

APPROVED_FORMS = frozenset(
    {
        "10-K",
        "10-K/A",
        "10-Q",
        "10-Q/A",
        "20-F",
        "20-F/A",
        "40-F",
        "40-F/A",
        "6-K",
    }
)
_DEFAULT_FORMS = APPROVED_FORMS
_SHA256_RE = re.compile(r"[0-9a-fA-F]{64}\Z", re.ASCII)
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z", re.ASCII)
_PAGE_NAME_RE = re.compile(r"CIK([0-9]{10})-submissions-([0-9]+)\.json\Z", re.ASCII)
_CACHE_PREFIX = PurePosixPath("data/raw/sec/financials")


class CatalogError(ValueError):
    """Raised when cached submission inputs are missing or malformed."""


class CatalogConflictError(CatalogError):
    """Raised when duplicate accession records contain conflicting known values."""


def _normalize_cik(value: str | int, *, label: str = "CIK") -> str:
    if isinstance(value, bool):
        raise CatalogError(f"{label} must be a numeric CIK")
    if isinstance(value, int):
        value = str(value)
    if not isinstance(value, str):
        raise CatalogError(f"{label} must be a numeric CIK")
    token = value.strip()
    if token[:3].upper() == "CIK":
        token = token[3:]
    if not token or not token.isascii() or not token.isdigit() or len(token) > 10:
        raise CatalogError(f"{label} must contain 1 to 10 ASCII digits")
    return token.zfill(10)


def _parallel_array_rows(
    columns: Mapping[str, Any], *, label: str
) -> tuple[Mapping[str, Any], ...]:
    for required in ("accessionNumber", "filingDate"):
        if required not in columns or not isinstance(columns[required], list):
            raise CatalogError(f"{label} must contain a {required} array")

    arrays: dict[str, list[Any]] = {}
    for key, value in columns.items():
        if isinstance(value, list):
            arrays[str(key)] = value
        elif key in ("accessionNumber", "filingDate"):
            raise CatalogError(f"{label}.{key} must be an array")

    lengths = {len(values) for values in arrays.values()}
    if len(lengths) > 1:
        raise CatalogError(f"{label} contains ragged parallel arrays")
    row_count = next(iter(lengths), 0)
    return tuple(
        {key: values[index] for key, values in arrays.items()} for index in range(row_count)
    )


def _table_rows(fields: Any, data: Any, *, label: str) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(fields, list) or not isinstance(data, list):
        raise CatalogError(f"{label}.fields and {label}.data must both be arrays")
    if any(not isinstance(field, str) or not field for field in fields):
        raise CatalogError(f"{label}.fields must contain non-empty strings")
    if len(set(fields)) != len(fields):
        raise CatalogError(f"{label}.fields contains duplicate field names")
    result: list[Mapping[str, Any]] = []
    for index, row in enumerate(data):
        if not isinstance(row, list) or len(row) != len(fields):
            raise CatalogError(f"{label}.data[{index}] does not match the fields width")
        result.append(dict(zip(fields, row, strict=True)))
    return tuple(result)


def submission_rows(
    payload: Mapping[str, Any], *, kind: Literal["submissions", "submissions-page"]
) -> tuple[Mapping[str, Any], ...]:
    """Expand a main or historical SEC submissions JSON payload into filing rows.

    Main submissions use ``filings.recent`` parallel arrays. Historical payloads
    use parallel top-level arrays; the explicit fields/data table form is also
    supported. Ragged arrays and malformed table widths are never truncated.
    """
    if kind not in ("submissions", "submissions-page"):
        raise CatalogError(f"unsupported submissions payload kind: {kind!r}")
    if not isinstance(payload, Mapping):
        raise CatalogError("submissions payload must be an object")

    if kind == "submissions":
        filings = payload.get("filings")
        if not isinstance(filings, Mapping):
            raise CatalogError("main submissions payload must contain filings")
        recent = filings.get("recent")
        if not isinstance(recent, Mapping):
            raise CatalogError("main submissions payload must contain filings.recent")
        return _parallel_array_rows(recent, label="filings.recent")

    has_fields = "fields" in payload
    has_data = "data" in payload
    if has_fields or has_data:
        if not (has_fields and has_data):
            raise CatalogError("historical submissions table must contain fields and data")
        return _table_rows(payload.get("fields"), payload.get("data"), label="submissions-page")

    if "filings" in payload:
        filings = payload.get("filings")
        recent = filings.get("recent") if isinstance(filings, Mapping) else None
        if not isinstance(recent, Mapping):
            raise CatalogError("historical submissions payload has an invalid filings.recent block")
        return _parallel_array_rows(recent, label="filings.recent")

    return _parallel_array_rows(payload, label="submissions-page")


def _manifest_records(resources: Any) -> dict[str, list[Any]]:
    """Flatten either supported manifest resource layout, preserving record order."""
    grouped: dict[str, list[Any]] = {}
    if isinstance(resources, Mapping):
        entries = resources.items()
        for logical_key, versions in entries:
            if not isinstance(logical_key, str):
                continue
            version_list = versions if isinstance(versions, list) else [versions]
            grouped.setdefault(logical_key, []).extend(version_list)
        return grouped
    if isinstance(resources, list):
        for entry in resources:
            if not isinstance(entry, Mapping):
                continue
            logical_key = entry.get("logical_key")
            if isinstance(logical_key, str):
                grouped.setdefault(logical_key, []).append(dict(entry))
        return grouped
    raise CatalogError("manifest resources must be an object or array")


def _parse_owned_key(logical_key: str) -> tuple[str, str | None] | None:
    """Parse exact tokenized submissions keys; never use substring ownership."""
    parts = logical_key.split(":")
    if len(parts) == 2 and parts[0] == "submissions":
        try:
            return _normalize_cik(parts[1], label="manifest owner"), None
        except CatalogError:
            return None
    if len(parts) == 3 and parts[0] == "submissions-page" and parts[2]:
        try:
            return _normalize_cik(parts[1], label="manifest owner"), parts[2]
        except CatalogError:
            return None
    return None


def _safe_cache_path(cache_root: Path, manifest_path: Any) -> Path:
    if not isinstance(manifest_path, str) or not manifest_path:
        raise CatalogError("manifest resource path must be a non-empty relative path")
    if "\\" in manifest_path:
        raise CatalogError("manifest resource path must use POSIX separators")
    relative = PurePosixPath(manifest_path)
    if relative.is_absolute() or any(part in ("", ".", "..") for part in relative.parts):
        raise CatalogError("manifest resource path must be a normalized relative path")
    parts = relative.parts
    if parts[: len(_CACHE_PREFIX.parts)] == _CACHE_PREFIX.parts:
        parts = parts[len(_CACHE_PREFIX.parts) :]
        if not parts:
            raise CatalogError("manifest resource path points at the cache directory")
    candidate = cache_root.joinpath(*parts)
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(cache_root)
    except (OSError, ValueError) as exc:
        raise CatalogError("manifest resource path is missing or escapes cache_root") from exc
    if not resolved.is_file():
        raise CatalogError("manifest resource path is not a regular file")
    return resolved


def _verified_resource(
    cache_root: Path, logical_key: str, record: Any
) -> tuple[CatalogResource, Mapping[str, Any]]:
    if not isinstance(record, Mapping):
        raise CatalogError(f"latest manifest record for {logical_key} is malformed")
    embedded_logical_key = record.get("logical_key")
    if "logical_key" in record and embedded_logical_key != logical_key:
        raise CatalogError(
            f"latest manifest record logical_key does not match manifest key {logical_key}"
        )
    status = record.get("status")
    if status is not None and status != "done":
        raise CatalogError(f"latest manifest record for {logical_key} is not complete")
    path_value = record.get("path")
    sha_value = record.get("sha256")
    byte_size = record.get("byte_size")
    source_url = record.get("url")
    if not isinstance(sha_value, str) or _SHA256_RE.fullmatch(sha_value) is None:
        raise CatalogError(f"latest manifest record for {logical_key} has an invalid SHA-256")
    if isinstance(byte_size, bool) or not isinstance(byte_size, int) or byte_size < 0:
        raise CatalogError(f"latest manifest record for {logical_key} has an invalid byte_size")
    if not isinstance(source_url, str) or not source_url:
        raise CatalogError(f"latest manifest record for {logical_key} has no source URL")
    source_path = _safe_cache_path(cache_root, path_value)
    try:
        content = source_path.read_bytes()
    except OSError as exc:
        raise CatalogError(f"cannot read latest resource for {logical_key}") from exc
    digest = hashlib.sha256(content).hexdigest()
    if len(content) != byte_size or digest != sha_value.lower():
        raise CatalogError(f"latest resource bytes do not match manifest for {logical_key}")
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CatalogError(f"latest resource for {logical_key} is not valid UTF-8 JSON") from exc
    if not isinstance(payload, Mapping):
        raise CatalogError(f"latest resource for {logical_key} must contain a JSON object")
    return (
        CatalogResource(
            logical_key=logical_key,
            sha256=digest,
            byte_size=byte_size,
            source_url=source_url,
            path=path_value,
            source_path=source_path,
        ),
        payload,
    )


def _parse_iso_date(value: Any, *, field: str, required: bool) -> date | None:
    if value is None or value == "":
        if required:
            raise CatalogError(f"filing row is missing {field}")
        return None
    if not isinstance(value, str) or _DATE_RE.fullmatch(value) is None:
        raise CatalogError(f"filing row has invalid {field}")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise CatalogError(f"filing row has invalid {field}") from exc


def _optional_text(value: Any, *, field: str) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise CatalogError(f"filing row has invalid {field}")
    return value


def _acceptance_datetime(value: Any) -> tuple[str | None, datetime | None]:
    raw = _optional_text(value, field="acceptanceDateTime")
    if raw is None:
        return None, None
    candidate = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return raw, None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return raw, None
    return raw, parsed.astimezone(timezone.utc)


def _ordered_sessions(sessions: Sequence[date]) -> tuple[date, ...]:
    result: list[date] = []
    for session in sessions:
        if isinstance(session, datetime) or not isinstance(session, date):
            raise CatalogError("xnys_sessions must contain date values, not datetimes")
        if result and session <= result[-1]:
            raise CatalogError("xnys_sessions must be strictly increasing")
        result.append(session)
    return tuple(result)


def _visible_session(filed_date: date, sessions: tuple[date, ...]) -> date:
    if not sessions or sessions[0] > filed_date:
        raise CatalogError(
            f"XNYS calendar does not contain a session on or before {filed_date.isoformat()}"
        )
    # Use an explicit upper-bound search: the availability date is strictly later.
    low, high = 0, len(sessions)
    while low < high:
        middle = (low + high) // 2
        if sessions[middle] <= filed_date:
            low = middle + 1
        else:
            high = middle
    if low == len(sessions):
        raise CatalogError(
            f"XNYS calendar does not contain a session strictly after {filed_date.isoformat()}"
        )
    return sessions[low]


def _missing(value: Any) -> bool:
    return value is None or value == ""


def _coalesce_rows(
    old: Mapping[str, Any], new: Mapping[str, Any], accession: str
) -> dict[str, Any]:
    merged = dict(old)
    for key, value in new.items():
        existing = merged.get(key)
        if _missing(existing):
            if not _missing(value):
                merged[key] = value
        elif not _missing(value) and existing != value:
            raise CatalogConflictError(
                f"conflicting submissions values for accession {accession}: {key}"
            )
    return merged


def _manifest_json(cache_root: Path) -> Mapping[str, Any]:
    manifest_path = cache_root / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CatalogError("cache_root must contain a readable manifest.json") from exc
    if not isinstance(manifest, Mapping):
        raise CatalogError("manifest.json must contain an object")
    if "resources" not in manifest:
        raise CatalogError("manifest.json is missing resources")
    return manifest


def read_cached_catalog(
    cache_root: Path,
    cik: str | int,
    *,
    xnys_sessions: Sequence[date],
    forms: Collection[str] | None = None,
    allow_partial: bool = False,
) -> CatalogResult:
    """Read an issuer's verified main and referenced historical submissions cache.

    ``cache_root`` is the directory containing ``manifest.json``.  Only the
    exact issuer-owned submissions keys and pages referenced by its main payload
    are read.  A missing referenced historical page may be tolerated explicitly
    with ``allow_partial=True``; corrupt latest versions always fail closed.
    """
    issuer_cik = _normalize_cik(cik, label="cik")
    if not isinstance(allow_partial, bool):
        raise CatalogError("allow_partial must be a bool")
    if forms is None:
        selected_forms = _DEFAULT_FORMS
    else:
        if isinstance(forms, (str, bytes)):
            raise CatalogError("forms must be a collection of form strings")
        selected_forms = frozenset(forms)
        if any(not isinstance(form, str) for form in selected_forms):
            raise CatalogError("forms must contain only strings")
        if not selected_forms <= APPROVED_FORMS:
            invalid = sorted(selected_forms - APPROVED_FORMS)
            raise CatalogError(f"forms override may only select approved forms: {invalid}")
    sessions = _ordered_sessions(xnys_sessions)

    try:
        resolved_root = Path(cache_root).resolve(strict=True)
    except (OSError, TypeError) as exc:
        raise CatalogError("cache_root must be an existing directory") from exc
    if not resolved_root.is_dir():
        raise CatalogError("cache_root must be an existing directory")
    manifest = _manifest_json(resolved_root)
    grouped = _manifest_records(manifest["resources"])

    main_keys = [
        key
        for key in grouped
        if (parsed := _parse_owned_key(key)) is not None
        and parsed[0] == issuer_cik
        and parsed[1] is None
    ]
    if len(main_keys) != 1:
        if not main_keys:
            raise CatalogError(f"no issuer-owned submissions resource for CIK {issuer_cik}")
        raise CatalogError(f"multiple submissions logical keys normalize to CIK {issuer_cik}")
    main_key = main_keys[0]
    main_versions = grouped[main_key]
    if not main_versions:
        raise CatalogError(f"no manifest versions for {main_key}")
    main_resource, main_payload = _verified_resource(resolved_root, main_key, main_versions[-1])
    expected_main_url = f"https://data.sec.gov/submissions/CIK{issuer_cik}.json"
    if main_resource.source_url != expected_main_url:
        raise CatalogError("main submissions resource has an unexpected SEC URL")
    payload_cik = main_payload.get("cik")
    if (
        payload_cik is None
        or _normalize_cik(payload_cik, label="main submissions cik") != issuer_cik
    ):
        raise CatalogError("main submissions payload CIK does not match requested CIK")

    filings_block = main_payload.get("filings")
    if not isinstance(filings_block, Mapping):
        raise CatalogError("main submissions payload must contain filings")
    page_references = filings_block.get("files")
    if not isinstance(page_references, list):
        raise CatalogError("main submissions payload must contain a filings.files array")

    page_names: list[str] = []
    for index, reference in enumerate(page_references):
        if not isinstance(reference, Mapping) or not isinstance(reference.get("name"), str):
            raise CatalogError(f"main submissions filings.files[{index}] is malformed")
        name = reference["name"]
        match = _PAGE_NAME_RE.fullmatch(name)
        if match is None or match.group(1) != issuer_cik:
            raise CatalogError(f"historical submissions filename is not owned by CIK {issuer_cik}")
        if name not in page_names:
            page_names.append(name)

    resources: list[CatalogResource] = [main_resource]
    payloads: list[tuple[str, CatalogResource, Mapping[str, Any]]] = [
        ("submissions", main_resource, main_payload)
    ]
    diagnostics: list[str] = []
    for page_name in page_names:
        page_keys = [
            key
            for key in grouped
            if (parsed := _parse_owned_key(key)) is not None and parsed == (issuer_cik, page_name)
        ]
        if not page_keys:
            if allow_partial:
                diagnostics.append(f"missing_historical_page:{page_name}")
                continue
            raise CatalogError(f"main submissions reference missing historical page {page_name}")
        if len(page_keys) != 1:
            raise CatalogError(f"multiple manifest keys normalize to historical page {page_name}")
        page_key = page_keys[0]
        versions = grouped[page_key]
        if not versions:
            raise CatalogError(f"no manifest versions for {page_key}")
        page_resource, page_payload = _verified_resource(resolved_root, page_key, versions[-1])
        expected_url = f"https://data.sec.gov/submissions/{page_name}"
        if page_resource.source_url != expected_url:
            raise CatalogError(f"historical page {page_name} has an unexpected SEC URL")
        page_cik = page_payload.get("cik")
        if (
            page_cik not in (None, "")
            and _normalize_cik(page_cik, label="historical submissions cik") != issuer_cik
        ):
            raise CatalogError(f"historical page {page_name} CIK does not match requested CIK")
        resources.append(page_resource)
        payloads.append(("submissions-page", page_resource, page_payload))

    gathered: dict[str, dict[str, Any]] = {}
    provenance: dict[str, tuple[int, CatalogResource, str]] = {}
    for source_rank, (kind, resource, payload) in enumerate(payloads):
        expanded = submission_rows(payload, kind=kind)  # type: ignore[arg-type]
        locator_prefix = "filings.recent" if kind == "submissions" else "rows"
        for index, source_row in enumerate(expanded):
            accession = source_row.get("accessionNumber")
            if not isinstance(accession, str):
                raise CatalogError(f"{locator_prefix}[{index}] is missing accessionNumber")
            # Validate the accession while retaining its literal agent prefix.
            filing_id(issuer_cik, accession)
            if accession in gathered:
                gathered[accession] = _coalesce_rows(gathered[accession], source_row, accession)
                previous = provenance[accession]
                if source_rank < previous[0]:
                    provenance[accession] = (
                        source_rank,
                        resource,
                        f"{locator_prefix}[{index}]",
                    )
            else:
                gathered[accession] = dict(source_row)
                provenance[accession] = (source_rank, resource, f"{locator_prefix}[{index}]")

    filing_rows: list[dict[str, Any]] = []
    for accession, source_row in gathered.items():
        form = source_row.get("form")
        if not isinstance(form, str) or not form:
            raise CatalogError(f"filing row {accession} is missing form")
        if form not in selected_forms:
            continue
        filed_date = _parse_iso_date(
            source_row.get("filingDate"), field="filingDate", required=True
        )
        assert filed_date is not None
        report_period_end = _parse_iso_date(
            source_row.get("reportDate"), field="reportDate", required=False
        )
        acceptance_raw, acceptance_utc = _acceptance_datetime(source_row.get("acceptanceDateTime"))
        primary_name = _optional_text(source_row.get("primaryDocument"), field="primaryDocument")
        _, resource, locator = provenance[accession]
        is_amendment = form.endswith("/A")
        filing_rows.append(
            {
                "filing_id": filing_id(issuer_cik, accession),
                "cik10": issuer_cik,
                "accession_number": accession,
                "form": form,
                "filed_date": filed_date,
                "report_period_end": report_period_end,
                "acceptance_datetime_raw": acceptance_raw,
                "acceptance_datetime_utc": acceptance_utc,
                "effective_visible_session": _visible_session(filed_date, sessions),
                "is_amendment": is_amendment,
                "parent_filing_id": None,
                "parent_link_source": None,
                "primary_document_name": primary_name,
                "scope_status": "candidate" if form == "6-K" else "included",
                "scope_evidence_json": None,
                "inventory_status": "not_inspected",
                "raw_coverage_status": "not_attempted",
                "source_submission_logical_key": resource.logical_key,
                "source_submission_sha256": resource.sha256,
                "source_submission_path": resource.path,
                "source_submission_locator": locator,
            }
        )

    filing_rows.sort(key=lambda row: (row["filed_date"], row["accession_number"]))
    resources_sorted = tuple(resources)
    input_status = "partial" if diagnostics else "complete"
    fingerprint_payload = {
        "cik10": issuer_cik,
        "forms": sorted(selected_forms),
        "resources": [
            {"logical_key": item.logical_key, "sha256": item.sha256, "byte_size": item.byte_size}
            for item in sorted(resources_sorted, key=lambda item: item.logical_key)
        ],
        "input_status": input_status,
        "diagnostics": list(diagnostics),
        "xnys_sessions": [session.isoformat() for session in sessions],
    }
    fingerprint_bytes = json.dumps(
        fingerprint_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    input_fingerprint = hashlib.sha256(fingerprint_bytes).hexdigest()
    return CatalogResult(
        filings=tuple(filing_rows),
        resources=resources_sorted,
        diagnostics=tuple(diagnostics),
        input_status=input_status,
        input_fingerprint=input_fingerprint,
    )
