"""Fetch and persist daily Yahoo Finance bars."""

from __future__ import annotations

import logging
import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from .config import SourcesConfig
from .errors import DownloadError

logger = logging.getLogger(__name__)


def fetch_market(
    ticker: str, start: date, end: date, cfg: SourcesConfig, raw_dir: Path
) -> Path | None:
    """Download a requested date interval; Yahoo's end date is exclusive."""
    try:
        import yfinance as yf
    except ImportError as exc:
        raise DownloadError("yfinance is required for market downloads") from exc
    output_dir = raw_dir / "yahoo" / ticker.upper()
    output = output_dir / f"{start.isoformat()}_{end.isoformat()}.csv"
    last_error: Exception | None = None
    for attempt in range(max(1, cfg.market.max_retries)):
        try:
            frame = yf.download(
                ticker,
                start=start.isoformat(),
                end=(end + timedelta(days=1)).isoformat(),
                interval="1d",
                auto_adjust=False,
                actions=True,
                threads=False,
                progress=False,
                timeout=cfg.market.timeout_seconds,
            )
            if frame is None or frame.empty:
                return None
            if isinstance(frame.columns, pd.MultiIndex):
                frame.columns = [str(column[0]) for column in frame.columns]
            frame.index.name = "date"
            output_dir.mkdir(parents=True, exist_ok=True)
            frame.to_csv(output, index=True)
            return output
        except Exception as exc:
            last_error = exc
            logger.warning("Yahoo request failed for %s (attempt %s): %s", ticker, attempt + 1, exc)
            if attempt + 1 < max(1, cfg.market.max_retries):
                time.sleep(min(2**attempt, 8))
    logger.error("Yahoo request failed for %s: %s", ticker, last_error)
    return None
