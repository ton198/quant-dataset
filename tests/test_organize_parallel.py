"""Coverage for resumable, process-parallel ticker organization."""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pandas as pd

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from download import manager, organize, universe


_CALENDAR = [date(2020, 1, 2), date(2020, 1, 3)]


def _write_market_input(raw_dir: Path, ticker: str, *, valid: bool = True) -> None:
    directory = raw_dir / "yahoo" / ticker
    directory.mkdir(parents=True, exist_ok=True)
    rows = {
        "Date": ["2020-01-02", "2020-01-03"],
        "Open": [10.0, 11.0],
        "High": [12.0, 13.0],
        "Low": [9.0, 10.0],
        "Close": [11.0, 12.0],
        "Adj Close": [11.0, 12.0],
        "Volume": [100.0, 120.0],
    }
    if not valid:
        rows.pop("Open")
    pd.DataFrame(rows).to_csv(directory / "prices.csv", index=False)


def _organized_paths(root: Path, ticker: str) -> tuple[Path, Path]:
    directory = root / "organized" / "stocks" / ticker
    return directory / "market.csv", directory / "_meta.json"


def test_process_results_match_serial_organization(tmp_path: Path) -> None:
    """A process-pool pass produces the same per-ticker outputs as serial work."""
    parallel_root = tmp_path / "parallel"
    serial_root = tmp_path / "serial"
    tickers = ["AAA", "BBB"]
    universe_rows = {
        ticker: universe.TickerRow(ticker, f"{index:010d}", "NYSE", "test")
        for index, ticker in enumerate(tickers, start=1)
    }
    for root in (parallel_root, serial_root):
        for ticker in tickers:
            _write_market_input(root / "raw", ticker)
        universe_dir = root / "raw" / "sec" / "universe"
        universe_dir.mkdir(parents=True)
        payload = {
            "fields": ["cik", "name", "ticker", "exchange"],
            "data": [[index, "test", ticker, "NYSE"] for index, ticker in enumerate(tickers, start=1)],
        }
        (universe_dir / "snapshot.json").write_text(json.dumps(payload), encoding="utf-8")

    succeeded, skipped, failed = manager._organize_tickers(
        tickers, universe_rows, parallel_root / "raw", parallel_root / "organized", _CALENDAR, 2,
    )
    assert (succeeded, skipped, failed) == (2, 0, 0)

    for ticker in tickers:
        cik10 = universe_rows[ticker].cik10
        organize.organize_market(ticker, serial_root / "raw", serial_root / "organized", _CALENDAR)
        organize.organize_financials(
            cik10, serial_root / "raw", serial_root / "organized", _CALENDAR,
        )
        parallel_market, parallel_meta = _organized_paths(parallel_root, ticker)
        serial_market, serial_meta = _organized_paths(serial_root, ticker)
        pd.testing.assert_frame_equal(pd.read_csv(parallel_market), pd.read_csv(serial_market))
        pd.testing.assert_frame_equal(
            pd.read_csv(parallel_market.parent / "financials.csv"),
            pd.read_csv(serial_market.parent / "financials.csv"),
        )
        parallel_outputs = {
            Path(record["path"]).name: (record["sha256"], record["rows"])
            for record in json.loads(parallel_meta.read_text(encoding="utf-8"))["outputs"]
        }
        serial_outputs = {
            Path(record["path"]).name: (record["sha256"], record["rows"])
            for record in json.loads(serial_meta.read_text(encoding="utf-8"))["outputs"]
        }
        assert parallel_outputs == serial_outputs


def test_second_organization_run_skips_completed_tickers(tmp_path: Path) -> None:
    """A second pass leaves completed output files untouched and reports skips."""
    raw_dir = tmp_path / "raw"
    organized_dir = tmp_path / "organized"
    tickers = ["AAA", "BBB"]
    for ticker in tickers:
        _write_market_input(raw_dir, ticker)

    assert manager._organize_tickers(tickers, {}, raw_dir, organized_dir, _CALENDAR, 2) == (2, 0, 0)
    before = {
        path: path.stat().st_mtime_ns
        for ticker in tickers
        for path in _organized_paths(tmp_path, ticker)
    }

    assert manager._organize_tickers(tickers, {}, raw_dir, organized_dir, _CALENDAR, 2) == (0, 2, 0)
    assert {path: path.stat().st_mtime_ns for path in before} == before


def test_market_only_partial_output_is_reorganized(tmp_path: Path) -> None:
    """An output without completion metadata is not mistaken for completed work."""
    raw_dir = tmp_path / "raw"
    organized_dir = tmp_path / "organized"
    _write_market_input(raw_dir, "AAA")
    market_path, meta_path = _organized_paths(tmp_path, "AAA")
    market_path.parent.mkdir(parents=True)
    market_path.write_text("incomplete\n", encoding="utf-8")

    assert manager._organize_tickers(["AAA"], {}, raw_dir, organized_dir, _CALENDAR, 1) == (1, 0, 0)

    assert meta_path.is_file()
    assert pd.read_csv(market_path).loc[0, "date"] == "2020-01-02"


def test_shared_cik_tickers_write_independent_outputs_in_parallel(tmp_path: Path) -> None:
    """Each ticker sharing a CIK gets its own financial output and metadata."""
    raw_dir = tmp_path / "raw"
    organized_dir = tmp_path / "organized"
    tickers = ["ABR-PD", "ABR-PE"]
    rows = {
        ticker: universe.TickerRow(ticker, "0001253986", "NYSE", "test")
        for ticker in tickers
    }
    for ticker in tickers:
        _write_market_input(raw_dir, ticker)

    assert manager._organize_tickers(
        tickers, rows, raw_dir, organized_dir, _CALENDAR, 2,
    ) == (2, 0, 0)

    for ticker in tickers:
        directory = organized_dir / "stocks" / ticker
        metadata = json.loads((directory / "_meta.json").read_text(encoding="utf-8"))
        assert (directory / "financials.csv").is_file()
        assert metadata["ticker"] == ticker
        assert any(Path(record["path"]).name == "financials.csv" for record in metadata["outputs"])


def test_empty_cik_does_not_require_financial_output(tmp_path: Path) -> None:
    """A universe row without a CIK only requires market organization to complete."""
    raw_dir = tmp_path / "raw"
    organized_dir = tmp_path / "organized"
    _write_market_input(raw_dir, "AAA")
    rows = {"AAA": universe.TickerRow("AAA", "", "NYSE", "test")}

    assert manager._organize_tickers(["AAA"], rows, raw_dir, organized_dir, _CALENDAR, 1) == (1, 0, 0)
    assert not (organized_dir / "stocks" / "AAA" / "financials.csv").exists()
    assert manager._ticker_is_organized("AAA", raw_dir, organized_dir, require_financials=False)
    assert manager._organize_tickers(["AAA"], rows, raw_dir, organized_dir, _CALENDAR, 1) == (0, 1, 0)


def test_missing_yahoo_data_with_cik_completes_and_is_skipped_next_run(tmp_path: Path) -> None:
    """No Yahoo CSV is a market skip, while available SEC organization can complete."""
    raw_dir = tmp_path / "raw"
    organized_dir = tmp_path / "organized"
    rows = {"AAA": universe.TickerRow("AAA", "0000000001", "NYSE", "test")}

    assert manager._organize_tickers(["AAA"], rows, raw_dir, organized_dir, _CALENDAR, 1) == (1, 0, 0)
    directory = organized_dir / "stocks" / "AAA"
    assert (directory / "financials.csv").is_file()
    assert not (directory / "market.csv").exists()
    metadata = json.loads((directory / "_meta.json").read_text(encoding="utf-8"))
    assert metadata["ticker"] == "AAA"
    assert manager._ticker_is_organized("AAA", raw_dir, organized_dir, require_financials=True)
    assert manager._organize_tickers(["AAA"], rows, raw_dir, organized_dir, _CALENDAR, 1) == (0, 1, 0)


def test_corrupt_meta_or_missing_recorded_output_is_reorganized(tmp_path: Path) -> None:
    """Broken metadata and missing recorded outputs both invalidate completion."""
    raw_dir = tmp_path / "raw"
    organized_dir = tmp_path / "organized"
    _write_market_input(raw_dir, "AAA")
    assert manager._organize_tickers(["AAA"], {}, raw_dir, organized_dir, _CALENDAR, 1) == (1, 0, 0)
    market_path, meta_path = _organized_paths(tmp_path, "AAA")

    meta_path.write_text("not json", encoding="utf-8")
    assert manager._organize_tickers(["AAA"], {}, raw_dir, organized_dir, _CALENDAR, 1) == (1, 0, 0)
    market_path.unlink()
    assert manager._organize_tickers(["AAA"], {}, raw_dir, organized_dir, _CALENDAR, 1) == (1, 0, 0)
    assert market_path.is_file()


def test_one_ticker_failure_does_not_block_other_tickers(tmp_path: Path) -> None:
    """A ticker with malformed market inputs fails in isolation from valid peers."""
    raw_dir = tmp_path / "raw"
    organized_dir = tmp_path / "organized"
    _write_market_input(raw_dir, "GOOD")
    _write_market_input(raw_dir, "BAD", valid=False)

    assert manager._organize_tickers(
        ["GOOD", "BAD"], {}, raw_dir, organized_dir, _CALENDAR, 2,
    ) == (1, 0, 1)
    assert (organized_dir / "stocks" / "GOOD" / "market.csv").is_file()
    assert not (organized_dir / "stocks" / "BAD" / "_meta.json").exists()
