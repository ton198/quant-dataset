"""Coordinate provider downloads, resumable progress, and optional organization."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import replace
from datetime import date
from pathlib import Path

from . import financials, macros, market, organize, progress, universe
from .config import SourcesConfig, load_secrets, load_sources
from .errors import ConfigError, DownloadError

logger = logging.getLogger(__name__)


STAGES = ("market", "financials", "macros", "organize")


def _with_data_dir(cfg: SourcesConfig, data_dir: Path | None, repo_root: Path) -> SourcesConfig:
    if data_dir is None:
        return cfg
    base = data_dir if data_dir.is_absolute() else repo_root / data_dir
    return replace(cfg, raw_dir=base / "raw", organized_dir=base / "organized",
                   progress_file=base / ".download_progress",
                   progress_tmp_file=base / ".download_progress.tmp",
                   progress_lock_file=base / ".download_progress.lock")


def _calendar(start: date, end: date) -> list[date]:
    try:
        import exchange_calendars as xcals
        import pandas as pd
        # Pass start/end to get_calendar() so the calendar is generated for the
        # requested range. Calling get_calendar("XNYS") without arguments uses a
        # default range (~2006-2027) that cannot reach back to 1990.
        cal = xcals.get_calendar("XNYS", start=pd.Timestamp(start), end=pd.Timestamp(end))
        # Use cal.sessions (already bounded by the constructor's start/end)
        # rather than sessions_in_range, which rejects a start earlier than
        # cal.first_session even when the constructor accepted it.
        return [item.date() for item in cal.sessions if start <= item.date() <= end]
    except Exception as exc:
        raise DownloadError(f"Unable to construct XNYS session calendar: {exc}") from exc


def _raw_tickers(raw_dir: Path) -> list[str]:
    directory = raw_dir / "yahoo"
    return sorted(path.name.upper() for path in directory.iterdir() if path.is_dir()) if directory.exists() else []


def _cached_universe(cfg: SourcesConfig) -> list[universe.TickerRow]:
    directory = cfg.raw_dir / "sec" / "universe"
    allowed = {item.casefold() for item in cfg.universe.exchanges}
    for path in sorted(directory.glob("*.json")) if directory.exists() else []:
        try:
            content = path.read_bytes()
            if hashlib.sha256(content).hexdigest() != path.stem:
                continue
            rows = universe._rows(json.loads(content.decode("utf-8")))
            result = [row for row in rows if row.exchange.casefold() in allowed]
            if result:
                return result
        except (OSError, ValueError, UnicodeDecodeError, DownloadError):
            continue
    return []


_ORGANIZE_SESSION_CALENDAR: list[date] = []


def _initialize_organize_worker(session_calendar: list[date]) -> None:
    """Install shared read-only calendar data once in each worker process."""
    global _ORGANIZE_SESSION_CALENDAR
    _ORGANIZE_SESSION_CALENDAR = list(session_calendar)


def _organize_ticker_worker(
    ticker: str, cik10: str | None, raw_dir: Path, organized_dir: Path,
) -> tuple[str, list[tuple[str, str]]]:
    """Organize one ticker, returning isolated operation errors to the parent."""
    errors: list[tuple[str, str]] = []
    # Invalidate any old completion record before writing outputs. If this worker
    # is killed between output writes, the next run cannot mistake stale metadata
    # for a completed organization pass.
    meta_path = organized_dir / "stocks" / ticker.upper() / "_meta.json"
    try:
        meta_path.unlink()
    except FileNotFoundError:
        pass
    try:
        organize.organize_market(ticker, raw_dir, organized_dir, _ORGANIZE_SESSION_CALENDAR)
    except Exception as exc:
        errors.append(("market", str(exc)))
    if cik10:
        try:
            organize.organize_financials(cik10, raw_dir, organized_dir,
                                         _ORGANIZE_SESSION_CALENDAR, output_ticker=ticker)
        except Exception as exc:
            errors.append(("financials", str(exc)))
    return ticker, errors


def _ticker_is_organized(
    ticker: str, raw_dir: Path, organized_dir: Path, *, require_financials: bool,
) -> bool:
    """Return true only when metadata and all expected ticker outputs are present."""
    ticker = ticker.upper()
    meta_path = organized_dir / "stocks" / ticker / "_meta.json"
    try:
        metadata = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return False
    if not isinstance(metadata, dict) or metadata.get("ticker") != ticker:
        return False
    outputs = metadata.get("outputs")
    if not isinstance(outputs, list) or not outputs:
        return False

    output_root = organized_dir.parent.parent
    recorded_outputs: set[Path] = set()
    for record in outputs:
        if not isinstance(record, dict) or not isinstance(record.get("path"), str):
            return False
        output_path = output_root / record["path"]
        if not output_path.is_file():
            return False
        recorded_outputs.add(output_path.resolve())

    market_output = (organized_dir / "stocks" / ticker / "market.csv").resolve()
    financials_output = (organized_dir / "stocks" / ticker / "financials.csv").resolve()
    yahoo_files = list((raw_dir / "yahoo" / ticker).glob("*.csv"))
    if yahoo_files and market_output not in recorded_outputs:
        return False
    if require_financials and financials_output not in recorded_outputs:
        return False
    return True


def _organize_tickers(
    tickers: list[str], by_ticker: dict[str, universe.TickerRow], raw_dir: Path,
    organized_dir: Path, session_calendar: list[date], workers: int,
) -> tuple[int, int, int]:
    """Run per-ticker organization in processes and return success/skip/failure counts."""
    succeeded = skipped = failed = 0
    todo: list[tuple[str, str | None]] = []
    for ticker in dict.fromkeys(tickers):
        record = by_ticker.get(ticker)
        cik10 = record.cik10 if record else None
        if _ticker_is_organized(ticker, raw_dir, organized_dir,
                                require_financials=bool(cik10)):
            skipped += 1
        else:
            todo.append((ticker, cik10))

    if todo:
        futures = {}
        with ProcessPoolExecutor(
            max_workers=workers, initializer=_initialize_organize_worker,
            initargs=(session_calendar,),
        ) as executor:
            for ticker, cik10 in todo:
                future = executor.submit(_organize_ticker_worker, ticker, cik10,
                                         raw_dir, organized_dir)
                futures[future] = ticker
            for future in as_completed(futures):
                ticker = futures[future]
                try:
                    _, errors = future.result()
                except Exception as exc:
                    errors = [("worker", str(exc))]
                if errors:
                    failed += 1
                    for operation, error in errors:
                        logger.error("%s organization failed for %s: %s",
                                     operation.capitalize(), ticker, error)
                else:
                    succeeded += 1

    logger.info("Ticker organization complete: succeeded=%d skipped=%d failed=%d",
                succeeded, skipped, failed)
    return succeeded, skipped, failed


def run_download(
    *,
    repo_root: Path,
    stages: list[str],
    start: date | None,
    end: date | None,
    force: bool,
    dry_run: bool,
    tickers: list[str] | None,
    data_dir: Path | None = None,
    workers: int = 16,
) -> int:
    """Run selected stages, returning 1 if any per-item operation fails."""
    try:
        if workers < 1:
            raise ConfigError("--workers must be at least 1")
        cfg = _with_data_dir(load_sources(repo_root), data_dir, repo_root)
        if any(stage not in STAGES for stage in stages):
            raise ConfigError(f"Unknown stage requested: {stages}")
        wants_market = "market" in stages
        if wants_market and (start is None or end is None):
            raise ConfigError("market stage requires --start and --end")
        if dry_run:
            planned = ", ".join(stages)
            details = f" for {', '.join(tickers)}" if tickers else " for configured universe"
            logger.info("Dry run: would run stages %s%s", planned, details)
            return 0
        needs_universe = "financials" in stages or ("market" in stages and tickers is None)
        secrets = load_secrets(repo_root) if needs_universe or "macros" in stages else None
        with progress.acquire_lock(cfg.progress_lock_file):
            rows = (universe.fetch_universe(cfg, secrets) if needs_universe and secrets is not None
                    else _cached_universe(cfg) if "organize" in stages else [])
            by_ticker = {item.ticker: item for item in rows}
            selected_tickers = [item.upper() for item in tickers] if tickers is not None else [item.ticker for item in rows]
            # Only fall back to "whatever is already in raw/" when running an
            # organize-only pass. If market or financials are also in stages we
            # must keep the universe so those stages can fetch the full set.
            wants_ticker_downloads = "market" in stages or "financials" in stages
            if "organize" in stages and not wants_ticker_downloads and (tickers is None or not selected_tickers):
                selected_tickers = _raw_tickers(cfg.raw_dir)
                if needs_universe:
                    by_ticker = {item.ticker: item for item in rows}
            unknown = [item for item in selected_tickers if needs_universe and item not in by_ticker]
            if unknown:
                logger.warning("Ignoring tickers absent from the SEC universe: %s", ", ".join(unknown))
                selected_tickers = [item for item in selected_tickers if item in by_ticker]
            macro_items = list(cfg.macros.series) if "macros" in stages else []
            progress_stages: dict[str, list[str]] = {}
            for stage in ("market", "financials"):
                if stage in stages:
                    progress_stages[stage] = list(selected_tickers)
            if "macros" in stages:
                progress_stages["macros"] = macro_items
            state = progress.initialize(progress_stages, cfg.universe.source, force,
                                        cfg.progress_file, cfg.progress_tmp_file, cfg.progress_lock_file)
            failures = 0
            if "market" in stages:
                assert start is not None and end is not None
                todo = progress.pending(state, "market")
                for index, ticker in enumerate(todo):
                    try:
                        output = market.fetch_market(ticker, start, end, cfg, cfg.raw_dir)
                    except Exception as exc:
                        logger.error("Market download failed for %s: %s", ticker, exc)
                        output = None
                    if output is None:
                        progress.mark_failed(state, "market", ticker, "empty or failed Yahoo download")
                        failures += 1
                    else:
                        progress.mark_done(state, "market", ticker)
                    progress.save_atomic(state, cfg.progress_file, cfg.progress_tmp_file)
                    if index + 1 < len(todo) and cfg.market.rate_limit_seconds:
                        time.sleep(cfg.market.rate_limit_seconds)
            if "financials" in stages:
                todo = progress.pending(state, "financials")
                for ticker in todo:
                    record = by_ticker.get(ticker)
                    if record is None:
                        progress.mark_failed(state, "financials", ticker, "ticker has no SEC CIK")
                        failures += 1
                    else:
                        try:
                            assert secrets is not None
                            result = financials.fetch_financials(record.cik10, cfg, secrets, cfg.raw_dir)
                            if not result:
                                raise DownloadError("SEC produced no financial payload")
                            progress.mark_done(state, "financials", ticker)
                        except Exception as exc:
                            progress.mark_failed(state, "financials", ticker, str(exc))
                            failures += 1
                            logger.error("Financial download failed for %s: %s", ticker, exc)
                    progress.save_atomic(state, cfg.progress_file, cfg.progress_tmp_file)
            if "macros" in stages:
                try:
                    assert secrets is not None
                    macros.fetch_macros(cfg, secrets, cfg.raw_dir, state)
                    for series_id in macro_items:
                        if state.stages.get("macros", {}).get(series_id) != "done":
                            failures += 1
                        progress.save_atomic(state, cfg.progress_file, cfg.progress_tmp_file)
                except Exception as exc:
                    logger.error("Macro downloads failed: %s", exc)
                    for series_id in progress.pending(state, "macros"):
                        progress.mark_failed(state, "macros", series_id, str(exc))
                        failures += 1
                    progress.save_atomic(state, cfg.progress_file, cfg.progress_tmp_file)
            if "organize" in stages:
                calendar_start = start or date(1990, 1, 1)
                calendar_end = end or date.today()
                session_calendar = _calendar(calendar_start, calendar_end)
                _, _, organize_failures = _organize_tickers(
                    selected_tickers, by_ticker, cfg.raw_dir, cfg.organized_dir,
                    session_calendar, workers,
                )
                failures += organize_failures
                try:
                    organize.organize_macros(cfg.raw_dir, cfg.organized_dir, session_calendar)
                except Exception as exc:
                    failures += 1
                    logger.error("Macro organization failed: %s", exc)
            progress.save_atomic(state, cfg.progress_file, cfg.progress_tmp_file)
        return 1 if failures else 0
    except (ConfigError, DownloadError) as exc:
        logger.error("Download run could not start: %s", exc)
        return 1
