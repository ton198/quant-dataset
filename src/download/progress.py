"""Atomic persistence and resumable work-list management for download runs."""

from __future__ import annotations

import errno
import json
import os
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from .errors import DownloadError


@dataclass
class Progress:
    """Persistent per-item state for a download run."""

    run_id: str
    started_at_utc: str
    universe_source: str
    stages: dict[str, dict[str, str]]


def load(path: Path) -> Progress | None:
    """Read progress, returning ``None`` when no valid file exists."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return Progress(
            run_id=str(value["run_id"]),
            started_at_utc=str(value["started_at_utc"]),
            universe_source=str(value["universe_source"]),
            stages={
                str(stage): {str(item): str(status) for item, status in items.items()}
                for stage, items in value["stages"].items()
            },
        )
    except FileNotFoundError:
        return None
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        raise DownloadError(f"Invalid progress file {path}: {exc}") from exc


def save_atomic(progress: Progress, path: Path, tmp_path: Path) -> None:
    """Write progress via a flushed temporary file followed by atomic rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(asdict(progress), indent=2, sort_keys=True) + "\n").encode("utf-8")
    try:
        with tmp_path.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.rename(tmp_path, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    except OSError as exc:
        raise DownloadError(f"Unable to save progress to {path}: {exc}") from exc


def _lock_file_entry(lock_path: Path) -> tuple[tuple[int, int], int | None, bool, float] | None:
    """Read lock ownership and identity, returning None if the file vanished."""
    try:
        with lock_path.open("rb") as handle:
            file_stat = os.fstat(handle.fileno())
            identity = (file_stat.st_dev, file_stat.st_ino)
            content = handle.read()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise DownloadError(f"Unable to inspect lock {lock_path}: {exc}") from exc

    try:
        pid = int(content.decode("ascii").strip())
        if pid <= 0:
            pid = None
    except (UnicodeDecodeError, ValueError):
        pid = None
    return identity, pid, not content.strip(), file_stat.st_mtime


def _pid_is_alive(pid: int) -> bool:
    """Check whether a PID exists; permission and unexpected errors fail safe."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return False
        if exc.errno == errno.EPERM:
            return True
        return True
    return True


def _unlink_if_same_inode(lock_path: Path, identity: tuple[int, int]) -> bool:
    """Unlink only if the pathname still refers to the inspected lock file."""
    try:
        current = lock_path.stat()
    except FileNotFoundError:
        return True
    except OSError as exc:
        raise DownloadError(f"Unable to inspect lock {lock_path}: {exc}") from exc
    if (current.st_dev, current.st_ino) != identity:
        return False
    try:
        lock_path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise DownloadError(f"Unable to remove stale lock {lock_path}: {exc}") from exc
    return True


def _reclaim_stale_lock(lock_path: Path) -> bool:
    """Remove a dead owner's unchanged lock, or report that it is active."""
    entry = _lock_file_entry(lock_path)
    if entry is None:
        return True
    identity, pid, empty, modified_at = entry
    # acquire_lock writes its PID immediately after O_EXCL creation. Keep a
    # very short grace window for the normal open/write interval so another
    # process cannot mistake a just-created (but not yet populated) lock for a
    # stale one. An older empty file is stale (e.g. left by an interrupted write).
    if empty and time.time() - modified_at < 0.05:
        return False
    # A missing or malformed PID is stale after the creation/write window.
    if pid is not None and _pid_is_alive(pid):
        return False
    return _unlink_if_same_inode(lock_path, identity)


@contextmanager
def acquire_lock(lock_path: Path) -> Iterator[None]:
    """Acquire an exclusive PID lock, reclaiming stale files with bounded retries."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    pid = os.getpid()
    descriptor: int | None = None
    for attempt in range(3):
        try:
            descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            if not _reclaim_stale_lock(lock_path):
                raise DownloadError(f"Another download run holds lock {lock_path}") from exc
            if attempt == 2:
                raise DownloadError(
                    f"Unable to acquire lock after stale-lock retries: {lock_path}"
                ) from exc
            continue
        try:
            os.write(descriptor, f"{pid}\n".encode("ascii"))
            os.fsync(descriptor)
        except OSError:
            identity_stat = os.fstat(descriptor)
            _unlink_if_same_inode(lock_path, (identity_stat.st_dev, identity_stat.st_ino))
            os.close(descriptor)
            raise
        identity_stat = os.fstat(descriptor)
        descriptor_identity = (identity_stat.st_dev, identity_stat.st_ino)
        entry = _lock_file_entry(lock_path)
        if entry is None or entry[0] != descriptor_identity or entry[1] != pid:
            # Another process reclaimed an empty file during a long scheduler
            # pause between O_EXCL and the PID write. Do not proceed unless the
            # pathname still names the lock inode we created.
            os.close(descriptor)
            descriptor = None
            continue
        break

    if descriptor is None:
        raise DownloadError(f"Unable to acquire lock {lock_path}")
    try:
        yield
    finally:
        try:
            entry = _lock_file_entry(lock_path)
            descriptor_stat = os.fstat(descriptor)
            descriptor_identity = (descriptor_stat.st_dev, descriptor_stat.st_ino)
            if entry is not None and entry[0] == descriptor_identity and entry[1] == pid:
                _unlink_if_same_inode(lock_path, descriptor_identity)
        finally:
            os.close(descriptor)


def initialize(
    stages: dict[str, list[str]],
    universe_source: str,
    force: bool,
    path: Path,
    tmp_path: Path,
    lock_path: Path,
) -> Progress:
    """Load resumable state or create a fresh progress work list."""
    del lock_path  # Lock lifetime is managed by ``acquire_lock`` at the caller.
    existing = None if force else load(path)
    if existing is None:
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        progress = Progress(
            uuid.uuid4().hex,
            now,
            universe_source,
            {stage: {item: "pending" for item in items} for stage, items in stages.items()},
        )
    else:
        progress = existing
        progress.universe_source = universe_source or progress.universe_source
        for stage, items in stages.items():
            progress.stages.setdefault(stage, {})
            for item in items:
                progress.stages[stage].setdefault(item, "pending")
    save_atomic(progress, path, tmp_path)
    return progress


def mark_done(progress: Progress, stage: str, item: str) -> None:
    """Mark a stage item complete."""
    progress.stages.setdefault(stage, {})[item] = "done"


def mark_failed(progress: Progress, stage: str, item: str, reason: str) -> None:
    """Mark a stage item failed with a concise diagnostic."""
    progress.stages.setdefault(stage, {})[item] = f"failed:{reason}"


def pending(progress: Progress, stage: str) -> list[str]:
    """Return items not marked complete, including prior failures for retry."""
    return [item for item, status in progress.stages.get(stage, {}).items() if status != "done"]
