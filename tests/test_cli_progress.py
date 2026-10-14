"""Tests for interactive and redirected CLI progress output."""

from __future__ import annotations

import io
import logging
from types import SimpleNamespace

from cli import progress


class _Terminal(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_tty_progress_uses_carriage_return_and_finishes_line(monkeypatch) -> None:
    terminal = _Terminal()
    monkeypatch.setattr(progress, "sys", SimpleNamespace(stderr=terminal))

    progress.report_progress(logging.getLogger("test.progress"), "step 1/2")
    progress.finish_progress()

    assert terminal.getvalue() == "\rstep 1/2\n"


def test_redirected_progress_logs_complete_line(monkeypatch, caplog) -> None:
    monkeypatch.setattr(progress.sys.stderr, "isatty", lambda: False)
    logger = logging.getLogger("test.redirected-progress")
    caplog.set_level(logging.INFO, logger=logger.name)

    progress.report_progress(logger, "step 1/2")

    assert "step 1/2" in caplog.text
