"""Coordinate raw provider downloads and resumable progress."""

from __future__ import annotations

import logging
import time
from dataclasses import replace
from datetime import date
from pathlib import Path

from . import financials, macros, market, progress, universe
from .config import SourcesConfig, load_secrets, load_sources
from .errors import ConfigError, DownloadError

logger = logging.getLogger(__name__)


STAGES = ("market", "financials", "macros")


def _with_data_dir(cfg: SourcesConfig, data_dir: Path | None, repo_root: Path) -> SourcesConfig:
    if data_dir is None:
        return cfg
    base = data_dir if data_dir.is_absolute() else repo_root / data_dir
    return replace(
        cfg,
        raw_dir=base / "raw",
        progress_file=base / ".download_progress",
        progress_tmp_file=base / ".download_progress.tmp",
        progress_lock_file=base / ".download_progress.lock",
    )


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
) -> int:
    """Run selected raw download stages, returning 1 on any item failure."""
    try:
        stages = list(STAGES) if "all" in stages else list(dict.fromkeys(stages))
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
        needs_universe = "financials" in stages or (wants_market and tickers is None)
        secrets = load_secrets(repo_root) if needs_universe or "macros" in stages else None
        with progress.acquire_lock(cfg.progress_lock_file):
            rows = universe.fetch_universe(cfg, secrets) if needs_universe else []
            by_ticker = {item.ticker: item for item in rows}
            selected_tickers = (
                [item.upper() for item in tickers]
                if tickers is not None
                else [item.ticker for item in rows]
            )
            unknown = [
                item for item in selected_tickers if needs_universe and item not in by_ticker
            ]
            if unknown:
                logger.warning(
                    "Ignoring tickers absent from the SEC universe: %s", ", ".join(unknown)
                )
                selected_tickers = [item for item in selected_tickers if item in by_ticker]
            macro_items = list(cfg.macros.series) if "macros" in stages else []
            progress_stages: dict[str, list[str]] = {}
            for stage in ("market", "financials"):
                if stage in stages:
                    progress_stages[stage] = list(selected_tickers)
            if "macros" in stages:
                progress_stages["macros"] = macro_items
            state = progress.initialize(
                progress_stages,
                cfg.universe.source,
                force,
                cfg.progress_file,
                cfg.progress_tmp_file,
                cfg.progress_lock_file,
            )
            failures = 0
            if wants_market:
                assert start is not None and end is not None
                todo = progress.pending(state, "market")
                for index, ticker in enumerate(todo):
                    try:
                        output = market.fetch_market(ticker, start, end, cfg, cfg.raw_dir)
                    except Exception as exc:
                        logger.error("Market download failed for %s: %s", ticker, exc)
                        output = None
                    if output is None:
                        progress.mark_failed(
                            state, "market", ticker, "empty or failed Yahoo download"
                        )
                        failures += 1
                    else:
                        progress.mark_done(state, "market", ticker)
                    progress.save_atomic(state, cfg.progress_file, cfg.progress_tmp_file)
                    if index + 1 < len(todo) and cfg.market.rate_limit_seconds:
                        time.sleep(cfg.market.rate_limit_seconds)
            if "financials" in stages:
                for ticker in progress.pending(state, "financials"):
                    record = by_ticker.get(ticker)
                    if record is None:
                        progress.mark_failed(state, "financials", ticker, "ticker has no SEC CIK")
                        failures += 1
                    else:
                        try:
                            assert secrets is not None
                            result = financials.fetch_financials(
                                record.cik10, cfg, secrets, cfg.raw_dir
                            )
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
            progress.save_atomic(state, cfg.progress_file, cfg.progress_tmp_file)
        return 1 if failures else 0
    except (ConfigError, DownloadError) as exc:
        logger.error("Download run could not start: %s", exc)
        return 1
