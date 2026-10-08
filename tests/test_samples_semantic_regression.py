"""Independent semantic checks for the current Samples behavior."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from samples import builder
from samples.builder import build_samples
from samples.contracts import MACRO_SERIES, STOCK_RAW


def _write_market(
    path: Path,
    dates: pd.DatetimeIndex,
    *,
    base: float,
    step: float,
    asset_index: int,
    glitch_position: int | None = None,
) -> None:
    regular_open = base + step * np.arange(len(dates), dtype=np.float64)
    open_values = regular_open.copy()
    if glitch_position is not None:
        open_values[glitch_position] *= 4.0
    close_values = regular_open * 1.05
    frame = pd.DataFrame(
        {
            "date": dates,
            "open": open_values,
            "close": close_values,
            "adj_close": close_values * 1.1,
            "volume": 1000.0 + np.arange(len(dates)) * 7.0 + asset_index,
            "return_1d": 0.01 * (asset_index + 1) + np.arange(len(dates)) / 10000.0,
            "return_5d": 0.02 * (asset_index + 1),
            "return_20d": 0.03 * (asset_index + 1),
            "volatility_20": 0.1 + np.arange(len(dates)) / 10000.0,
            "volume_ratio_20": 1.0 + np.arange(len(dates)) / 100.0,
            "intraday_range": 0.02 + np.arange(len(dates)) / 10000.0,
        }
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=False)


def _build_extreme_fixture(tmp_path: Path) -> pd.DataFrame:
    dates = pd.bdate_range("2022-01-03", periods=80)
    workspace = tmp_path / "workspace"
    exclusions_path = workspace / "config" / "universes" / "exclusions_v1.json"
    exclusions_path.parent.mkdir(parents=True)
    exclusions_path.write_text('{"entries": ["DROP"]}', encoding="utf-8")

    organized = tmp_path / "organized" / "stocks"
    _write_market(
        organized / "AAA" / "market.csv",
        dates,
        base=100.0,
        step=2.0,
        asset_index=0,
        glitch_position=20,
    )
    _write_market(
        organized / "BBB" / "market.csv", dates, base=200.0, step=3.0, asset_index=1
    )
    _write_market(
        organized / "CCC" / "market.csv", dates, base=300.0, step=4.0, asset_index=2
    )
    _write_market(
        organized / "UNIT-WT" / "market.csv", dates, base=150.0, step=1.0, asset_index=3
    )
    _write_market(
        organized / "DROP" / "market.csv", dates, base=80.0, step=0.5, asset_index=4
    )

    shared = organized.parent / "shared"
    shared.mkdir()
    macro = {"date": dates}
    for index, name in enumerate(MACRO_SERIES):
        macro[name] = index + 1.0 + np.arange(len(dates), dtype=np.float64)
    pd.DataFrame(macro).to_csv(shared / "macro.csv", index=False)

    output = tmp_path / "fresh-output"
    build_samples(
        organized.parent,
        output,
        exclusions_path,
        workspace_root=workspace,
        canonical_min_tickers=2,
        rank_batch_sessions=9,
        staging_tickers=2,
    )
    parts = sorted((output / "samples").glob("year=*/*.parquet"))
    return pd.concat([pd.read_parquet(path) for path in parts], ignore_index=True)


def test_average_tie_ranks_match_fixed_inverse_normal_quantiles() -> None:
    dates = pd.to_datetime(["2024-01-02"] * 4)
    frame = pd.DataFrame({"date": dates, "asset_id": ["A", "B", "C", "D"]})
    for feature in STOCK_RAW:
        frame[feature] = np.nan
    frame["f_raw_return_1d"] = np.asarray([0.1, 0.1, 0.9, np.nan], dtype=np.float32)

    builder._add_cross_sectional_features(frame)

    # Average ranks 1.5, 1.5, and 3 yield probabilities 1/3, 1/3, and 5/6.
    expected_low = np.float32(-0.43072729929545756)
    expected_high = np.float32(0.967421566101701)
    assert frame["f_cs_return_1d"].iloc[0] == expected_low
    assert frame["f_cs_return_1d"].iloc[1] == expected_low
    assert frame["f_cs_return_1d"].iloc[2] == expected_high
    assert pd.isna(frame["f_cs_return_1d"].iloc[3])


def test_extreme_label_rows_are_excluded_from_hand_computed_excess_mean(
    tmp_path: Path,
) -> None:
    samples = _build_extreme_fixture(tmp_path)
    day = pd.Timestamp("2022-01-21")  # Signal position 14; glitch is inside its label window.
    rows = samples.loc[pd.to_datetime(samples["date"]).eq(day)].set_index("asset_id")
    assert int(rows.loc["AAA", "flag_extreme_label"]) == 1
    assert int(rows.loc["BBB", "flag_extreme_label"]) == 0
    assert int(rows.loc["CCC", "flag_extreme_label"]) == 0

    # For t=14 and h=5, entry is session 15 and exit is session 20. The unit
    # ticker and flagged AAA are not in the benchmark mean; only BBB and CCC are.
    signal_position, horizon = 14, 5
    bbb_entry = 200.0 + 3.0 * (signal_position + 1)
    bbb_exit = 200.0 + 3.0 * (signal_position + 1 + horizon)
    ccc_entry = 300.0 + 4.0 * (signal_position + 1)
    ccc_exit = 300.0 + 4.0 * (signal_position + 1 + horizon)
    bbb = np.float32(bbb_exit / bbb_entry - 1.0)
    ccc = np.float32(ccc_exit / ccc_entry - 1.0)
    expected_excess_bbb = np.float32(bbb - np.mean(np.asarray([bbb, ccc], dtype=np.float32)))
    expected_excess_ccc = np.float32(ccc - np.mean(np.asarray([bbb, ccc], dtype=np.float32)))
    assert rows.loc["BBB", "target_return_5d"] == bbb
    assert rows.loc["CCC", "target_return_5d"] == ccc
    assert rows.loc["BBB", "excess_5d"] == expected_excess_bbb
    assert rows.loc["CCC", "excess_5d"] == expected_excess_ccc
    assert rows.loc["AAA", "excess_5d"] != rows.loc["AAA", "target_return_5d"]
