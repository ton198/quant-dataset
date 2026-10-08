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


def _write_empty_financial_manifest(raw_dir: Path) -> None:
    """Record an explicitly confirmed empty SEC resource inventory."""
    root = raw_dir / "sec" / "financials"
    root.mkdir(parents=True, exist_ok=True)
    (root / "manifest.json").write_text(json.dumps({"resources": []}), encoding="utf-8")


def _write_financial_input(raw_dir: Path, cik10: str, *, accession: str = "annual") -> None:
    """Write one nonempty SEC annual event/fact pair for a CIK."""
    root = raw_dir / "sec" / "financials"
    root.mkdir(parents=True, exist_ok=True)
    cik = int(cik10)
    suffix = cik10.zfill(10)
    filing = {
        "filingDate": "2020-02-01",
        "reportDate": "2019-12-31",
        "form": "10-K",
        "accessionNumber": f"{accession}-{suffix}",
        "fy": 2019,
        "fp": "FY",
    }
    facts_path = root / f"companyfacts-{suffix}.json"
    submissions_path = root / f"submissions-{suffix}.json"
    facts_path.write_text(
        json.dumps(
            {
                "cik": cik,
                "facts": {
                    "us-gaap": {
                        "Revenues": {
                            "units": {
                                "USD": [
                                    {
                                        "filed": filing["filingDate"],
                                        "end": filing["reportDate"],
                                        "start": "2019-01-01",
                                        "form": "10-K",
                                        "accn": filing["accessionNumber"],
                                        "fy": 2019,
                                        "fp": "FY",
                                        "val": 100 + cik,
                                    }
                                ]
                            }
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    submissions_path.write_text(
        json.dumps(
            {
                "cik": cik,
                "filings": {
                    "recent": {
                        key: [filing.get(key)]
                        for key in ("filingDate", "reportDate", "form", "accessionNumber", "fy", "fp")
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    manifest_path = root / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        manifest = {"resources": []}
    manifest.setdefault("resources", []).extend(
        [
            {"logical_key": f"companyfacts:{suffix}", "path": facts_path.name},
            {"logical_key": f"submissions:{suffix}", "path": submissions_path.name},
        ]
    )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


def _append_empty_companyfacts(raw_dir: Path, cik10: str) -> None:
    """Append valid raw payloads and ownership records to change the inventory."""
    root = raw_dir / "sec" / "financials"
    suffix = cik10.zfill(10)
    facts_path = root / f"companyfacts-appended-{suffix}.json"
    facts_path.write_text(
        json.dumps({"cik": int(cik10), "facts": {"us-gaap": {}}}), encoding="utf-8"
    )
    submissions_path = root / f"submissions-appended-{suffix}.json"
    submissions_path.write_text(
        json.dumps(
            {
                "cik": int(cik10),
                "filings": {
                    "recent": {
                        "accessionNumber": [],
                        "filingDate": [],
                        "reportDate": [],
                        "form": [],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["resources"].extend(
        [
            {"logical_key": f"companyfacts:{suffix}:appended", "path": facts_path.name},
            {"logical_key": f"submissions:{suffix}:appended", "path": submissions_path.name},
        ]
    )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


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
        data_root = root / "data"
        for ticker in tickers:
            _write_market_input(data_root / "raw", ticker)
            _write_financial_input(data_root / "raw", universe_rows[ticker].cik10)
        universe_dir = data_root / "raw" / "sec" / "universe"
        universe_dir.mkdir(parents=True)
        payload = {
            "fields": ["cik", "name", "ticker", "exchange"],
            "data": [
                [index, "test", ticker, "NYSE"] for index, ticker in enumerate(tickers, start=1)
            ],
        }
        (universe_dir / "snapshot.json").write_text(json.dumps(payload), encoding="utf-8")

    succeeded, skipped, failed = manager._organize_tickers(
        tickers,
        universe_rows,
        parallel_root / "data" / "raw",
        parallel_root / "data" / "organized",
        _CALENDAR,
        2,
    )
    assert (succeeded, skipped, failed) == (2, 0, 0)

    for ticker in tickers:
        cik10 = universe_rows[ticker].cik10
        organize.organize_market(
            ticker, serial_root / "data" / "raw", serial_root / "data" / "organized", _CALENDAR
        )
        organize.organize_financials(
            cik10,
            serial_root / "data" / "raw",
            serial_root / "data" / "organized",
            _CALENDAR,
        )
        parallel_market, parallel_meta = _organized_paths(parallel_root / "data", ticker)
        serial_market, serial_meta = _organized_paths(serial_root / "data", ticker)
        pd.testing.assert_frame_equal(pd.read_csv(parallel_market), pd.read_csv(serial_market))
        pd.testing.assert_frame_equal(
            pd.read_csv(parallel_market.parent / "financials.csv"),
            pd.read_csv(serial_market.parent / "financials.csv"),
        )
        parallel_events = pd.read_parquet(parallel_market.parent / "financial_events.parquet")
        serial_events = pd.read_parquet(serial_market.parent / "financial_events.parquet")
        parallel_facts = pd.read_parquet(parallel_market.parent / "financial_facts.parquet")
        serial_facts = pd.read_parquet(serial_market.parent / "financial_facts.parquet")
        pd.testing.assert_frame_equal(parallel_events, serial_events)
        pd.testing.assert_frame_equal(parallel_facts, serial_facts)
        assert parallel_events["asset_id"].tolist() == [ticker]
        assert parallel_facts["value"].tolist() == [100 + int(cik10)]
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


def test_completion_identity_tracks_cik_and_raw_inventory_changes(tmp_path: Path) -> None:
    """Changing the ticker CIK or appending a raw SEC payload forces reorganization."""
    raw_dir = tmp_path / "raw"
    organized_dir = tmp_path / "organized"
    _write_market_input(raw_dir, "AAA")
    _write_financial_input(raw_dir, "0000000001")
    rows = {"AAA": universe.TickerRow("AAA", "0000000001", "NYSE", "test")}

    assert manager._organize_tickers(["AAA"], rows, raw_dir, organized_dir, _CALENDAR, 1) == (1, 0, 0)
    assert manager._ticker_is_organized(
        "AAA", raw_dir, organized_dir, require_financials=True, expected_cik="0000000001", check_cik=True
    )
    assert not manager._ticker_is_organized(
        "AAA", raw_dir, organized_dir, require_financials=True, expected_cik="0000000002", check_cik=True
    )

    rows["AAA"] = universe.TickerRow("AAA", "0000000002", "NYSE", "changed CIK")
    assert manager._organize_tickers(["AAA"], rows, raw_dir, organized_dir, _CALENDAR, 1) == (1, 0, 0)
    _append_empty_companyfacts(raw_dir, "0000000002")
    assert not manager._ticker_is_organized(
        "AAA", raw_dir, organized_dir, require_financials=True, expected_cik="0000000002", check_cik=True
    )
    assert manager._organize_tickers(["AAA"], rows, raw_dir, organized_dir, _CALENDAR, 1) == (1, 0, 0)
    metadata = json.loads(
        (organized_dir / "stocks" / "AAA" / "_meta.json").read_text(encoding="utf-8")
    )
    assert metadata["cik10"] == "0000000002"
    assert metadata["raw_input_inventory_sha256"]


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
    rows = {ticker: universe.TickerRow(ticker, "0001253986", "NYSE", "test") for ticker in tickers}
    for ticker in tickers:
        _write_market_input(raw_dir, ticker)
    _write_financial_input(raw_dir, "0001253986", accession="shared")

    assert manager._organize_tickers(
        tickers,
        rows,
        raw_dir,
        organized_dir,
        _CALENDAR,
        2,
    ) == (2, 0, 0)

    for ticker in tickers:
        directory = organized_dir / "stocks" / ticker
        metadata = json.loads((directory / "_meta.json").read_text(encoding="utf-8"))
        assert (directory / "financials.csv").is_file()
        assert (directory / "financial_events.parquet").is_file()
        assert (directory / "financial_facts.parquet").is_file()
        assert metadata["ticker"] == ticker
        assert metadata["financial_events"]["contract_version"] == "financial_events_v1"
        assert any(Path(record["path"]).name == "financials.csv" for record in metadata["outputs"])
        events = pd.read_parquet(directory / "financial_events.parquet")
        facts = pd.read_parquet(directory / "financial_facts.parquet")
        assert events["asset_id"].tolist() == [ticker]
        assert facts["value"].tolist() == [100 + 1253986]
        assert facts["source_fact_path"].str.contains("companyfacts-0001253986.json").all()


def test_empty_cik_does_not_require_financial_output(tmp_path: Path) -> None:
    """A universe row without a CIK only requires market organization to complete."""
    raw_dir = tmp_path / "raw"
    organized_dir = tmp_path / "organized"
    _write_market_input(raw_dir, "AAA")
    rows = {"AAA": universe.TickerRow("AAA", "", "NYSE", "test")}

    assert manager._organize_tickers(["AAA"], rows, raw_dir, organized_dir, _CALENDAR, 1) == (
        1,
        0,
        0,
    )
    assert not (organized_dir / "stocks" / "AAA" / "financials.csv").exists()
    directory = organized_dir / "stocks" / "AAA"
    assert (directory / "financial_events.parquet").is_file()
    assert (directory / "financial_facts.parquet").is_file()
    metadata = json.loads((directory / "_meta.json").read_text(encoding="utf-8"))
    assert metadata["financial_events"]["empty_reason"] == "no_cik"
    assert manager._ticker_is_organized("AAA", raw_dir, organized_dir, require_financials=False)
    assert manager._organize_tickers(["AAA"], rows, raw_dir, organized_dir, _CALENDAR, 1) == (
        0,
        1,
        0,
    )


def test_missing_yahoo_data_with_cik_completes_and_is_skipped_next_run(tmp_path: Path) -> None:
    """No Yahoo CSV is a market skip, while available SEC organization can complete."""
    raw_dir = tmp_path / "raw"
    organized_dir = tmp_path / "organized"
    rows = {"AAA": universe.TickerRow("AAA", "0000000001", "NYSE", "test")}
    _write_empty_financial_manifest(raw_dir)

    assert manager._organize_tickers(["AAA"], rows, raw_dir, organized_dir, _CALENDAR, 1) == (
        1,
        0,
        0,
    )
    directory = organized_dir / "stocks" / "AAA"
    assert (directory / "financials.csv").is_file()
    assert not (directory / "market.csv").exists()
    metadata = json.loads((directory / "_meta.json").read_text(encoding="utf-8"))
    assert metadata["ticker"] == "AAA"
    assert manager._ticker_is_organized("AAA", raw_dir, organized_dir, require_financials=True)
    assert manager._organize_tickers(["AAA"], rows, raw_dir, organized_dir, _CALENDAR, 1) == (
        0,
        1,
        0,
    )


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
        ["GOOD", "BAD"],
        {},
        raw_dir,
        organized_dir,
        _CALENDAR,
        2,
    ) == (1, 0, 1)
    assert (organized_dir / "stocks" / "GOOD" / "market.csv").is_file()
    assert not (organized_dir / "stocks" / "BAD" / "_meta.json").exists()


def test_old_or_incomplete_financial_artifact_meta_forces_rebuild(tmp_path: Path) -> None:
    """Legacy metadata and a broken artifact cannot satisfy ticker completion."""
    raw_dir = tmp_path / "raw"
    organized_dir = tmp_path / "organized"
    rows = {"AAA": universe.TickerRow("AAA", "0000000001", "NYSE", "test")}
    _write_empty_financial_manifest(raw_dir)
    assert manager._organize_tickers(["AAA"], rows, raw_dir, organized_dir, _CALENDAR, 1) == (
        1,
        0,
        0,
    )
    directory = organized_dir / "stocks" / "AAA"
    meta_path = directory / "_meta.json"
    metadata = json.loads(meta_path.read_text(encoding="utf-8"))
    metadata.pop("financial_events")
    meta_path.write_text(json.dumps(metadata), encoding="utf-8")

    assert not manager._ticker_is_organized("AAA", raw_dir, organized_dir, require_financials=True)
    assert manager._organize_tickers(["AAA"], rows, raw_dir, organized_dir, _CALENDAR, 1) == (
        1,
        0,
        0,
    )
    metadata = json.loads(meta_path.read_text(encoding="utf-8"))
    assert metadata["financial_events"]["contract_version"] == "financial_events_v1"

    (directory / "financial_facts.parquet").write_bytes(b"broken")
    assert not manager._ticker_is_organized("AAA", raw_dir, organized_dir, require_financials=True)
    assert manager._organize_tickers(["AAA"], rows, raw_dir, organized_dir, _CALENDAR, 1) == (
        1,
        0,
        0,
    )
    assert manager._ticker_is_organized("AAA", raw_dir, organized_dir, require_financials=True)
