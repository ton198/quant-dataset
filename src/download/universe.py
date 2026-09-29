"""Download and cache the SEC-listed exchange universe."""

from __future__ import annotations

import hashlib
import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Secrets, SourcesConfig
from .errors import DownloadError

logger = logging.getLogger(__name__)
TICKER_CIK_OVERRIDES_PATH = (
    Path(__file__).resolve().parents[2] / "config" / "universes" / "ticker_cik_overrides.json"
)


def load_ticker_cik_overrides(path: Path | None = None) -> dict[str, str]:
    """Load vetted ticker-to-CIK corrections; a missing file means no overrides."""
    override_path = path or TICKER_CIK_OVERRIDES_PATH
    try:
        payload = json.loads(override_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return {}
    if not isinstance(payload, dict) or not isinstance(payload.get("overrides"), dict):
        return {}

    overrides: dict[str, str] = {}
    for ticker, cik in payload["overrides"].items():
        normalized_ticker = str(ticker).strip().upper()
        normalized_cik = str(cik).strip()
        if normalized_ticker and normalized_cik.isdigit() and len(normalized_cik) <= 10:
            overrides[normalized_ticker] = normalized_cik.zfill(10)
    return overrides


def apply_ticker_cik_mapping_overrides(
    ticker_to_cik: dict[str, str], cik_to_ticker: dict[str, str],
    path: Path | None = None,
) -> None:
    """Apply configured corrections consistently to forward and reverse maps."""
    for ticker, cik10 in load_ticker_cik_overrides(path).items():
        previous_cik = ticker_to_cik.get(ticker)
        if previous_cik and previous_cik != cik10 and cik_to_ticker.get(previous_cik) == ticker:
            del cik_to_ticker[previous_cik]
        ticker_to_cik[ticker] = cik10
        cik_to_ticker[cik10] = ticker


@dataclass(frozen=True)
class TickerRow:
    """An exchange-listed ticker and its SEC CIK."""

    ticker: str
    cik10: str
    exchange: str
    company_name: str


def _rows(payload: Any) -> list[TickerRow]:
    if not isinstance(payload, dict):
        raise DownloadError("SEC exchange universe response must be a JSON object")
    fields = payload.get("fields", [])
    rows = payload.get("data", [])
    if not isinstance(fields, list) or not isinstance(rows, list):
        raise DownloadError("SEC exchange universe has an invalid fields/data structure")
    indexes = {str(name).casefold(): index for index, name in enumerate(fields)}
    try:
        cik_i, name_i, ticker_i, exchange_i = (indexes[key] for key in ("cik", "name", "ticker", "exchange"))
    except KeyError as exc:
        raise DownloadError("SEC exchange universe is missing expected columns") from exc
    result: list[TickerRow] = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) <= max(cik_i, name_i, ticker_i, exchange_i):
            continue
        try:
            cik = str(int(row[cik_i])).zfill(10)
        except (ValueError, TypeError):
            continue
        ticker = str(row[ticker_i]).strip().upper()
        exchange = str(row[exchange_i]).strip()
        if ticker and len(cik) == 10:
            result.append(TickerRow(ticker, cik, exchange, str(row[name_i]).strip()))
    overrides = load_ticker_cik_overrides()
    if not overrides:
        return result
    return [
        TickerRow(item.ticker, overrides.get(item.ticker, item.cik10), item.exchange, item.company_name)
        for item in result
    ]


def fetch_universe(cfg: SourcesConfig, secrets: Secrets) -> list[TickerRow]:
    """Load a valid cached SEC snapshot or download and content-address it."""
    directory = cfg.raw_dir / "sec" / "universe"
    for candidate in sorted(directory.glob("*.json")) if directory.exists() else []:
        try:
            content = candidate.read_bytes()
            digest = hashlib.sha256(content).hexdigest()
            if candidate.name == f"{digest}.json":
                records = _rows(json.loads(content.decode("utf-8")))
                filtered = [item for item in records if item.exchange.casefold() in {e.casefold() for e in cfg.universe.exchanges}]
                if filtered:
                    return filtered
        except (OSError, ValueError, UnicodeDecodeError, DownloadError):
            continue
    last_error: Exception | None = None
    for attempt in range(max(1, cfg.market.max_retries)):
        response = None
        try:
            request = urllib.request.Request(cfg.universe.url, headers={"User-Agent": secrets.sec_user_agent,
                                                                         "Accept": "application/json"})
            response = urllib.request.urlopen(request, timeout=cfg.market.timeout_seconds)
            content = response.read()
            records = _rows(json.loads(content.decode("utf-8")))
            filtered = [item for item in records if item.exchange.casefold() in {e.casefold() for e in cfg.universe.exchanges}]
            if not filtered:
                raise DownloadError("SEC exchange universe contains no configured exchanges")
            directory.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256(content).hexdigest()
            path = directory / f"{digest}.json"
            if not path.exists():
                path.write_bytes(content)
            return filtered
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, UnicodeDecodeError, DownloadError) as exc:
            last_error = exc
            if attempt + 1 < max(1, cfg.market.max_retries):
                time.sleep(min(2 ** attempt, 8))
        finally:
            if response is not None:
                response.close()
    raise DownloadError(f"Unable to fetch SEC exchange universe: {last_error}") from last_error
