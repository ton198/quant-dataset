"""Domain-specific exceptions used by the download pipeline."""

from __future__ import annotations


class ConfigError(RuntimeError):
    """Invalid or incomplete pipeline configuration."""


class DownloadError(RuntimeError):
    """An expected provider or download failure."""


class OrganizeError(RuntimeError):
    """An invalid or unusable raw input during organization."""
