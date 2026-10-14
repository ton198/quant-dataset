"""Build and validate cleaned SEC filing report samples."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import time
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from .catalog import scan_cached_filings
from .content_parse import parse_content
from .document_selection import enumerate_documents, refine_document_plan
from .download import fetch_document
from .models import filing_id
from .sec_client import FetchResponse, SecClient, SecClientError, SecNotFoundError, validate_sec_url

logger = logging.getLogger(__name__)

BLOCK_TYPES = {"financial_statement", "note", "other_context", "unknown"}
_SCHEMA = pa.schema(
    [
        pa.field("sample_id", pa.string(), nullable=False),
        pa.field("entity_id", pa.string(), nullable=True),
        pa.field("filed_date", pa.date32(), nullable=True),
        pa.field(
            "blocks",
            pa.list_(
                pa.struct(
                    [
                        pa.field("order_index", pa.int32(), nullable=False),
                        pa.field("block_type", pa.string(), nullable=False),
                        pa.field("text", pa.string(), nullable=False),
                    ]
                )
            ),
            nullable=False,
        ),
    ]
)
_NUMBER = re.compile(r"(?<![A-Za-z0-9])(?:\(?\$?\s*[+-]?\s*\d[\d,]*(?:\.\d+)?%?\)?)(?![A-Za-z0-9])")


class ReportSamplesError(ValueError):
    """Invalid report sample inputs, paths, or published results."""


def _numbers(text: str) -> list[str]:
    return [re.sub(r"\s+", "", token) for token in _NUMBER.findall(text)]


def _clean(text: str) -> str:
    text = text.translate(dict.fromkeys(map(ord, "\u200b\u200c\u200d\ufeff"), None))
    return "\n".join(" ".join(line.split()) for line in text.splitlines()).strip()


def _classify(text: str, kind: str) -> str:
    lower = text.casefold()
    if kind == "table" or any(
        term in lower
        for term in (
            "balance sheet",
            "income statement",
            "cash flow",
            "statement of operations",
            "statement of comprehensive",
        )
    ):
        return "financial_statement"
    if re.search(r"\bnote\s+\d+\b", lower) or lower.startswith("notes to"):
        return "note"
    if re.search(r"\b(?:item|part)\s+[0-9ivx]+\b", lower):
        return "other_context"
    return "unknown"


def _reject_symlink_components(path: Path) -> None:
    absolute = path.absolute()
    for component in (absolute, *absolute.parents):
        if component.is_symlink():
            raise ReportSamplesError(f"symlink paths are not allowed: {component}")


def _safe_paths(out: Path, work: Path, cache: Path) -> None:
    protected = [
        Path("data/raw").resolve(),
        Path("data/baselines").resolve(),
        Path("baselines").resolve(),
        Path("archive").resolve(),
    ]
    for candidate in (out, work):
        if candidate.is_symlink():
            raise ReportSamplesError(f"symlink paths are not allowed: {candidate}")
        resolved = candidate.resolve(strict=False)
        if (
            resolved in {Path.cwd().resolve(), cache.resolve()}
            or cache.resolve() in resolved.parents
        ):
            raise ReportSamplesError(f"path overlaps SEC cache: {candidate}")
        for root in protected:
            if root in resolved.parents or resolved in root.parents or resolved == root:
                raise ReportSamplesError(f"path overlaps protected data: {candidate}")
    if out.resolve(strict=False) == work.resolve(strict=False):
        raise ReportSamplesError("output and work directory must be distinct")


def _diagnostic_path(root: Path) -> Path:
    return root / "diagnostics.jsonl"


def _read_diagnostics(root: Path) -> list[dict[str, Any]]:
    path = _diagnostic_path(root)
    try:
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    except (OSError, ValueError) as exc:
        raise ReportSamplesError(f"diagnostics are missing or invalid: {exc}") from exc


def _collect_blocks(parsed: Any) -> tuple[list[dict[str, str]], list[str], list[dict[str, Any]]]:
    blocks: list[dict[str, str]] = []
    statuses: list[str] = []
    for document in parsed.documents:
        statuses.append(str(document.get("status", "unknown")))
    for block in parsed.blocks:
        original = str(block.get("text", ""))
        cleaned = _clean(original)
        if not cleaned:
            continue
        if _numbers(original) != _numbers(cleaned):
            raise ReportSamplesError("cleaning changed numeric token sequence")
        blocks.append(
            {"block_type": _classify(cleaned, str(block.get("block_kind", ""))), "text": cleaned}
        )
    return blocks, statuses, parsed.documents


def _commit_filing(
    root: Path,
    identity: str,
    sample: dict[str, Any] | None,
    diagnostic: dict[str, Any],
) -> None:
    path = root / f"{hashlib.sha256(identity.encode()).hexdigest()}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {"sample": sample, "diagnostic": diagnostic},
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(payload + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _filing_worker(
    filing: Any,
    user_agent: str,
    work_root: Path,
    *,
    client: SecClient | None = None,
    committed_root: Path,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    identity = filing_id(filing.cik10, filing.accession_number)
    raw_dir = Path(tempfile.mkdtemp(prefix="filing-", dir=work_root))
    diagnostic: dict[str, Any] = {
        "filing_id": identity,
        "sample_id": identity,
        "status": "failed",
        "reason": None,
        "sources": [],
        "parser_status": [],
        "content_status": "unknown",
        "degradation": [],
        "coverage_status": "unknown",
        "block_count": 0,
        "table_count": 0,
    }
    sec = client or SecClient(user_agent)
    sample: dict[str, Any] | None = None
    try:
        responses: list[tuple[str, str, FetchResponse]] = []
        plan = None
        primary = None
        if not filing.primary_document:
            accession_digits = filing.accession_number.replace("-", "")
            full_url = validate_sec_url(
                f"https://www.sec.gov/Archives/edgar/data/{int(filing.cik10)}/"
                f"{accession_digits}/{filing.accession_number}.txt"
            )
            response = sec.get(full_url)
            responses.append((f"{filing.accession_number}.txt", "full_submission", response))
        else:
            plan = enumerate_documents(
                filing.cik10,
                filing.accession_number,
                primary_document=filing.primary_document,
                form=filing.form,
                client=sec,
            )
            primary = next(
                candidate for candidate in plan.candidates if candidate.role == "primary"
            )
            try:
                primary_response = fetch_document(primary, sec)
            except SecNotFoundError:
                accession_digits = filing.accession_number.replace("-", "")
                full_url = validate_sec_url(
                    f"https://www.sec.gov/Archives/edgar/data/{int(filing.cik10)}/"
                    f"{accession_digits}/{filing.accession_number}.txt"
                )
                primary_response = sec.get(full_url)
                diagnostic["degradation"].append("primary_missing_full_submission_fallback")
                responses.append(
                    (f"{filing.accession_number}.txt", "full_submission", primary_response)
                )
            else:
                responses.append((primary.filename, primary.role, primary_response))
                if (
                    filing.form in {"40-F", "40-F/A"}
                    or plan.selection_status != "selected_financial"
                ):
                    plan = refine_document_plan(plan, primary_response=primary_response)
        for candidate in () if plan is None else plan.candidates:
            if candidate is primary or not candidate.selected:
                continue
            if candidate.role not in {"exhibit", "xbrl_instance", "schema", "linkbase"}:
                continue
            try:
                responses.append(
                    (candidate.filename, candidate.role, fetch_document(candidate, sec))
                )
            except SecClientError as exc:
                diagnostic["degradation"].append(
                    f"selected_document_fetch_failed:{candidate.filename}:{type(exc).__name__}"
                )
        if plan is not None and plan.selection_status != "selected_financial":
            diagnostic["coverage_status"] = "partial"
            diagnostic["degradation"].extend(plan.reasons)
        parsed_blocks: list[dict[str, str]] = []
        for filename, role, response in responses:
            source_path = raw_dir / Path(filename).name
            source_path.write_bytes(response.body)
            raw_bytes = source_path.read_bytes()
            digest = hashlib.sha256(raw_bytes).hexdigest()
            if digest != response.sha256 or len(raw_bytes) != len(response.body):
                raise ReportSamplesError("source byte hash or size validation failed")
            diagnostic["sources"].append(
                {"name": filename, "role": role, "sha256": digest, "byte_size": len(raw_bytes)}
            )
            document = {
                "document_id": hashlib.sha256(f"{identity}\\0{filename}".encode()).hexdigest(),
                "role": role,
                "filename": filename,
                "sha256": digest,
            }
            result = parse_content(
                raw_bytes,
                filing={
                    "cik10": filing.cik10,
                    "accession_number": filing.accession_number,
                    "form": filing.form,
                    "filed_date": filing.filed_date,
                    "report_date": filing.report_date,
                },
                document=document,
            )
            clean_blocks, statuses, docs = _collect_blocks(result)
            parsed_blocks.extend(clean_blocks)
            diagnostic["parser_status"].extend(statuses)
            if any(status in {"partial", "failed", "unsupported"} for status in statuses):
                diagnostic["content_status"] = "partial"
            elif statuses and diagnostic["content_status"] == "unknown":
                diagnostic["content_status"] = "full"
            diagnostic["table_count"] += sum(int(doc.get("table_count", 0)) for doc in docs)
        if diagnostic["degradation"] and diagnostic["content_status"] == "full":
            diagnostic["content_status"] = "partial"
        if parsed_blocks:
            sample = {
                "sample_id": identity,
                "entity_id": filing.cik10,
                "filed_date": date.fromisoformat(filing.filed_date),
                "blocks": [
                    {"order_index": index, **block} for index, block in enumerate(parsed_blocks)
                ],
            }
            diagnostic["status"] = "emitted"
            diagnostic["block_count"] = len(parsed_blocks)
            if diagnostic["coverage_status"] == "unknown":
                diagnostic["coverage_status"] = (
                    "complete"
                    if plan is not None
                    and plan.selection_status == "selected_financial"
                    and diagnostic["content_status"] == "full"
                    and not diagnostic["degradation"]
                    else "partial"
                )
        else:
            diagnostic["status"] = "skipped"
            diagnostic["reason"] = "no extractable report body"
        _commit_filing(committed_root, identity, sample, diagnostic)
        return sample, diagnostic
    except Exception as exc:
        diagnostic["status"] = "failed"
        diagnostic["reason"] = f"{type(exc).__name__}: {exc}"
        try:
            _commit_filing(committed_root, identity, None, diagnostic)
        except OSError:
            pass
        return None, diagnostic
    finally:
        shutil.rmtree(raw_dir, ignore_errors=True)


def _write_shards(
    root: Path,
    rows: list[dict[str, Any]],
    batch_size: int = 500,
    *,
    start_index: int = 0,
) -> int:
    by_year: dict[int | str, list[dict[str, Any]]] = {}
    for row in rows:
        filed_date = row["filed_date"]
        year = filed_date.year if filed_date else "unknown"
        by_year.setdefault(year, []).append(row)
    next_index = start_index
    for year, year_rows in sorted(by_year.items(), key=lambda item: str(item[0])):
        shard_dir = root / f"year={year}"
        shard_dir.mkdir(parents=True, exist_ok=True)
        for offset in range(0, len(year_rows), batch_size):
            batch = year_rows[offset : offset + batch_size]
            path = shard_dir / f"part-{next_index:05d}.parquet"
            table = pa.Table.from_pylist(batch, schema=_SCHEMA)
            temporary = path.with_suffix(".parquet.tmp")
            pq.write_table(table, temporary)
            os.replace(temporary, path)
            next_index += 1
    return next_index


def _all_rows(root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*.parquet")):
        rows.extend(pq.read_table(path).to_pylist())
    return rows


def build_report_samples(
    cache_root: Path = Path("data/raw/sec/financials"),
    out: Path = Path("data/financial_reports"),
    work_dir: Path | None = None,
    limit: int | None = None,
    filing_ids: Iterable[str] | None = None,
    workers: int = 4,
    resume: bool = False,
    user_agent: str | None = None,
    client: SecClient | None = None,
    forms: Iterable[str] = ("10-K", "10-K/A", "10-Q", "10-Q/A", "20-F", "20-F/A", "40-F", "40-F/A"),
) -> dict[str, Any]:
    """Build report-level Parquet samples from cached SEC filing identities."""
    if workers < 1:
        raise ReportSamplesError("workers must be at least 1")
    if limit is not None and limit < 1:
        raise ReportSamplesError("limit must be a positive integer")
    cache = Path(cache_root).resolve()
    output_arg = Path(out)
    work_arg = Path(work_dir) if work_dir is not None else None
    _reject_symlink_components(output_arg)
    if work_arg is not None:
        _reject_symlink_components(work_arg)
    output = output_arg.resolve(strict=False)
    work = (work_arg or (output.parent / f".{output.name}.work")).resolve(strict=False)
    _safe_paths(output, work, cache)
    if output.exists() and any(output.iterdir()):
        if not resume:
            raise ReportSamplesError("output path must be absent or empty")
        if not (_diagnostic_path(output).exists() and (output / "identity.json").exists()):
            raise ReportSamplesError("resume output lacks owned diagnostics or identity metadata")
    output.parent.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    _reject_symlink_components(work_arg or work)
    for orphan in work.glob("filing-*"):
        if orphan.is_dir():
            shutil.rmtree(orphan)
    scan = scan_cached_filings(cache, forms=tuple(forms))
    selected = list(scan.filings)
    if filing_ids:
        wanted = set(filing_ids)
        selected = [row for row in selected if filing_id(row.cik10, row.accession_number) in wanted]
        missing = wanted - {filing_id(row.cik10, row.accession_number) for row in selected}
        if missing:
            raise ReportSamplesError(
                f"filing ids are not in current cache identity list: {sorted(missing)}"
            )
    if limit is not None:
        selected = selected[:limit]
    if user_agent is None:
        try:
            import tomllib
        except ModuleNotFoundError:
            import tomli as tomllib
        try:
            values = tomllib.loads(Path("config/secrets.toml").read_text(encoding="utf-8"))[
                "secrets"
            ]
            user_agent = str(values["sec_user_agent"])
        except (OSError, KeyError, ValueError) as exc:
            raise ReportSamplesError(f"SEC contact configuration unavailable: {exc}") from exc
    wanted_ids = {filing_id(item.cik10, item.accession_number) for item in selected}
    committed = work / "committed"
    prior: dict[str, dict[str, Any]] = {}
    prior_checkpoints: list[Path] = []
    if resume and committed.exists():
        for checkpoint in committed.glob("*.json"):
            try:
                value = json.loads(checkpoint.read_text(encoding="utf-8"))
                diagnostic, sample = value["diagnostic"], value["sample"]
                identity = diagnostic.get("filing_id")
                if identity in wanted_ids and diagnostic.get("status") in {
                    "emitted",
                    "skipped",
                    "failed",
                }:
                    prior[identity] = diagnostic
                    if sample is not None:
                        prior_checkpoints.append(checkpoint)
            except (OSError, ValueError, KeyError, TypeError):
                checkpoint.unlink(missing_ok=True)
    done = set(prior)
    pending = (row for row in selected if filing_id(row.cik10, row.accession_number) not in done)
    logger.info(
        "Starting SEC report build: selected=%d cache_total=%d resumed=%d workers=%d",
        len(selected),
        len(scan.filings),
        len(prior),
        workers,
    )
    diagnostics_by_id = dict(prior)
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}.staging-", dir=output.parent))
    rows_written = 0
    try:
        shard_rows: list[dict[str, Any]] = []
        shard_number = 0
        rows_written = 0
        with (staging / "diagnostics.jsonl").open("w", encoding="utf-8") as diagnostic_file:
            for diagnostic in sorted(prior.values(), key=lambda item: item["filing_id"]):
                diagnostic_file.write(
                    json.dumps(diagnostic, ensure_ascii=False, sort_keys=True) + "\n"
                )
            for checkpoint in prior_checkpoints:
                sample = json.loads(checkpoint.read_text(encoding="utf-8"))["sample"]
                if isinstance(sample.get("filed_date"), str):
                    sample["filed_date"] = date.fromisoformat(sample["filed_date"])
                shard_rows.append(sample)
                rows_written += 1
                if len(shard_rows) >= 500:
                    shard_number = _write_shards(staging, shard_rows, start_index=shard_number)
                    shard_rows.clear()
            completed = len(prior)
            emitted = sum(item.get("status") == "emitted" for item in prior.values())
            skipped = sum(item.get("status") == "skipped" for item in prior.values())
            failed = sum(item.get("status") == "failed" for item in prior.values())
            started_at = time.monotonic()
            last_log_at = started_at
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {}
                for _ in range(workers):
                    try:
                        item = next(pending)
                    except StopIteration:
                        break
                    futures[
                        pool.submit(
                            _filing_worker,
                            item,
                            user_agent,
                            work,
                            client=client,
                            committed_root=committed,
                        )
                    ] = item
                while futures:
                    future = next(as_completed(futures))
                    item = futures.pop(future)
                    sample, diagnostic = future.result()
                    identity = filing_id(item.cik10, item.accession_number)
                    completed += 1
                    emitted += diagnostic.get("status") == "emitted"
                    skipped += diagnostic.get("status") == "skipped"
                    failed += diagnostic.get("status") == "failed"
                    if diagnostic.get("status") == "failed":
                        logger.warning(
                            "SEC report failed filing_id=%s reason=%s",
                            identity,
                            diagnostic.get("reason"),
                        )
                    now = time.monotonic()
                    elapsed = now - started_at
                    should_log = (
                        completed == len(selected)
                        or completed % 100 == 0
                        or now - last_log_at >= 30
                    )
                    if should_log:
                        rate = (completed - len(prior)) / elapsed * 3600 if elapsed else 0.0
                        remaining = max(0, len(selected) - completed)
                        eta_hours = remaining / rate if rate else float("inf")
                        logger.info(
                            "SEC report progress %d/%d emitted=%d skipped=%d failed=%d "
                            "rate=%.1f/hour eta=%.1fh last=%s",
                            completed,
                            len(selected),
                            emitted,
                            skipped,
                            failed,
                            rate,
                            eta_hours,
                            diagnostic["filing_id"],
                        )
                        last_log_at = now
                    diagnostics_by_id[identity] = diagnostic
                    diagnostic_file.write(
                        json.dumps(diagnostic, ensure_ascii=False, sort_keys=True) + "\n"
                    )
                    if sample is not None:
                        shard_rows.append(sample)
                        rows_written += 1
                        if len(shard_rows) >= 500:
                            shard_number = _write_shards(
                                staging, shard_rows, start_index=shard_number
                            )
                            shard_rows.clear()
                    try:
                        next_item = next(pending)
                    except StopIteration:
                        continue
                    futures[
                        pool.submit(
                            _filing_worker,
                            next_item,
                            user_agent,
                            work,
                            client=client,
                            committed_root=committed,
                        )
                    ] = next_item
        if shard_rows:
            _write_shards(staging, shard_rows, start_index=shard_number)
        (staging / "identity.json").write_text(
            json.dumps(sorted(wanted_ids), ensure_ascii=False), encoding="utf-8"
        )
        validate_report_samples(staging, cache_root=cache, expected_ids=wanted_ids)
        if output.exists():
            if any(output.iterdir()):
                if not resume or not (
                    _diagnostic_path(output).exists() and (output / "identity.json").exists()
                ):
                    raise ReportSamplesError("refusing to replace non-empty report output")
                backup = output.with_name(f".{output.name}.previous-{os.getpid()}")
                if backup.exists():
                    raise ReportSamplesError(f"resume backup path already exists: {backup}")
                os.replace(output, backup)
                try:
                    os.replace(staging, output)
                except Exception:
                    os.replace(backup, output)
                    raise
                shutil.rmtree(backup)
            else:
                output.rmdir()
                os.replace(staging, output)
        else:
            os.replace(staging, output)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return {
        "output": str(output),
        "filings": len(selected),
        "samples": rows_written,
        "diagnostics": len(diagnostics_by_id),
    }


def validate_report_samples(
    path: Path = Path("data/financial_reports"),
    *,
    cache_root: Path = Path("data/raw/sec/financials"),
    expected_ids: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Validate exact nested Parquet schema, identity coverage, and per-filing diagnostics."""
    root = Path(path).resolve()
    try:
        diagnostics = _read_diagnostics(root)
        identity_file = json.loads((root / "identity.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReportSamplesError(f"report sample metadata is missing or unreadable: {exc}") from exc
    identities = set(expected_ids) if expected_ids is not None else set(identity_file)
    shards = sorted(root.rglob("*.parquet"))
    seen: set[str] = set()
    emitted = 0
    if not shards and any(item.get("status") == "emitted" for item in diagnostics):
        raise ReportSamplesError("emitted diagnostics exist but no Parquet shards were found")
    for shard in shards:
        try:
            parquet = pq.ParquetFile(shard)
            if not parquet.schema_arrow.equals(_SCHEMA, check_metadata=False):
                raise ReportSamplesError(f"strict sample schema mismatch: {shard}")
            for batch in parquet.iter_batches(batch_size=256):
                for row in batch.to_pylist():
                    sample_id = row["sample_id"]
                    if sample_id in seen:
                        raise ReportSamplesError("duplicate sample_id values")
                    seen.add(sample_id)
                    if sample_id not in identities:
                        raise ReportSamplesError(
                            "sample ids are absent from the current identity list"
                        )
                    for index, block in enumerate(row["blocks"]):
                        if (
                            block["order_index"] != index
                            or block["block_type"] not in BLOCK_TYPES
                            or not block["text"].strip()
                        ):
                            raise ReportSamplesError(
                                f"invalid block schema/content for {sample_id}"
                            )
                    emitted += 1
        except (OSError, pa.ArrowException) as exc:
            raise ReportSamplesError(f"Parquet shard is unreadable: {shard}: {exc}") from exc
    counts = {
        status: sum(item.get("status") == status for item in diagnostics)
        for status in ("emitted", "skipped", "failed")
    }
    if counts["emitted"] != emitted:
        raise ReportSamplesError("emitted diagnostic count does not match sample row count")
    diagnostic_ids = [item.get("filing_id") for item in diagnostics]
    if len(diagnostic_ids) != len(set(diagnostic_ids)) or set(diagnostic_ids) != identities:
        raise ReportSamplesError("diagnostic filing identities do not match expected identity list")
    return {
        "filings": len(diagnostics),
        "samples": emitted,
        "shards": len(shards),
        "status_counts": counts,
    }
