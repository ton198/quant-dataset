"""Tests for the partitioned training sample long table builder."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from build_samples import MACRO_SERIES, _financial_features, build_samples


def _fixture(
    tmp_path: Path,
    dates: pd.DatetimeIndex | None = None,
    *,
    infinite_raw_returns: bool = False,
) -> tuple[Path, Path]:
    organized = tmp_path / "organized"
    stocks = organized / "stocks"
    stocks.mkdir(parents=True)
    if dates is None:
        dates = pd.bdate_range("2020-01-02", periods=45)
    tickers = {"AAA": 0, "BBB": 1, "CCC": 2, "UNIT-WT": 3, "DROP": 4}
    for ticker, offset in tickers.items():
        ticker_dir = stocks / ticker
        ticker_dir.mkdir()
        keep = np.ones(len(dates), dtype=bool)
        if ticker == "CCC":
            # This date remains on the global calendar but has no exact bar for CCC.
            keep[6] = False
        indices = np.flatnonzero(keep)
        opens = 100.0 + offset * 10.0 + indices * 2.0
        closes = opens + 1.0
        frame = pd.DataFrame(
            {
                "date": dates[indices].strftime("%Y-%m-%d"),
                "open": opens,
                "high": closes + 1.0,
                "low": opens - 1.0,
                "close": closes,
                "adj_close": closes * 2.0,
                "volume": 1000.0 + indices * 3.0,
                "adjustment_factor": 2.0,
                "return_1d": (offset + 1) * 0.01 + indices * 0.0001,
                "return_5d": (offset + 1) * 0.02,
                "return_20d": (offset + 1) * 0.03,
                "volatility_20": (offset + 1) * 0.1,
                "volume_ratio_20": (offset + 1) * 1.2,
                "intraday_range": (offset + 1) * 0.005,
                "quality_flag": "ok",
            }
        )
        if ticker == "BBB":
            frame.loc[frame.index[5], "return_1d"] = np.nan
            if infinite_raw_returns:
                frame.loc[frame.index[6], "return_1d"] = np.inf
                frame.loc[frame.index[7], "return_5d"] = -np.inf
                frame.loc[frame.index[8], "return_20d"] = np.inf
            # source price glitch at session 40 (e.g. unadjusted split): open jumps ~20x
            frame.loc[frame.index[40] :, ["open", "high", "low", "close", "adj_close"]] *= 20.0
        frame.to_csv(ticker_dir / "market.csv", index=False)
        financials = pd.DataFrame(
            {
                "date": dates.strftime("%Y-%m-%d"),
                "available_as_of": dates.strftime("%Y-%m-%d"),
                "accession_number": [f"{ticker}-{index}" for index in range(len(dates))],
                "form": "10-Q",
                "is_amendment": False,
                "fiscal_year": 2023,
                "fiscal_period": "Q1",
                "report_period_end": "2023-03-31",
                "days_since_filing": 0,
                "revenue": 100.0 + np.arange(len(dates)),
                "gross_profit": 50.0,
                "operating_income": 10.0 + np.arange(len(dates)),
                "net_income": 8.0 + np.arange(len(dates)),
                "operating_cash_flow": 12.0,
                "capital_expenditure": 2.0,
                "assets": 500.0 + np.arange(len(dates)),
                "liabilities": 200.0,
                "equity": 300.0,
                "quality_status": "ok",
            }
        )
        financials.to_csv(ticker_dir / "financials.csv", index=False)

    shared = organized / "shared"
    shared.mkdir()
    macro = pd.DataFrame({"date": dates.strftime("%Y-%m-%d")})
    for index, series in enumerate(MACRO_SERIES):
        macro[series] = np.arange(len(dates), dtype=float) + index + 1.0
    macro.to_csv(shared / "macro.csv", index=False)
    exclusions = tmp_path / "exclusions.json"
    exclusions.write_text(
        json.dumps({"schema": "test", "entries": [{"asset_id": "DROP"}]}), encoding="utf-8"
    )
    return organized, exclusions


def _read_samples(output: Path) -> pd.DataFrame:
    parts = sorted((output / "samples").glob("year=*/*.parquet"))
    return pd.concat([pd.read_parquet(path) for path in parts], ignore_index=True)


def test_training_sample_build_labels_features_exclusions_and_splits(tmp_path: Path) -> None:
    organized, exclusions = _fixture(tmp_path)
    output = tmp_path / "output"
    summary = build_samples(
        organized,
        output,
        exclusions,
        rank_batch_sessions=10,
        staging_tickers=2,
        canonical_min_tickers=2,
    )
    samples = _read_samples(output)

    assert summary["rows"] == 45 * 4 - 1
    assert "DROP" not in set(samples["asset_id"])
    assert samples.loc[samples["asset_id"].eq("UNIT-WT"), "is_common"].eq(False).all()
    assert samples.loc[samples["asset_id"].eq("AAA"), "is_common"].eq(True).all()

    first_date = samples.loc[samples["asset_id"].eq("AAA"), "date"].min()
    first = samples.loc[samples["asset_id"].eq("AAA") & samples["date"].eq(first_date)].iloc[0]
    source_open = 100.0 + 2.0 * np.arange(45)
    for horizon in (5, 21, 30):
        expected = source_open[1 + horizon] / source_open[1] - 1.0
        assert first[f"target_return_{horizon}d"] == np.float32(expected)
    ccc_first = samples.loc[samples["asset_id"].eq("CCC") & samples["date"].eq(first_date)].iloc[0]
    assert pd.isna(ccc_first["target_return_5d"])
    assert ccc_first["target_return_6d"] == np.float32(
        (120.0 + 2.0 * 7.0) / (120.0 + 2.0 * 1.0) - 1.0
    )

    rank_date = samples.loc[samples["asset_id"].eq("AAA"), "date"].iloc[10]
    cross_section = samples.loc[samples["date"].eq(rank_date)].sort_values("f_raw_return_1d")
    ranks = cross_section["f_cs_return_1d"].to_numpy()
    assert np.all(np.diff(ranks) >= 0)
    assert abs(float(np.median(ranks))) < 1e-6
    assert not any(name.startswith("f_cs_m_") for name in samples.columns)

    missing_row = samples.loc[
        samples["asset_id"].eq("BBB")
        & samples["date"].eq(samples.loc[samples.asset_id.eq("BBB"), "date"].iloc[5])
    ].iloc[0]
    assert np.isnan(missing_row["f_raw_return_1d"])
    assert missing_row["miss_return_1d"] == 1
    assert samples["miss_return_1d"].isin([0, 1]).all()
    assert pd.api.types.is_integer_dtype(samples["miss_return_1d"])

    aaa_rows = samples.loc[samples["asset_id"].eq("AAA")]
    assert aaa_rows["flag_extreme_label"].eq(0).all()
    bbb_rows = samples.loc[samples["asset_id"].eq("BBB")]
    assert bbb_rows.loc[bbb_rows["date"].eq(first_date), "flag_extreme_label"].eq(0).all()
    assert int(bbb_rows["flag_extreme_label"].sum()) > 0
    assert samples["flag_extreme_label"].isin([0, 1]).all()

    date_rows = samples.loc[samples["date"].eq(first_date)]
    eligible = date_rows["is_common"] & date_rows["flag_extreme_label"].eq(0)
    common_mean = date_rows.loc[eligible, "target_return_5d"].mean()
    for row in date_rows.itertuples(index=False):
        assert np.isclose(row.excess_5d, row.target_return_5d - common_mean, equal_nan=True)

    splits = json.loads((output / "splits.json").read_text(encoding="utf-8"))
    assert splits["fit"][0] == summary["date_start"]
    assert splits["reserve"][1] == summary["date_end"]
    assert splits["purge_sessions"] == 30
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == "samples_v1"
    assert manifest["build_params"]["canonical_minimum_common_tickers"] == 2
    assert "latest revised" in " ".join(manifest["known_biases"])


def test_financial_features_are_asof_and_use_prior_fiscal_period(tmp_path: Path) -> None:
    path = tmp_path / "financials.csv"
    pd.DataFrame(
        {
            "available_as_of": ["2020-04-01", "2021-04-01", "2022-04-01"],
            "report_period_end": ["2020-03-31", "2021-03-31", "2022-03-31"],
            "fiscal_year": [2020, 2021, 2022],
            "fiscal_period": ["Q1", "Q1", "Q1"],
            "revenue": [100.0, 125.0, 150.0],
            "net_income": [10.0, 15.0, 20.0],
            "operating_income": [20.0, 25.0, 30.0],
            "assets": [1000.0, 1100.0, 1200.0],
        }
    ).to_csv(path, index=False)
    dates = pd.DatetimeIndex(pd.to_datetime(["2021-03-31", "2021-04-01", "2022-04-01"]))
    features = _financial_features(path, dates)

    assert np.isnan(features["f_raw_revenue_yoy"][0])
    assert features["f_raw_revenue_yoy"][1] == np.float32(0.25)
    assert features["f_raw_net_income_yoy"][1] == np.float32(0.5)
    assert features["f_raw_assets_yoy"][2] == np.float32(1200.0 / 1100.0 - 1.0)
    assert features["f_raw_days_since_filing"].tolist() == [364.0, 0.0, 0.0]


def test_financial_yoy_falls_back_to_nearest_prior_report_period(tmp_path: Path) -> None:
    path = tmp_path / "financials.csv"
    pd.DataFrame(
        {
            "available_as_of": ["2019-04-01", "2019-05-01", "2020-04-01"],
            "report_period_end": ["2019-03-20", "2019-04-05", "2020-03-28"],
            "revenue": [100.0, 200.0, 300.0],
            "net_income": [10.0, 20.0, 30.0],
            "operating_income": [20.0, 40.0, 60.0],
            "assets": [1000.0, 2000.0, 3000.0],
        }
    ).to_csv(path, index=False)
    features = _financial_features(path, pd.DatetimeIndex(pd.to_datetime(["2020-04-01"])))

    # The candidate dates are equally distant from 2019-03-28; the earlier
    # report-period end wins, so the comparison is against the 100 value.
    assert features["f_raw_revenue_yoy"][0] == np.float32(2.0)
    assert features["f_raw_net_income_yoy"][0] == np.float32(2.0)


def test_fiscal_identifiers_take_precedence_over_closer_report_end(tmp_path: Path) -> None:
    path = tmp_path / "financials.csv"
    pd.DataFrame(
        {
            "available_as_of": ["2019-04-01", "2019-05-01", "2020-04-01"],
            "report_period_end": ["2018-03-30", "2018-04-01", "2019-04-01"],
            "fiscal_year": [2018, 2018, 2019],
            "fiscal_period": ["Q1", "Q2", "Q1"],
            "revenue": [100.0, 200.0, 300.0],
            "net_income": [10.0, 20.0, 30.0],
            "operating_income": [20.0, 40.0, 60.0],
            "assets": [1000.0, 2000.0, 3000.0],
        }
    ).to_csv(path, index=False)
    features = _financial_features(path, pd.DatetimeIndex(pd.to_datetime(["2020-04-01"])))

    # Q1 2018 must match Q1 2019 even though the Q2 report has the exact
    # one-year-earlier report-period date.
    assert features["f_raw_revenue_yoy"][0] == np.float32(2.0)


def test_build_purges_label_windows_at_split_boundaries(tmp_path: Path) -> None:
    dates = pd.bdate_range("2018-11-01", "2025-03-03")
    organized, exclusions = _fixture(tmp_path, dates=dates)
    output = tmp_path / "output"
    summary = build_samples(
        organized,
        output,
        exclusions,
        rank_batch_sessions=40,
        staging_tickers=2,
        canonical_min_tickers=2,
    )
    samples = _read_samples(output)
    splits = json.loads((output / "splits.json").read_text(encoding="utf-8"))
    positions = {day.date(): index for index, day in enumerate(dates)}

    assert set(splits["purged_windows"]) == {"select", "screen", "reserve"}
    for _split_name, window in splits["purged_windows"].items():
        boundary_position = positions[pd.Timestamp(window["boundary_date"]).date()]
        preceding_split = window["purged_split"]
        if preceding_split == "fit":
            split_rows = samples.loc[samples["date"].le(pd.Timestamp("2018-12-31").date())]
        elif preceding_split == "select":
            split_rows = samples.loc[
                samples["date"].between(
                    pd.Timestamp("2019-01-01").date(), pd.Timestamp("2020-12-31").date()
                )
            ]
        else:
            split_rows = samples.loc[
                samples["date"].between(
                    pd.Timestamp("2021-01-01").date(), pd.Timestamp("2024-12-31").date()
                )
            ]

        assert window["sessions"] == 31
        assert window["rows_removed"] == splits["rows_by_split"][preceding_split]["purged"]
        assert splits["rows_by_split"][preceding_split]["purged"] > 0
        for row in split_rows.itertuples(index=False):
            if np.isfinite(row.target_return_30d):
                exit_position = positions[row.date] + 31
                assert exit_position < boundary_position

    assert summary["rows"] == sum(summary["year_rows"].values())
    assert splits["rows_by_split"]["fit"]["retained"] == (
        splits["rows_by_split"]["fit"]["before_purge"] - splits["rows_by_split"]["fit"]["purged"]
    )
    assert "excluded at build time" in splits["purge_semantics"]
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["row_counts"]["samples"] == summary["rows"]
    assert manifest["build_params"]["rows_by_split"] == splits["rows_by_split"]
    assert "within 15 calendar days" in manifest["build_params"]["financial_asof"]


def test_infinite_raw_returns_are_missing_with_missing_indicator(tmp_path: Path) -> None:
    organized, exclusions = _fixture(tmp_path, infinite_raw_returns=True)
    output = tmp_path / "output"
    build_samples(
        organized,
        output,
        exclusions,
        rank_batch_sessions=10,
        staging_tickers=2,
        canonical_min_tickers=2,
    )
    samples = _read_samples(output)
    rows = samples.loc[samples["asset_id"].eq("BBB")].set_index("date")

    expected = {
        pd.Timestamp("2020-01-10").date(): ("f_raw_return_1d", "miss_return_1d"),
        pd.Timestamp("2020-01-13").date(): ("f_raw_return_5d", "miss_return_5d"),
        pd.Timestamp("2020-01-14").date(): ("f_raw_return_20d", "miss_return_20d"),
    }
    for day, (feature, indicator) in expected.items():
        assert pd.isna(rows.loc[day, feature])
        assert rows.loc[day, indicator] == 1


def test_outputs_use_date32_and_float32(tmp_path: Path) -> None:
    organized, exclusions = _fixture(tmp_path)
    output = tmp_path / "output"
    build_samples(
        organized,
        output,
        exclusions,
        rank_batch_sessions=20,
        staging_tickers=5,
        canonical_min_tickers=2,
    )
    path = next((output / "samples").glob("year=*/*.parquet"))
    schema = pq.read_schema(path)
    assert str(schema.field("date").type) == "date32[day]"
    assert str(schema.field("f_raw_return_1d").type) == "float"
    assert str(schema.field("miss_return_1d").type) == "uint8"
    assert str(schema.field("flag_extreme_label").type) == "uint8"
    assert "f_cs_m_CPIAUCSL" not in schema.names
