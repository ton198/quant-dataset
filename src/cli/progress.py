"""Shared command-line logging and progress rendering."""

from __future__ import annotations

import logging
import sys

_progress_active = False


class _ProgressHandler(logging.StreamHandler):
    def emit(self, record: logging.LogRecord) -> None:
        if _progress_active and sys.stderr.isatty():
            print(file=sys.stderr, flush=True)
        super().emit(record)


def configure_logging() -> None:
    """Send INFO progress messages to stderr and keep logs off the active progress line."""
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if not root.handlers:
        handler = _ProgressHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        root.addHandler(handler)
    else:
        for handler in root.handlers:
            handler.setLevel(logging.INFO)


def report_progress(logger: logging.Logger, message: str) -> None:
    """Render a live in-place TTY update or a normal redirected log line."""
    global _progress_active
    if sys.stderr.isatty():
        print(f"\r{message}", file=sys.stderr, end="", flush=True)
        _progress_active = True
    else:
        logger.info(message)


def finish_progress() -> None:
    """Terminate the current in-place progress line, if one is being rendered."""
    global _progress_active
    if _progress_active and sys.stderr.isatty():
        print(file=sys.stderr, flush=True)
    _progress_active = False
