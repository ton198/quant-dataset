"""Thread-safe stderr progress display for the financial extraction CLI."""

from __future__ import annotations

import sys
import threading
import time

from financial_extraction.workflow.progress import ExtractionProgress


class ExtractionProgressReporter:
    def __init__(self, *, stream=None, interval: float = 1.0) -> None:
        self.stream = stream if stream is not None else sys.stderr
        self.interval = interval
        self.tty = bool(getattr(self.stream, "isatty", lambda: False)())
        self._started = time.monotonic()
        self._last_output = 0.0
        self._phase = "准备提取"
        self._current = ""
        self._filing_total: int | None = None
        self._filing_prepared = 0
        self._discovered = 0
        self._queued = 0
        self._total: int | None = None
        self._finished = 0
        self._failed = 0
        self._replayed = 0
        self._unresolved = 0
        self._inflight = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._closed = False
        self._final_output = False
        self._saved = 0
        self._lock = threading.Lock()

    def __enter__(self) -> ExtractionProgressReporter:
        self._thread = threading.Thread(
            target=self._heartbeat, name="extraction-progress", daemon=True
        )
        self._thread.start()
        self._write(force=True)
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if exc_type is not None and issubclass(exc_type, KeyboardInterrupt):
            self._phase = "已中断；等待运行中的请求收尾"
        elif exc_type is not None:
            self._phase = "提取失败"
        elif not self._closed:
            self._phase = "结束"
        self.close()

    def __call__(self, event: ExtractionProgress) -> None:
        with self._lock:
            if event.kind == "archive_started":
                self._phase = "校验档案"
                self._filing_total = event.total
            elif event.kind == "journal_count":
                self._saved = event.total or 0
            elif event.kind == "sources_started":
                self._phase = "准备来源"
            elif event.kind == "source_started":
                self._current = event.document_id or ""
            elif event.kind == "source_prepared":
                self._filing_prepared += 1
                self._current = event.document_id or ""
            elif event.kind == "evidence_started":
                self._phase = "构建证据与提取"
                self._current = event.document_id or self._current
            elif event.kind == "window_discovered":
                self._discovered += 1
                self._phase = "构建证据与提取"
                self._current = event.document_id or self._current
            elif event.kind == "window_queued":
                self._queued += 1
            elif event.kind == "window_dequeued":
                self._queued = max(0, self._queued - 1)
                self._current = event.document_id or self._current
            elif event.kind == "window_submitted":
                self._inflight += 1
                self._current = event.document_id or self._current
            elif event.kind == "window_response":
                self._inflight = max(0, self._inflight - 1)
                self._current = event.document_id or self._current
            elif event.kind == "response_saved":
                self._saved += 1
            elif event.kind == "window_finished":
                self._finished += 1
                self._current = event.document_id or self._current
                if event.replayed:
                    self._replayed += 1
                if event.outcome is not None and event.outcome.status == "failed":
                    self._failed += 1
                elif event.outcome is not None and event.outcome.validation is not None:
                    self._unresolved += sum(
                        record.status != "valid" for record in event.outcome.validation.records
                    )
            elif event.kind == "windows_exhausted":
                self._total = event.total
                self._phase = "等待窗口收尾" if self._inflight else "提取完成"
        force = event.kind in {
            "archive_started",
            "sources_started",
            "source_prepared",
            "windows_exhausted",
        }
        force = force or (self.tty and event.kind == "window_finished")
        force = force or (
            event.kind == "window_finished"
            and event.outcome is not None
            and event.outcome.status == "failed"
        )
        self._write(force=force)

    def result_writing(self) -> None:
        with self._lock:
            self._phase = "写入结果"
        self._write(force=True)

    def finish(self, status: str) -> None:
        with self._lock:
            self._phase = f"完成：{status}"
            self._closed = True
        self.close()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join()
            self._thread = None
        if not self._final_output:
            self._final_output = True
            self._write(force=True, final=True)

    def _heartbeat(self) -> None:
        while not self._stop.wait(self.interval):
            self._write(force=False)

    def _line(self) -> str:
        elapsed = int(time.monotonic() - self._started)
        minutes, seconds = divmod(elapsed, 60)
        elapsed_text = f"{minutes:02d}:{seconds:02d}"
        if self._phase == "准备提取":
            counts = ""
        elif self._phase in {"校验档案", "准备来源"}:
            counts = f" filings={self._filing_prepared}/{self._filing_total or '?'}"
        else:
            total = str(self._total) if self._total is not None else "?"
            completed = (
                f"{self._finished}/{total}"
                if self._total is not None
                else f"{self._finished} (发现 {self._discovered}，总数未知)"
            )
            counts = (
                f" windows={completed} queued={self._queued} inflight={self._inflight}"
                f" saved={self._saved} failed={self._failed}"
                f" replayed={self._replayed} unresolved={self._unresolved}"
            )
        current = f" current={self._current}" if self._current else ""
        return f"[financial-extraction] {self._phase}{counts}{current} elapsed={elapsed_text}"

    def _write(self, *, force: bool, final: bool = False) -> None:
        now = time.monotonic()
        with self._lock:
            interval = self.interval if self.tty else max(self.interval, 10.0)
            if not force and now - self._last_output < interval:
                return
            line = self._line()
            self._last_output = now
            try:
                if self.tty and not final:
                    self.stream.write("\r\x1b[2K" + line)
                else:
                    if self.tty and final:
                        self.stream.write("\r\x1b[2K")
                    self.stream.write(line + "\n")
                self.stream.flush()
            except (BrokenPipeError, OSError, ValueError):
                self._stop.set()
