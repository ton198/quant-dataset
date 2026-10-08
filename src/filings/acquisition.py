"""Bounded resumable acquisition into an explicitly initialized filing archive."""

from __future__ import annotations

import hashlib
import json
import re
import urllib.parse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pyarrow as pa

from .archive import (
    ArchiveConflictError,
    ArchiveCorruptionError,
    ArchiveError,
    _restore_spec,
    open_archive,
    read_snapshot,
)
from .document_selection import (
    DocumentCandidate,
    DocumentPlan,
    FilingInventoryError,
    enumerate_documents,
    refine_document_plan,
)
from .download import fetch_document
from .models import (
    DOCUMENTS_SCHEMA,
    FILINGS_SCHEMA,
    RawObjectRef,
    RunSpec,
    Snapshot,
)
from .models import (
    document_id as make_document_id,
)
from .sec_client import (
    FetchResponse,
    SecClient,
    SecClientError,
    SecForbiddenError,
    SecNotFoundError,
)


class AcquisitionError(ValueError):
    """The existing archive cannot safely accept this acquisition request."""


class AcquisitionConflictError(AcquisitionError):
    """Existing acquisition rows conflict with the immutable input/selection identity."""


@dataclass(frozen=True, slots=True)
class AcquisitionResult:
    """Published snapshot and bounded work summary from one acquisition call."""

    snapshot: Snapshot
    processed_filing_ids: tuple[str, ...]
    skipped_completed_filing_ids: tuple[str, ...]
    selection_statuses: Mapping[str, str]
    acquired_document_count: int
    unavailable_document_count: int
    error_document_count: int
    blocked_filing_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "selection_statuses", MappingProxyType(dict(self.selection_statuses))
        )


_APPROVED_SCOPE = frozenset({"included", "candidate"})
_VALID_SELECTION = frozenset({"required", "candidate", "out_of_scope"})
_VALID_PLAN_STATUSES = frozenset({"selected_financial", "needs_review", "non_financial"})
_VALID_ROLES = frozenset(
    {"filing_index", "primary", "xbrl_instance", "schema", "linkbase", "exhibit", "other"}
)
_MAX_FILINGS = 50
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)


def _canonical_json_text(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise AcquisitionError("acquisition provenance must contain canonical JSON values") from exc


def load_archive_runspec(archive_root: Path) -> RunSpec:
    """Compatibility wrapper for the archive-owned RunSpec reader."""
    return _restore_spec(read_snapshot(Path(archive_root)))


def _validate_protected_paths(protected_paths: Sequence[Path]) -> tuple[Path, ...]:
    if isinstance(protected_paths, (str, bytes)) or not isinstance(protected_paths, Sequence):
        raise AcquisitionError("protected_paths must be a non-empty explicit sequence of paths")
    if not protected_paths:
        raise AcquisitionError("at least one caller-known protected path is required")
    normalized: list[Path] = []
    for value in protected_paths:
        if not isinstance(value, (str, Path)) or not str(value):
            raise AcquisitionError("protected_paths must contain non-empty filesystem paths")
        normalized.append(Path(value).expanduser())
    return tuple(normalized)


def _validate_request(
    filing_ids: Sequence[str] | None,
    max_filings: int,
    protected_paths: Sequence[Path],
) -> tuple[tuple[str, ...] | None, tuple[Path, ...]]:
    if (
        isinstance(max_filings, bool)
        or not isinstance(max_filings, int)
        or not 1 <= max_filings <= _MAX_FILINGS
    ):
        raise ValueError(f"max_filings must be an integer from 1 to {_MAX_FILINGS}")
    if filing_ids is None:
        requested_ids = None
    else:
        if isinstance(filing_ids, (str, bytes)) or not isinstance(filing_ids, Sequence):
            raise ValueError("filing_ids must be a sequence of explicit filing identities")
        requested_ids = tuple(filing_ids)
        if any(not isinstance(identity, str) or not identity for identity in requested_ids):
            raise ValueError("filing_ids must contain non-empty strings")
        if len(set(requested_ids)) != len(requested_ids):
            raise ValueError("filing_ids must not contain duplicates")
        if len(requested_ids) > max_filings:
            raise ValueError(
                "explicit filing_ids exceed max_filings; no identifiers were truncated"
            )
    return requested_ids, _validate_protected_paths(protected_paths)


def _core_tables(snapshot: Snapshot) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    filings_table = snapshot.tables.get("filings")
    if not isinstance(filings_table, pa.Table) or not filings_table.schema.equals(
        FILINGS_SCHEMA, check_metadata=True
    ):
        raise AcquisitionError("existing archive must have a committed filings table")
    documents_table = snapshot.tables.get("documents")
    if documents_table is None:
        documents = pa.Table.from_pylist([], schema=DOCUMENTS_SCHEMA)
    elif isinstance(documents_table, pa.Table) and documents_table.schema.equals(
        DOCUMENTS_SCHEMA, check_metadata=True
    ):
        documents = documents_table
    else:
        raise ArchiveCorruptionError("existing archive documents table does not match core schema")
    return filings_table.to_pylist(), documents.to_pylist()


def _group_documents(rows: Sequence[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        # Keep row identity: resume fetch updates must flow into the full replacement table.
        grouped.setdefault(str(row["filing_id"]), []).append(row)
    return grouped


def _is_complete(row: Mapping[str, Any], docs: Sequence[Mapping[str, Any]]) -> bool:
    if (
        row.get("inventory_status") != "known"
        or row.get("scope_status") != "included"
        or row.get("raw_coverage_status") != "scoped_complete"
        or not docs
        or _plan_status_from_rows(docs) != "selected_financial"
    ):
        return False
    indexes = [doc for doc in docs if doc.get("role") == "filing_index"]
    if not indexes or any(doc.get("fetch_status") != "present" for doc in indexes):
        return False
    primary = [doc for doc in docs if doc.get("role") == "primary"]
    primary_name = row.get("primary_document_name")
    if (
        len(primary) != 1
        or not isinstance(primary_name, str)
        or not primary_name
        or primary[0].get("original_filename") != primary_name
        or primary[0].get("selection_status") != "required"
        or primary[0].get("fetch_status") != "present"
    ):
        return False
    return _all_required_present(docs)


def _needs_work(row: Mapping[str, Any], docs: Sequence[Mapping[str, Any]]) -> bool:
    # A blocked filing requires explicit operator selection before another SEC attempt.
    if row.get("raw_coverage_status") == "blocked":
        return False
    if any(
        doc.get("diagnostic_code") in {"sec_forbidden", "sec_retry_deferred"}
        for doc in docs
        if doc.get("selection_status") == "required"
    ):
        return False
    return not _is_complete(row, docs)


def _select_filings(
    filing_rows: list[dict[str, Any]],
    documents_by_filing: Mapping[str, Sequence[Mapping[str, Any]]],
    requested_ids: tuple[str, ...] | None,
    max_filings: int,
) -> tuple[list[dict[str, Any]], tuple[str, ...]]:
    by_id = {row["filing_id"]: row for row in filing_rows}
    if requested_ids is not None:
        missing = sorted(set(requested_ids) - set(by_id))
        if missing:
            raise AcquisitionError(
                f"explicit filing_ids are absent from the active snapshot: {missing}"
            )
        excluded = sorted(
            identity for identity in requested_ids if by_id[identity]["scope_status"] == "excluded"
        )
        if excluded:
            raise AcquisitionError(
                f"explicit filing_ids are excluded from acquisition scope: {excluded}"
            )
        eligible = [by_id[identity] for identity in requested_ids]
    else:
        eligible = [
            row
            for row in filing_rows
            if row["scope_status"] in _APPROVED_SCOPE
            and _needs_work(row, documents_by_filing.get(row["filing_id"], ()))
        ]

    eligible.sort(key=lambda row: (row["filed_date"], row["filing_id"]))
    if requested_ids is None:
        return eligible[:max_filings], ()
    selected = eligible
    skipped = tuple(
        row["filing_id"]
        for row in selected
        if _is_complete(row, documents_by_filing.get(row["filing_id"], ()))
    )
    return selected, skipped


def _safe_diagnostic(exc: Exception) -> tuple[str, str, int | None]:
    name = type(exc).__name__
    status = getattr(exc, "status", None)
    status_value = (
        status
        if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599
        else None
    )
    if isinstance(exc, SecNotFoundError) or name == "SecNotFoundError":
        return "unavailable", "sec_not_found", 404
    if isinstance(exc, SecForbiddenError) or status_value == 403 or name == "SecRetryDeferredError":
        code = "sec_retry_deferred" if name == "SecRetryDeferredError" else "sec_forbidden"
        return "blocked", code, status_value or 403
    if status_value is not None and status_value != 200:
        return "error", f"http_{status_value}", status_value
    client_code = getattr(exc, "code", None)
    if (
        isinstance(client_code, str)
        and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", client_code)
        and client_code not in {"sec_client_error", "http_status_error"}
    ):
        return "error", client_code, status_value
    if "Timeout" in name or name == "socket.timeout":
        return "error", "sec_timeout", None
    return "error", "sec_transport_error", None


def _inventory_failure(exc: Exception) -> tuple[str, str, str, int | None]:
    if isinstance(exc, FilingInventoryError):
        return "blocked", "inventory_invalid", "blocked", None
    state, diagnostic, status = _safe_diagnostic(exc)
    ledger_status = (
        "unavailable" if state == "unavailable" else "blocked" if state == "blocked" else "error"
    )
    return state, diagnostic, ledger_status, status


def _record_attempt(writer: Any, item_id: str, status: str, diagnostic: str | None) -> None:
    writer.record_attempt(item_id, status, diagnostic)


def _put_response(writer: Any, response: FetchResponse) -> RawObjectRef:
    body_hash = hashlib.sha256(response.body).hexdigest()
    return writer.put_raw_bytes(response.body, expected_sha256=body_hash)


def _url_filename(url: str) -> str:
    try:
        value = urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1]
    except ValueError:
        value = ""
    if not value or value in {".", ".."} or "/" in value or "\\" in value:
        raise AcquisitionError("inventory response has an unsafe source URL")
    return value


def _make_inventory_row(
    filing_id_value: str,
    response: FetchResponse,
    ref: RawObjectRef,
    *,
    plan_status: str,
    plan_reasons: Sequence[str],
) -> dict[str, Any]:
    filename = _url_filename(response.request_url)
    identity = make_document_id(filing_id_value, response.request_url)
    locator = _canonical_json_text(
        {
            "plan_status": plan_status,
            "plan_reasons": list(plan_reasons),
            "response": {
                "sha256": ref.sha256,
                "locator": "$",
                "final_url": response.final_url,
            },
        }
    )
    return {
        "document_id": identity,
        "filing_id": filing_id_value,
        "original_filename": filename,
        "source_url": response.request_url,
        "role": "filing_index",
        "selection_status": "required",
        "fetch_status": "present",
        "source_inventory_sha256": ref.sha256,
        "source_inventory_locator": locator,
        "raw_sha256": ref.sha256,
        "raw_path": ref.path,
        "byte_size": ref.byte_size,
        "media_type": response.content_type,
        "fetched_at_utc": response.fetched_at_utc,
        "http_status": response.status,
        "diagnostic_code": None,
        "fact_extraction_status": "not_attempted",
        "text_extraction_status": "not_attempted",
    }


def _index_item_positions(index_response: FetchResponse) -> dict[str, int]:
    try:
        parsed = json.loads(index_response.body.decode("utf-8-sig"))
        items = parsed["directory"]["item"]
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError):
        raise AcquisitionError(
            "document selection returned an unreadable SEC index response"
        ) from None
    if not isinstance(items, list):
        raise AcquisitionError("document selection returned an invalid SEC index item list")
    positions: dict[str, int] = {}
    for index, item in enumerate(items):
        if isinstance(item, Mapping) and isinstance(item.get("name"), str):
            name = item["name"]
            positions.setdefault(name, index)
    return positions


def _inventory_provenance(
    candidate: DocumentCandidate,
    index_ref: RawObjectRef,
    index_position: int,
    detail_ref: RawObjectRef | None,
    plan_status: str,
    plan_reasons: Sequence[str],
) -> str:
    value: dict[str, Any] = {
        "plan_status": plan_status,
        "plan_reasons": list(plan_reasons),
        "selection_status": candidate.selection_status,
        "selection_reason": candidate.selection_reason,
        "role": candidate.role,
        "document_type": candidate.document_type,
        "description": candidate.description,
        "sequence": candidate.sequence,
        "inventory_metadata": dict(candidate.inventory_metadata),
        "sources": [{"sha256": index_ref.sha256, "locator": f"$.directory.item[{index_position}]"}],
    }
    if detail_ref is not None and any(
        item is not None
        for item in (candidate.document_type, candidate.description, candidate.sequence)
    ):
        value["sources"].append(
            {
                "sha256": detail_ref.sha256,
                "locator": f"tableFile/document/{candidate.filename}",
            }
        )
    return _canonical_json_text(value)


def _make_candidate_row(
    filing_id_value: str,
    candidate: DocumentCandidate,
    index_ref: RawObjectRef,
    index_position: int,
    detail_ref: RawObjectRef | None,
    plan_status: str,
    plan_reasons: Sequence[str],
    *,
    nonfinancial: bool,
) -> dict[str, Any]:
    expected_id = make_document_id(filing_id_value, candidate.url)
    if candidate.document_id != expected_id or candidate.filing_id != filing_id_value:
        raise AcquisitionConflictError(
            "document selector identity differs from the archive core identity"
        )
    if candidate.role not in _VALID_ROLES:
        raise AcquisitionError("document selector returned an unsupported core document role")
    selection_status = "out_of_scope" if nonfinancial else candidate.selection_status
    if selection_status not in _VALID_SELECTION:
        raise AcquisitionError("document selector returned an unsupported selection status")
    return {
        "document_id": expected_id,
        "filing_id": filing_id_value,
        "original_filename": candidate.filename,
        "source_url": candidate.url,
        "role": candidate.role,
        "selection_status": selection_status,
        "fetch_status": "not_requested",
        "source_inventory_sha256": index_ref.sha256,
        "source_inventory_locator": _inventory_provenance(
            candidate, index_ref, index_position, detail_ref, plan_status, plan_reasons
        ),
        "raw_sha256": None,
        "raw_path": None,
        "byte_size": None,
        "media_type": None,
        "fetched_at_utc": None,
        "http_status": None,
        "diagnostic_code": None,
        "fact_extraction_status": "not_attempted",
        "text_extraction_status": "not_attempted",
    }


def _plan_status_from_rows(docs: Sequence[Mapping[str, Any]]) -> str:
    if not docs:
        return "needs_review"
    observed: str | None = None
    for row in docs:
        raw_locator = row.get("source_inventory_locator")
        if not isinstance(raw_locator, str):
            return "needs_review"
        try:
            locator = json.loads(raw_locator)
        except json.JSONDecodeError:
            return "needs_review"
        if not isinstance(locator, Mapping):
            return "needs_review"
        plan_status = locator.get("plan_status")
        if not isinstance(plan_status, str) or plan_status not in _VALID_PLAN_STATUSES:
            return "needs_review"
        if observed is None:
            observed = plan_status
        elif plan_status != observed:
            return "needs_review"
    return observed or "needs_review"


def _candidate_from_row(row: Mapping[str, Any]) -> DocumentCandidate:
    try:
        locator = json.loads(row["source_inventory_locator"])
    except (TypeError, json.JSONDecodeError):
        raise ArchiveCorruptionError("document inventory locator is not valid JSON") from None
    if not isinstance(locator, Mapping):
        raise ArchiveCorruptionError("document inventory locator is not a JSON object")
    candidate = DocumentCandidate(
        filename=row["original_filename"],
        url=row["source_url"],
        document_type=locator.get("document_type"),
        description=locator.get("description"),
        sequence=locator.get("sequence"),
        role=row["role"],
        selected=row["selection_status"] == "required",
        selection_reason=str(locator.get("selection_reason") or "Resumed required document"),
        filing_id=row["filing_id"],
        document_id=row["document_id"],
        selection_status=row["selection_status"],
        inventory_metadata=locator.get("inventory_metadata", {}),
    )
    if candidate.selected and candidate.document_id != make_document_id(
        candidate.filing_id, candidate.url
    ):
        raise ArchiveCorruptionError("resumed document identity no longer matches its source URL")
    return candidate


def _apply_fetch_result(
    row: dict[str, Any],
    response: FetchResponse,
    writer: Any,
    raw_refs: dict[str, RawObjectRef],
) -> None:
    if response.request_url != row["source_url"]:
        raise AcquisitionConflictError(
            "SEC client response request URL differs from the selected document URL"
        )
    ref = _put_response(writer, response)
    raw_refs[ref.path] = ref
    row.update(
        {
            "fetch_status": "present",
            "raw_sha256": ref.sha256,
            "raw_path": ref.path,
            "byte_size": ref.byte_size,
            "media_type": response.content_type,
            "fetched_at_utc": response.fetched_at_utc,
            "http_status": response.status,
            "diagnostic_code": None,
        }
    )


def _apply_fetch_error(
    row: dict[str, Any],
    exc: Exception,
    writer: Any,
    *,
    filing_row: dict[str, Any],
) -> tuple[str, bool]:
    state, diagnostic, http_status = _safe_diagnostic(exc)
    row.update(
        {
            "fetch_status": "unavailable" if state == "unavailable" else "error",
            "raw_sha256": None,
            "raw_path": None,
            "byte_size": None,
            "media_type": None,
            "fetched_at_utc": None,
            "http_status": http_status,
            "diagnostic_code": diagnostic,
        }
    )
    filing_row["raw_coverage_status"] = "blocked" if state == "blocked" else "partial"
    _record_attempt(writer, row["document_id"], state, diagnostic)
    return diagnostic, state == "blocked"


def _all_required_present(docs: Sequence[Mapping[str, Any]]) -> bool:
    required = [
        row
        for row in docs
        if row.get("selection_status") == "required" and row.get("role") != "filing_index"
    ]
    primary = [row for row in docs if row.get("role") == "primary"]
    return (
        bool(required)
        and len(primary) == 1
        and primary[0].get("selection_status") == "required"
        and all(row.get("fetch_status") == "present" for row in required)
    )


def _coverage_after_plan(
    filing_row: Mapping[str, Any],
    plan_status: str,
    docs: Sequence[Mapping[str, Any]],
    *,
    blocked: bool,
) -> str:
    if blocked:
        return "blocked"
    if plan_status == "non_financial":
        return "not_attempted"
    if plan_status != "selected_financial":
        return "partial"
    indexes = [row for row in docs if row.get("role") == "filing_index"]
    if not indexes or any(row.get("fetch_status") != "present" for row in indexes):
        return "partial"
    return "scoped_complete" if _all_required_present(docs) else "partial"


def _validate_inventory_response_continuity(
    existing_docs: Sequence[Mapping[str, Any]], plan: DocumentPlan
) -> None:
    """Require committed index/detail bytes to be identical before selection replay."""
    response_by_url = {response.request_url: response for response in plan.inventory_responses}
    previous_indexes = [row for row in existing_docs if row.get("role") == "filing_index"]
    if any(row.get("source_url") not in response_by_url for row in previous_indexes):
        raise AcquisitionConflictError(
            "refreshed inventory no longer returns a committed SEC index resource"
        )
    for row in previous_indexes:
        if row.get("fetch_status") != "present":
            continue
        response = response_by_url[row["source_url"]]
        if row.get("raw_sha256") != response.sha256 or row.get("byte_size") != len(response.body):
            raise AcquisitionConflictError(
                "refreshed document type conflicts or 6-K scope evidence conflicts with "
                "committed SEC index/detail bytes"
            )


def _validate_inventory_continuity(
    filing_id_value: str,
    existing_docs: Sequence[Mapping[str, Any]],
    plan: DocumentPlan,
    *,
    primary_response: FetchResponse | None = None,
) -> None:
    """Reject URL/type/source/evidence drift while permitting a proven candidate promotion."""
    _validate_inventory_response_continuity(existing_docs, plan)

    current_by_id: dict[str, DocumentCandidate] = {}
    for candidate in plan.candidates:
        expected = make_document_id(filing_id_value, candidate.url)
        if candidate.document_id != expected or candidate.filing_id != filing_id_value:
            raise AcquisitionConflictError(
                "refreshed selector identity differs from the archive core identity"
            )
        if expected in current_by_id:
            raise AcquisitionConflictError("refreshed inventory repeats a document identity")
        current_by_id[expected] = candidate

    for old in existing_docs:
        if old.get("role") == "filing_index":
            continue
        current = current_by_id.get(old["document_id"])
        if current is None:
            raise AcquisitionConflictError(
                "refreshed inventory removed a previously recorded document"
            )
        if current.url != old["source_url"] or current.filename != old["original_filename"]:
            raise AcquisitionConflictError(
                "refreshed inventory conflicts with a recorded document URL/name"
            )
        try:
            old_meta = json.loads(old["source_inventory_locator"])
        except (TypeError, json.JSONDecodeError):
            raise ArchiveCorruptionError("document inventory locator is not valid JSON") from None
        old_type = old_meta.get("document_type") if isinstance(old_meta, Mapping) else None
        if old_type is not None and current.document_type != old_type:
            raise AcquisitionConflictError(
                "refreshed SEC document type conflicts with committed metadata"
            )
        old_inventory = (
            old_meta.get("inventory_metadata", {}) if isinstance(old_meta, Mapping) else {}
        )
        current_inventory = current.inventory_metadata
        old_primary_proof = (
            old_inventory.get("primary_exhibit_evidence")
            if isinstance(old_inventory, Mapping)
            else None
        )
        current_primary_proof = current_inventory.get("primary_exhibit_evidence")
        if old_primary_proof is not None and old_primary_proof != current_primary_proof:
            raise AcquisitionConflictError(
                "refreshed primary-exhibit evidence conflicts with committed provenance"
            )
        if current_primary_proof is not None:
            if not isinstance(current_primary_proof, Mapping):
                raise ArchiveCorruptionError("primary-exhibit provenance is malformed")
            primary_source = current_primary_proof.get("primary_source")
            index_source = current_primary_proof.get("accession_index_source")
            if not isinstance(primary_source, Mapping) or not isinstance(index_source, Mapping):
                raise ArchiveCorruptionError("primary-exhibit source references are malformed")
            primary_sha = primary_source.get("source_sha256")
            primary_url = primary_source.get("source_url")
            current_index = next(
                (
                    response
                    for response in plan.inventory_responses
                    if _url_filename(response.request_url).casefold() == "index.json"
                ),
                None,
            )
            if (
                not isinstance(primary_sha, str)
                or _SHA256_RE.fullmatch(primary_sha) is None
                or not isinstance(primary_url, str)
                or primary_url
                != next(
                    (item.url for item in plan.candidates if item.role == "primary"),
                    None,
                )
                or current_index is None
                or index_source.get("source_url") != current_index.request_url
                or index_source.get("source_sha256") != current_index.sha256
            ):
                raise AcquisitionConflictError(
                    "primary-exhibit proof does not match this filing's primary/index sources"
                )
            if primary_response is not None and (
                primary_response.request_url != primary_url
                or primary_response.sha256 != primary_sha
            ):
                raise AcquisitionConflictError(
                    "primary-exhibit proof differs from the verified primary response"
                )
            prior_primary = next(
                (item for item in existing_docs if item.get("role") == "primary"), None
            )
            if prior_primary is not None and prior_primary.get("fetch_status") == "present":
                if (
                    prior_primary.get("source_url") != primary_url
                    or prior_primary.get("raw_sha256") != primary_sha
                ):
                    raise AcquisitionConflictError(
                        "primary-exhibit proof differs from committed primary bytes"
                    )
            elif primary_response is None:
                raise AcquisitionConflictError(
                    "candidate promotion requires present primary bytes or this call's "
                    "verified response"
                )
        if old["selection_status"] == "required" and (
            not current.selected or plan.selection_status == "non_financial"
        ):
            raise AcquisitionConflictError(
                "refreshed selection would unselect a previously required document"
            )
        if old["role"] != current.role and not (
            old["role"] == "other"
            and old["selection_status"] != "required"
            and current.selected
            and plan.selection_status != "non_financial"
        ):
            raise AcquisitionConflictError(
                "refreshed document role conflicts with committed selection"
            )


def _needs_primary_exhibit_refinement(
    filing_row: Mapping[str, Any],
    plan: DocumentPlan,
    existing_docs: Sequence[Mapping[str, Any]],
) -> bool:
    if filing_row.get("form") not in {"6-K", "40-F", "40-F/A"}:
        return False
    if plan.selection_status == "needs_review":
        return True
    if any(
        candidate.selected
        and candidate.selection_status == "required"
        and candidate.role in {"primary", "exhibit"}
        and "description explicitly identifies" in candidate.selection_reason.casefold()
        for candidate in plan.candidates
    ):
        return True
    for row in existing_docs:
        try:
            locator = json.loads(str(row.get("source_inventory_locator", "")))
        except (TypeError, json.JSONDecodeError):
            continue
        inventory = locator.get("inventory_metadata") if isinstance(locator, Mapping) else None
        if isinstance(inventory, Mapping) and isinstance(
            inventory.get("primary_exhibit_evidence"), Mapping
        ):
            return True
    return False


def _verified_cached_primary_response(
    candidate: DocumentCandidate,
    existing_docs: Sequence[Mapping[str, Any]],
    archive_root: Path,
    raw_refs: Mapping[str, RawObjectRef],
) -> FetchResponse | None:
    rows = [row for row in existing_docs if row.get("document_id") == candidate.document_id]
    if len(rows) > 1:
        raise ArchiveCorruptionError("archive repeats a primary document identity")
    if not rows or rows[0].get("fetch_status") != "present":
        return None
    row = rows[0]
    if (
        row.get("role") != "primary"
        or row.get("selection_status") != "required"
        or row.get("source_url") != candidate.url
    ):
        raise AcquisitionConflictError("cached primary row differs from the selected primary")
    raw_path = row.get("raw_path")
    raw_hash = row.get("raw_sha256")
    size = row.get("byte_size")
    if (
        not isinstance(raw_path, str)
        or Path(raw_path).is_absolute()
        or ".." in Path(raw_path).parts
    ):
        raise ArchiveCorruptionError("cached primary raw_path is unsafe")
    reference = raw_refs.get(raw_path)
    if (
        reference is None
        or reference.sha256 != raw_hash
        or reference.byte_size != size
        or not isinstance(raw_hash, str)
        or _SHA256_RE.fullmatch(raw_hash) is None
    ):
        raise ArchiveCorruptionError("cached primary bytes are not in the active verified archive")
    root = Path(archive_root).resolve(strict=True)
    path = root / raw_path
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError):
        raise ArchiveCorruptionError(
            "cached primary path is missing or leaves the archive"
        ) from None
    body = resolved.read_bytes()
    if len(body) != size or hashlib.sha256(body).hexdigest() != raw_hash:
        raise ArchiveCorruptionError("cached primary bytes no longer match their active hash")
    return FetchResponse(
        body=body,
        request_url=candidate.url,
        final_url=candidate.url,
        content_type=row.get("media_type"),
        fetched_at_utc=row.get("fetched_at_utc"),
        attempts=0,
        status=200,
    )


def _prepare_primary_exhibit_refinement(
    plan: DocumentPlan,
    *,
    filing_row: Mapping[str, Any],
    existing_docs: Sequence[Mapping[str, Any]],
    archive_root: Path,
    raw_refs: Mapping[str, RawObjectRef],
    client: SecClient,
) -> tuple[DocumentPlan, dict[str, FetchResponse], dict[str, SecClientError], FetchResponse | None]:
    if not _needs_primary_exhibit_refinement(filing_row, plan, existing_docs):
        return plan, {}, {}, None
    primaries = [
        candidate
        for candidate in plan.candidates
        if candidate.role == "primary" and candidate.selected
    ]
    if len(primaries) != 1:
        raise AcquisitionConflictError(
            "cover-exhibit refinement requires exactly one selected primary document"
        )
    primary = primaries[0]
    cached = _verified_cached_primary_response(primary, existing_docs, archive_root, raw_refs)
    if cached is not None:
        return refine_document_plan(plan, primary_response=cached), {}, {}, cached
    try:
        response = fetch_document(primary, client)
    except SecClientError as exc:
        return plan, {}, {primary.document_id: exc}, None
    refined = refine_document_plan(plan, primary_response=response)
    return refined, {primary.document_id: response}, {}, response


def _scope_document_snapshot(
    docs: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], ...]:
    """Capture immutable source-row fields before refreshed rows are merged in place."""
    fields = (
        "document_id",
        "filing_id",
        "original_filename",
        "source_url",
        "role",
        "selection_status",
        "fetch_status",
        "source_inventory_sha256",
        "raw_sha256",
        "raw_path",
        "byte_size",
        "source_inventory_locator",
    )
    return tuple({name: row.get(name) for name in fields} for row in docs)


def _selected_financial_ids_from_rows(
    docs: Sequence[Mapping[str, Any]],
) -> set[str] | None:
    selected: set[str] = set()
    for row in docs:
        if (
            row.get("role") not in {"primary", "exhibit"}
            or row.get("selection_status") != "required"
        ):
            continue
        try:
            locator = json.loads(str(row.get("source_inventory_locator", "")))
        except (TypeError, json.JSONDecodeError):
            return None
        if not isinstance(locator, Mapping):
            return None
        inventory = locator.get("inventory_metadata")
        primary_proof = (
            inventory.get("primary_exhibit_evidence") if isinstance(inventory, Mapping) else None
        )
        reason = locator.get("selection_reason")
        if (
            isinstance(reason, str) and "description explicitly identifies" in reason.casefold()
        ) or isinstance(primary_proof, Mapping):
            identity = row.get("document_id")
            if not isinstance(identity, str) or identity in selected:
                return None
            selected.add(identity)
    return selected


def _merge_refreshed_inventory(
    existing_docs: list[dict[str, Any]],
    refreshed_rows: list[dict[str, Any]],
    all_document_rows: list[dict[str, Any]],
    all_rows_by_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    refreshed_by_id = {row["document_id"]: row for row in refreshed_rows}
    merged: list[dict[str, Any]] = []
    existing_ids: set[str] = set()
    for old in existing_docs:
        existing_ids.add(old["document_id"])
        fresh = refreshed_by_id.get(old["document_id"])
        if fresh is None:
            # The continuity check only permits a previously stored detail resource
            # to disappear if the current plan has no such row; retain that old audit row.
            merged.append(old)
            continue
        preserved = {
            key: old.get(key)
            for key in (
                "raw_sha256",
                "raw_path",
                "byte_size",
                "media_type",
                "fetched_at_utc",
                "http_status",
            )
        }
        old_fetch_status = old.get("fetch_status")
        old_fact_status = old.get("fact_extraction_status", "not_attempted")
        old_text_status = old.get("text_extraction_status", "not_attempted")
        old.clear()
        old.update(fresh)
        if old_fetch_status == "present" and old.get("role") != "filing_index":
            old.update(preserved)
            old["fetch_status"] = "present"
            old["diagnostic_code"] = None
        else:
            old["diagnostic_code"] = None
        old["fact_extraction_status"] = old_fact_status
        old["text_extraction_status"] = old_text_status
        merged.append(old)
        all_rows_by_id[old["document_id"]] = old

    for fresh in refreshed_rows:
        if fresh["document_id"] in existing_ids:
            continue
        merged.append(fresh)
        all_document_rows.append(fresh)
        all_rows_by_id[fresh["document_id"]] = fresh
    return merged


def _new_inventory_rows(
    filing_id_value: str,
    plan: DocumentPlan,
    writer: Any,
    raw_refs: dict[str, RawObjectRef],
) -> list[dict[str, Any]]:
    if not plan.inventory_responses:
        raise AcquisitionError("document plan did not retain its SEC inventory response")
    responses = plan.inventory_responses
    index_responses = [
        response
        for response in responses
        if _url_filename(response.request_url).casefold() == "index.json"
    ]
    if len(index_responses) != 1:
        raise AcquisitionError(
            "document plan must retain exactly one accession index.json response"
        )
    index_response = index_responses[0]
    index_positions = _index_item_positions(index_response)
    refs: dict[str, RawObjectRef] = {}
    response_rows: list[dict[str, Any]] = []
    response_rows_by_url: dict[str, dict[str, Any]] = {}
    for response in responses:
        ref = _put_response(writer, response)
        raw_refs[ref.path] = ref
        refs[response.request_url] = ref
        row = _make_inventory_row(
            filing_id_value,
            response,
            ref,
            plan_status=plan.selection_status,
            plan_reasons=plan.reasons,
        )
        response_rows.append(row)
        response_rows_by_url[response.request_url] = row
        _record_attempt(writer, row["document_id"], "present", None)
    index_ref = refs[index_response.request_url]
    detail_refs = [
        refs[response.request_url]
        for response in responses
        if _url_filename(response.request_url).casefold().endswith("-index.html")
    ]
    if len(detail_refs) > 1:
        raise AcquisitionError("document plan retained multiple filing detail pages")
    detail_ref = detail_refs[0] if detail_refs else None

    nonfinancial = plan.selection_status == "non_financial"
    for candidate in plan.candidates:
        if candidate.filename not in index_positions:
            raise AcquisitionConflictError(
                "document candidate is absent from its SEC index response"
            )
        response_row = response_rows_by_url.get(candidate.url)
        if response_row is not None:
            expected_id = make_document_id(filing_id_value, candidate.url)
            if (
                candidate.filing_id != filing_id_value
                or candidate.document_id != expected_id
                or response_row["document_id"] != expected_id
                or candidate.filename != response_row["original_filename"]
                or candidate.filename != _url_filename(candidate.url)
                or candidate.inventory_metadata.get("name") != candidate.filename
            ):
                raise AcquisitionConflictError(
                    "SEC inventory candidate identity or filename conflicts with its response"
                )
            if candidate.selected or candidate.selection_status == "required":
                raise AcquisitionConflictError(
                    "SEC inventory response metadata conflicts with a selected filing document"
                )

            index_position = index_positions[candidate.filename]
            locator = json.loads(response_row["source_inventory_locator"])
            locator["sources"] = [
                {"sha256": response_row["raw_sha256"], "locator": "$"},
                {
                    "sha256": index_ref.sha256,
                    "locator": f"$.directory.item[{index_position}]",
                },
            ]
            locator["directory_item"] = {
                "filename": candidate.filename,
                "position": index_position,
                "document_type": candidate.document_type,
                "description": candidate.description,
                "sequence": candidate.sequence,
                "role": candidate.role,
                "selected": candidate.selected,
                "selection_status": candidate.selection_status,
                "selection_reason": candidate.selection_reason,
                "inventory_metadata": dict(candidate.inventory_metadata),
                "source": {
                    "sha256": index_ref.sha256,
                    "locator": f"$.directory.item[{index_position}]",
                },
            }
            response_row["source_inventory_locator"] = _canonical_json_text(locator)
            continue

        row = _make_candidate_row(
            filing_id_value,
            candidate,
            index_ref,
            index_positions[candidate.filename],
            detail_ref,
            plan.selection_status,
            plan.reasons,
            nonfinancial=nonfinancial,
        )
        response_rows.append(row)
    return response_rows


def _explicit_financial_candidates(plan: DocumentPlan) -> tuple[DocumentCandidate, ...]:
    return tuple(
        candidate
        for candidate in plan.candidates
        if candidate.selected
        and candidate.selection_status == "required"
        and candidate.role in {"primary", "exhibit"}
        and "description explicitly identifies " in candidate.selection_reason.casefold()
    )


def _scope_evidence_for_6k(
    plan: DocumentPlan,
    docs: Sequence[Mapping[str, Any]],
    *,
    decision: str,
) -> str:
    index_rows = [
        row
        for row in docs
        if row.get("role") == "filing_index"
        and row.get("original_filename", "").casefold() == "index.json"
        and row.get("fetch_status") == "present"
    ]
    index_responses = [
        response
        for response in plan.inventory_responses
        if _url_filename(response.request_url).casefold() == "index.json"
    ]
    if len(index_rows) != 1 or len(index_responses) != 1:
        raise AcquisitionError("6-K scope evidence requires exactly one verified index.json source")
    index_response = index_responses[0]
    inventory_sha256 = hashlib.sha256(index_response.body).hexdigest()
    if _SHA256_RE.fullmatch(inventory_sha256) is None:
        raise AcquisitionError("6-K scope evidence index source has no verified SHA-256")

    docs_by_id = {str(row["document_id"]): row for row in docs}
    if decision == "included":
        support_candidates = _explicit_financial_candidates(plan)
        selected_ids = [candidate.document_id for candidate in support_candidates]
        reasons = [candidate.selection_reason for candidate in support_candidates]
    elif decision == "excluded":
        support_candidates = tuple(
            candidate
            for candidate in plan.candidates
            if candidate.selection_reason.startswith(
                "SEC description identifies non-financial exhibit"
            )
        )
        selected_ids = []
        reasons = list(plan.reasons) or [
            candidate.selection_reason for candidate in support_candidates
        ]
    else:
        raise AcquisitionError("unsupported 6-K scope evidence decision")

    if decision == "included" and not selected_ids:
        raise AcquisitionError("included 6-K scope has no explicitly identified financial document")
    if decision == "excluded" and not reasons:
        raise AcquisitionError("excluded 6-K scope has no explicit non-financial evidence reason")

    support_metadata: list[dict[str, Any]] = []
    inventory_sources: dict[tuple[str, str, str], dict[str, str]] = {}
    source_url_by_hash = {
        hashlib.sha256(response.body).hexdigest(): response.request_url
        for response in plan.inventory_responses
    }
    index_url = index_response.request_url
    inventory_sources[(inventory_sha256, "$.directory.item", index_url)] = {
        "sha256": inventory_sha256,
        "locator": "$.directory.item",
        "source_url": index_url,
    }

    for candidate in support_candidates:
        row = docs_by_id.get(candidate.document_id)
        if row is None:
            raise AcquisitionConflictError(
                "6-K scope evidence candidate is absent from the committed document inventory"
            )
        if row.get("source_inventory_sha256") != inventory_sha256:
            raise AcquisitionConflictError(
                "6-K selected document source does not match the current index inventory"
            )
        try:
            locator = json.loads(str(row["source_inventory_locator"]))
        except (TypeError, json.JSONDecodeError) as exc:
            raise ArchiveCorruptionError("6-K candidate inventory locator is unreadable") from exc
        if not isinstance(locator, Mapping):
            raise ArchiveCorruptionError("6-K candidate inventory locator is not an object")
        sources = locator.get("sources", [])
        if not isinstance(sources, list):
            raise ArchiveCorruptionError("6-K candidate inventory sources are malformed")
        for source in sources:
            if not isinstance(source, Mapping):
                raise ArchiveCorruptionError("6-K candidate inventory source is malformed")
            source_hash = source.get("sha256")
            source_locator = source.get("locator")
            if (
                not isinstance(source_hash, str)
                or _SHA256_RE.fullmatch(source_hash) is None
                or not isinstance(source_locator, str)
                or not source_locator
            ):
                raise ArchiveCorruptionError("6-K candidate inventory source is incomplete")
            source_url = source_url_by_hash.get(source_hash, "")
            inventory_sources[(source_hash, source_locator, source_url)] = {
                "sha256": source_hash,
                "locator": source_locator,
                "source_url": source_url,
            }
        inventory_metadata = locator.get("inventory_metadata", {})
        primary_proof = (
            inventory_metadata.get("primary_exhibit_evidence")
            if isinstance(inventory_metadata, Mapping)
            else None
        )
        if primary_proof is not None:
            if (
                not isinstance(primary_proof, Mapping)
                or primary_proof.get("rule_version") != "primary-exhibit-title-v1"
            ):
                raise ArchiveCorruptionError("primary-exhibit evidence metadata is malformed")
            primary_source = primary_proof.get("primary_source")
            index_source = primary_proof.get("accession_index_source")
            if not isinstance(primary_source, Mapping) or not isinstance(index_source, Mapping):
                raise ArchiveCorruptionError("primary-exhibit source metadata is malformed")
            primary_sha = primary_source.get("source_sha256")
            primary_url = primary_source.get("source_url")
            primary_loc = primary_proof.get("source_locator")
            primary_candidates = [item for item in plan.candidates if item.role == "primary"]
            if (
                not isinstance(primary_sha, str)
                or _SHA256_RE.fullmatch(primary_sha) is None
                or not isinstance(primary_url, str)
                or len(primary_candidates) != 1
                or primary_url != primary_candidates[0].url
                or not isinstance(primary_loc, str)
                or not primary_loc.startswith("primary_html:")
                or index_source.get("source_sha256") != inventory_sha256
                or index_source.get("source_url") != index_url
            ):
                raise AcquisitionConflictError(
                    "primary-exhibit evidence does not match the filing's active source identities"
                )
            primary_inventory_locator = str(primary_loc)
            inventory_sources[(primary_sha, primary_inventory_locator, primary_url)] = {
                "sha256": primary_sha,
                "locator": primary_inventory_locator,
                "source_url": primary_url,
            }
        else:
            primary_proof = None
        support_metadata.append(
            {
                "document_id": candidate.document_id,
                "source_url": candidate.url,
                "filename": candidate.filename,
                "role": candidate.role,
                "document_type": locator.get("document_type"),
                "description": locator.get("description"),
                "sequence": locator.get("sequence"),
                "selection_reason": candidate.selection_reason,
                "primary_exhibit_evidence": primary_proof,
                "source_inventory": locator,
            }
        )

    metadata = {
        "plan_status": plan.selection_status,
        "plan_reasons": list(plan.reasons),
        "inventory_sources": [inventory_sources[key] for key in sorted(inventory_sources)],
        "supporting_documents": support_metadata,
    }
    return _canonical_json_text(
        {
            "policy": "sec-metadata-financial-v1",
            "decision": decision,
            "inventory_sha256": inventory_sha256,
            "inventory_locator": "$.directory.item",
            "selected_financial_document_ids": selected_ids,
            "reasons": reasons,
            "metadata": metadata,
        }
    )


def _scope_evidence_sources(
    value: Mapping[str, Any],
) -> set[tuple[str, str, str]] | None:
    metadata = value.get("metadata")
    sources = metadata.get("inventory_sources") if isinstance(metadata, Mapping) else None
    if not isinstance(sources, list) or not sources:
        return None
    result: set[tuple[str, str, str]] = set()
    for source in sources:
        if not isinstance(source, Mapping):
            return None
        digest = source.get("sha256")
        locator = source.get("locator")
        source_url = source.get("source_url")
        if (
            not isinstance(digest, str)
            or _SHA256_RE.fullmatch(digest) is None
            or not isinstance(locator, str)
            or not locator
            or not isinstance(source_url, str)
        ):
            return None
        result.add((digest, locator, source_url))
    return result


def _scope_supporting_documents(
    value: Mapping[str, Any],
) -> dict[str, Mapping[str, Any]] | None:
    metadata = value.get("metadata")
    supports = metadata.get("supporting_documents") if isinstance(metadata, Mapping) else None
    if not isinstance(supports, list):
        return None
    result: dict[str, Mapping[str, Any]] = {}
    for item in supports:
        if not isinstance(item, Mapping):
            return None
        identity = item.get("document_id")
        if not isinstance(identity, str) or identity in result:
            return None
        result[identity] = item
    return result


def _additive_6k_scope_evidence_is_valid(
    previous_evidence: str | None,
    next_evidence: str,
    plan: DocumentPlan,
    previous_docs: Sequence[Mapping[str, Any]],
    current_docs: Sequence[Mapping[str, Any]],
) -> bool:
    """Accept only a source-identical superset of an already-included 6-K scope."""
    try:
        old, new = json.loads(previous_evidence or ""), json.loads(next_evidence)
    except (TypeError, json.JSONDecodeError):
        return False
    if not isinstance(old, Mapping) or not isinstance(new, Mapping):
        return False
    if any(
        value.get("policy") != "sec-metadata-financial-v1"
        or value.get("decision") != "included"
        or value.get("inventory_locator") != "$.directory.item"
        for value in (old, new)
    ):
        return False
    index_responses = [
        response
        for response in plan.inventory_responses
        if _url_filename(response.request_url).casefold() == "index.json"
    ]
    if len(index_responses) != 1:
        return False
    index_response = index_responses[0]
    index_hash = hashlib.sha256(index_response.body).hexdigest()
    if old.get("inventory_sha256") != index_hash or new.get("inventory_sha256") != index_hash:
        return False

    def selected_ids(value: Mapping[str, Any]) -> set[str] | None:
        items = value.get("selected_financial_document_ids")
        if (
            not isinstance(items, list)
            or not items
            or any(not isinstance(item, str) for item in items)
            or len(set(items)) != len(items)
        ):
            return None
        return set(items)

    old_ids, new_ids = selected_ids(old), selected_ids(new)
    if (
        old_ids is None
        or new_ids is None
        or _selected_financial_ids_from_rows(previous_docs) != old_ids
        or not old_ids.issubset(new_ids)
    ):
        return False
    explicit = {item.document_id: item for item in _explicit_financial_candidates(plan)}
    if not new_ids.issubset(explicit):
        return False
    old_supports, new_supports = _scope_supporting_documents(old), _scope_supporting_documents(new)
    if (
        old_supports is None
        or new_supports is None
        or set(old_supports) != old_ids
        or set(new_supports) != new_ids
    ):
        return False
    old_sources, new_sources = _scope_evidence_sources(old), _scope_evidence_sources(new)
    if old_sources is None or new_sources is None or not old_sources.issubset(new_sources):
        return False

    def rows_by_id(rows: Sequence[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]] | None:
        result = {str(row.get("document_id")): row for row in rows}
        return (
            result
            if len(result) == len(rows) and all(row.get("document_id") for row in rows)
            else None
        )

    prior_rows, current_rows = rows_by_id(previous_docs), rows_by_id(current_docs)
    if prior_rows is None or current_rows is None or not set(prior_rows).issubset(current_rows):
        return False
    for identity, prior in prior_rows.items():
        current = current_rows[identity]
        for field in ("filing_id", "original_filename", "source_url", "source_inventory_sha256"):
            if prior.get(field) != current.get(field):
                return False
        if prior.get("role") != current.get("role") and not (
            prior.get("role") == "other"
            and prior.get("selection_status") != "required"
            and current.get("role") == "exhibit"
            and current.get("selection_status") == "required"
            and identity in new_ids
        ):
            return False
        if (
            prior.get("selection_status") == "required"
            and current.get("selection_status") != "required"
        ):
            return False
        if prior.get("fetch_status") == "present" and any(
            prior.get(field) != current.get(field)
            for field in ("fetch_status", "raw_sha256", "raw_path", "byte_size")
        ):
            return False

    primaries = [row for row in previous_docs if row.get("role") == "primary"]
    current_primaries = [row for row in current_docs if row.get("role") == "primary"]
    indexes = [
        row
        for row in previous_docs
        if row.get("role") == "filing_index" and row.get("original_filename") == "index.json"
    ]
    current_indexes = [
        row
        for row in current_docs
        if row.get("role") == "filing_index" and row.get("original_filename") == "index.json"
    ]
    planned_primaries = [item for item in plan.candidates if item.role == "primary"]
    if (
        len(primaries) != 1
        or len(current_primaries) != 1
        or len(indexes) != 1
        or len(current_indexes) != 1
        or len(planned_primaries) != 1
    ):
        return False
    prior_primary, current_primary = primaries[0], current_primaries[0]
    prior_index, current_index = indexes[0], current_indexes[0]
    if (
        prior_primary.get("fetch_status") != "present"
        or prior_index.get("fetch_status") != "present"
        or any(
            prior_primary.get(k) != current_primary.get(k)
            for k in ("document_id", "source_url", "raw_sha256", "raw_path", "byte_size")
        )
        or any(
            prior_index.get(k) != current_index.get(k)
            for k in ("document_id", "source_url", "raw_sha256", "raw_path", "byte_size")
        )
        or current_primary.get("source_url") != planned_primaries[0].url
        or current_index.get("source_url") != index_response.request_url
        or current_index.get("raw_sha256") != index_hash
    ):
        return False

    for identity in old_ids:
        prior, current = old_supports[identity], new_supports[identity]
        if any(
            prior.get(k) != current.get(k)
            for k in ("source_url", "filename", "role", "document_type", "description", "sequence")
        ):
            return False
        old_inventory, new_inventory = (
            prior.get("source_inventory"),
            current.get("source_inventory"),
        )
        if not isinstance(old_inventory, Mapping) or not isinstance(new_inventory, Mapping):
            return False
        if any(
            old_inventory.get(k) != new_inventory.get(k)
            for k in ("document_type", "description", "sequence")
        ):
            return False
        old_meta, new_meta = (
            old_inventory.get("inventory_metadata"),
            new_inventory.get("inventory_metadata"),
        )
        if not isinstance(old_meta, Mapping) or not isinstance(new_meta, Mapping):
            return False
        old_meta, new_meta = dict(old_meta), dict(new_meta)
        old_embedded, new_embedded = (
            old_meta.pop("primary_exhibit_evidence", None),
            new_meta.pop("primary_exhibit_evidence", None),
        )
        if (
            old_meta != new_meta
            or prior.get("primary_exhibit_evidence") != old_embedded
            or current.get("primary_exhibit_evidence") != new_embedded
        ):
            return False
        if old_embedded is not None and old_embedded != new_embedded:
            return False
        old_items, new_items = old_inventory.get("sources"), new_inventory.get("sources")
        if not isinstance(old_items, list) or not isinstance(new_items, list):
            return False

        def item_sources(items: list[Any]) -> set[tuple[str, str]] | None:
            result: set[tuple[str, str]] = set()
            for item in items:
                digest = item.get("sha256") if isinstance(item, Mapping) else None
                locator = item.get("locator") if isinstance(item, Mapping) else None
                if (
                    not isinstance(digest, str)
                    or _SHA256_RE.fullmatch(digest) is None
                    or not isinstance(locator, str)
                    or not locator
                ):
                    return None
                result.add((digest, locator))
            return result

        old_item_sources, new_item_sources = item_sources(old_items), item_sources(new_items)
        if (
            old_item_sources is None
            or new_item_sources is None
            or not old_item_sources.issubset(new_item_sources)
        ):
            return False

    response_hashes = {
        response.request_url: hashlib.sha256(response.body).hexdigest()
        for response in plan.inventory_responses
    }
    primary_url = current_primary["source_url"]
    response_hashes[primary_url] = current_primary["raw_sha256"]
    for digest, locator, source_url in new_sources:
        if response_hashes.get(source_url) != digest:
            return False
        if source_url == primary_url:
            if not locator.startswith("primary_html:"):
                return False
        elif source_url == index_response.request_url:
            if locator != "$.directory.item" and not locator.startswith("$.directory.item["):
                return False
        elif not locator.startswith("tableFile/document/"):
            return False

    for identity in new_ids:
        candidate, row, support = (
            explicit[identity],
            current_rows.get(identity),
            new_supports[identity],
        )
        if (
            row is None
            or row.get("selection_status") != "required"
            or row.get("role") != candidate.role
            or row.get("source_url") != candidate.url
            or row.get("original_filename") != candidate.filename
            or row.get("source_inventory_sha256") != index_hash
            or any(
                support.get(k) != v
                for k, v in (
                    ("document_id", candidate.document_id),
                    ("source_url", candidate.url),
                    ("filename", candidate.filename),
                    ("role", candidate.role),
                    ("document_type", candidate.document_type),
                    ("description", candidate.description),
                    ("sequence", candidate.sequence),
                )
            )
        ):
            return False
        proof = support.get("primary_exhibit_evidence")
        if proof is not None:
            primary_source = proof.get("primary_source") if isinstance(proof, Mapping) else None
            index_source = (
                proof.get("accession_index_source") if isinstance(proof, Mapping) else None
            )
            matched = proof.get("matched_document") if isinstance(proof, Mapping) else None
            href = proof.get("linked_href") if isinstance(proof, Mapping) else None
            locator = proof.get("source_locator") if isinstance(proof, Mapping) else None
            try:
                linked_name = _url_filename(href) if isinstance(href, str) else None
            except AcquisitionError:
                return False
            if (
                not isinstance(proof, Mapping)
                or proof.get("rule_version") != "primary-exhibit-title-v1"
                or not isinstance(primary_source, Mapping)
                or primary_source.get("source_url") != primary_url
                or primary_source.get("source_sha256") != current_primary.get("raw_sha256")
                or not isinstance(index_source, Mapping)
                or index_source.get("source_url") != index_response.request_url
                or index_source.get("source_sha256") != index_hash
                or not isinstance(matched, Mapping)
                or matched.get("filename") != candidate.filename
                or matched.get("source_url") != candidate.url
                or linked_name != candidate.filename
                or not isinstance(locator, str)
                or not locator.startswith("primary_html:")
            ):
                return False
    return True


def _update_6k_scope(
    filing_row: dict[str, Any],
    plan: DocumentPlan,
    docs: Sequence[Mapping[str, Any]],
    *,
    previous_docs: Sequence[Mapping[str, Any]] = (),
) -> None:
    if filing_row["form"] != "6-K":
        return
    previous_status = filing_row["scope_status"]
    previous_evidence = filing_row.get("scope_evidence_json")
    if plan.selection_status == "non_financial":
        next_status = "excluded"
        next_evidence = _scope_evidence_for_6k(plan, docs, decision="excluded")
    else:
        explicit_financial = _explicit_financial_candidates(plan)
        if explicit_financial:
            next_status = "included"
            next_evidence = _scope_evidence_for_6k(plan, docs, decision="included")
        else:
            next_status = "candidate"
            next_evidence = None

    if previous_status == "included":
        if next_status != "included":
            raise AcquisitionConflictError(
                "refreshed 6-K scope evidence conflicts with committed included selection"
            )
        if next_evidence == previous_evidence:
            # Retain the exact canonical evidence string already committed in history.
            filing_row["scope_status"] = "included"
            filing_row["scope_evidence_json"] = previous_evidence
            return
        if not _additive_6k_scope_evidence_is_valid(
            previous_evidence,
            next_evidence,
            plan,
            previous_docs,
            docs,
        ):
            raise AcquisitionConflictError(
                "refreshed 6-K scope evidence is not a source-identical additive refinement"
            )
        filing_row["scope_status"] = "included"
        filing_row["scope_evidence_json"] = next_evidence
        return
    if previous_status == "excluded":
        raise AcquisitionConflictError("excluded 6-K scope cannot be automatically reclassified")
    filing_row["scope_status"] = next_status
    filing_row["scope_evidence_json"] = next_evidence


def _fetch_selected_rows(
    docs: list[dict[str, Any]],
    *,
    filing_row: dict[str, Any],
    client: SecClient,
    writer: Any,
    raw_refs: dict[str, RawObjectRef],
    preloaded_responses: Mapping[str, FetchResponse] | None = None,
    preloaded_errors: Mapping[str, SecClientError] | None = None,
) -> tuple[int, int, int, bool]:
    acquired = unavailable = errors = 0
    blocked = False
    plan_status = _plan_status_from_rows(docs)
    if plan_status == "non_financial":
        filing_row["raw_coverage_status"] = "not_attempted"
        return acquired, unavailable, errors, blocked

    for row in docs:
        if row.get("role") == "filing_index" or row.get("selection_status") != "required":
            continue
        if row.get("fetch_status") == "present":
            continue
        if blocked:
            row.update(
                {
                    "fetch_status": "not_requested",
                    "raw_sha256": None,
                    "raw_path": None,
                    "byte_size": None,
                    "media_type": None,
                    "fetched_at_utc": None,
                    "http_status": None,
                    "diagnostic_code": None,
                }
            )
            continue
        candidate = _candidate_from_row(row)
        preloaded_error = (preloaded_errors or {}).get(candidate.document_id)
        if preloaded_error is not None:
            _, is_blocked = _apply_fetch_error(row, preloaded_error, writer, filing_row=filing_row)
            if row["fetch_status"] == "unavailable":
                unavailable += 1
            else:
                errors += 1
            blocked = blocked or is_blocked
            continue
        response = (preloaded_responses or {}).get(candidate.document_id)
        try:
            if response is None:
                response = fetch_document(candidate, client)
        except SecClientError as exc:
            _, is_blocked = _apply_fetch_error(row, exc, writer, filing_row=filing_row)
            if row["fetch_status"] == "unavailable":
                unavailable += 1
            else:
                errors += 1
            blocked = blocked or is_blocked
        else:
            _apply_fetch_result(row, response, writer, raw_refs)
            acquired += 1
            _record_attempt(writer, row["document_id"], "present", None)

    filing_row["raw_coverage_status"] = _coverage_after_plan(
        filing_row,
        plan_status,
        docs,
        blocked=blocked,
    )
    return acquired, unavailable, errors, blocked


def _inventory_failure_row(
    filing_row: dict[str, Any],
    exc: Exception,
    writer: Any,
) -> tuple[str, bool]:
    state, diagnostic, ledger_status, _ = _inventory_failure(exc)
    filing_row["inventory_status"] = "partial"
    filing_row["raw_coverage_status"] = (
        "blocked" if state in {"blocked", "unavailable"} else "partial"
    )
    if filing_row["form"] == "6-K":
        filing_row["scope_status"] = "candidate"
    _record_attempt(writer, filing_row["filing_id"], ledger_status, diagnostic)
    return diagnostic, state in {"blocked", "unavailable"}


def download_archive(
    archive_root: Path,
    *,
    client: SecClient,
    protected_paths: Sequence[Path],
    filing_ids: Sequence[str] | None = None,
    max_filings: int = 5,
) -> AcquisitionResult:
    """Acquire a bounded number of catalog filings into an initialized archive.

    The function only resumes a previously published archive. It reconstructs the
    exact RunSpec from the hash-verified head, stores all inventory bytes before
    candidate bytes, and commits full core-table replacements without changing
    parser extraction fields or touching legacy financial outputs.
    """
    requested_ids, protected = _validate_request(filing_ids, max_filings, protected_paths)
    root = Path(archive_root).expanduser()
    if not root.exists() or not root.is_dir():
        raise AcquisitionError("download_archive requires an existing initialized archive root")

    current_snapshot = read_snapshot(root)
    spec = _restore_spec(current_snapshot)
    acquired = unavailable = errors = 0
    blocked_ids: list[str] = []
    processed: list[str] = []
    skipped: list[str] = []
    selections: dict[str, str] = {}

    try:
        writer_context = open_archive(root, spec, protected_paths=protected, resume=True)
    except (ArchiveError, OSError) as exc:
        raise AcquisitionError(
            "archive could not be safely resumed with its persisted RunSpec"
        ) from exc

    with writer_context as writer:
        # Read after acquiring the exclusive writer lock so row updates use the current head.
        snapshot = read_snapshot(root)
        if snapshot.manifest.get("run_spec_sha256") != spec.sha256:
            raise ArchiveConflictError("published RunSpec changed during acquisition setup")
        filing_rows, document_rows = _core_tables(snapshot)
        documents_by_filing = _group_documents(document_rows)
        selected_rows, completed_explicit = _select_filings(
            filing_rows,
            documents_by_filing,
            requested_ids,
            max_filings,
        )
        skipped.extend(completed_explicit)

        raw_refs = {ref.path: ref for ref in snapshot.raw_objects}
        rows_by_id = {row["filing_id"]: row for row in filing_rows}
        document_rows_by_id = {row["document_id"]: row for row in document_rows}
        changed = False

        for original_row in selected_rows:
            filing_row = rows_by_id[original_row["filing_id"]]
            identity = filing_row["filing_id"]
            current_docs = documents_by_filing.get(identity, ())
            if _is_complete(filing_row, current_docs) or (
                requested_ids is None and not _needs_work(filing_row, current_docs)
            ):
                if (
                    requested_ids is not None
                    and identity not in skipped
                    and _is_complete(filing_row, current_docs)
                ):
                    skipped.append(identity)
                continue
            processed.append(identity)
            existing_docs = documents_by_filing.get(identity, [])

            # A partial acquisition refreshes both SEC inventories, reconciles identities
            # and selection against committed rows, then retries only missing selected bytes.
            has_index = any(
                row.get("role") == "filing_index" and row.get("fetch_status") == "present"
                for row in existing_docs
            )
            if filing_row.get("inventory_status") == "known" and has_index:
                _old_raw_coverage = filing_row["raw_coverage_status"]
                try:
                    plan = enumerate_documents(
                        filing_row["cik10"],
                        filing_row["accession_number"],
                        primary_document=filing_row["primary_document_name"] or "",
                        form=filing_row["form"],
                        client=client,
                    )
                except (SecClientError, FilingInventoryError) as exc:
                    state, diagnostic, ledger_status, _ = _inventory_failure(exc)
                    if state == "unavailable":
                        unavailable += 1
                    else:
                        errors += 1
                    filing_row["raw_coverage_status"] = (
                        "blocked" if state in {"blocked", "unavailable"} else "partial"
                    )
                    _record_attempt(writer, identity, ledger_status, diagnostic)
                    selections[identity] = _plan_status_from_rows(existing_docs)
                    if state in {"blocked", "unavailable"}:
                        blocked_ids.append(identity)
                    changed = True
                    continue

                _validate_inventory_response_continuity(existing_docs, plan)
                (
                    plan,
                    preloaded_responses,
                    preloaded_errors,
                    primary_response,
                ) = _prepare_primary_exhibit_refinement(
                    plan,
                    filing_row=filing_row,
                    existing_docs=existing_docs,
                    archive_root=root,
                    raw_refs=raw_refs,
                    client=client,
                )
                _validate_inventory_continuity(
                    identity,
                    existing_docs,
                    plan,
                    primary_response=primary_response,
                )
                refreshed_rows = _new_inventory_rows(identity, plan, writer, raw_refs)
                previous_scope_docs = _scope_document_snapshot(existing_docs)
                merged_docs = _merge_refreshed_inventory(
                    existing_docs,
                    refreshed_rows,
                    document_rows,
                    document_rows_by_id,
                )
                documents_by_filing[identity] = merged_docs
                filing_row["inventory_status"] = "known"
                _update_6k_scope(filing_row, plan, merged_docs, previous_docs=previous_scope_docs)
                selections[identity] = plan.selection_status

                nonfinancial = plan.selection_status == "non_financial"
                if nonfinancial:
                    for row in merged_docs:
                        if row["role"] != "filing_index":
                            if row["selection_status"] == "required":
                                raise AcquisitionConflictError(
                                    "refreshed non-financial plan would unselect a "
                                    "required document"
                                )
                            row["selection_status"] = "out_of_scope"
                            row["fetch_status"] = "not_requested"
                    filing_row["raw_coverage_status"] = "not_attempted"
                else:
                    doc_acquired, doc_unavailable, doc_errors, is_blocked = _fetch_selected_rows(
                        merged_docs,
                        filing_row=filing_row,
                        client=client,
                        writer=writer,
                        raw_refs=raw_refs,
                        preloaded_responses=preloaded_responses,
                        preloaded_errors=preloaded_errors,
                    )
                    acquired += doc_acquired
                    unavailable += doc_unavailable
                    errors += doc_errors
                    if is_blocked:
                        blocked_ids.append(identity)
                changed = True
                continue

            if existing_docs:
                raise AcquisitionConflictError(
                    "archive has document rows without a reusable committed filing inventory"
                )
            try:
                plan = enumerate_documents(
                    filing_row["cik10"],
                    filing_row["accession_number"],
                    primary_document=filing_row["primary_document_name"] or "",
                    form=filing_row["form"],
                    client=client,
                )
            except (SecClientError, FilingInventoryError) as exc:
                selections[identity] = (
                    "blocked" if isinstance(exc, FilingInventoryError) else "needs_review"
                )
                failure_state, _, _, _ = _inventory_failure(exc)
                if failure_state == "unavailable":
                    unavailable += 1
                else:
                    errors += 1
                _, is_blocked = _inventory_failure_row(filing_row, exc, writer)
                if is_blocked:
                    blocked_ids.append(identity)
                changed = True
                continue

            (
                plan,
                preloaded_responses,
                preloaded_errors,
                _,
            ) = _prepare_primary_exhibit_refinement(
                plan,
                filing_row=filing_row,
                existing_docs=(),
                archive_root=root,
                raw_refs=raw_refs,
                client=client,
            )
            new_rows = _new_inventory_rows(identity, plan, writer, raw_refs)
            for row in new_rows:
                if row["document_id"] in document_rows_by_id:
                    raise AcquisitionConflictError(
                        "new inventory conflicts with a pre-existing document identity"
                    )
                document_rows_by_id[row["document_id"]] = row
                document_rows.append(row)
                documents_by_filing.setdefault(identity, []).append(row)
            filing_row["inventory_status"] = "known"
            _update_6k_scope(filing_row, plan, documents_by_filing[identity])
            selections[identity] = plan.selection_status
            nonfinancial = plan.selection_status == "non_financial"
            if nonfinancial:
                for row in documents_by_filing[identity]:
                    if row["role"] != "filing_index":
                        row["selection_status"] = "out_of_scope"
                        row["fetch_status"] = "not_requested"
                filing_row["raw_coverage_status"] = "not_attempted"
            else:
                doc_acquired, doc_unavailable, doc_errors, is_blocked = _fetch_selected_rows(
                    documents_by_filing[identity],
                    filing_row=filing_row,
                    client=client,
                    writer=writer,
                    raw_refs=raw_refs,
                    preloaded_responses=preloaded_responses,
                    preloaded_errors=preloaded_errors,
                )
                acquired += doc_acquired
                unavailable += doc_unavailable
                errors += doc_errors
                if is_blocked:
                    blocked_ids.append(identity)
            changed = True

        if not changed:
            final_snapshot = snapshot
        else:
            filings_table = pa.Table.from_pylist(filing_rows, schema=FILINGS_SCHEMA)
            documents_table = pa.Table.from_pylist(document_rows, schema=DOCUMENTS_SCHEMA)
            final_snapshot = writer.commit(
                tables={"filings": filings_table, "documents": documents_table},
                raw_objects=tuple(raw_refs.values()),
                expected_manifest_version=snapshot.manifest_version,
            )

    return AcquisitionResult(
        snapshot=final_snapshot,
        processed_filing_ids=tuple(processed),
        skipped_completed_filing_ids=tuple(dict.fromkeys(skipped)),
        selection_statuses=selections,
        acquired_document_count=acquired,
        unavailable_document_count=unavailable,
        error_document_count=errors,
        blocked_filing_ids=tuple(dict.fromkeys(blocked_ids)),
    )
