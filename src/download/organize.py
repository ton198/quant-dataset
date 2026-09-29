"""Clean raw provider payloads and classify rows into organized datasets."""

from __future__ import annotations

import calendar as pycalendar
import hashlib
import json
import logging
import math
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from .errors import OrganizeError
from .organize_financials import organize_financials

logger = logging.getLogger(__name__)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _input_record(path: Path, base: Path) -> dict[str, str]:
    try:
        relative = str(path.relative_to(base.parent.parent))
    except ValueError:
        relative = str(path)
    return {"path": relative, "sha256": _sha256(path)}


def _output_record(path: Path, organized_dir: Path, rows: int) -> dict[str, Any]:
    try:
        relative = str(path.relative_to(organized_dir.parent.parent))
    except ValueError:
        relative = str(path)
    return {"path": relative, "sha256": _sha256(path), "rows": rows}


def _write_meta(
    path: Path,
    ticker: str,
    inputs: list[dict[str, str]],
    outputs: list[dict[str, Any]],
    row_counts: dict[str, int],
    known_issues: list[str] | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            payload = {}
    except (OSError, ValueError):
        payload = {}
    merged_inputs = {
        item.get("path"): item for item in payload.get("inputs", []) if isinstance(item, dict)
    }
    merged_inputs.update({item["path"]: item for item in inputs})
    merged_outputs = {
        item.get("path"): item for item in payload.get("outputs", []) if isinstance(item, dict)
    }
    merged_outputs.update({item["path"]: item for item in outputs})
    counts = payload.get("row_counts", {})
    if not isinstance(counts, dict):
        counts = {}
    counts.update(row_counts)
    issues = list(dict.fromkeys([*payload.get("known_issues", []), *(known_issues or [])]))
    payload.update(
        {
            "ticker": ticker,
            "generated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "cleaning_rules_version": "v1",
            "inputs": list(merged_inputs.values()),
            "outputs": list(merged_outputs.values()),
            "row_counts": counts,
            "known_issues": issues,
        }
    )
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_meta(ticker: str, organized_dir: Path, meta: dict[str, Any]) -> Path:
    """Write supplied metadata under the ticker's organized output directory."""
    path = organized_dir / "stocks" / ticker.upper() / "_meta.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _market_quality(row: pd.Series) -> str:
    try:
        open_, high, low, close, volume = (
            float(row[name]) for name in ("open", "high", "low", "close", "volume")
        )
    except (KeyError, TypeError, ValueError):
        return "invalid_ohlc"
    if close < 0 or open_ < 0 or high < 0 or low < 0:
        return "negative_price"
    if close <= 0 or volume < 0 or high < max(open_, close, low) or low > min(open_, close, high):
        return "invalid_ohlc"
    if not all(math.isfinite(value) for value in (open_, high, low, close, volume)):
        return "invalid_ohlc"
    return "ok"


def organize_market(
    ticker: str, raw_dir: Path, organized_dir: Path, calendar: list[date]
) -> Path | None:
    """Clean and derive daily market fields when Yahoo data is available."""
    directory = raw_dir / "yahoo" / ticker.upper()
    files = sorted(directory.glob("*.csv")) if directory.exists() else []
    if not files:
        logger.info(
            "Skipping market organization for %s: no Yahoo CSV found in %s", ticker, directory
        )
        return None
    try:
        frames = [pd.read_csv(path) for path in files]
        for frame_part in frames:
            frame_part.columns = [
                str(name).strip().casefold().replace(" ", "_") for name in frame_part.columns
            ]
        frame = pd.concat(frames, ignore_index=True)
        names = {str(name).strip().casefold().replace(" ", "_"): name for name in frame.columns}
        date_col = names.get("date") or names.get("datetime")
        if date_col is None:
            raise OrganizeError(f"Market input for {ticker} has no date column")
        aliases = {
            "open": "open",
            "high": "high",
            "low": "low",
            "close": "close",
            "adj_close": "adj_close",
            "adjclose": "adj_close",
            "volume": "volume",
        }
        selected: dict[str, Any] = {}
        for key, target in aliases.items():
            if key in names:
                selected[target] = frame[names[key]]
        data = pd.DataFrame(selected)
        for column in ("open", "high", "low", "close", "volume"):
            if column not in data:
                raise OrganizeError(f"Market input for {ticker} is missing {column}")
        data["date"] = pd.to_datetime(frame[date_col], errors="coerce").dt.date
        data["adj_close"] = data.get("adj_close", data["close"])
        data = (
            data.dropna(subset=["date"])
            .sort_values("date")
            .drop_duplicates(subset=["date"], keep="last")
        )
        input_count = len(data)
        data = data[data["date"].isin(set(calendar))].copy()
        dropped = input_count - len(data)
        data["quality_flag"] = data.apply(_market_quality, axis=1)
        close = pd.to_numeric(data["close"], errors="coerce")
        volume = pd.to_numeric(data["volume"], errors="coerce")
        data["adjustment_factor"] = pd.to_numeric(data["adj_close"], errors="coerce") / close
        returns = close.pct_change()
        data["return_1d"] = returns
        data["return_5d"] = close.pct_change(5)
        data["return_20d"] = close.pct_change(20)
        data["volatility_20"] = returns.rolling(20).std()
        data["volume_ratio_20"] = volume / volume.rolling(20).mean()
        data["intraday_range"] = (
            pd.to_numeric(data["high"], errors="coerce")
            - pd.to_numeric(data["low"], errors="coerce")
        ) / close
        columns = [
            "date",
            "open",
            "high",
            "low",
            "close",
            "adj_close",
            "volume",
            "adjustment_factor",
            "return_1d",
            "return_5d",
            "return_20d",
            "volatility_20",
            "volume_ratio_20",
            "intraday_range",
            "quality_flag",
        ]
        data = data[columns].sort_values("date")
    except OrganizeError:
        raise
    except Exception as exc:
        raise OrganizeError(f"Unable to organize market data for {ticker}: {exc}") from exc
    output = organized_dir / "stocks" / ticker.upper() / "market.csv"
    output.parent.mkdir(parents=True, exist_ok=True)
    data.to_csv(output, index=False, date_format="%Y-%m-%d")
    _write_meta(
        output.parent / "_meta.json",
        ticker.upper(),
        [_input_record(path, raw_dir) for path in files],
        [_output_record(output, organized_dir, len(data))],
        {
            "market_input": input_count,
            "market_output": len(data),
            "market_dropped_non_session": dropped,
        },
    )
    return output


def organize_macros(raw_dir: Path, organized_dir: Path, calendar: list[date]) -> Path:
    """Build a session-aligned wide FRED matrix with conservative release dates."""
    root = raw_dir / "fred"
    records: dict[str, dict[date, float]] = {}
    inputs: list[dict[str, str]] = []
    if root.exists():
        for path in sorted(root.glob("*/*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                series_id = path.parent.name
                parsed: dict[date, float] = {}
                for observation in payload.get("observations", []):
                    try:
                        reference = date.fromisoformat(observation["date"])
                        month_index = reference.year * 12 + reference.month
                        year, month_zero = divmod(month_index, 12)
                        month = month_zero + 1
                        approximate_release = date(
                            year, month, min(reference.day, pycalendar.monthrange(year, month)[1])
                        )
                        visible = next(
                            (
                                session
                                for session in sorted(calendar)
                                if session > approximate_release
                            ),
                            None,
                        )
                        if visible is not None:
                            parsed[visible] = float(observation["value"])
                    except (KeyError, TypeError, ValueError):
                        continue
                if parsed:
                    records[series_id] = parsed
                    inputs.append(_input_record(path, raw_dir))
            except (OSError, ValueError):
                continue
    sessions = sorted(calendar)
    data = pd.DataFrame({"date": sessions})
    for series_id, values in sorted(records.items()):
        data[series_id] = pd.Series(values, dtype="float64").reindex(sessions).ffill().values
    output = organized_dir / "shared" / "macro.csv"
    output.parent.mkdir(parents=True, exist_ok=True)
    data.to_csv(output, index=False, date_format="%Y-%m-%d")
    _write_meta(
        output.parent / "_meta.json",
        "shared",
        inputs,
        [_output_record(output, organized_dir, len(data))],
        {"macro_output": len(data)},
        [
            "FRED observations are treated as visible after reference period + one month + one session; response omits release timestamps."
        ],
    )
    return output
