"""Stable filing identities, core Arrow schemas, and catalog result models.

This module is intentionally independent of optional XBRL tooling.  The schemas
here describe the immutable core filing ledger consumed by later archive code.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pyarrow as pa

FILINGS_SCHEMA = pa.schema(
    [
        pa.field("filing_id", pa.string(), nullable=False),
        pa.field("cik10", pa.string(), nullable=False),
        pa.field("accession_number", pa.string(), nullable=False),
        pa.field("form", pa.string(), nullable=False),
        pa.field("filed_date", pa.date32(), nullable=False),
        pa.field("report_period_end", pa.date32(), nullable=True),
        pa.field("acceptance_datetime_raw", pa.string(), nullable=True),
        pa.field("acceptance_datetime_utc", pa.timestamp("us", tz="UTC"), nullable=True),
        pa.field("effective_visible_session", pa.date32(), nullable=False),
        pa.field("is_amendment", pa.bool_(), nullable=False),
        pa.field("parent_filing_id", pa.string(), nullable=True),
        pa.field("parent_link_source", pa.string(), nullable=True),
        pa.field("primary_document_name", pa.string(), nullable=True),
        pa.field("scope_status", pa.string(), nullable=False),
        pa.field("scope_evidence_json", pa.string(), nullable=True),
        pa.field("inventory_status", pa.string(), nullable=False),
        pa.field("raw_coverage_status", pa.string(), nullable=False),
        pa.field("source_submission_logical_key", pa.string(), nullable=False),
        pa.field("source_submission_sha256", pa.string(), nullable=False),
        pa.field("source_submission_path", pa.string(), nullable=False),
        pa.field("source_submission_locator", pa.string(), nullable=False),
    ]
)

DOCUMENTS_SCHEMA = pa.schema(
    [
        pa.field("document_id", pa.string(), nullable=False),
        pa.field("filing_id", pa.string(), nullable=False),
        pa.field("original_filename", pa.string(), nullable=False),
        pa.field("source_url", pa.string(), nullable=False),
        pa.field("role", pa.string(), nullable=False),
        pa.field("selection_status", pa.string(), nullable=False),
        pa.field("fetch_status", pa.string(), nullable=False),
        pa.field("source_inventory_sha256", pa.string(), nullable=False),
        pa.field("source_inventory_locator", pa.string(), nullable=False),
        pa.field("raw_sha256", pa.string(), nullable=True),
        pa.field("raw_path", pa.string(), nullable=True),
        pa.field("byte_size", pa.int64(), nullable=True),
        pa.field("media_type", pa.string(), nullable=True),
        pa.field("fetched_at_utc", pa.timestamp("us", tz="UTC"), nullable=True),
        pa.field("http_status", pa.int16(), nullable=True),
        pa.field("diagnostic_code", pa.string(), nullable=True),
        pa.field("fact_extraction_status", pa.string(), nullable=False),
        pa.field("text_extraction_status", pa.string(), nullable=False),
    ]
)


_ACCESSION_RE = re.compile(r"[0-9]{10}-[0-9]{2}-[0-9]{6}\Z", re.ASCII)
_CIK10_RE = re.compile(r"[0-9]{10}\Z", re.ASCII)


def filing_id(cik10: str, accession_number: str) -> str:
    """Return the literal issuer-CIK/accession identity after strict validation.

    An accession's first ten digits identify its SEC submitting agent, which is
    intentionally not required to match the issuer CIK.
    """
    if not isinstance(cik10, str) or _CIK10_RE.fullmatch(cik10) is None:
        raise ValueError("cik10 must contain exactly 10 ASCII digits")
    if not isinstance(accession_number, str) or _ACCESSION_RE.fullmatch(accession_number) is None:
        raise ValueError("accession_number must match NNNNNNNNNN-NN-NNNNNN")
    return f"{cik10}:{accession_number}"


def document_id(filing: str, source_url: str) -> str:
    """Return the stable SHA-256 document identity for a filing/source URL pair."""
    if not isinstance(filing, str) or not filing:
        raise ValueError("filing must be a non-empty filing_id")
    if not isinstance(source_url, str) or not source_url:
        raise ValueError("source_url must be a non-empty string")
    canonical = json.dumps(
        ["document-v1", filing, source_url],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


@dataclass(frozen=True)
class RunSpec:
    """Stable archive run configuration; canonicalized independent of root path."""

    approved_forms: tuple[str, ...] = (
        "10-K",
        "10-K/A",
        "10-Q",
        "10-Q/A",
        "20-F",
        "20-F/A",
        "40-F",
        "40-F/A",
        "6-K",
    )
    policy: Mapping[str, Any] = field(default_factory=dict)
    input_fingerprint: str = ""
    calendar_provenance: Mapping[str, Any] = field(default_factory=dict)
    coverage_provenance: Mapping[str, Any] = field(default_factory=dict)
    scope_provenance: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        forms = tuple(self.approved_forms)
        allowed_forms = {
            "10-K",
            "10-K/A",
            "10-Q",
            "10-Q/A",
            "20-F",
            "20-F/A",
            "40-F",
            "40-F/A",
            "6-K",
        }
        if any(not isinstance(form, str) or form not in allowed_forms for form in forms):
            raise ValueError("approved_forms must be unique approved SEC forms")
        if len(set(forms)) != len(forms):
            raise ValueError("approved_forms must be unique approved SEC forms")
        if not isinstance(self.input_fingerprint, str):
            raise ValueError("input_fingerprint must be a string")
        for name in (
            "policy",
            "calendar_provenance",
            "coverage_provenance",
            "scope_provenance",
        ):
            value = getattr(self, name)
            if not isinstance(value, Mapping):
                raise ValueError(f"{name} must be a mapping")
            try:
                json.dumps(
                    _thaw_json(value),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{name} must contain canonical JSON values") from exc
        object.__setattr__(self, "approved_forms", forms)
        for name in (
            "policy",
            "calendar_provenance",
            "coverage_provenance",
            "scope_provenance",
        ):
            object.__setattr__(self, name, _freeze_json(dict(getattr(self, name))))

    def canonical_dict(self) -> dict[str, Any]:
        """Return the root-independent JSON object hashed into archive manifests."""
        return {
            "approved_forms": list(self.approved_forms),
            "policy": _thaw_json(self.policy),
            "input_fingerprint": self.input_fingerprint,
            "calendar_provenance": _thaw_json(self.calendar_provenance),
            "coverage_provenance": _thaw_json(self.coverage_provenance),
            "scope_provenance": _thaw_json(self.scope_provenance),
        }

    @property
    def sha256(self) -> str:
        canonical = json.dumps(
            self.canonical_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()


@dataclass(frozen=True)
class RawObjectRef:
    """Immutable content-addressed object reference inside an archive root."""

    path: str
    sha256: str
    byte_size: int


@dataclass(frozen=True)
class Snapshot:
    """Read-only view of one fully verified published archive snapshot."""

    root: Path
    manifest: Mapping[str, Any]
    tables: Mapping[str, Any]
    raw_objects: tuple[RawObjectRef, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "manifest", _freeze_json(dict(self.manifest)))
        object.__setattr__(self, "tables", MappingProxyType(dict(self.tables)))
        object.__setattr__(self, "raw_objects", tuple(self.raw_objects))

    @property
    def snapshot_id(self) -> str:
        return str(self.manifest["snapshot_id"])

    @property
    def manifest_version(self) -> int:
        return int(self.manifest["manifest_version"])


@dataclass(frozen=True)
class CatalogResource:
    """A verified cached submission object and its manifest provenance."""

    logical_key: str
    sha256: str
    byte_size: int
    source_url: str
    path: str
    source_path: Path


@dataclass(frozen=True)
class CatalogResult:
    """Catalog output retaining verified source references for later archive copy."""

    filings: tuple[Mapping[str, Any], ...]
    resources: tuple[CatalogResource, ...]
    diagnostics: tuple[str, ...]
    input_status: str
    input_fingerprint: str
