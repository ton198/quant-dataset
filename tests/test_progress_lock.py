"""Tests for stale progress lock recovery."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from download.errors import DownloadError
from download.progress import acquire_lock


def test_stale_pid_lock_is_reclaimed(tmp_path: Path) -> None:
    """A lock owned by a nonexistent PID is removed so acquisition can proceed."""
    lock_path = tmp_path / ".download_progress.lock"
    lock_path.write_text(f"{1 << 30}\n", encoding="ascii")

    with acquire_lock(lock_path):
        assert lock_path.read_text(encoding="ascii") == f"{os.getpid()}\n"

    assert not lock_path.exists()


def test_live_pid_lock_is_not_reclaimed(tmp_path: Path) -> None:
    """A lock owned by this live process continues to block acquisition."""
    lock_path = tmp_path / ".download_progress.lock"
    lock_path.write_text(f"{os.getpid()}\n", encoding="ascii")

    with pytest.raises(DownloadError, match="Another download run holds lock"):
        with acquire_lock(lock_path):
            pytest.fail("an active lock must not be acquired")

    assert lock_path.read_text(encoding="ascii") == f"{os.getpid()}\n"


def test_lock_is_removed_after_normal_acquisition(tmp_path: Path) -> None:
    """The owner removes its lock file after leaving the context."""
    lock_path = tmp_path / ".download_progress.lock"

    with acquire_lock(lock_path):
        assert lock_path.exists()
        assert lock_path.read_text(encoding="ascii") == f"{os.getpid()}\n"

    assert not lock_path.exists()


@pytest.mark.parametrize("contents", ["", "not-a-pid\n", "-2\n"])
def test_malformed_lock_is_reclaimed(tmp_path: Path, contents: str) -> None:
    """Empty and invalid owner values are reclaimed as stale lock files."""
    lock_path = tmp_path / ".download_progress.lock"
    lock_path.write_text(contents, encoding="ascii")
    os.utime(lock_path, (time.time() - 1, time.time() - 1))

    with acquire_lock(lock_path):
        assert lock_path.read_text(encoding="ascii") == f"{os.getpid()}\n"

    assert not lock_path.exists()
