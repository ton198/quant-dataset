"""Pure binding of already-selected SEC document bytes to domain DTOs.

This adapter does not read snapshots or archives, fetch URLs, infer filing scope,
run XBRL/numeric parsers, or publish output. ``caller_verified_record`` retains
the host's identity and policy claims together with a recheck of only the bytes
passed here; it is not proof of an entire archive or of host authenticity.
"""

from __future__ import annotations

import codecs
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from filings.source.sec_envelope import extract_sec_envelope
from financial_extraction.domain import VerifiedDocument


class SourceBindingError(ValueError):
    """Selected source bytes fail host-binding or strict source validation."""


@dataclass(frozen=True, slots=True)
class HostSourceBinding:
    """Native host record for one already-selected filing document.

    Values are carried through unchanged for the host/audit layer. This type
    does not certify snapshot authenticity, policy correctness, or SEC scope.
    """

    snapshot_id: str
    filing_id: str
    document_id: str
    source_url: str
    scope_status: str
    numeric_parser_status: str
    raw_expected_sha256: str
    raw_expected_bytes: int
    public_availability: Any = None
    effective_visible_session: Any = None
    policy_ref: str | None = None
    cik: str | None = None
    accession: str | None = None
    filing_record: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CallerVerifiedRecord:
    """Unchanged caller binding plus verification scoped to received bytes only."""

    binding: HostSourceBinding
    actual_raw_sha256: str
    actual_raw_bytes: int
    hash_scope: str = "selected_document_bytes"
    payload_sha256: str | None = None
    payload_kind: str | None = None
    payload_start_byte: int | None = None
    payload_end_byte: int | None = None
    payload_encoding: str | None = None
    used_fallback: bool = False
    envelope_metadata: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SourceBindingDecision:
    """Non-publishable result of source/scope/parser eligibility checks."""

    status: str
    reasons: tuple[str, ...]
    caller_verified_record: CallerVerifiedRecord
    document: VerifiedDocument | None = None
    publishable: bool = field(default=False, init=False)


def _opaque_id(prefix: str, *parts: str) -> str:
    canonical = json.dumps(
        ["sec-financial-extraction-binding-v1", *parts],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(canonical).hexdigest()[:32]}"


def _validate_binding(binding: HostSourceBinding) -> None:
    if not isinstance(binding, HostSourceBinding):
        raise TypeError("binding must be a HostSourceBinding DTO")
    required = (
        binding.snapshot_id,
        binding.filing_id,
        binding.document_id,
        binding.source_url,
        binding.scope_status,
        binding.numeric_parser_status,
    )
    if any(not isinstance(value, str) or not value.strip() for value in required):
        raise SourceBindingError(
            "host binding identifiers, URL and statuses must be non-empty strings"
        )
    if not re.fullmatch(r"[0-9a-f]{64}", binding.raw_expected_sha256):
        raise SourceBindingError("raw_expected_sha256 must be a lowercase SHA-256 digest")
    if (
        not isinstance(binding.raw_expected_bytes, int)
        or isinstance(binding.raw_expected_bytes, bool)
        or binding.raw_expected_bytes < 0
    ):
        raise SourceBindingError("raw_expected_bytes must be a non-negative integer")
    if not isinstance(binding.filing_record, Mapping):
        raise SourceBindingError("filing_record must be a mapping")
    for name, value in (
        ("policy_ref", binding.policy_ref),
        ("cik", binding.cik),
        ("accession", binding.accession),
    ):
        if value is not None and not isinstance(value, str):
            raise SourceBindingError(f"{name} must be a string or null")


def _decision(
    status: str,
    reason: str,
    record: CallerVerifiedRecord,
) -> SourceBindingDecision:
    return SourceBindingDecision(status, (reason,), record)


def _strict_decode(payload: bytes, encoding: str) -> None:
    try:
        codecs.lookup(encoding)
        payload.decode(encoding, errors="strict")
    except (LookupError, UnicodeDecodeError) as exc:
        raise SourceBindingError(f"payload cannot be decoded strictly as {encoding!r}") from exc


def bind_financial_extraction_source(
    raw_bytes: bytes,
    binding: HostSourceBinding,
    *,
    max_source_bytes: int,
    source_type: str,
    plain_html_encoding: str | None = None,
    fixture_host_declaration: str | None = None,
) -> SourceBindingDecision:
    """Bind selected raw bytes to ``VerifiedDocument`` only for eligible HTML.

    The expected hash and byte count are checked against ``raw_bytes`` only; no
    archive/CAS lookup or broader proof is claimed. A recognized malformed SEC
    wrapper raises and never falls through to plain-HTML interpretation. For
    unwrapped HTML, ``plain_html_encoding`` must be provided and decoding is
    strict with no charset guess. Recognized envelopes supply their own checked
    byte-range encoding. Only envelope payload kind ``html`` is supported.

    Included input binds only when the host's numeric parser status is exactly
    ``unsupported``. Candidate scope is quarantined and excluded scope skipped;
    no adapter branch promotes filing selection. ``fixture_only`` is false by
    default and true only for the explicit synthetic type or a non-empty host
    fixture declaration.
    """
    _validate_binding(binding)
    if not isinstance(raw_bytes, bytes):
        raise TypeError("raw_bytes must be bytes already selected by the host")
    if (
        not isinstance(max_source_bytes, int)
        or isinstance(max_source_bytes, bool)
        or max_source_bytes <= 0
    ):
        raise ValueError("max_source_bytes must be a positive integer")
    if len(raw_bytes) > max_source_bytes:
        raise SourceBindingError("selected source exceeds max_source_bytes")
    if len(raw_bytes) != binding.raw_expected_bytes:
        raise SourceBindingError("selected source byte count differs from host expectation")
    actual_raw_hash = hashlib.sha256(raw_bytes).hexdigest()
    if actual_raw_hash != binding.raw_expected_sha256:
        raise SourceBindingError("selected source hash differs from host expectation")
    if not raw_bytes:
        raise SourceBindingError("selected source is empty")
    if fixture_host_declaration is not None and (
        not isinstance(fixture_host_declaration, str) or not fixture_host_declaration.strip()
    ):
        raise SourceBindingError("fixture host declaration must be a non-empty string or null")

    record = CallerVerifiedRecord(
        binding=binding,
        actual_raw_sha256=actual_raw_hash,
        actual_raw_bytes=len(raw_bytes),
    )
    if binding.scope_status == "excluded":
        return _decision("skipped", "SCOPE_EXCLUDED", record)
    if binding.scope_status == "candidate":
        return _decision("quarantined", "SCOPE_UNRESOLVED", record)
    if binding.scope_status != "included":
        return _decision("quarantined", "SCOPE_STATUS_UNKNOWN", record)

    numeric_status_reasons = {
        "full": ("skipped", "NUMERIC_PARSER_FULL"),
        "partial": ("quarantined", "NUMERIC_PARSER_PARTIAL"),
        "failed": ("quarantined", "NUMERIC_PARSER_FAILED"),
        "not_requested": ("skipped", "NUMERIC_PARSER_NOT_REQUESTED"),
    }
    if binding.numeric_parser_status != "unsupported":
        status, reason = numeric_status_reasons.get(
            binding.numeric_parser_status, ("quarantined", "NUMERIC_PARSER_STATUS_UNKNOWN")
        )
        return _decision(status, reason, record)

    try:
        envelope = extract_sec_envelope(raw_bytes)
    except ValueError as exc:
        raise SourceBindingError(f"SEC envelope failed closed: {exc}") from exc

    if not isinstance(source_type, str) or source_type not in {
        "sec_html",
        "synthetic_sec_fixture",
    }:
        return _decision("unsupported", "UNSUPPORTED_SOURCE_TYPE", record)
    if fixture_host_declaration is not None and fixture_host_declaration.strip():
        fixture_only = True
    else:
        fixture_only = source_type == "synthetic_sec_fixture"

    if envelope is not None:
        payload_kind = envelope["payload_kind"]
        payload = envelope["payload"]
        payload_encoding = envelope["encoding"]
        start_byte = envelope["start_byte"]
        end_byte = envelope["end_byte"]
        used_fallback = envelope["used_fallback"]
        envelope_metadata = dict(envelope["metadata"])
        if payload_kind != "html":
            unsupported_record = replace(
                record,
                payload_sha256=hashlib.sha256(payload).hexdigest(),
                payload_kind=payload_kind,
                payload_start_byte=start_byte,
                payload_end_byte=end_byte,
                payload_encoding=payload_encoding,
                used_fallback=used_fallback,
                envelope_metadata=envelope_metadata,
            )
            return _decision("unsupported", "UNSUPPORTED_PAYLOAD_KIND", unsupported_record)
        container_kind = "sec_sgml_text"
    else:
        if (
            plain_html_encoding is None
            or not isinstance(plain_html_encoding, str)
            or not plain_html_encoding.strip()
        ):
            raise SourceBindingError("plain HTML requires an explicit plain_html_encoding")
        payload = raw_bytes
        payload_encoding = plain_html_encoding
        _strict_decode(payload, payload_encoding)
        payload_kind = "html"
        start_byte = 0
        end_byte = len(raw_bytes)
        used_fallback = False
        envelope_metadata = {}
        container_kind = "plain_html"

    # SEC header metadata and host IDs stay in caller_verified_record, not the
    # generic origin projection passed into the source-agnostic domain.
    payload_hash = hashlib.sha256(payload).hexdigest()
    actual_encoding = payload_encoding
    revision = ":".join(
        (
            "sec-html-payload-v1",
            container_kind,
            actual_encoding.casefold(),
            "fallback" if used_fallback else "strict",
        )
    )
    identity_parts = (
        binding.snapshot_id,
        binding.filing_id,
        binding.document_id,
        actual_raw_hash,
    )
    source_id = _opaque_id("source", *identity_parts)
    document_id = _opaque_id("document", *identity_parts)
    document = VerifiedDocument(
        source_id=source_id,
        document_id=document_id,
        source_type=source_type,
        fixture_only=fixture_only,
        payload=payload,
        encoding=actual_encoding,
        raw_sha256=actual_raw_hash,
        raw_bytes=len(raw_bytes),
        payload_sha256=payload_hash,
        preprocessing_revision=revision,
        origin={
            "container_kind": container_kind,
            "payload_kind": "html",
            "locator_kind": "byte_range_in_received_document",
            "start_byte": start_byte,
            "end_byte": end_byte,
            "preprocessing_kind": "sec-html-payload-v1",
            "used_fallback": used_fallback,
        },
        limits={"max_source_bytes": max_source_bytes},
    )
    verified_record = replace(
        record,
        payload_sha256=payload_hash,
        payload_kind="html",
        payload_start_byte=start_byte,
        payload_end_byte=end_byte,
        payload_encoding=actual_encoding,
        used_fallback=used_fallback,
        envelope_metadata=envelope_metadata,
    )
    return SourceBindingDecision("bound", (), verified_record, document)
