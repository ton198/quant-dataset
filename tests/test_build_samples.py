"""Current sample builder semantics, provenance, and safety tests."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import pytest

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from samples import builder
from samples.builder import build_samples
from samples.contracts import (
    CS_FEATURES,
    FEATURE_LIST,
    LABELS,
    MACRO_RAW,
    MACRO_SERIES,
    MISS_FEATURES,
    RAW_FEATURES,
    SAMPLE_SCHEMA,
    SCHEMA_VERSION,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _workspace(root: Path, excluded: tuple[str, ...] = ("DROP",)) -> Path:
    workspace = root / "workspace"
    config = workspace / "config" / "universes"
    config.mkdir(parents=True, exist_ok=True)
    (config / "exclusions_v1.json").write_text(
        json.dumps({"entries": list(excluded)}), encoding="utf-8"
    )
    return workspace


def _market_frame(
    dates: pd.DatetimeIndex,
    asset_index: int,
    *,
    missing: set[int] | None = None,
    nonfinite: bool = False,
) -> pd.DataFrame:
    missing = missing or set()
    base, step = ((100.0, 2.0), (200.0, 3.0), (300.0, 4.0), (150.0, 1.0), (80.0, 0.5))[asset_index]
    positions = np.arange(len(dates), dtype=np.float64)
    open_values = base + step * positions
    close_values = open_values * 1.05
    frame = pd.DataFrame(
        {
            "date": dates,
            "open": open_values,
            "close": close_values,
            "adj_close": close_values * 1.1,
            "volume": 1000.0 + positions * 10.0 + asset_index,
            "return_1d": (asset_index + 1) / 100.0 + positions / 10000.0,
            "return_5d": (asset_index + 1) / 50.0 + positions / 10000.0,
            "return_20d": (asset_index + 1) / 25.0 + positions / 10000.0,
            "volatility_20": 0.1 + positions / 10000.0,
            "volume_ratio_20": 1.0 + positions / 100.0,
            "intraday_range": 0.02 + positions / 10000.0,
        }
    )
    if nonfinite:
        frame.loc[frame.index[40], "return_1d"] = np.inf
    if missing:
        frame = frame.loc[~frame.index.isin(missing)].reset_index(drop=True)
    return frame


def _macro_frame(dates: pd.DatetimeIndex, *, missing_cpi_position: int | None = 35) -> pd.DataFrame:
    data: dict[str, Any] = {"date": dates}
    for index, series in enumerate(MACRO_SERIES):
        data[series] = index + 1.0 + np.arange(len(dates), dtype=np.float64)
    cpi = 100.0 + np.arange(len(dates), dtype=np.float64) * 2.0
    if missing_cpi_position is not None:
        cpi[missing_cpi_position] = np.nan
    data["CPIAUCSL"] = cpi
    return pd.DataFrame(data)


def _fixture(
    root: Path,
    *,
    dates: pd.DatetimeIndex | None = None,
    missing_aaa: set[int] | None = None,
    nonfinite_aaa: bool = True,
) -> tuple[Path, Path, Path, pd.DatetimeIndex]:
    dates = dates if dates is not None else pd.bdate_range("2018-11-15", periods=45)
    workspace = _workspace(root)
    organized = root / "organized-input"
    stocks = organized / "stocks"
    stocks.mkdir(parents=True)
    for asset_index, asset_id in reversed(
        list(enumerate(("AAA", "BBB", "CCC", "UNIT-WT", "DROP")))
    ):
        frame = _market_frame(
            dates,
            asset_index,
            missing=missing_aaa if asset_id == "AAA" else None,
            nonfinite=nonfinite_aaa and asset_id == "AAA",
        )
        ticker_dir = stocks / asset_id
        ticker_dir.mkdir()
        frame.to_csv(ticker_dir / "market.csv", index=False)
    shared = organized / "shared"
    shared.mkdir()
    _macro_frame(dates).to_csv(shared / "macro.csv", index=False)
    exclusions = workspace / "config" / "universes" / "exclusions_v1.json"
    return workspace, organized, exclusions, dates


def _read_samples(output: Path) -> pd.DataFrame:
    paths = sorted((output / "samples").glob("year=*/*.parquet"))
    return pd.concat([pd.read_parquet(path) for path in paths], ignore_index=True)


def _find_row(samples: pd.DataFrame, asset_id: str, signal: pd.Timestamp) -> pd.Series:
    return samples.loc[samples["asset_id"].eq(asset_id) & samples["date"].eq(signal)].iloc[0]


def _expected_return(base: float, step: float, signal_position: int, horizon: int) -> float:
    entry = base + step * (signal_position + 1)
    exit_value = base + step * (signal_position + 1 + horizon)
    return exit_value / entry - 1.0


def test_current_build_matches_hand_computed_labels_excess_macro_and_nulls(tmp_path: Path) -> None:
    dates = pd.bdate_range("2018-11-15", periods=90)
    workspace, organized, exclusions, dates = _fixture(tmp_path, dates=dates, missing_aaa={36})
    output = tmp_path / "fresh-output"
    result = build_samples(
        organized,
        output,
        exclusions,
        workspace_root=workspace,
        canonical_min_tickers=2,
        staging_tickers=2,
        rank_batch_sessions=7,
    )
    samples = _read_samples(output)
    samples["date"] = pd.to_datetime(samples["date"])

    assert result["rows"] == len(dates) * 4 - 31 * 4 - 1
    assert set(samples["asset_id"]) == {"AAA", "BBB", "CCC", "UNIT-WT"}
    assert samples.loc[samples["asset_id"].eq("UNIT-WT"), "is_common"].eq(False).all()
    assert samples.loc[samples["asset_id"].eq("AAA"), "is_common"].eq(True).all()

    signal_position = 34
    signal = dates[signal_position]
    aaa = _find_row(samples, "AAA", signal)
    bbb = _find_row(samples, "BBB", signal)
    for horizon in (1, 5, 21, 30):
        expected = _expected_return(200.0, 3.0, signal_position, horizon)
        assert bbb[f"target_return_{horizon}d"] == np.float32(expected)
    assert pd.isna(aaa["target_return_1d"])
    assert aaa["target_return_5d"] == np.float32(_expected_return(100.0, 2.0, signal_position, 5))

    common_targets_5d = np.asarray(
        [
            _expected_return(base, step, signal_position, 5)
            for base, step in ((100.0, 2.0), (200.0, 3.0), (300.0, 4.0))
        ],
        dtype=np.float32,
    )
    assert aaa["excess_5d"] == np.float32(common_targets_5d[0] - np.mean(common_targets_5d))
    common_targets_21d = np.asarray(
        [
            _expected_return(base, step, signal_position, 21)
            for base, step in ((100.0, 2.0), (200.0, 3.0), (300.0, 4.0))
        ],
        dtype=np.float32,
    )
    assert aaa["excess_21d"] == np.float32(common_targets_21d[0] - np.mean(common_targets_21d))

    # The missing AAA bar is first used as an entry and then as an exit; neither label is filled.
    assert pd.isna(_find_row(samples, "AAA", dates[35])["target_return_1d"])
    assert pd.isna(_find_row(samples, "AAA", dates[34])["target_return_1d"])
    assert pd.isna(_find_row(samples, "AAA", dates[-1])["target_return_1d"])
    assert pd.isna(_find_row(samples, "AAA", dates[-1])["target_return_30d"])

    cpi = _find_row(samples, "AAA", dates[34])
    assert cpi["f_raw_m_CPIAUCSL"] == np.float32(100.0 + 34 * 2.0)
    assert pd.isna(_find_row(samples, "BBB", dates[35])["f_raw_m_CPIAUCSL"])
    assert pd.isna(_find_row(samples, "BBB", dates[36])["f_raw_m_CPIAUCSL_d1"])
    assert _find_row(samples, "BBB", dates[37])["f_raw_m_CPIAUCSL_d1"] == np.float32(2.0)
    assert _find_row(samples, "BBB", dates[36])["f_raw_m_CPIAUCSL_d5"] == np.float32(10.0)

    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    splits = json.loads((output / "splits.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == SCHEMA_VERSION == "samples"
    assert manifest["build_params"]["canonical_minimum_common_tickers"] == 2
    assert manifest["input_inventory"]["scope"] == "counts only; not a complete content inventory"
    assert len(manifest["input_provenance"]["files"]) == 6
    assert all(
        Path(item["path"]).is_absolute() is False for item in manifest["input_provenance"]["files"]
    )
    assert not any("DROP" in item["path"] for item in manifest["input_provenance"]["files"])
    assert {item["role"] for item in manifest["input_provenance"]["files"]} == {
        "exclusions",
        "macro",
        "market",
    }
    assert manifest["semantic_fingerprint"]
    assert set(manifest["code_identity"]["files"]) == {
        "samples/__init__.py",
        "samples/builder.py",
        "samples/contracts.py",
        "samples/query.py",
        "samples/validation.py",
    }
    assert splits["purged_windows"]["select"]["sessions"] == 31
    assert splits["purged_windows"]["select"]["rows_removed"] == 31 * 4
    assert splits["rows_by_split"]["fit"]["retained"] == 2 * 4
    assert splits["rows_by_split"]["fit"]["purged"] == 31 * 4


def test_schema_registry_and_output_hash_manifest_are_exact(tmp_path: Path) -> None:
    workspace, organized, exclusions, _ = _fixture(tmp_path)
    output = tmp_path / "fresh-output"
    build_samples(
        organized,
        output,
        exclusions,
        workspace_root=workspace,
        canonical_min_tickers=2,
    )
    paths = sorted((output / "samples").glob("year=*/*.parquet"))
    schema = pq.read_schema(paths[0])
    assert schema.names == list(SAMPLE_SCHEMA.names)
    assert [(field.name, field.type) for field in schema] == [
        (field.name, field.type) for field in SAMPLE_SCHEMA
    ]
    assert len(RAW_FEATURES) == 43
    assert len(CS_FEATURES) == 10
    assert len(MISS_FEATURES) == 43
    assert len(FEATURE_LIST) == 96
    assert len(LABELS) == 32
    assert len(schema) == 132
    assert str(schema.field("date").type) == "date32[day]"
    assert str(schema.field("is_common").type) == "bool"
    assert str(schema.field("flag_extreme_label").type) == "uint8"
    assert all(
        schema.field(name).type == pq.read_schema(paths[0]).field(name).type
        for name in RAW_FEATURES
    )
    assert all(
        name not in schema.names for name in ("revenue", "financial_events", "financial_facts")
    )

    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    for relative, record in manifest["outputs"].items():
        path = output / relative
        assert hashlib.sha256(path.read_bytes()).hexdigest() == record["sha256"]
        assert path.stat().st_size == record["bytes"]


def test_output_is_deterministic_across_input_creation_and_staging_order(tmp_path: Path) -> None:
    workspace_a, organized_a, exclusions_a, _ = _fixture(tmp_path / "a")
    workspace_b, organized_b, exclusions_b, _ = _fixture(tmp_path / "b")
    first_output, second_output = tmp_path / "out-a", tmp_path / "out-b"
    build_samples(
        organized_a,
        first_output,
        exclusions_a,
        workspace_root=workspace_a,
        canonical_min_tickers=2,
        staging_tickers=1,
        rank_batch_sessions=5,
    )
    build_samples(
        organized_b,
        second_output,
        exclusions_b,
        workspace_root=workspace_b,
        canonical_min_tickers=2,
        staging_tickers=4,
        rank_batch_sessions=11,
    )
    pd.testing.assert_frame_equal(_read_samples(first_output), _read_samples(second_output))


def test_staged_reads_are_bounded_by_configured_session_chunk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, organized, exclusions, _ = _fixture(tmp_path, nonfinite_aaa=False)
    observed_rows: list[int] = []
    real_dataset = builder.ds.dataset

    class DatasetSpy:
        def __init__(self, dataset: Any) -> None:
            self.dataset = dataset

        def to_table(self, *args: Any, **kwargs: Any) -> Any:
            table = self.dataset.to_table(*args, **kwargs)
            observed_rows.append(table.num_rows)
            return table

    def tracked_dataset(*args: Any, **kwargs: Any) -> DatasetSpy:
        return DatasetSpy(real_dataset(*args, **kwargs))

    monkeypatch.setattr(builder.ds, "dataset", tracked_dataset)
    build_samples(
        organized,
        tmp_path / "fresh-output",
        exclusions,
        workspace_root=workspace,
        canonical_min_tickers=2,
        rank_batch_sessions=5,
        staging_tickers=1,
    )
    assert observed_rows
    assert max(observed_rows) <= 5 * 4


def test_nonfinite_raw_values_become_null_with_matching_missing_flag(tmp_path: Path) -> None:
    workspace, organized, exclusions, dates = _fixture(tmp_path, nonfinite_aaa=True)
    output = tmp_path / "fresh-output"
    build_samples(
        organized,
        output,
        exclusions,
        workspace_root=workspace,
        canonical_min_tickers=2,
    )
    samples = _read_samples(output)
    row = samples.loc[
        samples["asset_id"].eq("AAA") & (pd.to_datetime(samples["date"]) == dates[40])
    ].iloc[0]
    assert pd.isna(row["f_raw_return_1d"])
    assert row["miss_return_1d"] == 1


def test_future_market_and_macro_changes_do_not_change_past_features(tmp_path: Path) -> None:
    dates = pd.bdate_range("2023-01-02", periods=80)
    first = tmp_path / "first"
    second = tmp_path / "second"
    _, organized_a, _, _ = _fixture(first, dates=dates, nonfinite_aaa=False)
    _, organized_b, _, _ = _fixture(second, dates=dates, nonfinite_aaa=False)
    market_a = organized_a / "stocks" / "AAA" / "market.csv"
    market_b = organized_b / "stocks" / "AAA" / "market.csv"
    macro_a = organized_a / "shared" / "macro.csv"
    macro_b = organized_b / "shared" / "macro.csv"
    market = pd.read_csv(market_b)
    market.loc[71:, ["open", "close", "adj_close", "volume", "return_1d"]] *= 7
    market.to_csv(market_b, index=False)
    macro = pd.read_csv(macro_b)
    macro.loc[71:, MACRO_SERIES] *= 3
    macro.to_csv(macro_b, index=False)
    calendar = pd.DatetimeIndex(dates, name="date")
    macro_matrix_a = builder._macro_matrix(macro_a, calendar)
    macro_matrix_b = builder._macro_matrix(macro_b, calendar)
    left = builder._ticker_samples("AAA", market_a, calendar, macro_matrix_a, True)
    right = builder._ticker_samples("AAA", market_b, calendar, macro_matrix_b, True)
    signal = pd.Timestamp(dates[70])
    left_row = left.loc[left["date"].eq(signal)].iloc[0]
    right_row = right.loc[right["date"].eq(signal)].iloc[0]
    for feature in RAW_FEATURES:
        assert left_row[feature] == right_row[feature] or (
            pd.isna(left_row[feature]) and pd.isna(right_row[feature])
        )


def test_builder_does_not_read_financial_files_or_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, organized, exclusions, _ = _fixture(tmp_path)
    for ticker in (organized / "stocks").iterdir():
        (ticker / "financial_events.parquet").write_bytes(b"not parquet; must not be read")
        (ticker / "financial_facts.parquet").write_bytes(b"not parquet; must not be read")
        (ticker / "_meta.json").write_text("not json; must not be read", encoding="utf-8")

    real_read_csv = pd.read_csv
    real_read_schema = pq.read_schema
    real_read_table = pq.read_table

    def guard_path(value: Any) -> None:
        name = Path(value).name.casefold()
        assert not (name.startswith("financial_") or name == "_meta.json"), value

    def guarded_read_csv(path: Any, *args: Any, **kwargs: Any) -> Any:
        guard_path(path)
        return real_read_csv(path, *args, **kwargs)

    def guarded_read_schema(path: Any, *args: Any, **kwargs: Any) -> Any:
        guard_path(path)
        return real_read_schema(path, *args, **kwargs)

    def guarded_read_table(path: Any, *args: Any, **kwargs: Any) -> Any:
        guard_path(path)
        return real_read_table(path, *args, **kwargs)

    monkeypatch.setattr(pd, "read_csv", guarded_read_csv)
    monkeypatch.setattr(pq, "read_schema", guarded_read_schema)
    monkeypatch.setattr(pq, "read_table", guarded_read_table)
    output = tmp_path / "fresh-output"
    result = build_samples(
        organized,
        output,
        exclusions,
        workspace_root=workspace,
        canonical_min_tickers=2,
    )
    assert result["rows"] > 0
    assert (output / "manifest.json").is_file()


def test_input_mutation_during_build_prevents_manifest_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, organized, exclusions, _ = _fixture(tmp_path)
    output = tmp_path / "fresh-output"
    market_path = organized / "stocks" / "AAA" / "market.csv"
    original = builder._ticker_samples
    changed = False

    def mutate_after_read(*args: Any, **kwargs: Any) -> pd.DataFrame:
        nonlocal changed
        frame = original(*args, **kwargs)
        if not changed:
            with market_path.open("a", encoding="utf-8") as handle:
                handle.write("\n")
            changed = True
        return frame

    monkeypatch.setattr(builder, "_ticker_samples", mutate_after_read)
    with pytest.raises(ValueError, match="consumed input changed during sample build"):
        build_samples(
            organized,
            output,
            exclusions,
            workspace_root=workspace,
            canonical_min_tickers=2,
        )
    assert not (output / "manifest.json").exists()


def test_workspace_default_exclusions_are_cwd_anchored_and_missing_config_is_clear(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, organized, _, _ = _fixture(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    output = tmp_path / "out-from-cwd"
    result = build_samples(
        organized,
        output,
        workspace_root=workspace,
        canonical_min_tickers=2,
    )
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    exclusions_record = next(
        item for item in manifest["input_provenance"]["files"] if item["role"] == "exclusions"
    )
    assert exclusions_record["path"] == "workspace/config/universes/exclusions_v1.json"
    assert result["rows"] > 0

    monkeypatch.chdir(workspace)
    default_root_output = tmp_path / "default-root-output"
    build_samples(organized, default_root_output, canonical_min_tickers=2)
    default_root_manifest = json.loads(
        (default_root_output / "manifest.json").read_text(encoding="utf-8")
    )
    default_exclusions = next(
        item
        for item in default_root_manifest["input_provenance"]["files"]
        if item["role"] == "exclusions"
    )
    assert default_exclusions["path"] == "workspace/config/universes/exclusions_v1.json"

    empty_workspace = tmp_path / "empty-workspace"
    empty_workspace.mkdir()
    with pytest.raises(FileNotFoundError, match="provide --exclusions-file"):
        build_samples(
            organized,
            tmp_path / "missing-config-output",
            workspace_root=empty_workspace,
            canonical_min_tickers=2,
        )


def test_explicit_relative_exclusions_resolve_from_cwd_not_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, organized, _, _ = _fixture(tmp_path)
    caller = tmp_path / "caller"
    caller.mkdir()
    explicit = caller / "custom-exclusions.json"
    explicit.write_text('{"entries": ["DROP"]}', encoding="utf-8")
    monkeypatch.chdir(caller)
    output = tmp_path / "explicit-output"
    result = build_samples(
        organized,
        output,
        "custom-exclusions.json",
        workspace_root=workspace,
        canonical_min_tickers=2,
    )
    assert result["rows"] > 0
    record = next(
        item
        for item in json.loads((output / "manifest.json").read_text(encoding="utf-8"))[
            "input_provenance"
        ]["files"]
        if item["role"] == "exclusions"
    )
    assert record["path"] == "external/custom-exclusions.json"


def test_output_guards_reject_overlaps_symlinks_and_known_workspace_data(
    tmp_path: Path,
) -> None:
    workspace, organized, exclusions, _ = _fixture(tmp_path)
    for output in (organized, organized / "child", organized.parent):
        with pytest.raises(ValueError, match="overlap organized input"):
            build_samples(
                organized,
                output,
                exclusions,
                workspace_root=workspace,
                canonical_min_tickers=2,
            )

    protected = REPO_ROOT / "data" / "output"
    with pytest.raises(ValueError, match="protected workspace data path"):
        build_samples(
            organized,
            protected,
            exclusions,
            workspace_root=REPO_ROOT,
            canonical_min_tickers=2,
        )

    link = tmp_path / "output-link"
    link.symlink_to(tmp_path / "never-created", target_is_directory=True)
    with pytest.raises(ValueError, match="must not be a symlink"):
        build_samples(
            organized,
            link,
            exclusions,
            workspace_root=workspace,
            canonical_min_tickers=2,
        )
    assert link.is_symlink()


def test_preexisting_nonempty_output_is_preserved(tmp_path: Path) -> None:
    workspace, organized, exclusions, _ = _fixture(tmp_path)
    output = tmp_path / "already-used"
    output.mkdir()
    sentinel = output / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")
    with pytest.raises(ValueError, match="fresh and empty"):
        build_samples(
            organized,
            output,
            exclusions,
            workspace_root=workspace,
            canonical_min_tickers=2,
        )
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_cli_passes_workspace_root_to_builder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cli import main as cli_main

    defaults = cli_main._parser().parse_args(["build-samples"])
    assert defaults.workspace_root == Path.cwd()
    assert defaults.out == Path("data/samples-output")

    observed: dict[str, Any] = {}

    def fake_build(
        data_dir: Path, output_dir: Path, exclusions_file: Path | None, **kwargs: Any
    ) -> dict[str, Any]:
        observed.update(
            data_dir=data_dir,
            output_dir=output_dir,
            exclusions_file=exclusions_file,
            **kwargs,
        )
        return {
            "rows": 1,
            "date_start": "2020-01-01",
            "date_end": "2020-01-02",
            "failures": [],
            "output_dir": str(output_dir),
        }

    monkeypatch.setattr(cli_main, "build_samples", fake_build)
    code = cli_main.main(
        [
            "build-samples",
            "--data-dir",
            str(tmp_path / "organized"),
            "--out",
            str(tmp_path / "output"),
            "--exclusions-file",
            str(tmp_path / "exclusions.json"),
            "--workspace-root",
            str(tmp_path / "workspace"),
        ]
    )
    assert code == 0
    assert observed["workspace_root"] == tmp_path / "workspace"


def test_current_contract_public_constants_are_importable() -> None:
    assert MACRO_RAW == tuple(
        feature
        for series in MACRO_SERIES
        for feature in (f"f_raw_m_{series}", f"f_raw_m_{series}_d1", f"f_raw_m_{series}_d5")
    )
    assert builder.build_samples is build_samples
