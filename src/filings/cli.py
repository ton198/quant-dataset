"""CLI registration and dispatch for local, bounded filings workflows."""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from contextlib import nullcontext
from datetime import date
from pathlib import Path
from typing import Any

from .catalog import APPROVED_FORMS

_DATE_PATTERN = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z", re.ASCII)
_SEC_TAXONOMY_HOSTS = frozenset({"www.sec.gov", "data.sec.gov", "xbrl.sec.gov"})
_DEFAULT_TAXONOMY_HOSTS = frozenset(
    {
        "xbrl.fasb.org",
        "www.xbrl.org",
        "xbrl.ifrs.org",
        "www.w3.org",
        "taxonomies.xbrl.us",
    }
)
_APPROVED_TAXONOMY_HOSTS = _SEC_TAXONOMY_HOSTS | _DEFAULT_TAXONOMY_HOSTS


def _cik_arg(value: str) -> str:
    if re.fullmatch(r"[0-9]{1,10}", value, re.ASCII) is None:
        raise argparse.ArgumentTypeError("CIK must contain 1 to 10 ASCII digits")
    return value


def _date_arg(value: str) -> date:
    if _DATE_PATTERN.fullmatch(value) is None:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must be a valid YYYY-MM-DD date") from exc


def _query_limit_arg(value: str) -> int:
    try:
        limit = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("limit must be an integer from 1 through 1000") from exc
    if not 1 <= limit <= 1000:
        raise argparse.ArgumentTypeError("limit must be from 1 through 1000")
    return limit


def _max_filings_arg(value: str) -> int:
    try:
        maximum = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "max-filings must be an integer from 1 through 50"
        ) from exc
    if not 1 <= maximum <= 50:
        raise argparse.ArgumentTypeError("max-filings must be from 1 through 50")
    return maximum


def _workers_arg(value: str) -> int:
    try:
        workers = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("workers must be a positive integer") from exc
    if workers <= 0:
        raise argparse.ArgumentTypeError("workers must be a positive integer")
    return workers


def add_filings_parser(subparsers: Any) -> None:
    """Register local catalog, verification, query, and acquisition commands."""
    filings = subparsers.add_parser(
        "filings", help="catalog, verify, query, or download into a local filing archive"
    )
    commands = filings.add_subparsers(dest="filings_command", required=True)

    catalog = commands.add_parser(
        "catalog", help="catalog locally cached SEC submissions into an archive"
    )
    catalog.add_argument(
        "--archive", type=Path, required=True, help="explicit filing archive run root"
    )
    catalog.add_argument(
        "--cache-root",
        type=Path,
        required=True,
        help="local SEC financial cache directory containing manifest.json",
    )
    catalog.add_argument(
        "--cik",
        action="append",
        type=_cik_arg,
        required=True,
        metavar="DIGITS",
        help="issuer CIK; repeat to select multiple issuers",
    )
    catalog.add_argument(
        "--start", type=_date_arg, required=True, help="inclusive filing-date start (YYYY-MM-DD)"
    )
    catalog.add_argument(
        "--end", type=_date_arg, required=True, help="inclusive filing-date end (YYYY-MM-DD)"
    )
    catalog.add_argument(
        "--form",
        action="append",
        choices=sorted(APPROVED_FORMS),
        help=(
            "approved SEC form to include; repeat to narrow the catalog "
            "(default: all approved forms)"
        ),
    )
    catalog.add_argument(
        "--resume", action="store_true", help="resume only an archive with a matching RunSpec"
    )
    catalog.add_argument(
        "--allow-partial",
        action="store_true",
        help="explicitly allow referenced historical pages that are missing from the local cache",
    )

    verify = commands.add_parser("verify", help="verify an existing archive snapshot")
    verify.add_argument(
        "--archive", type=Path, required=True, help="existing filing archive run root"
    )

    query = commands.add_parser(
        "query",
        help="run one read-only SELECT over local archive tables (trusted local SQL)",
    )
    query.add_argument(
        "--archive", type=Path, required=True, help="existing filing archive run root"
    )
    query.add_argument("--sql", required=True, help="one SELECT or SELECT CTE to execute")
    query.add_argument(
        "--limit",
        type=_query_limit_arg,
        default=20,
        help="maximum output rows (1..1000; default: 20)",
    )

    download = commands.add_parser(
        "download", help="acquire a bounded number of documents into an existing archive"
    )
    download.add_argument(
        "--archive", type=Path, required=True, help="existing catalog archive run root"
    )
    download.add_argument(
        "--max-filings",
        type=_max_filings_arg,
        default=5,
        help="maximum filings to process (1..50; default: 5)",
    )
    download.add_argument(
        "--filing-id",
        action="append",
        metavar="ID",
        help=(
            "explicit filing identity to acquire; repeat to select multiple "
            "(default: bounded pending filings)"
        ),
    )
    download.add_argument(
        "--secrets",
        type=Path,
        help="TOML secrets file with [secrets].sec_user_agent (default: config/secrets.toml)",
    )

    extract = commands.add_parser(
        "extract-financials",
        help="extract selected eligible filing documents without publishing results",
    )
    extract.add_argument(
        "--archive", type=Path, required=True, help="existing verified filing archive"
    )
    extract.add_argument(
        "--work-dir",
        type=Path,
        required=True,
        help="private journal and result output directory",
    )
    extract.add_argument(
        "--config",
        type=Path,
        default=Path("config/extraction.toml"),
        help="provider and extraction TOML config",
    )
    extract.add_argument(
        "--secrets",
        type=Path,
        default=Path("config/secrets.toml"),
        help="TOML secrets file with [secrets].api_key (default: config/secrets.toml)",
    )
    extract.add_argument(
        "--filing-id",
        action="append",
        required=True,
        metavar="ID",
        help="frozen eligible filing ID; repeat up to 50 times",
    )
    extract.add_argument(
        "--max-request-bytes", type=int, help="override configured request-byte limit"
    )
    extract.add_argument(
        "--max-output-tokens", type=int, help="override configured output-token limit"
    )
    extract.add_argument(
        "--timeout-seconds", type=float, help="override configured provider timeout"
    )
    extract.add_argument(
        "--workers",
        type=_workers_arg,
        help="override configured local processing worker count",
    )
    extract.add_argument(
        "--model-workers",
        type=_workers_arg,
        dest="model_workers",
        help="override configured concurrent model request count",
    )
    extract.add_argument(
        "--prefetch-windows",
        type=_workers_arg,
        dest="prefetch_windows",
        help="override configured window prefetch bound",
    )
    extract.add_argument(
        "--no-progress",
        action="store_true",
        help="disable live progress output on stderr",
    )

    process = commands.add_parser(
        "parse",
        help="parse bounded documents in an existing archive (offline by default)",
    )
    process.add_argument(
        "--archive", type=Path, required=True, help="existing published filing archive run root"
    )
    process.add_argument(
        "--workspace-root",
        type=Path,
        required=True,
        help="existing private workspace directory outside archive and protected data roots",
    )
    process.add_argument(
        "--filing-id",
        action="append",
        metavar="ID",
        help="exact active filing identity to parse; repeat to select multiple",
    )
    process.add_argument(
        "--max-filings",
        type=_max_filings_arg,
        default=5,
        help="maximum filings to process (1..50; default: 5)",
    )
    process.add_argument(
        "--prepare-dependencies",
        action="store_true",
        help="explicitly allow bounded taxonomy fetches before Arelle's offline parse",
    )
    process.add_argument(
        "--taxonomy-host",
        action="append",
        choices=sorted(_APPROVED_TAXONOMY_HOSTS),
        metavar="HOST",
        help=(
            "narrow the approved taxonomy host set; repeat to allow several "
            "(requires --prepare-dependencies)"
        ),
    )
    process.add_argument(
        "--secrets",
        type=Path,
        help=(
            "SEC contact TOML used only with --prepare-dependencies (default: config/secrets.toml)"
        ),
    )

def _run_extract_financials(args: argparse.Namespace) -> int:
    import os

    from financial_extraction.domain import ExtractionLimits, ExtractionTask
    from financial_extraction.domain.validation import canonical_json
    from financial_extraction.runtime import (
        ExtractionConfigError,
        OpenAICompatibleClient,
        load_extraction_config,
    )

    from .extraction import FilingExtractionError, extract_selected_filings

    if not 1 <= len(args.filing_id) <= 50 or len(set(args.filing_id)) != len(args.filing_id):
        print("error: provide 1..50 unique frozen --filing-id values", file=sys.stderr)
        return 2
    try:
        config = load_extraction_config(args.config, secrets_path=args.secrets)
    except ExtractionConfigError as exc:
        print(f"error: extraction config: {exc}", file=sys.stderr)
        return 2
    timeout = args.timeout_seconds if args.timeout_seconds is not None else config.timeout_seconds
    max_request_bytes = (
        args.max_request_bytes
        if args.max_request_bytes is not None
        else config.limits.max_request_bytes
    )
    max_output_tokens = (
        args.max_output_tokens
        if args.max_output_tokens is not None
        else config.limits.max_output_tokens
    )
    workers = args.workers if args.workers is not None else config.workers
    model_workers = args.model_workers if args.model_workers is not None else config.model_workers
    prefetch_windows = args.prefetch_windows if args.prefetch_windows is not None else config.prefetch_windows
    if (
        timeout <= 0
        or max_request_bytes <= 0
        or max_output_tokens <= 0
        or workers <= 0
        or model_workers <= 0
        or prefetch_windows <= 0
    ):
        print(
            "error: timeout, request/output limits, and workers/model-workers/prefetch-windows must be positive",
            file=sys.stderr,
        )
        return 2
    if model_workers > workers or model_workers > prefetch_windows:
        print(
            "error: model-workers must not exceed workers or prefetch-windows",
            file=sys.stderr,
        )
        return 2
    try:
        from .extraction_progress import ExtractionProgressReporter

        reporter_context = (
            nullcontext(None) if args.no_progress else ExtractionProgressReporter()
        )
        args.work_dir.mkdir(parents=True, exist_ok=True)
        output_path = args.work_dir / "result.json"
        if output_path.is_symlink():
            raise FilingExtractionError("result.json must not be a symlink")
        with OpenAICompatibleClient(
            config.base_url,
            config.model,
            config.api_key,
            timeout_seconds=timeout,
            structured_mode=config.structured_mode,
        ) as client:
            with reporter_context as reporter:
                run = extract_selected_filings(
                    args.archive,
                    tuple(args.filing_id),
                    client=client,
                    task=ExtractionTask(numeric_policies=config.numeric_policies),
                    limits=ExtractionLimits(max_request_bytes, max_output_tokens),
                    work_dir=args.work_dir,
                    workers=workers,
                    model_workers=model_workers,
                    prefetch_windows=prefetch_windows,
                    on_progress=reporter,
                )
            if reporter is not None:
                reporter.result_writing()
            temporary_path = args.work_dir / ".result.json.tmp"
            with temporary_path.open("w", encoding="utf-8") as stream:
                stream.write(canonical_json(run) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, output_path)
            if reporter is not None:
                reporter.finish(run.status)
    except (FilingExtractionError, OSError, ValueError, RuntimeError) as exc:
        print(f"error: financial extraction failed: {exc}", file=sys.stderr)
        return 1
    _emit_json(
        {
            "command": "filings extract-financials",
            "run_id": run.run_id,
            "status": run.status,
            "filing_count": len(args.filing_id),
            "window_count": len(run.windows),
            "record_count": len(run.records),
            "problem_count": len(run.problems),
            "publishable": run.publishable,
            "result_path": str(output_path),
            "work_dir": str(args.work_dir),
        }
    )
    return 1 if run.status != "complete" else 0


def _emit_json(value: dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def _run_catalog(args: argparse.Namespace) -> int:
    from .workflow import FilingWorkflowError, run_catalog

    try:
        result = run_catalog(
            archive=args.archive,
            cache_root=args.cache_root,
            ciks=args.cik,
            start_date=args.start,
            end_date=args.end,
            forms=args.form,
            resume=args.resume,
            allow_partial=args.allow_partial,
        )
    except (FilingWorkflowError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    _emit_json(
        {
            "command": "filings catalog",
            "archive_path": str(result.snapshot.root),
            "snapshot_id": result.snapshot.snapshot_id,
            "manifest_version": result.snapshot.manifest_version,
            "input_fingerprint": result.snapshot.manifest["input_fingerprint"],
            "catalog_status": (
                "partial_catalog_only" if result.input_status == "partial" else "catalog_only"
            ),
            "input_status": result.input_status,
            "requested_ciks": list(result.requested_ciks),
            "forms": list(result.forms),
            "filed_date_start_inclusive": result.start_date.isoformat(),
            "filed_date_end_inclusive": result.end_date.isoformat(),
            "filing_count": result.filing_count,
            "verified_resource_count": result.verified_resource_count,
            "active_raw_object_count": result.active_raw_object_count,
            "diagnostics": list(result.diagnostics),
            "idempotent": result.idempotent,
            "coverage_claim": (
                "catalog metadata only; no filing-document inventory or bytes verified"
            ),
        }
    )
    return 0


def _run_verify(args: argparse.Namespace) -> int:
    from .archive import ArchiveError
    from .verify import verify_archive

    try:
        report = verify_archive(args.archive)
    except (ArchiveError, OSError, ValueError) as exc:
        print(f"error: archive verification failed: {exc}", file=sys.stderr)
        return 1
    _emit_json(
        {
            "command": "filings verify",
            "archive_path": report.archive_path,
            "run_id": report.run_id,
            "snapshot_id": report.snapshot_id,
            "manifest_version": report.manifest_version,
            "input_fingerprint": report.input_fingerprint,
            "integrity_status": report.integrity_status,
            "coverage_claim": report.coverage_claim,
            "table_row_counts": report.table_row_counts,
            "raw_object_count": report.raw_object_count,
            "scope_provenance": report.scope_provenance,
            "coverage_provenance": report.coverage_provenance,
        }
    )
    return 0


def _run_query(args: argparse.Namespace) -> int:
    from .query import FilingQueryError, query_archive

    try:
        result = query_archive(args.archive, args.sql, limit=args.limit)
    except (FilingQueryError, OSError) as exc:
        print(f"error: archive query failed: {exc}", file=sys.stderr)
        return 1

    writer = csv.writer(sys.stdout)
    writer.writerow(result.columns)
    writer.writerows(result.rows)
    print(
        f"archive query metadata: format={result.format} "
        f"manifest_version={result.manifest_version} rows={len(result.rows)}",
        file=sys.stderr,
    )
    return 0


def _run_download(args: argparse.Namespace) -> int:
    if args.filing_id is not None:
        if len(set(args.filing_id)) != len(args.filing_id):
            print("error: --filing-id values must be unique", file=sys.stderr)
            return 1
        if len(args.filing_id) > args.max_filings:
            print("error: explicit --filing-id values exceed --max-filings", file=sys.stderr)
            return 1

    from .acquisition import AcquisitionError, download_archive, load_archive_runspec
    from .archive import ArchiveError
    from .config import FilingConfigError, load_sec_user_agent
    from .sec_client import SecClient
    from .workflow import protected_archive_paths

    secrets_path = args.secrets or (Path.cwd() / "config" / "secrets.toml")
    try:
        # Verify the existing snapshot before loading credentials or creating a client.
        persisted_spec = load_archive_runspec(args.archive)
        user_agent = load_sec_user_agent(secrets_path)
        client = SecClient(user_agent=user_agent)
        protected_paths = list(protected_archive_paths(Path("data/raw/sec/financials")))
        protected_paths.append(secrets_path)
        result = download_archive(
            args.archive,
            client=client,
            protected_paths=tuple(protected_paths),
            filing_ids=args.filing_id,
            max_filings=args.max_filings,
        )
    except (AcquisitionError, ArchiveError, FilingConfigError, OSError) as exc:
        print(f"error: filings download failed: {exc}", file=sys.stderr)
        return 1

    needs_review = sorted(
        identity
        for identity, status in result.selection_statuses.items()
        if status == "needs_review"
    )
    failed = bool(result.blocked_filing_ids) or bool(
        result.unavailable_document_count or result.error_document_count
    )
    if failed:
        status = "partial"
    elif needs_review:
        status = "needs_review"
    else:
        status = "completed"
    _emit_json(
        {
            "command": "filings download",
            "status": status,
            "archive_path": str(result.snapshot.root),
            "snapshot_id": result.snapshot.snapshot_id,
            "manifest_version": result.snapshot.manifest_version,
            "approved_forms": list(persisted_spec.approved_forms),
            "max_filings": args.max_filings,
            "requested_filing_ids": list(args.filing_id) if args.filing_id else None,
            "processed_filing_ids": list(result.processed_filing_ids),
            "skipped_completed_filing_ids": list(result.skipped_completed_filing_ids),
            "selection_statuses": dict(result.selection_statuses),
            "needs_review_filing_ids": needs_review,
            "acquired_document_count": result.acquired_document_count,
            "unavailable_document_count": result.unavailable_document_count,
            "error_document_count": result.error_document_count,
            "blocked_filing_ids": list(result.blocked_filing_ids),
            "coverage_claim": (
                "bounded SEC acquisition statuses only; parser extraction, broad completeness, "
                "and financial integrity are not assessed"
            ),
        }
    )
    return 1 if failed else 0


def _processing_diagnostic_codes(result: Any) -> tuple[list[str], int]:
    attempted = set(result.parse_statuses)
    table = result.snapshot.tables.get("parses")
    if not attempted or table is None or not hasattr(table, "to_pylist"):
        return [], 0
    counts: dict[str, int] = {}
    for row in table.to_pylist():
        key = f"{row.get('document_id')}:{row.get('parser_name')}"
        if key not in attempted:
            continue
        try:
            diagnostics = json.loads(row.get("errors_json", "[]"))
        except (TypeError, json.JSONDecodeError):
            diagnostics = [{"code": "diagnostic_unavailable"}]
        if not isinstance(diagnostics, list):
            diagnostics = [{"code": "diagnostic_unavailable"}]
        for diagnostic in diagnostics:
            code = diagnostic.get("code") if isinstance(diagnostic, dict) else None
            if isinstance(code, str) and code:
                counts[code] = counts.get(code, 0) + 1
    ordered = sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))
    return [code for code, _count in ordered[:20]], max(0, len(ordered) - 20)


def _run_parse(args: argparse.Namespace) -> int:
    if args.taxonomy_host and not args.prepare_dependencies:
        print("error: --taxonomy-host requires --prepare-dependencies", file=sys.stderr)
        return 2
    if args.secrets is not None and not args.prepare_dependencies:
        print("error: --secrets is only used with --prepare-dependencies", file=sys.stderr)
        return 2
    if args.filing_id is not None:
        if len(set(args.filing_id)) != len(args.filing_id):
            print("error: --filing-id values must be unique", file=sys.stderr)
            return 1
        if len(args.filing_id) > args.max_filings:
            print("error: explicit --filing-id values exceed --max-filings", file=sys.stderr)
            return 1

    # Fail on a missing/corrupt archive before importing parser code or consulting
    # credentials. The processing API repeats this check under the archive lock.
    from .acquisition import load_archive_runspec
    from .archive import ArchiveError

    try:
        load_archive_runspec(args.archive)
    except (ArchiveError, OSError, ValueError) as exc:
        print(f"error: filings parse requires an existing verified archive: {exc}", file=sys.stderr)
        return 1

    from .processing import ProcessingError, parse_archive
    from .workflow import protected_archive_paths

    secrets_path = args.secrets or (Path.cwd() / "config" / "secrets.toml")
    protected = list(protected_archive_paths(Path("data/raw/sec/financials")))
    if args.prepare_dependencies:
        protected.append(secrets_path)

    selected_taxonomy_hosts = set(args.taxonomy_host or _DEFAULT_TAXONOMY_HOSTS)
    allowed_hosts = tuple(sorted(_SEC_TAXONOMY_HOSTS | selected_taxonomy_hosts))
    config_errors: list[str] = []
    client_holder: list[Any] = []
    fetch_dependencies = None
    if args.prepare_dependencies:

        def fetch_taxonomy(url: str) -> Any:
            if not client_holder:
                from .config import FilingConfigError, load_sec_user_agent
                from .sec_client import TaxonomyClient

                try:
                    user_agent = load_sec_user_agent(secrets_path)
                    client_holder.append(
                        TaxonomyClient(user_agent=user_agent, allowed_hosts=allowed_hosts)
                    )
                except FilingConfigError as exc:
                    config_errors.append(str(exc))
                    raise ProcessingError(str(exc)) from exc
            return client_holder[0].fetch_taxonomy(url)

        fetch_dependencies = fetch_taxonomy

    try:
        result = parse_archive(
            args.archive,
            protected_paths=tuple(protected),
            workspace_root=args.workspace_root,
            filing_ids=args.filing_id,
            max_filings=args.max_filings,
            fetch_dependencies=fetch_dependencies,
            allowed_taxonomy_hosts=allowed_hosts if args.prepare_dependencies else (),
        )
    except (ProcessingError, ArchiveError, OSError, ValueError) as exc:
        print(f"error: filings parse failed: {exc}", file=sys.stderr)
        return 1

    diagnostic_codes, additional_codes = _processing_diagnostic_codes(result)
    partial = (
        result.partial_parse_count > 0
        or result.failed_parse_count > 0
        or any(status in {"partial", "failed"} for status in result.filing_statuses.values())
    )
    if partial:
        status = "partial"
    elif result.unsupported_parse_count:
        status = "completed_with_unsupported"
    else:
        status = "completed"
    report: dict[str, Any] = {
        "command": "filings parse",
        "status": status,
        "archive_path": str(result.snapshot.root),
        "snapshot_id": result.snapshot.snapshot_id,
        "manifest_version": result.snapshot.manifest_version,
        "max_filings": args.max_filings,
        "requested_filing_ids": list(args.filing_id) if args.filing_id else None,
        "selected_filing_ids": list(result.selected_filing_ids),
        "processed_filing_ids": list(result.processed_filing_ids),
        "skipped_filing_ids": list(result.skipped_filing_ids),
        "filing_statuses": dict(result.filing_statuses),
        "fact_rows_written": result.fact_rows_written,
        "section_rows_written": result.section_rows_written,
        "dependency_rows_written": result.dependency_rows_written,
        "full_parse_count": result.full_parse_count,
        "partial_parse_count": result.partial_parse_count,
        "unsupported_parse_count": result.unsupported_parse_count,
        "failed_parse_count": result.failed_parse_count,
        "diagnostic_codes": diagnostic_codes,
        "additional_diagnostic_code_count": additional_codes,
        "dependencies_prepared": bool(args.prepare_dependencies),
        "coverage_claim": (
            "parser attempt statuses only; not financial-statement correctness, "
            "broad filing completeness, or SEC-wide coverage"
        ),
    }
    if config_errors:
        report["configuration_error"] = config_errors[0]
    if "arelle_dependency_missing" in diagnostic_codes:
        report["install_hint"] = (
            "Install optional parser support with `uv sync --frozen --extra filings`."
        )
    _emit_json(report)
    if config_errors:
        print(f"error: taxonomy preparation configuration: {config_errors[0]}", file=sys.stderr)
    return 1 if partial or config_errors else 0


def run_filings_command(args: argparse.Namespace) -> int:
    """Dispatch a parsed filings command without importing parser backends."""
    if args.filings_command == "catalog":
        return _run_catalog(args)
    if args.filings_command == "verify":
        return _run_verify(args)
    if args.filings_command == "query":
        return _run_query(args)
    if args.filings_command == "download":
        return _run_download(args)
    if args.filings_command == "parse":
        return _run_parse(args)
    if args.filings_command == "extract-financials":
        return _run_extract_financials(args)
    return 2
