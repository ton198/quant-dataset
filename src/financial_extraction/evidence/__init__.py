"""Bounded evidence extraction; lxml is loaded only by ``build_evidence``."""

from .blocks import (
    EvidenceBuildError,
    EvidenceLimitError,
    build_evidence,
    iter_windows,
    render_window,
)

__all__ = [
    "EvidenceBuildError",
    "EvidenceLimitError",
    "build_evidence",
    "iter_windows",
    "render_window",
]
