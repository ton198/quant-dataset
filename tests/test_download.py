"""Offline tests for the download pipeline and command-line interface."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

import download.manager as manager
from cli.main import main
from download.config import Secrets, load_secrets, load_sources
from download.errors import ConfigError, DownloadError
from download.progress import (
    Progress,
    acquire_lock,
    initialize,
    load,
    mark_done,
    mark_failed,
    pending,
    save_atomic,
)


def test_download_reports_per_ticker_progress_without_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from types import SimpleNamespace

    monkeypatch.setattr(manager, "load_sources", lambda root: load_sources(REPO_ROOT))
    monkeypatch.setattr(manager, "load_secrets", lambda root: Secrets("key", "user@example.com"))
    monkeypatch.setattr(
        manager.universe,
        "fetch_universe",
        lambda cfg, secrets: [SimpleNamespace(ticker="AAA", cik10="0000000001")],
    )
    monkeypatch.setattr(manager.market, "fetch_market", lambda *args: ["bar"])

    with caplog.at_level(logging.INFO, logger="download.manager"):
        result = manager.run_download(
            repo_root=REPO_ROOT,
            stages=["market"],
            start=date(2024, 1, 1),
            end=date(2024, 1, 2),
            force=False,
            dry_run=False,
            tickers=["AAA"],
            data_dir=tmp_path,
        )

    assert result == 0
    assert "Download market 1/1 ticker=AAA" in caplog.text


REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPO_ROOT / "src"


# config.py


def test_load_secrets_returns_credentials(tmp_path: Path) -> None:
    """Load both required credentials from the secrets table."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "secrets.toml").write_text(
        '[secrets]\nfred_api_key = "fred-test-key"\nsec_user_agent = "test@example.com"\n',
        encoding="utf-8",
    )

    assert load_secrets(tmp_path) == Secrets("fred-test-key", "test@example.com")


def test_load_secrets_requires_fred_api_key(tmp_path: Path) -> None:
    """Reject secrets files without the FRED API key."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "secrets.toml").write_text(
        '[secrets]\nsec_user_agent = "test@example.com"\n', encoding="utf-8"
    )

    with pytest.raises(ConfigError, match="fred_api_key"):
        load_secrets(tmp_path)


def test_load_secrets_requires_sec_user_agent(tmp_path: Path) -> None:
    """Reject secrets files without the SEC user agent."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "secrets.toml").write_text(
        '[secrets]\nfred_api_key = "fred-test-key"\n', encoding="utf-8"
    )

    with pytest.raises(ConfigError, match="sec_user_agent"):
        load_secrets(tmp_path)


def test_load_sources_parses_provider_settings() -> None:
    """Parse the checked-in provider values and typed series/rate-limit fields."""
    config = load_sources(REPO_ROOT)

    assert config.universe.source == "sec.company_tickers_exchange"
    assert config.market.provider == "yahoo"
    assert isinstance(config.market.rate_limit_seconds, float)
    assert config.market.rate_limit_seconds == 0.5
    # SourcesConfig intentionally freezes this TOML array as a tuple.
    assert isinstance(config.macros.series, tuple)
    assert list(config.macros.series)[0] == "CPIAUCSL"


def test_load_sources_reads_report_sample_forms() -> None:
    config = load_sources(REPO_ROOT)
    assert config.filing_forms == (
        "10-K",
        "10-K/A",
        "10-Q",
        "10-Q/A",
        "20-F",
        "20-F/A",
        "40-F",
        "40-F/A",
    )


def test_cli_rejects_retired_download_stages() -> None:
    for stage in ("statements", "organize"):
        with pytest.raises(SystemExit) as exit_info:
            main(["download", "--stage", stage])
        assert exit_info.value.code == 2


def test_load_sources_requires_filings_table(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    source_text = (REPO_ROOT / "config" / "sources.toml").read_text(encoding="utf-8")
    marker = "\n[filings]\n"
    assert marker in source_text
    (config_dir / "sources.toml").write_text(
        source_text.split(marker, 1)[0] + "\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="Invalid or incomplete sources configuration"):
        load_sources(tmp_path)


# progress.py


def test_progress_atomic_save_and_load_round_trip(tmp_path: Path) -> None:
    """Persist and reload every progress field without changing its value."""
    original = Progress(
        "run-1",
        "2020-01-01T00:00:00Z",
        "test-universe",
        {"market": {"ABC": "done", "XYZ": "failed:offline"}},
    )
    path = tmp_path / "progress.json"

    save_atomic(original, path, tmp_path / "progress.tmp")

    assert load(path) == original


def test_initialize_force_replaces_existing_work_list(tmp_path: Path) -> None:
    """Force initialization discards old statuses and creates fresh pending items."""
    path = tmp_path / "progress.json"
    save_atomic(
        Progress("old", "old-time", "old-source", {"market": {"OLD": "done"}}),
        path,
        tmp_path / "progress.tmp",
    )

    fresh = initialize(
        {"market": ["NEW"]},
        "new-source",
        True,
        path,
        tmp_path / "progress.tmp",
        tmp_path / "progress.lock",
    )

    assert fresh.run_id != "old"
    assert fresh.universe_source == "new-source"
    assert fresh.stages == {"market": {"NEW": "pending"}}


def test_initialize_resumes_existing_work_list(tmp_path: Path) -> None:
    """Resume preserves completed work and adds newly requested items as pending."""
    path = tmp_path / "progress.json"
    original = Progress("run-1", "time", "universe", {"market": {"DONE": "done"}})
    save_atomic(original, path, tmp_path / "progress.tmp")

    resumed = initialize(
        {"market": ["DONE", "NEW"]},
        "",
        False,
        path,
        tmp_path / "progress.tmp",
        tmp_path / "progress.lock",
    )

    assert resumed.run_id == "run-1"
    assert resumed.stages["market"] == {"DONE": "done", "NEW": "pending"}


def test_mark_done_updates_item_status() -> None:
    """Marking an item complete changes its stage status to done."""
    state = Progress("run", "time", "source", {"market": {"ABC": "pending"}})

    mark_done(state, "market", "ABC")

    assert state.stages["market"]["ABC"] == "done"


def test_mark_failed_records_reason() -> None:
    """Marking an item failed records the reason with the failed prefix."""
    state = Progress("run", "time", "source", {"market": {"ABC": "pending"}})

    mark_failed(state, "market", "ABC", "timeout")

    assert state.stages["market"]["ABC"] == "failed:timeout"


def test_pending_returns_unfinished_items_for_stage() -> None:
    """Return pending work for one stage while excluding completed items."""
    state = Progress(
        "run",
        "time",
        "source",
        {
            "market": {"WAIT": "pending", "DONE": "done", "RETRY": "failed:timeout"},
            "macros": {"SERIES": "pending"},
        },
    )

    assert pending(state, "market") == ["WAIT", "RETRY"]


def test_acquire_lock_rejects_existing_lock(tmp_path: Path) -> None:
    """An already-created lock prevents a second run from acquiring it."""
    lock_path = tmp_path / "progress.lock"
    lock_path.touch()

    with pytest.raises(DownloadError, match="holds lock"):
        with acquire_lock(lock_path):
            pytest.fail("existing lock must not be acquired")


# cli/main.py


def _run_cli(*arguments: str | Path) -> subprocess.CompletedProcess[str]:
    """Run the CLI in a subprocess with the local src tree importable."""
    environment = os.environ.copy()
    existing_pythonpath = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = os.pathsep.join(
        item for item in (str(SOURCE_ROOT), existing_pythonpath) if item
    )
    return subprocess.run(
        [sys.executable, "-m", "cli.main", *(str(item) for item in arguments)],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def test_cli_help_exits_successfully() -> None:
    """The root CLI help command exits with status zero."""
    result = _run_cli("--help")

    assert result.returncode == 0
    assert "usage:" in result.stdout.lower()


def test_cli_download_help_exits_successfully() -> None:
    """The download subcommand help command exits with status zero."""
    result = _run_cli("download", "--help")

    assert result.returncode == 0
    assert "--stage" in result.stdout


def test_cli_market_stage_requires_dates() -> None:
    """Selecting market without both date bounds reports a helpful parser error."""
    result = _run_cli("download", "--stage", "market")

    assert result.returncode != 0
    assert "--start and --end are required" in result.stderr


def test_cli_macros_dry_run_does_not_create_data_files(tmp_path: Path) -> None:
    """A macro dry run leaves its isolated data directory completely empty."""
    result = _run_cli("download", "--dry-run", "--stage", "macros", "--data-dir", tmp_path)

    assert result.returncode == 0, result.stderr
    assert list(tmp_path.iterdir()) == []


def test_main_help_can_be_caught_as_system_exit() -> None:
    """Calling main with help raises argparse's successful SystemExit."""
    with pytest.raises(SystemExit) as exit_info:
        main(["--help"])

    assert exit_info.value.code == 0
