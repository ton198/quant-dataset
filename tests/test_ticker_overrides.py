"""Regression coverage for vetted SEC ticker corrections and forced organization."""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from cli import main as cli_main
from download import manager, universe
from download.organize_financials import _reset_ticker_cache, _ticker_for_cik

_XOM_SNAPSHOT_ROW = [2115436, "ExxonMobil Holdings Corp", "XOM", "NYSE"]


def _write_xom_snapshot(raw_dir: Path) -> None:
    directory = raw_dir / "sec" / "universe"
    directory.mkdir(parents=True)
    (directory / "snapshot.json").write_text(
        json.dumps(
            {
                "fields": ["cik", "name", "ticker", "exchange"],
                "data": [_XOM_SNAPSHOT_ROW],
            }
        ),
        encoding="utf-8",
    )


def test_xom_forward_lookup_uses_vetted_cik_override() -> None:
    """Universe rows passed to financial downloads use the historical filer CIK."""
    rows = universe._rows(
        {
            "fields": ["cik", "name", "ticker", "exchange"],
            "data": [_XOM_SNAPSHOT_ROW],
        }
    )

    assert len(rows) == 1
    assert rows[0].ticker == "XOM"
    assert rows[0].cik10 == "0000034088"


def test_xom_reverse_lookup_uses_vetted_cik_override(tmp_path: Path) -> None:
    """Organization maps the corrected historical CIK back to XOM, not the snapshot CIK."""
    raw_dir = tmp_path / "raw"
    _write_xom_snapshot(raw_dir)
    _reset_ticker_cache(raw_dir)

    assert _ticker_for_cik(raw_dir, "0000034088") == "XOM"
    assert _ticker_for_cik(raw_dir, "0002115436") is None


def test_missing_override_file_preserves_snapshot_mappings(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Without the optional corrections file both directions retain old behavior."""
    monkeypatch.setattr(
        universe,
        "TICKER_CIK_OVERRIDES_PATH",
        tmp_path / "missing-overrides.json",
    )
    raw_dir = tmp_path / "raw"
    _write_xom_snapshot(raw_dir)
    _reset_ticker_cache(raw_dir)

    forward_rows = universe._rows(
        {
            "fields": ["cik", "name", "ticker", "exchange"],
            "data": [_XOM_SNAPSHOT_ROW],
        }
    )
    assert forward_rows[0].cik10 == "0002115436"
    assert _ticker_for_cik(raw_dir, "0002115436") == "XOM"
    assert _ticker_for_cik(raw_dir, "0000034088") is None


def test_cli_exposes_force_rebuild_flag() -> None:
    """The download CLI accepts the force-rebuild option for follow-up jobs."""
    args = cli_main._parser().parse_args(["download", "--force-rebuild", "--stage", "organize"])

    assert args.force_rebuild


def test_force_rebuild_reorganizes_completed_tickers(tmp_path: Path) -> None:
    """The force-rebuild option bypasses completion checks for every selected ticker."""
    raw_dir = tmp_path / "raw"
    organized_dir = tmp_path / "organized"
    rows = {"XOM": universe.TickerRow("XOM", "0000034088", "NYSE", "Exxon Mobil")}
    tickers = ["XOM"]
    calendar = [date(2020, 1, 2)]

    assert manager._organize_tickers(
        tickers,
        rows,
        raw_dir,
        organized_dir,
        calendar,
        1,
    ) == (1, 0, 0)
    assert manager._organize_tickers(
        tickers,
        rows,
        raw_dir,
        organized_dir,
        calendar,
        1,
    ) == (0, 1, 0)
    assert manager._organize_tickers(
        tickers,
        rows,
        raw_dir,
        organized_dir,
        calendar,
        1,
        force_rebuild=True,
    ) == (1, 0, 0)
