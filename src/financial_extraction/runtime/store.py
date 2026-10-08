"""Crash-safe request/response journal with exact replay and fail-closed pending calls."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from .client import ModelRequest, ModelResponse, canonical_json


class StoreError(RuntimeError):
    """The durable journal is corrupt, unsafe, or unavailable."""


class RequestStateError(StoreError):
    """One request cannot be resumed safely; other requests may proceed."""


class ReplayStore:
    def __init__(
        self,
        work_dir: str | Path,
        *,
        protected_paths: tuple[str | Path, ...] = (),
    ) -> None:
        raw_root = Path(work_dir).expanduser()
        if raw_root.is_symlink():
            raise StoreError("work_dir must not be a symlink")
        self.root = raw_root.resolve()
        protected = tuple(Path(path).resolve() for path in protected_paths)
        if any(
            self.root == path or self.root in path.parents or path in self.root.parents
            for path in protected
        ):
            raise StoreError("work_dir overlaps a protected path")
        self.root.mkdir(parents=True, exist_ok=True)
        if self.root.is_symlink():
            raise StoreError("work_dir must not be a symlink")
        self.journal = self.root / "journal.jsonl"
        if self.journal.is_symlink():
            raise StoreError("journal must not be a symlink")

    def _read(self) -> dict[str, dict[str, Any]]:
        if not self.journal.exists():
            return {}
        rows: dict[str, dict[str, Any]] = {}
        try:
            with self.journal.open("r", encoding="utf-8") as stream:
                for line_number, line in enumerate(stream, 1):
                    item = json.loads(line)
                    if not isinstance(item, dict) or "identity" not in item or "state" not in item:
                        raise StoreError(f"invalid journal record at line {line_number}")
                    rows[item["identity"]] = item
        except (OSError, json.JSONDecodeError) as exc:
            raise StoreError("journal is unreadable or corrupt") from exc
        return rows

    def begin(self, request: ModelRequest) -> tuple[str, ModelResponse | None]:
        identity = request.identity
        with self._lock():
            row = self._read().get(identity)
            if row is not None:
                if row.get("request") != request.canonical_dict():
                    raise RequestStateError("request identity collision")
                if row["state"] == "complete":
                    response = row["response"]
                    raw = response["raw_json"]
                    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
                    if digest != response["response_sha256"]:
                        raise StoreError("stored response hash mismatch")
                    return identity, ModelResponse(
                        raw_json=raw,
                        input_tokens=response["input_tokens"],
                        output_tokens=response["output_tokens"],
                        finish_reason=response["finish_reason"],
                        raw_response=response.get("raw_response"),
                    )
                raise RequestStateError("prior request state is unknown; automatic resubmission is unsafe")
            self._append(
                {"identity": identity, "state": "pending", "request": request.canonical_dict()}
            )
            return identity, None

    def complete(self, identity: str, response: ModelResponse) -> None:
        with self._lock():
            row = self._read().get(identity)
            if row is None or row.get("state") != "pending":
                raise StoreError("response does not match one pending request")
            raw = response.raw_json
            record = {
                "identity": identity,
                "state": "complete",
                "request": row["request"],
                "response": {
                    "raw_json": raw,
                    "response_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
                    "input_tokens": response.input_tokens,
                    "output_tokens": response.output_tokens,
                    "finish_reason": response.finish_reason,
                    "raw_response": response.raw_response,
                },
            }
            self._append(record)

    def completed_count(self) -> int:
        """Return the number of latest completed request identities."""
        with self._lock():
            return sum(row.get("state") == "complete" for row in self._read().values())
    def _lock(self):
        lock_path = self.root / ".journal.lock"
        if lock_path.is_symlink():
            raise StoreError("journal lock must not be a symlink")
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(lock_path, flags, 0o600)
        stream = os.fdopen(fd, "r+b")
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        return stream

    def _append(self, value: dict[str, Any]) -> None:
        encoded = (canonical_json(value) + "\n").encode("utf-8")
        flags = os.O_CREAT | os.O_APPEND | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self.journal, flags, 0o600)
            with os.fdopen(fd, "ab") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            dir_fd = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError as exc:
            raise StoreError("journal write failed") from exc
