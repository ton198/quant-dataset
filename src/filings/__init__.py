"""Stable public filing-catalog APIs without optional parser imports."""

from .archive import (
    ArchiveConflictError,
    ArchiveCorruptionError,
    ArchiveError,
    ArchiveWriter,
    open_archive,
    read_snapshot,
)
from .catalog import (
    APPROVED_FORMS,
    CatalogConflictError,
    CatalogError,
    read_cached_catalog,
    submission_rows,
)
from .models import (
    DOCUMENTS_SCHEMA,
    FILINGS_SCHEMA,
    CatalogResource,
    CatalogResult,
    RawObjectRef,
    RunSpec,
    Snapshot,
    document_id,
    filing_id,
)

__all__ = [
    "APPROVED_FORMS",
    "ArchiveConflictError",
    "ArchiveCorruptionError",
    "ArchiveError",
    "ArchiveWriter",
    "CatalogConflictError",
    "CatalogError",
    "CatalogResource",
    "CatalogResult",
    "RawObjectRef",
    "RunSpec",
    "Snapshot",
    "DOCUMENTS_SCHEMA",
    "FILINGS_SCHEMA",
    "document_id",
    "filing_id",
    "open_archive",
    "read_cached_catalog",
    "read_snapshot",
    "submission_rows",
]
