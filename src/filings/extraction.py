"""Explicit, non-publishing extraction over a frozen list of verified filing IDs."""

from __future__ import annotations
from collections.abc import Callable
from pathlib import Path
from typing import Any

from financial_extraction.domain import ExtractionLimits, ExtractionTask
from financial_extraction.runtime import ModelClient
from financial_extraction.workflow import run_extraction
from financial_extraction.workflow.progress import ExtractionProgress, ProgressCallback

from .adapters.financial_extraction import HostSourceBinding, bind_financial_extraction_source
from .archive import read_snapshot
from .packages import _entry_rows, _read_rows, _safe_source_path


class FilingExtractionError(ValueError):
    """The frozen filing manifest or verified source is not eligible."""


_PARSER_LIMITS = {
    "maxpayloadbytes": 20_000_000,
    "maxnodes": 500_000,
    "maxcells": 500_000,
    "maxgridspan": 1_000_000,
}


def _emit_progress(callback: ProgressCallback | None, event: ExtractionProgress) -> None:
    if callback is not None:
        try:
            callback(event)
        except Exception:
            pass

def extract_selected_filings(
    archive_root: Path,
    filing_ids: tuple[str, ...],
    *,
    client: ModelClient,
    task: ExtractionTask,
    limits: ExtractionLimits,
    work_dir: Path,
    workers: int = 4,
    model_workers: int = 1,
    prefetch_windows: int = 4,
    on_progress: ProgressCallback | None = None,
    on_window_complete: Callable[..., Any] | None = None,
) -> Any:
    if not isinstance(filing_ids, tuple) or not filing_ids or len(filing_ids) > 50:
        raise FilingExtractionError("explicit filing_ids must contain between 1 and 50 items")
    if not isinstance(workers, int) or isinstance(workers, bool) or workers <= 0:
        raise FilingExtractionError("workers must be a positive integer")
    if not isinstance(model_workers, int) or isinstance(model_workers, bool) or model_workers <= 0:
        raise FilingExtractionError("model_workers must be a positive integer")
    if not isinstance(prefetch_windows, int) or isinstance(prefetch_windows, bool) or prefetch_windows <= 0:
        raise FilingExtractionError("prefetch_windows must be a positive integer")
    if model_workers > workers or model_workers > prefetch_windows:
        raise FilingExtractionError("model_workers must not exceed workers or prefetch_windows")
    if len(set(filing_ids)) != len(filing_ids):
        raise FilingExtractionError("filing_ids must be unique")
    _emit_progress(on_progress, ExtractionProgress("archive_started", total=len(filing_ids)))
    snapshot = read_snapshot(Path(archive_root))
    filings, documents = _read_rows(snapshot)
    _emit_progress(on_progress, ExtractionProgress("sources_started", total=len(filing_ids)))
    refs = {ref.path: ref for ref in snapshot.raw_objects}
    prepared = []
    for index, identity in enumerate(filing_ids, 1):
        _emit_progress(
            on_progress,
            ExtractionProgress("source_started", identity, index, len(filing_ids)),
        )
        filing, rows = _entry_rows(identity, filings, documents, refs)
        if filing["scope_status"] != "included":
            raise FilingExtractionError(f"filing {identity} is not included")
        if filing["raw_coverage_status"] != "scoped_complete":
            raise FilingExtractionError(f"filing {identity} does not have complete raw coverage")
        required = [
            row
            for row in rows
            if row["selection_status"] == "required"
            and row["role"] in {"primary", "xbrl_instance", "schema", "linkbase", "exhibit"}
        ]
        if any(row["fetch_status"] != "present" for row in required):
            raise FilingExtractionError(f"filing {identity} has missing required source documents")
        primary = [
            row
            for row in required
            if row["role"] == "primary" and row["fetch_status"] == "present"
        ]
        if len(primary) != 1:
            raise FilingExtractionError(
                f"filing {identity} does not have exactly one present primary document"
            )
        row = primary[0]
        raw_ref = refs.get(row["raw_path"])
        if raw_ref is None:
            raise FilingExtractionError(f"filing {identity} primary raw ref is not verified")
        raw_path = _safe_source_path(snapshot.root, raw_ref.path)
        raw_bytes = raw_path.read_bytes()
        if len(raw_bytes) > _PARSER_LIMITS["maxpayloadbytes"]:
            raise FilingExtractionError("primary document exceeds 20 MB technical limit")
        binding = HostSourceBinding(
            snapshot_id=snapshot.snapshot_id,
            filing_id=identity,
            document_id=row["document_id"],
            source_url=row["source_url"],
            scope_status=filing["scope_status"],
            numeric_parser_status=row["fact_extraction_status"],
            raw_expected_sha256=row["raw_sha256"],
            raw_expected_bytes=row["byte_size"],
            cik=filing["cik10"],
            accession=filing["accession_number"],
        )
        decision = bind_financial_extraction_source(
            raw_bytes,
            binding,
            max_source_bytes=_PARSER_LIMITS["maxpayloadbytes"],
            source_type="sec_html",
            plain_html_encoding="utf-8",
        )
        if decision.status != "bound" or decision.document is None:
            raise FilingExtractionError(
                f"filing {identity} primary is not extraction-eligible: {decision.reasons}"
            )
        document = decision.document
        prepared.append(
            type(document)(
                source_id=document.source_id,
                document_id=document.document_id,
                source_type=document.source_type,
                fixture_only=document.fixture_only,
                payload=document.payload,
                encoding=document.encoding,
                raw_sha256=document.raw_sha256,
                raw_bytes=document.raw_bytes,
                payload_sha256=document.payload_sha256,
                preprocessing_revision=document.preprocessing_revision,
                origin=document.origin,
                limits=_PARSER_LIMITS,
            )
        )
        _emit_progress(
            on_progress,
            ExtractionProgress("source_prepared", identity, index, len(filing_ids)),
        )
    protected = (Path(archive_root).resolve(),)
    return run_extraction(
        prepared,
        client=client,
        task=task,
        limits=limits,
        work_dir=work_dir,
        protected_paths=protected,
        workers=workers,
        model_workers=model_workers,
        prefetch_windows=prefetch_windows,
        on_progress=on_progress,
        on_window_complete=on_window_complete,
    )
