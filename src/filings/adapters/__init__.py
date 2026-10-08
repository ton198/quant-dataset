"""Thin adapters from host-approved filing inputs into financial extraction."""

from .financial_extraction import (
    CallerVerifiedRecord,
    HostSourceBinding,
    SourceBindingDecision,
    SourceBindingError,
    bind_financial_extraction_source,
)

__all__ = [
    "CallerVerifiedRecord",
    "HostSourceBinding",
    "SourceBindingDecision",
    "SourceBindingError",
    "bind_financial_extraction_source",
]
