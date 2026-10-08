"""Immutable contracts for source-grounded open-field financial extraction."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from .metrics import NumericPolicy


@dataclass(frozen=True, slots=True)
class VerifiedDocument:
    source_id: str
    document_id: str
    source_type: str
    fixture_only: bool
    payload: bytes
    encoding: str
    raw_sha256: str
    raw_bytes: int
    payload_sha256: str
    preprocessing_revision: str
    origin: Mapping[str, Any] = field(default_factory=dict)
    limits: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    document_id: str
    evidence_id: str
    start: int | None = None
    end: int | None = None
    quote: str | None = None


@dataclass(frozen=True, slots=True)
class EvidenceBlock:
    block_id: str
    document_id: str
    kind: str
    text: str
    xpath: str
    canonical_start: int
    canonical_end: int
    parent_block_id: str | None = None
    table_id: str | None = None
    heading_context: tuple[str, ...] = ()
    context_refs: tuple[str, ...] = ()
    raw_byte_start: None = None
    raw_byte_end: None = None
    limitations: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EvidenceCell:
    cell_id: str
    document_id: str
    table_id: str
    parent_table_id: str | None
    row_index: int
    column_index: int
    text: str
    xpath: str
    rowspan: int
    colspan: int
    is_header: bool
    covered_positions: tuple[tuple[int, int], ...]
    canonical_start: int
    canonical_end: int
    heading_context: tuple[str, ...] = ()
    context_refs: tuple[str, ...] = ()
    raw_byte_start: None = None
    raw_byte_end: None = None
    limitations: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EvidenceBundle:
    documents: tuple[VerifiedDocument, ...]
    blocks: tuple[EvidenceBlock, ...]
    cells: tuple[EvidenceCell, ...]
    canonical_text: str
    canonical_text_hash: str
    preprocessing_config: Mapping[str, Any]
    preprocessing_config_hash: str
    limits: Mapping[str, int]
    limitations: tuple[str, ...] = ()
    bundle_hash: str = ""


@dataclass(frozen=True, slots=True)
class Quote:
    ref: str
    text: str


@dataclass(frozen=True, slots=True)
class Period:
    kind: str | None = None
    start: str | None = None
    end: str | None = None
    sources: tuple[Quote, ...] = ()


@dataclass(frozen=True, slots=True)
class Dimension:
    name: str
    value: str
    sources: tuple[Quote, ...] = ()


@dataclass(frozen=True, slots=True)
class ExtractedRecord:
    source_label: str
    name: str
    category: str
    value: str
    value_type: Literal["number", "percentage", "ratio", "text"]
    value_source: Quote
    label_source: Quote
    numeric_policy_id: str | None = None
    unit: str | None = None
    currency: str | None = None
    scale: str | None = None
    unit_sources: tuple[Quote, ...] = ()
    currency_sources: tuple[Quote, ...] = ()
    scale_sources: tuple[Quote, ...] = ()
    period: Period = field(default_factory=Period)
    dimensions: tuple[Dimension, ...] = ()


@dataclass(frozen=True, slots=True)
class SourceLocation:
    document_id: str
    evidence_id: str
    xpath: str
    canonical_start: int
    canonical_end: int


@dataclass(frozen=True, slots=True)
class WindowItem:
    ref: str
    kind: str
    text: str
    source: SourceLocation


@dataclass(frozen=True, slots=True)
class ExtractionWindow:
    window_id: str
    document_id: str
    core: tuple[WindowItem, ...]
    context: tuple[WindowItem, ...] = ()


@dataclass(frozen=True, slots=True)
class ValidationProblem:
    code: str
    field: str | None
    message: str


@dataclass(frozen=True, slots=True)
class ResolvedSource:
    field: str
    location: SourceLocation
    quote: str


@dataclass(frozen=True, slots=True)
class ValidatedRecord:
    record_id: str
    extracted: ExtractedRecord
    normalized_amount: str | None
    sources: tuple[ResolvedSource, ...]
    problems: tuple[ValidationProblem, ...]
    status: str


@dataclass(frozen=True, slots=True)
class WindowValidation:
    records: tuple[ValidatedRecord, ...]
    problems: tuple[ValidationProblem, ...]


@dataclass(frozen=True, slots=True)
class ExtractionTask:
    numeric_policies: tuple[NumericPolicy, ...]
    prompt_revision: str = "open-financial-v3"
    schema_revision: str = "open-records-v1"


@dataclass(frozen=True, slots=True)
class ExtractionLimits:
    max_request_bytes: int = 100_000
    max_output_tokens: int = 12_000


@dataclass(frozen=True, slots=True)
class WindowOutcome:
    window_id: str
    document_id: str
    status: str
    validation: WindowValidation | None = None
    error: str | None = None
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class ExtractionRun:
    run_id: str
    status: str
    records: tuple[ValidatedRecord, ...]
    windows: tuple[WindowOutcome, ...]
    problems: tuple[ValidationProblem, ...]
    publishable: bool = field(default=False, init=False)
