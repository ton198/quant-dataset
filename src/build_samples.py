"""Build the partitioned, point-in-time training sample long table."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import shutil
import tempfile
from datetime import date
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.dataset as ds
import pyarrow.parquet as pq

logger = logging.getLogger(__name__)

MACRO_SERIES = (
    "BAMLH0A0HYM2", "CPIAUCSL", "CPILFESL", "DCOILWTICO", "DEXUSEU",
    "DGS10", "DGS2", "FEDFUNDS", "PAYEMS", "UNRATE", "VIXCLS",
)
MARKET_RAW = (
    "f_raw_return_1d", "f_raw_return_5d", "f_raw_return_20d",
    "f_raw_volatility_20", "f_raw_volume_ratio_20", "f_raw_intraday_range",
)
DERIVED_RAW = (
    "f_raw_momentum_60", "f_raw_momentum_120", "f_raw_volatility_60",
    "f_raw_volume_zscore_60",
)
FINANCIAL_RAW = (
    "f_raw_revenue_yoy", "f_raw_net_income_yoy", "f_raw_operating_income_yoy",
    "f_raw_assets_yoy", "f_raw_days_since_filing",
)
STOCK_RAW = (*MARKET_RAW, *DERIVED_RAW, *FINANCIAL_RAW)
MACRO_RAW = tuple(
    feature
    for series in MACRO_SERIES
    for feature in (f"f_raw_m_{series}", f"f_raw_m_{series}_d1", f"f_raw_m_{series}_d5")
)
RAW_FEATURES = (*STOCK_RAW, *MACRO_RAW)
CS_FEATURES = tuple(f"f_cs_{name.removeprefix('f_raw_')}" for name in STOCK_RAW)
MISS_FEATURES = tuple(f"miss_{name.removeprefix('f_raw_')}" for name in RAW_FEATURES)
LABELS = (*tuple(f"target_return_{horizon}d" for horizon in range(1, 31)), "excess_5d", "excess_21d")

_MARKET_REQUIRED = {
    "date", "open", "close", "adj_close", "volume", "return_1d", "return_5d",
    "return_20d", "volatility_20", "volume_ratio_20", "intraday_range",
}
_FINANCIAL_VALUES = ("revenue", "net_income", "operating_income", "assets")
_PURGE_SESSIONS = 30
_SPLIT_TRANSITIONS = (
    ("select", "2019-01-01", "2020-12-31"),
    ("screen", "2021-01-01", "2024-12-31"),
    ("reserve", "2025-01-01", None),
)
_PURGE_SEMANTICS = (
    "At each split boundary, rows are excluded at build time when their maximum-horizon label "
    "would reach the next split: for a 30-session horizon, the 31 signal sessions immediately "
    "before the boundary are purged because labels enter at t+1 and exit at t+31. "
    "Only boundaries with canonical sessions in the following split window are purged; rows at "
    "the dataset end with naturally missing labels are retained."
)


def _purge_windows(
    calendar: pd.DatetimeIndex, purge_sessions: int = _PURGE_SESSIONS,
) -> dict[str, dict[str, Any]]:
    """Return canonical-session embargo windows before each populated split boundary."""
    windows: dict[str, dict[str, Any]] = {}
    for split_name, start, end in _SPLIT_TRANSITIONS:
        boundary_position = int(calendar.searchsorted(pd.Timestamp(start), side="left"))
        if boundary_position == 0 or boundary_position >= len(calendar):
            continue
        boundary_date = calendar[boundary_position]
        if end is not None and boundary_date > pd.Timestamp(end):
            continue

        # The 30-session label exits at position t + 1 + 30. Include a signal
        # exactly 31 positions before the boundary because its label exits on it.
        first_position = max(0, boundary_position - (purge_sessions + 1))
        if first_position == boundary_position:
            continue
        positions = tuple(range(first_position, boundary_position))
        windows[split_name] = {
            "split_start": start,
            "purged_split": {"select": "fit", "screen": "select", "reserve": "screen"}[split_name],
            "boundary_date": boundary_date.strftime("%Y-%m-%d"),
            "first_purged_session": calendar[first_position].strftime("%Y-%m-%d"),
            "last_purged_session": calendar[boundary_position - 1].strftime("%Y-%m-%d"),
            "sessions": len(positions),
            "rows_removed": 0,
            "_positions": positions,
        }
    return windows


def _public_purge_windows(windows: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        name: {key: value for key, value in window.items() if not key.startswith("_")}
        for name, window in windows.items()
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _is_common(asset_id: str) -> bool:
    """Heuristically identify units, warrants, preferreds, and rights tickers."""
    if "-" not in asset_id:
        return True
    suffix = asset_id.rsplit("-", 1)[1].upper()
    return not (
        suffix in {"UN", "WT", "W", "P", "R", "U", "WS", "RT", "R"}
        or suffix.startswith(("P", "W", "R", "U"))
        or suffix.endswith(("UN", "WT", "WS", "RT"))
    )


def _load_exclusions(path: Path) -> list[str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        entries = payload.get("entries")
    else:
        entries = payload
    if not isinstance(entries, list):
        raise ValueError(f"exclusions file must contain an entries list: {path}")
    result: list[str] = []
    for item in entries:
        value = item.get("asset_id") if isinstance(item, dict) else item
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"exclusions file has an invalid asset_id: {path}")
        result.append(value.strip().upper())
    if len(set(result)) != len(result):
        raise ValueError(f"exclusions file contains duplicate asset_id values: {path}")
    return sorted(result)


def _normal_date_column(values: pd.Series, label: str) -> pd.Series:
    parsed = pd.to_datetime(values, errors="coerce")
    if parsed.isna().any():
        raise ValueError(f"{label} contains invalid dates")
    return parsed.dt.normalize()


def _macro_matrix(path: Path, calendar: pd.DatetimeIndex) -> pd.DataFrame:
    macro = pd.read_csv(path)
    missing = sorted({"date", *MACRO_SERIES} - set(macro.columns))
    if missing:
        raise ValueError("macro.csv is missing required columns: " + ", ".join(missing))
    macro["date"] = _normal_date_column(macro["date"], "macro.csv date")
    if macro["date"].duplicated().any():
        raise ValueError("macro.csv contains duplicate dates")
    macro = macro.set_index("date").sort_index()
    aligned = macro.loc[:, MACRO_SERIES].apply(pd.to_numeric, errors="coerce").reindex(calendar)
    result: dict[str, np.ndarray] = {}
    for series in MACRO_SERIES:
        values = aligned[series]
        result[f"f_raw_m_{series}"] = values.to_numpy(dtype=np.float32)
        result[f"f_raw_m_{series}_d1"] = values.diff(1).to_numpy(dtype=np.float32)
        result[f"f_raw_m_{series}_d5"] = values.diff(5).to_numpy(dtype=np.float32)
    return pd.DataFrame(result, index=calendar)


def _positive_finite(values: np.ndarray) -> np.ndarray:
    return np.isfinite(values) & (values > 0)


def _aligned_numeric(frame: pd.DataFrame, name: str, size: int, positions: np.ndarray) -> np.ndarray:
    aligned = np.full(size, np.nan, dtype=np.float64)
    aligned[positions] = pd.to_numeric(frame[name], errors="coerce").to_numpy(dtype=np.float64)
    return aligned


def _rolling_std(values: np.ndarray, window: int) -> np.ndarray:
    return pd.Series(values).rolling(window, min_periods=window).std(ddof=1).to_numpy(dtype=np.float64)


def _financial_features(path: Path | None, signal_dates: pd.DatetimeIndex) -> dict[str, np.ndarray]:
    result = {name: np.full(len(signal_dates), np.nan, dtype=np.float32) for name in FINANCIAL_RAW}
    if path is None or not path.exists() or signal_dates.empty:
        return result

    frame = pd.read_csv(path)
    needed = {"available_as_of", "report_period_end", *_FINANCIAL_VALUES}
    missing = sorted(needed - set(frame.columns))
    if missing:
        raise ValueError(f"{path.name} is missing required columns: " + ", ".join(missing))
    available = pd.to_datetime(frame["available_as_of"], errors="coerce").dt.normalize()
    frame = frame.loc[available.notna()].copy()
    if frame.empty:
        return result
    frame["_available"] = available.loc[frame.index]
    frame["_source_order"] = np.arange(len(frame), dtype=np.int64)
    frame = frame.sort_values(["_available", "_source_order"], kind="mergesort")
    # Organized financials are daily snapshots. Keep the last snapshot for each
    # availability date, yielding the filing information actually visible then.
    frame = frame.drop_duplicates("_available", keep="last").reset_index(drop=True)
    for column in _FINANCIAL_VALUES:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    fiscal_year = (
        pd.to_numeric(frame["fiscal_year"], errors="coerce")
        if "fiscal_year" in frame
        else pd.Series(np.nan, index=frame.index, dtype=np.float64)
    )
    period = (
        frame["fiscal_period"].astype("string").str.upper()
        if "fiscal_period" in frame
        else pd.Series(pd.NA, index=frame.index, dtype="string")
    )
    report_end = pd.to_datetime(frame["report_period_end"], errors="coerce")
    yoys: dict[str, np.ndarray] = {
        f"f_raw_{column}_yoy": np.full(len(frame), np.nan, dtype=np.float64)
        for column in _FINANCIAL_VALUES
    }
    fiscal_history: dict[tuple[int, str], int] = {}
    report_year_history: dict[int, dict[pd.Timestamp, int]] = {}
    for index, row in frame.iterrows():
        year_value = fiscal_year.iloc[index]
        period_value = period.iloc[index]
        end_value = report_end.iloc[index]
        has_fiscal_identifiers = pd.notna(year_value) and pd.notna(period_value)
        prior_index: int | None = None
        if has_fiscal_identifiers:
            prior_index = fiscal_history.get((int(year_value) - 1, str(period_value)))
        elif pd.notna(end_value):
            prior_target = end_value - pd.DateOffset(years=1)
            candidates = [
                (candidate_date, candidate_index)
                for candidate_date, candidate_index in report_year_history.get(int(end_value.year) - 1, {}).items()
                if abs((candidate_date - prior_target).days) <= 15
            ]
            if candidates:
                _, prior_index = min(
                    candidates,
                    key=lambda candidate: (
                        abs((candidate[0] - prior_target).days), candidate[0],
                    ),
                )

        if prior_index is not None:
            prior = frame.iloc[prior_index]
            for column in _FINANCIAL_VALUES:
                current_value = row[column]
                prior_value = prior[column]
                if pd.notna(current_value) and pd.notna(prior_value) and prior_value != 0:
                    yoys[f"f_raw_{column}_yoy"][index] = current_value / prior_value - 1.0

        if has_fiscal_identifiers:
            fiscal_history[(int(year_value), str(period_value))] = index
        if pd.notna(end_value) and row[list(_FINANCIAL_VALUES)].notna().any():
            report_year_history.setdefault(int(end_value.year), {})[end_value] = index

    positions = np.searchsorted(
        frame["_available"].to_numpy(dtype="datetime64[ns]"),
        signal_dates.to_numpy(dtype="datetime64[ns]"),
        side="right",
    ) - 1
    visible = positions >= 0
    mapped = {
        "f_raw_revenue_yoy": yoys["f_raw_revenue_yoy"],
        "f_raw_net_income_yoy": yoys["f_raw_net_income_yoy"],
        "f_raw_operating_income_yoy": yoys["f_raw_operating_income_yoy"],
        "f_raw_assets_yoy": yoys["f_raw_assets_yoy"],
    }
    for feature, values in mapped.items():
        target = result[feature]
        target[visible] = values[positions[visible]].astype(np.float32)
    available_ns = frame["_available"].to_numpy(dtype="datetime64[ns]")
    signal_ns = signal_dates.to_numpy(dtype="datetime64[ns]")
    days = (signal_ns[visible] - available_ns[positions[visible]]) / np.timedelta64(1, "D")
    result["f_raw_days_since_filing"][visible] = days.astype(np.float32)
    return result


def _ticker_samples(
    asset_id: str,
    market_path: Path,
    financial_path: Path | None,
    calendar: pd.DatetimeIndex,
    macro: pd.DataFrame,
    is_common: bool,
) -> pd.DataFrame:
    market = pd.read_csv(market_path)
    missing = sorted(_MARKET_REQUIRED - set(market.columns))
    if missing:
        raise ValueError("market.csv is missing required columns: " + ", ".join(missing))
    market["date"] = _normal_date_column(market["date"], f"{asset_id} market date")
    if market["date"].duplicated().any():
        raise ValueError("market.csv contains duplicate dates")
    market = market.sort_values("date", kind="mergesort").reset_index(drop=True)
    positions_all = calendar.get_indexer(market["date"])
    on_axis = positions_all >= 0
    sample_source = market.loc[on_axis].reset_index(drop=True)
    positions = positions_all[on_axis]
    if sample_source.empty:
        return pd.DataFrame(columns=["date", "asset_id", "is_common", *RAW_FEATURES, *LABELS])

    size = len(calendar)
    opens = _aligned_numeric(sample_source, "open", size, positions)
    closes = _aligned_numeric(sample_source, "close", size, positions)
    adjusted_closes = _aligned_numeric(sample_source, "adj_close", size, positions)
    volumes = _aligned_numeric(sample_source, "volume", size, positions)
    factor = np.divide(
        adjusted_closes, closes,
        out=np.full(size, np.nan, dtype=np.float64),
        where=np.isfinite(closes) & (closes != 0),
    )
    adjusted_opens = opens * factor
    valid_bar = (
        _positive_finite(opens) & _positive_finite(closes) & _positive_finite(factor)
        & np.isfinite(adjusted_opens)
    )

    raw: dict[str, np.ndarray] = {}
    for source, target in (
        ("return_1d", "f_raw_return_1d"), ("return_5d", "f_raw_return_5d"),
        ("return_20d", "f_raw_return_20d"), ("volatility_20", "f_raw_volatility_20"),
        ("volume_ratio_20", "f_raw_volume_ratio_20"), ("intraday_range", "f_raw_intraday_range"),
    ):
        raw[target] = pd.to_numeric(sample_source[source], errors="coerce").to_numpy(dtype=np.float32)

    for days in (60, 120):
        momentum = np.full(size, np.nan, dtype=np.float64)
        valid_positions = positions >= days
        current = positions[valid_positions]
        prior = current - days
        valid = _positive_finite(adjusted_closes[current]) & _positive_finite(adjusted_closes[prior])
        values = np.full(len(current), np.nan, dtype=np.float64)
        values[valid] = adjusted_closes[current[valid]] / adjusted_closes[prior[valid]] - 1.0
        momentum[current] = values
        raw[f"f_raw_momentum_{days}"] = momentum[positions].astype(np.float32)

    canonical_returns = np.full(size, np.nan, dtype=np.float64)
    current = np.arange(1, size)
    prior_values = adjusted_closes[current - 1]
    current_values = adjusted_closes[current]
    valid = _positive_finite(prior_values) & _positive_finite(current_values)
    canonical_returns[current[valid]] = current_values[valid] / prior_values[valid] - 1.0
    raw["f_raw_volatility_60"] = _rolling_std(canonical_returns, 60)[positions].astype(np.float32)
    volume_series = pd.Series(volumes)
    volume_mean = volume_series.rolling(60, min_periods=60).mean().to_numpy(dtype=np.float64)
    volume_std = volume_series.rolling(60, min_periods=60).std(ddof=1).to_numpy(dtype=np.float64)
    volume_z = np.divide(
        volumes - volume_mean, volume_std,
        out=np.full(size, np.nan, dtype=np.float64),
        where=np.isfinite(volume_std) & (volume_std > 0),
    )
    raw["f_raw_volume_zscore_60"] = volume_z[positions].astype(np.float32)
    raw.update(_financial_features(financial_path, pd.DatetimeIndex(sample_source["date"])))

    for feature in RAW_FEATURES:
        if feature in MACRO_RAW:
            raw[feature] = macro.iloc[positions][feature].to_numpy(dtype=np.float32)

    # Raw features and their paired miss_* indicators must never expose infinities.
    for feature, values in raw.items():
        finite = np.isfinite(values)
        if not finite.all():
            clean_values = values.copy()
            clean_values[~finite] = np.nan
            raw[feature] = clean_values

    result: dict[str, Any] = {
        "date": sample_source["date"].to_numpy(dtype="datetime64[ns]"),
        "asset_id": np.repeat(asset_id, len(sample_source)),
        "is_common": np.repeat(is_common, len(sample_source)),
        **raw,
    }
    entry_positions = positions + 1
    for horizon in range(1, 31):
        exit_positions = positions + 1 + horizon
        values = np.full(len(positions), np.nan, dtype=np.float64)
        exists = (entry_positions < size) & (exit_positions < size)
        safe_entry = np.minimum(entry_positions, size - 1)
        safe_exit = np.minimum(exit_positions, size - 1)
        valid = exists & valid_bar[safe_entry] & valid_bar[safe_exit]
        values[valid] = adjusted_opens[safe_exit[valid]] / adjusted_opens[safe_entry[valid]] - 1.0
        result[f"target_return_{horizon}d"] = values.astype(np.float32)

    # Data-quality flag: a label window is corrupted when it crosses a source price
    # glitch (consecutive-session adjusted-open ratio outside [0.5, 2.0], e.g. an
    # unadjusted reverse split). Such rows are kept but flagged; the downstream
    # training layer can drop them and the excess benchmark means exclude them.
    glitch = np.zeros(size, dtype=bool)
    if size > 1:
        pair_ok = valid_bar[:-1] & valid_bar[1:]
        moves = np.full(size, np.nan, dtype=np.float64)
        with np.errstate(invalid="ignore"):
            np.divide(adjusted_opens[1:], adjusted_opens[:-1], out=moves[1:], where=pair_ok)
        glitch[1:] = pair_ok & ((moves[1:] > 2.0) | (moves[1:] < 0.5))
    glitch_cdf = np.concatenate(([0], np.cumsum(glitch, dtype=np.int64)))
    lo = np.minimum(positions + 2, size)
    hi = np.minimum(positions + 32, size)
    result["flag_extreme_label"] = ((glitch_cdf[hi] - glitch_cdf[lo]) > 0).astype(np.uint8)

    return pd.DataFrame(result, columns=[
        "date", "asset_id", "is_common", "flag_extreme_label", *RAW_FEATURES,
        *tuple(f"target_return_{h}d" for h in range(1, 31)),
    ])


def _inverse_normal(probabilities: np.ndarray) -> np.ndarray:
    """Vectorized Acklam inverse-normal approximation for probabilities in (0, 1)."""
    p = np.asarray(probabilities, dtype=np.float64)
    out = np.full(p.shape, np.nan, dtype=np.float64)
    valid = np.isfinite(p) & (p > 0.0) & (p < 1.0)
    x = p[valid]
    a = (-3.969683028665376e1, 2.209460984245205e2, -2.759285104469687e2,
         1.383577518672690e2, -3.066479806614716e1, 2.506628277459239)
    b = (-5.447609879822406e1, 1.615858368580409e2, -1.556989798598866e2,
         6.680131188771972e1, -1.328068155288572e1)
    c = (-7.784894002430293e-3, -3.223964580411365e-1, -2.400758277161838,
         -2.549732539343734, 4.374664141464968, 2.938163982698783)
    d = (7.784695709041462e-3, 3.224671290700398e-1, 2.445134137142996, 3.754408661907416)
    low = 0.02425
    high = 1.0 - low
    lower = x < low
    upper = x > high
    middle = ~(lower | upper)
    values = np.empty_like(x)
    if lower.any():
        q = np.sqrt(-2.0 * np.log(x[lower]))
        values[lower] = (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
            ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0)
    if upper.any():
        q = np.sqrt(-2.0 * np.log(1.0 - x[upper]))
        values[upper] = -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
            ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0)
    if middle.any():
        q = x[middle] - 0.5
        r = q * q
        values[middle] = (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / \
            (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1.0)
    out[valid] = values
    return out


def _add_cross_sectional_features(frame: pd.DataFrame) -> None:
    grouped = frame.groupby("date", sort=False)
    counts = grouped[list(STOCK_RAW)].transform("count")
    for raw_name, cs_name in zip(STOCK_RAW, CS_FEATURES, strict=True):
        ranks = grouped[raw_name].rank(method="average")
        probabilities = (ranks - 0.5) / counts[raw_name]
        frame[cs_name] = _inverse_normal(probabilities.to_numpy()).astype(np.float32)


def _arrow_table(frame: pd.DataFrame) -> pa.Table:
    dates = pa.array(frame["date"].dt.date, type=pa.date32())
    without_date = frame.drop(columns="date")
    table = pa.Table.from_pandas(without_date, preserve_index=False)
    return table.add_column(0, "date", dates)


def _clean_outputs(output: Path) -> None:
    samples = output / "samples"
    if samples.exists():
        shutil.rmtree(samples)
    for stale_stage in output.glob("samples-stage-*"):
        if stale_stage.is_dir():
            shutil.rmtree(stale_stage)
        else:
            stale_stage.unlink()
    for name in ("meta.parquet", "manifest.json", "splits.json", "qc_report.md", "qc_report.json"):
        path = output / name
        if path.exists():
            path.unlink()
    samples.mkdir(parents=True, exist_ok=True)


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")


def _stats_block(values: np.ndarray) -> dict[str, float | int | None]:
    if values.size == 0:
        return {"n": 0, "mean": None, "std": None, "p50": None, "p99": None,
                "exact_zero_fraction": None}
    return {
        "n": int(values.size), "mean": float(values.mean()),
        "std": float(values.std(ddof=1)) if values.size > 1 else 0.0,
        "p50": float(np.percentile(values, 50)), "p99": float(np.percentile(values, 99)),
        "exact_zero_fraction": float((values == 0.0).mean()),
    }


def _label_qc(samples_dir: Path) -> dict[str, dict[str, dict[str, float | int | None]]]:
    parquet_files = sorted(samples_dir.glob("year=*/*.parquet"))
    result: dict[str, dict[str, dict[str, float | int | None]]] = {}
    for column in (*(f"target_return_{h}d" for h in range(1, 31)), "excess_5d", "excess_21d"):
        all_chunks: list[np.ndarray] = []
        clean_chunks: list[np.ndarray] = []
        for path in parquet_files:
            table = pq.read_table(path, columns=[column, "flag_extreme_label"])
            values = np.asarray(table[column].to_numpy(zero_copy_only=False), dtype=np.float64)
            flags = np.asarray(table["flag_extreme_label"].to_numpy(zero_copy_only=False)).astype(bool)
            finite = np.isfinite(values)
            values, flags = values[finite], flags[finite]
            if values.size:
                all_chunks.append(values)
                if (~flags).any():
                    clean_chunks.append(values[~flags])
        result[column] = {
            "all": _stats_block(np.concatenate(all_chunks) if all_chunks else np.empty(0)),
            "clean": _stats_block(np.concatenate(clean_chunks) if clean_chunks else np.empty(0)),
        }
    return result


def _hash_output_files(output: Path, year_rows: dict[int, int], meta_rows: int) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for path in sorted(output.rglob("*")):
        if not path.is_file() or path.name == "manifest.json":
            continue
        relative = str(path.relative_to(output))
        row_count = None
        if path.parent.parent == output / "samples":
            year = int(path.parent.name.removeprefix("year="))
            row_count = year_rows.get(year, 0)
        elif path.name == "meta.parquet":
            row_count = meta_rows
        records[relative] = {"sha256": _sha256(path), "rows": row_count, "bytes": path.stat().st_size}
    return records


def build_samples(
    data_dir: str | Path,
    output_dir: str | Path,
    exclusions_file: str | Path | None = None,
    *,
    canonical_min_tickers: int = 500,
    rank_batch_sessions: int = 40,
    staging_tickers: int = 50,
) -> dict[str, Any]:
    """Create the stock/date training sample long table and its audit outputs.

    ``data_dir`` points to the organized directory containing ``stocks/`` and
    ``shared/macro.csv``. Processing is streamed per ticker, with temporary
    bounded ticker batches used to calculate date-level cross-sectional ranks.
    """
    organized = Path(data_dir)
    output = Path(output_dir)
    stocks_dir = organized / "stocks"
    macro_path = organized / "shared" / "macro.csv"
    if not stocks_dir.is_dir():
        raise FileNotFoundError(f"organized stocks directory does not exist: {stocks_dir}")
    if not macro_path.is_file():
        raise FileNotFoundError(f"organized macro file does not exist: {macro_path}")
    if canonical_min_tickers < 1:
        raise ValueError("canonical_min_tickers must be a positive integer")
    if rank_batch_sessions < 1 or staging_tickers < 1:
        raise ValueError("rank_batch_sessions and staging_tickers must be positive")
    if exclusions_file is None:
        exclusions_path = Path(__file__).resolve().parents[1] / "config" / "universes" / "exclusions_v1.json"
    else:
        exclusions_path = Path(exclusions_file)
    exclusions = _load_exclusions(exclusions_path)
    exclusion_set = set(exclusions)

    stock_directories = sorted(path.name.upper() for path in stocks_dir.iterdir() if path.is_dir())
    ticker_paths = sorted(stocks_dir.glob("*/market.csv"))
    market_ticker_ids = {path.parent.name.upper() for path in ticker_paths}
    missing_market_ticker_ids = sorted(
        set(stock_directories) - market_ticker_ids - exclusion_set
    )
    if not ticker_paths:
        raise ValueError(f"no stock market.csv files found under {stocks_dir}")
    ticker_map: dict[str, Path] = {}
    discovered_asset_ids = {path.parent.name.upper() for path in ticker_paths}
    financial_ticker_count = sum((path.parent / "financials.csv").is_file() for path in ticker_paths)
    first_pass_errors: list[dict[str, str]] = []
    date_counts: dict[pd.Timestamp, int] = {}
    common_tickers = 0
    for market_path in ticker_paths:
        asset_id = market_path.parent.name.upper()
        if asset_id in exclusion_set:
            continue
        if asset_id in ticker_map:
            raise ValueError(f"duplicate ticker directories after uppercasing: {asset_id}")
        ticker_map[asset_id] = market_path
        common = _is_common(asset_id)
        if common:
            common_tickers += 1
        try:
            dates = pd.read_csv(market_path, usecols=["date"])["date"]
            parsed = pd.to_datetime(dates, errors="coerce").dropna().dt.normalize().drop_duplicates()
            if common:
                for value in parsed:
                    date_counts[value] = date_counts.get(value, 0) + 1
        except Exception as exc:
            first_pass_errors.append({"asset_id": asset_id, "stage": "calendar_scan", "error": str(exc)})
    if common_tickers == 0:
        raise ValueError("no non-excluded common-stock tickers are available to form the canonical calendar")
    minimum_count = canonical_min_tickers
    calendar_values = sorted(value for value, count in date_counts.items() if count >= minimum_count)
    if not calendar_values:
        raise ValueError(
            f"canonical calendar is empty: no dates meet the absolute floor of {minimum_count} "
            f"common tickers (universe has {common_tickers})"
        )
    calendar = pd.DatetimeIndex(calendar_values, name="date")
    macro = _macro_matrix(macro_path, calendar)
    purge_windows = _purge_windows(calendar)
    purge_split_by_date = {
        calendar[position]: window["purged_split"]
        for window in purge_windows.values()
        for position in window["_positions"]
    }

    _clean_outputs(output)
    failures = list(first_pass_errors)
    meta_records: list[dict[str, Any]] = []
    ticker_items = list(ticker_map.items())
    output_target_columns = [f"target_return_{horizon}d" for horizon in range(1, 31)]
    year_rows: dict[int, int] = {}
    split_names = ("fit", "select", "screen", "reserve")
    split_rows = {name: 0 for name in split_names}
    pre_purge_split_rows = {name: 0 for name in split_names}
    purge_rows_by_split = {name: 0 for name in split_names}
    missing_counts = {column: 0 for column in (*RAW_FEATURES, *CS_FEATURES, *MISS_FEATURES)}
    total_rows = 0
    cross_section_counts: dict[pd.Timestamp, int] = {}
    extreme_return_count = 0
    extreme_examples: list[dict[str, Any]] = []
    writers: dict[int, pq.ParquetWriter] = {}

    try:
        with tempfile.TemporaryDirectory(prefix="samples-stage-", dir=output) as stage_name:
            stage_dir = Path(stage_name)
            for batch_start in range(0, len(ticker_items), staging_tickers):
                frames: list[pd.DataFrame] = []
                for asset_id, market_path in ticker_items[batch_start:batch_start + staging_tickers]:
                    financial_path = market_path.with_name("financials.csv")
                    try:
                        frame = _ticker_samples(
                            asset_id, market_path,
                            financial_path if financial_path.is_file() else None,
                            calendar, macro, _is_common(asset_id),
                        )
                    except Exception as exc:
                        failures.append({"asset_id": asset_id, "stage": "ticker_build", "error": str(exc)})
                        logger.exception("Failed to build samples for %s", asset_id)
                        continue
                    if frame.empty:
                        continue
                    purged_splits = frame["date"].map(purge_split_by_date)
                    for split_name, count in purged_splits.value_counts().items():
                        purge_rows_by_split[str(split_name)] += int(count)
                    pre_purge_split_rows["fit"] += int(frame["date"].le(pd.Timestamp("2018-12-31")).sum())
                    pre_purge_split_rows["select"] += int(frame["date"].between("2019-01-01", "2020-12-31").sum())
                    pre_purge_split_rows["screen"] += int(frame["date"].between("2021-01-01", "2024-12-31").sum())
                    pre_purge_split_rows["reserve"] += int(frame["date"].ge(pd.Timestamp("2025-01-01")).sum())
                    frame = frame.loc[purged_splits.isna()].reset_index(drop=True)
                    split_rows["fit"] += int(frame["date"].le(pd.Timestamp("2018-12-31")).sum())
                    split_rows["select"] += int(frame["date"].between("2019-01-01", "2020-12-31").sum())
                    split_rows["screen"] += int(frame["date"].between("2021-01-01", "2024-12-31").sum())
                    split_rows["reserve"] += int(frame["date"].ge(pd.Timestamp("2025-01-01")).sum())
                    if frame.empty:
                        continue
                    raw_missing = int(frame.loc[:, RAW_FEATURES].isna().to_numpy().sum())
                    raw_total = int(frame.shape[0] * len(RAW_FEATURES))
                    meta_records.append({
                        "asset_id": asset_id, "is_common": _is_common(asset_id),
                        "first_date": frame["date"].min(), "last_date": frame["date"].max(),
                        "n_rows": int(len(frame)), "missing_frac": raw_missing / raw_total if raw_total else 0.0,
                    })
                    frames.append(frame)
                if not frames:
                    continue
                combined = pd.concat(frames, ignore_index=True)
                combined = combined.sort_values(["date", "asset_id"], kind="mergesort")
                session_positions = calendar.get_indexer(combined["date"])
                chunk_ids = session_positions // rank_batch_sessions
                combined["_date_chunk"] = chunk_ids.astype(np.int32)
                for chunk_id, chunk_frame in combined.groupby("_date_chunk", sort=True):
                    stage_path = stage_dir / f"chunk={int(chunk_id):05d}" / \
                        f"stage-{batch_start // staging_tickers:05d}.parquet"
                    stage_path.parent.mkdir(parents=True, exist_ok=True)
                    chunk_frame.drop(columns="_date_chunk").to_parquet(
                        stage_path, engine="pyarrow", compression="snappy", index=False, row_group_size=8192,
                    )
                del combined, frames

            for date_start in range(0, len(calendar), rank_batch_sessions):
                chunk_id = date_start // rank_batch_sessions
                chunk_path = stage_dir / f"chunk={chunk_id:05d}"
                if not chunk_path.exists():
                    continue
                staged = ds.dataset(chunk_path, format="parquet")
                batch = staged.to_table().to_pandas()

                if batch.empty:
                    continue
                batch["date"] = pd.to_datetime(batch["date"])
                _add_cross_sectional_features(batch)
                for raw_name, miss_name in zip(RAW_FEATURES, MISS_FEATURES, strict=True):
                    batch[miss_name] = batch[raw_name].isna().astype(np.uint8)
                for raw_name in RAW_FEATURES:
                    missing_counts[raw_name] += int(batch[raw_name].isna().sum())
                for cs_name in CS_FEATURES:
                    missing_counts[cs_name] += int(batch[cs_name].isna().sum())
                for miss_name in MISS_FEATURES:
                    missing_counts[miss_name] += int(batch[miss_name].isna().sum())

                common_mask = batch["is_common"].astype(bool) & batch["flag_extreme_label"].eq(0)
                for horizon in (5, 21):
                    target = f"target_return_{horizon}d"
                    mean = batch.loc[common_mask].groupby("date", sort=False)[target].mean()
                    batch[f"excess_{horizon}d"] = batch[target] - batch["date"].map(mean)
                for date_value, count in batch.groupby("date", sort=False).size().items():
                    cross_section_counts[pd.Timestamp(date_value)] = int(count)
                extreme = batch["flag_extreme_label"].eq(1)
                extreme_return_count += int(extreme.sum())
                if len(extreme_examples) < 100:
                    for row in batch.loc[extreme].loc[:, ["date", "asset_id", "target_return_1d"]].head(
                        100 - len(extreme_examples)
                    ).itertuples(index=False):
                        extreme_examples.append({
                            "date": row.date.strftime("%Y-%m-%d"), "asset_id": row.asset_id,
                            "target_return_1d": float(row.target_return_1d),
                        })

                batch = batch.sort_values(["date", "asset_id"], kind="mergesort").reset_index(drop=True)
                for year, year_frame in batch.groupby(batch["date"].dt.year, sort=True):
                    year = int(year)
                    for column in RAW_FEATURES:
                        year_frame[column] = year_frame[column].astype(np.float32)
                    for column in CS_FEATURES:
                        year_frame[column] = year_frame[column].astype(np.float32)
                    for column in LABELS:
                        year_frame[column] = year_frame[column].astype(np.float32)
                    column_order = ["date", "asset_id", "is_common", "flag_extreme_label", *RAW_FEATURES,
                                    *CS_FEATURES, *MISS_FEATURES, *LABELS]
                    table = _arrow_table(year_frame.loc[:, column_order])
                    destination = output / "samples" / f"year={year}" / "part-00000.parquet"
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    if year not in writers:
                        writers[year] = pq.ParquetWriter(destination, table.schema, compression="snappy")
                    writers[year].write_table(table)
                    row_count = len(year_frame)
                    year_rows[year] = year_rows.get(year, 0) + row_count
                    total_rows += row_count
                del batch
    finally:
        for writer in writers.values():
            writer.close()

    if total_rows == 0:
        raise ValueError("sample build produced no rows")
    meta = pd.DataFrame(meta_records, columns=["asset_id", "is_common", "first_date", "last_date",
                                               "n_rows", "missing_frac"])
    meta = meta.sort_values("asset_id", kind="mergesort").reset_index(drop=True)
    meta_table = pa.Table.from_pandas(meta, preserve_index=False)
    if "first_date" in meta_table.column_names:
        meta_table = meta_table.set_column(meta_table.schema.get_field_index("first_date"), "first_date",
                                           pa.array(pd.to_datetime(meta["first_date"]).dt.date, type=pa.date32()))
        meta_table = meta_table.set_column(meta_table.schema.get_field_index("last_date"), "last_date",
                                           pa.array(pd.to_datetime(meta["last_date"]).dt.date, type=pa.date32()))
    pq.write_table(meta_table, output / "meta.parquet", compression="snappy")

    data_start = min(item["first_date"] for item in meta_records).date().isoformat()
    data_end = max(item["last_date"] for item in meta_records).date().isoformat()
    public_purge_windows = _public_purge_windows(purge_windows)
    for window in public_purge_windows.values():
        window["rows_removed"] = purge_rows_by_split[window["purged_split"]]
    splits = {
        "fit": [data_start, "2018-12-31"],
        "select": ["2019-01-01", "2020-12-31"],
        "screen": ["2021-01-01", "2024-12-31"],
        "reserve": ["2025-01-01", data_end],
        "purge_sessions": _PURGE_SESSIONS,
        "purge_semantics": _PURGE_SEMANTICS,
        "purged_windows": public_purge_windows,
        "rows_by_split": {
            name: {
                "before_purge": pre_purge_split_rows[name],
                "purged": purge_rows_by_split[name],
                "retained": split_rows[name],
            }
            for name in split_names
        },
        "rows_removed_by_split": purge_rows_by_split,
    }
    _write_json(output / "splits.json", splits)

    label_stats = _label_qc(output / "samples")
    year_cross_sections: dict[str, dict[str, float | int]] = {}
    for year in sorted(year_rows):
        sizes = [count for date_value, count in cross_section_counts.items() if date_value.year == year]
        if sizes:
            year_cross_sections[str(year)] = {
                "dates": len(sizes), "min": int(min(sizes)),
                "median": float(np.median(sizes)), "max": int(max(sizes)),
            }
    missingness = {
        column: {"missing": missing_counts[column], "fraction": missing_counts[column] / total_rows}
        for column in (*RAW_FEATURES, *CS_FEATURES)
    }
    qc = {
        "rows_total": total_rows,
        "rows_per_year": {str(year): count for year, count in sorted(year_rows.items())},
        "label_stats": label_stats,
        "missingness_per_feature": missingness,
        "cross_section_size_per_year": year_cross_sections,
        "purge": {
            "purge_sessions": _PURGE_SESSIONS,
            "semantics": _PURGE_SEMANTICS,
            "rows_by_split": splits["rows_by_split"],
        },
        "extreme_labels": {
            "flag_extreme_label_count": extreme_return_count,
            "flag_extreme_label_rule": (
                "row flagged when any 1..30-session label window crosses a source price glitch: "
                "consecutive-session adjusted-open ratio outside [0.5, 2.0]"
            ),
            "examples": extreme_examples,
        },
        "ticker_failures": failures,
    }
    _write_json(output / "qc_report.json", qc)
    lines = [
        "# Training sample build QC report", "", f"- Total rows: {total_rows:,}",
        f"- Date range: {data_start} through {data_end}", f"- Ticker failures: {len(failures)}", "",
        "## Rows per year", "", "| Year | Rows |", "|---:|---:|",
    ]
    lines.extend(f"| {year} | {count:,} |" for year, count in sorted(year_rows.items()))
    lines.extend(["", "## Label statistics (clean = excluding flag_extreme_label rows)", "",
                  "| Label | N | Mean | Std | P50 | P99 | ZeroFrac | N(clean) | Std(clean) | P99(clean) |",
                  "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"])
    for column, block in label_stats.items():
        all_s, clean_s = block["all"], block["clean"]
        def _render(stats: dict[str, float | int | None]) -> list[str]:
            out = [f"{stats['n']:,}"]
            out.extend("NA" if stats[key] is None else f"{stats[key]:.6g}" for key in ("mean", "std", "p50", "p99"))
            return out
        row = _render(all_s)
        row.append("NA" if all_s["exact_zero_fraction"] is None else f"{all_s['exact_zero_fraction']:.4f}")
        row += [f"{clean_s['n']:,}",
                "NA" if clean_s["std"] is None else f"{clean_s['std']:.6g}",
                "NA" if clean_s["p99"] is None else f"{clean_s['p99']:.6g}"]
        lines.append(f"| {column} | " + " | ".join(row) + " |")
    lines.extend(["", "## Cross-section size by year", "", "| Year | Dates | Min | Median | Max |",
                  "|---:|---:|---:|---:|---:|"])
    lines.extend(
        f"| {year} | {stats['dates']} | {stats['min']} | {stats['median']:.1f} | {stats['max']} |"
        for year, stats in year_cross_sections.items()
    )
    lines.extend(["", "## Missingness per feature", "", "| Feature | Missing | Fraction |", "|---|---:|---:|"])
    lines.extend(
        f"| {column} | {stats['missing']:,} | {stats['fraction']:.6%} |"
        for column, stats in missingness.items()
    )
    lines.extend(["", "## Extreme labels (flag_extreme_label)", "",
                  f"Flagged rows: {extreme_return_count}. Rule: any 1..30-session label window crosses a "
                  "source price glitch (consecutive-session adjusted-open ratio outside [0.5, 2.0]). "
                  "Flagged rows are kept but excluded from excess_5d/21d benchmark means.", ""])
    lines.extend(["## Per-ticker failures", ""])
    if failures:
        lines.extend(f"- {item['asset_id']} ({item['stage']}): {item['error']}" for item in failures)
    else:
        lines.append("- None")
    (output / "qc_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    feature_list = [*RAW_FEATURES, *CS_FEATURES, *MISS_FEATURES]
    output_files = _hash_output_files(output, year_rows, len(meta))
    manifest = {
        "schema_version": "samples_v1",
        "outputs": output_files,
        "row_counts": {"samples": total_rows, "meta": len(meta), "by_year": qc["rows_per_year"]},
        "date_range": {"start": data_start, "end": data_end},
        "feature_list": feature_list,
        "feature_contract": {
            "raw_features": list(RAW_FEATURES),
            "cross_sectional_features": list(CS_FEATURES),
            "cross_sectional_method": "per-date average rank transformed by inverse normal CDF at (rank - 0.5) / non-null count",
            "cross_sectional_scope": "all included stocks with a non-null stock-varying raw feature on that date; macro columns are excluded because they are date-constant",
            "missing_indicators": {raw: miss for raw, miss in zip(RAW_FEATURES, MISS_FEATURES, strict=True)},
            "macro_differences": "d1 and d5 are exact positional differences on the canonical date axis; no forward-fill",
        },
        "label_semantics": (
            "For signal session t, entry is adjusted open at canonical session t+1 and exit for horizon h is "
            "adjusted open at canonical session t+1+h. adjusted_open = open * adj_close / close; "
            "target_return_hd = adjusted_open(t+1+h) / adjusted_open(t+1) - 1. Horizons are positional "
            "canonical-session offsets; exact entry and exit bars must exist and be positive/finite, with no "
            "substitution, forward-fill, or intermediate-bar requirement. Primary future-model targets are "
            "excess_5d and excess_21d; other target_return horizons are auxiliary. Because SPY is absent, "
            "excess returns subtract the same-date equal-weight mean target among is_common stocks."
        ),
        "build_params": {
            "canonical_axis_rule": "absolute floor: dates with >= canonical_min_tickers is_common tickers present",
            "canonical_min_tickers": canonical_min_tickers,
            "canonical_minimum_common_tickers": minimum_count,
            "common_tickers_in_denominator": common_tickers,
            "canonical_session_count": len(calendar),
            "rank_batch_sessions": rank_batch_sessions,
            "staging_tickers": staging_tickers,
            "parquet_compression": "snappy",
            "sample_partitioning": "samples/year=YYYY/part-00000.parquet",
            "derived_windows": "60 and 120 canonical sessions; past/current data only",
            "financial_asof": "latest available_as_of <= signal date; year-over-year compares the same fiscal_period in fiscal_year - 1 when identifiers exist, otherwise the nearest report-period end in the prior report year within 15 calendar days of the date one year earlier (closest match, ties choose earlier period-end)",
            "purge_sessions": _PURGE_SESSIONS,
            "purge_semantics": _PURGE_SEMANTICS,
            "rows_by_split": splits["rows_by_split"],
        },
        "data_quality_flags": {
            "flag_extreme_label": {
                "dtype": "uint8",
                "rule": "1 when any 1..30-session label window (entry t+1 .. exit t+1+h) crosses a source "
                        "price glitch: consecutive-session adjusted-open ratio outside [0.5, 2.0]",
                "handling": "rows are kept; excluded from excess_5d/21d same-date benchmark means; "
                            "training layer should drop or down-weight them",
                "count": extreme_return_count,
            },
        },
        "known_biases": [
            "Survivorship: the universe is the current SEC ticker list and has no delisted tickers.",
            "FRED values are latest revised values rather than vintage values.",
            "Adjusted-open label prices are not executable fills.",
            "is_common is a suffix heuristic; it does not replace security-master classification.",
        ],
        "exclusions_applied": {
            "file": str(exclusions_path), "asset_ids": exclusions,
            "dropped_asset_ids_present": sorted(exclusion_set.intersection(discovered_asset_ids)),
        },
        "input_inventory": {
            "stock_directories": len(stock_directories),
            "market_ticker_files": len(ticker_paths),
            "directories_without_market_csv": len(missing_market_ticker_ids),
            "tickers_without_market_csv": missing_market_ticker_ids,
            "financial_ticker_files": financial_ticker_count,
            "financial_features_missing_for_universe": financial_ticker_count == 0,
            "calendar_denominator_scope": "is_common tickers with market.csv files, excluding explicit exclusions",
        },
        "is_common_column": "Rows whose ticker suffix heuristically indicates units, warrants, preferreds, or rights are retained with is_common=false; all rows remain eligible for cross-sectional feature ranks.",
        "benchmark": "No SPY exists in the organized data; excess_5d and excess_21d subtract the same-day equal-weight is_common-stock target mean.",
        "manifest_hash_note": "manifest.json is omitted from its own outputs hash map to avoid a self-referential hash.",
    }
    _write_json(output / "manifest.json", manifest)
    logger.info("Built %s sample rows from %s through %s", total_rows, data_start, data_end)
    return {"rows": total_rows, "date_start": data_start, "date_end": data_end,
            "year_rows": year_rows, "failures": failures, "output_dir": str(output)}


__all__ = ["build_samples"]
