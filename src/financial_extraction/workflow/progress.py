"""Transient coordinator events, excluded from requests, journals and result contracts."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from ..domain.contracts import WindowOutcome


@dataclass(frozen=True, slots=True)
class ExtractionProgress:
    kind: Literal[
        "archive_started",
        "sources_started",
        "source_started",
        "source_prepared",
        "evidence_started",
        "window_discovered",
        "windows_exhausted",
        "journal_count",
        "window_queued",
        "window_dequeued",
        "window_submitted",
        "window_response",
        "response_saved",
        "window_finished",
        "interrupted",
    ]
    document_id: str | None = None
    index: int | None = None
    total: int | None = None
    submitted: bool = False
    outcome: WindowOutcome | None = None
    replayed: bool = False


ProgressCallback = Callable[[ExtractionProgress], None]
