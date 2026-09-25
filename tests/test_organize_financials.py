"""Regression tests for SEC financial input selection and universe caching."""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pandas as pd

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from download.organize_financials import (
    _owned_paths,
    _reset_ticker_cache,
    _ticker_for_cik,
    organize_financials,
)


def _financials_root(tmp_path: Path) -> Path:
    root = tmp_path / "raw" / "sec" / "financials"
    root.mkdir(parents=True)
    return root


def test_owned_paths_manifest_miss_does_not_read_unrelated_json(
    tmp_path: Path, monkeypatch,
) -> None:
    """A manifest miss only considers filenames and does not read unrelated data."""
    root = _financials_root(tmp_path)
    (root / "manifest.json").write_text(json.dumps({"resources": {}}), encoding="utf-8")
    unrelated = [root / f"{index:064x}.json" for index in range(4)]
    for path in unrelated:
        path.write_text('{"cik": 999}', encoding="utf-8")

    read_paths: list[Path] = []
    read_text = Path.read_text

    def tracked_read_text(path: Path, *args, **kwargs) -> str:
        read_paths.append(path)
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", tracked_read_text)

    assert _owned_paths(root, "0000000042") == []
    assert read_paths == [root / "manifest.json"]
    assert not set(unrelated).intersection(read_paths)


def test_owned_paths_fallback_keeps_matching_filename_only(tmp_path: Path) -> None:
    """Files named with the padded CIK remain discoverable without manifest entries."""
    root = _financials_root(tmp_path)
    (root / "manifest.json").write_text(json.dumps({"resources": {}}), encoding="utf-8")
    matching = root / "orphan-0000000042-companyfacts.json"
    matching.write_text('{"cik": 42}', encoding="utf-8")
    (root / "unrelated-companyfacts.json").write_text('{"cik": 7}', encoding="utf-8")

    assert _owned_paths(root, "0000000042") == [matching]


def test_missing_sec_financials_still_writes_missing_rows_and_empty_provenance(
    tmp_path: Path,
) -> None:
    """No SEC inputs produce the regular all-missing CSV and no unrelated meta inputs."""
    root = _financials_root(tmp_path)
    (root / "manifest.json").write_text(json.dumps({"resources": {}}), encoding="utf-8")
    for index in range(3):
        (root / f"unrelated-{index}.json").write_text('{"cik": 999}', encoding="utf-8")

    ticker_meta = tmp_path / "organized" / "stocks" / "0000000042" / "_meta.json"
    ticker_meta.parent.mkdir(parents=True)
    ticker_meta.write_text(json.dumps({"inputs": [
        {"path": "data/raw/sec/financials/previously-unrelated.json", "sha256": "stale"},
        {"path": "data/raw/yahoo/0000000042/prices.csv", "sha256": "valid"},
    ]}), encoding="utf-8")

    _reset_ticker_cache()
    output = organize_financials("0000000042", tmp_path / "raw", tmp_path / "organized", [date(2024, 1, 2)])
    frame = pd.read_csv(output)
    metadata = json.loads(output.parent.joinpath("_meta.json").read_text(encoding="utf-8"))

    assert len(frame) == 1
    assert frame.loc[0, "quality_status"] == "missing"
    assert pd.isna(frame.loc[0, "available_as_of"])
    assert metadata["inputs"] == [
        {"path": "data/raw/yahoo/0000000042/prices.csv", "sha256": "valid"}
    ]
    assert metadata["row_counts"]["financials_input"] == 0


def test_ticker_lookup_caches_universe_mapping(tmp_path: Path, monkeypatch) -> None:
    """The first CIK lookup reads universe files; subsequent lookups use the cache."""
    universe = tmp_path / "raw" / "sec" / "universe"
    universe.mkdir(parents=True)
    universe_file = universe / "tickers.json"
    universe_file.write_text(
        json.dumps({"fields": ["cik", "name", "ticker"], "data": [[42, "Example", "exm"]]}),
        encoding="utf-8",
    )

    read_paths: list[Path] = []
    read_text = Path.read_text

    def tracked_read_text(path: Path, *args, **kwargs) -> str:
        if path.parent == universe:
            read_paths.append(path)
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", tracked_read_text)
    _reset_ticker_cache(tmp_path / "raw")

    assert _ticker_for_cik(tmp_path / "raw", "0000000042") == "EXM"
    assert _ticker_for_cik(tmp_path / "raw", "0000000042") == "EXM"
    assert read_paths == [universe_file]

    _reset_ticker_cache(tmp_path / "raw")
    assert _ticker_for_cik(tmp_path / "raw", "0000000042") == "EXM"
    assert read_paths == [universe_file, universe_file]

    _reset_ticker_cache(tmp_path / "raw")
