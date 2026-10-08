"""Pure SEC-byte binding tests using synthetic documents only."""

from __future__ import annotations

import hashlib
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from filings.adapters.financial_extraction import (
    HostSourceBinding,
    SourceBindingError,
    bind_financial_extraction_source,
)
from filings.source.sec_envelope import extract_sec_envelope
from financial_extraction.domain import VerifiedDocument

MAX_SOURCE_BYTES = 100_000


def _binding(raw: bytes, **changes) -> HostSourceBinding:
    values = {
        "snapshot_id": "synthetic-snapshot-7",
        "filing_id": "synthetic-native-filing",
        "document_id": "synthetic-native-document",
        "source_url": "https://example.invalid/synthetic/statement.htm",
        "scope_status": "included",
        "numeric_parser_status": "unsupported",
        "raw_expected_sha256": hashlib.sha256(raw).hexdigest(),
        "raw_expected_bytes": len(raw),
        "public_availability": date(2030, 1, 31),
        "effective_visible_session": "2030-02-01",
        "policy_ref": "synthetic-policy-v1",
        "cik": "0000000000",
        "accession": "synthetic-accession",
        "filing_record": {"fixture": True, "public_time": "2030-01-31"},
    }
    values.update(changes)
    return HostSourceBinding(**values)


def _wrapped(payload: bytes, *, encoding: str = "utf-8") -> bytes:
    text = (
        "<DOCUMENT>\n"
        "<TYPE>EX-99.1\n"
        "<SEQUENCE>1\n"
        "<FILENAME>synthetic.htm\n"
        "<TEXT>\n" + payload.decode("utf-8") + "\n</TEXT>\n</DOCUMENT>\n"
    )
    if encoding == "utf-16-le":
        return b"\xff\xfe" + text.encode("utf-16-le")
    return text.encode(encoding)


def test_wrapped_html_binds_distinct_raw_and_payload_hashes_and_generic_offsets():
    html_bytes = b"<html><body><h1>Synthetic statement</h1></body></html>"
    raw = _wrapped(html_bytes)
    envelope = extract_sec_envelope(raw)
    assert envelope is not None and envelope["payload_kind"] == "html"
    binding = _binding(raw)

    decision = bind_financial_extraction_source(
        raw,
        binding,
        max_source_bytes=MAX_SOURCE_BYTES,
        source_type="sec_html",
    )
    assert decision.status == "bound"
    assert decision.reasons == ()
    assert decision.publishable is False
    assert isinstance(decision.document, VerifiedDocument)
    document = decision.document
    assert document.fixture_only is False
    assert document.payload == envelope["payload"]
    assert document.raw_sha256 == binding.raw_expected_sha256
    assert document.raw_sha256 != document.payload_sha256
    assert document.payload_sha256 == hashlib.sha256(envelope["payload"]).hexdigest()
    assert document.raw_bytes == len(raw)
    assert document.encoding == "utf-8"
    assert document.origin["start_byte"] == envelope["start_byte"]
    assert document.origin["end_byte"] == envelope["end_byte"]
    assert document.origin["locator_kind"] == "byte_range_in_received_document"
    assert "cik" not in document.origin and "accession" not in document.origin
    assert "metadata" not in document.origin and "public_availability" not in document.origin

    record = decision.caller_verified_record
    assert record.binding is binding
    assert record.actual_raw_bytes == len(raw)
    assert record.actual_raw_sha256 == hashlib.sha256(raw).hexdigest()
    assert record.hash_scope == "selected_document_bytes"
    assert record.envelope_metadata["filename"] == "synthetic.htm"
    assert record.binding.public_availability == date(2030, 1, 31)
    assert record.binding.effective_visible_session == "2030-02-01"
    assert record.binding.policy_ref == "synthetic-policy-v1"
    assert document.source_id != binding.filing_id
    assert document.document_id != binding.document_id


def test_plain_html_requires_declared_source_type_and_explicit_strict_encoding():
    raw = "<html><body>café</body></html>".encode("cp1252")
    binding = _binding(raw)
    with pytest.raises(SourceBindingError, match="explicit plain_html_encoding"):
        bind_financial_extraction_source(
            raw,
            binding,
            max_source_bytes=MAX_SOURCE_BYTES,
            source_type="sec_html",
        )
    with pytest.raises(SourceBindingError, match="decoded strictly"):
        bind_financial_extraction_source(
            raw,
            binding,
            max_source_bytes=MAX_SOURCE_BYTES,
            source_type="sec_html",
            plain_html_encoding="utf-8",
        )

    decision = bind_financial_extraction_source(
        raw,
        binding,
        max_source_bytes=MAX_SOURCE_BYTES,
        source_type="sec_html",
        plain_html_encoding="cp1252",
    )
    assert decision.status == "bound"
    assert decision.document is not None
    assert decision.document.encoding == "cp1252"
    assert "café" in decision.document.payload.decode("cp1252")
    assert decision.document.origin["start_byte"] == 0
    assert decision.document.origin["end_byte"] == len(raw)
    assert decision.document.fixture_only is False


def test_utf16_sec_wrapper_uses_envelope_encoding_and_byte_offsets():
    payload = b"<html><body>UTF wrapper fixture</body></html>"
    raw = _wrapped(payload, encoding="utf-16-le")
    envelope = extract_sec_envelope(raw)
    assert envelope is not None and envelope["encoding"] == "utf-16-le"
    decision = bind_financial_extraction_source(
        raw,
        _binding(raw),
        max_source_bytes=MAX_SOURCE_BYTES,
        source_type="sec_html",
    )
    assert decision.document is not None
    assert decision.document.encoding == "utf-16-le"
    assert decision.document.payload == envelope["payload"]
    assert "UTF wrapper fixture" in decision.document.payload.decode("utf-16-le")
    assert decision.document.origin["start_byte"] == envelope["start_byte"]
    assert decision.document.origin["end_byte"] == envelope["end_byte"]


def test_candidate_excluded_and_numeric_parser_statuses_never_create_documents():
    raw = b"<html><body>Synthetic only</body></html>"
    candidate = bind_financial_extraction_source(
        raw,
        _binding(raw, scope_status="candidate"),
        max_source_bytes=MAX_SOURCE_BYTES,
        source_type="sec_html",
        plain_html_encoding="utf-8",
    )
    assert candidate.status == "quarantined"
    assert candidate.reasons == ("SCOPE_UNRESOLVED",)
    assert candidate.document is None

    excluded = bind_financial_extraction_source(
        raw,
        _binding(raw, scope_status="excluded"),
        max_source_bytes=MAX_SOURCE_BYTES,
        source_type="sec_html",
        plain_html_encoding="utf-8",
    )
    assert excluded.status == "skipped"
    assert excluded.reasons == ("SCOPE_EXCLUDED",)
    assert excluded.document is None

    expected = {
        "full": ("skipped", "NUMERIC_PARSER_FULL"),
        "partial": ("quarantined", "NUMERIC_PARSER_PARTIAL"),
        "failed": ("quarantined", "NUMERIC_PARSER_FAILED"),
        "not_requested": ("skipped", "NUMERIC_PARSER_NOT_REQUESTED"),
    }
    for parser_status, (status, reason) in expected.items():
        decision = bind_financial_extraction_source(
            raw,
            _binding(raw, numeric_parser_status=parser_status),
            max_source_bytes=MAX_SOURCE_BYTES,
            source_type="sec_html",
            plain_html_encoding="utf-8",
        )
        assert (decision.status, decision.reasons, decision.document) == (status, (reason,), None)


def test_malformed_wrapper_fails_closed_and_legacy_or_pdf_payloads_are_unsupported():
    malformed = b"<DOCUMENT>\n<TYPE>EX-99.1\n<TEXT>\n<html>incomplete\n</TEXT>\n"
    with pytest.raises(SourceBindingError, match="SEC envelope failed closed"):
        bind_financial_extraction_source(
            malformed,
            _binding(malformed),
            max_source_bytes=MAX_SOURCE_BYTES,
            source_type="sec_html",
            plain_html_encoding="utf-8",
        )

    legacy = _wrapped(b"<PAGE>\n<TABLE>\n<S>legacy rows\n</TABLE>\n")
    legacy_decision = bind_financial_extraction_source(
        legacy,
        _binding(legacy),
        max_source_bytes=MAX_SOURCE_BYTES,
        source_type="sec_html",
    )
    assert legacy_decision.status == "unsupported"
    assert legacy_decision.reasons == ("UNSUPPORTED_PAYLOAD_KIND",)
    assert legacy_decision.document is None
    assert legacy_decision.caller_verified_record.payload_kind == "legacy_text"

    pdf = _wrapped(b"%PDF-1.7\nsynthetic binary placeholder\n")
    pdf_decision = bind_financial_extraction_source(
        pdf,
        _binding(pdf),
        max_source_bytes=MAX_SOURCE_BYTES,
        source_type="sec_html",
    )
    assert pdf_decision.status == "unsupported"
    assert pdf_decision.document is None


def test_hash_size_and_limit_are_rechecked_only_for_received_document_bytes():
    raw = b"<html><body>Fixture</body></html>"
    with pytest.raises(SourceBindingError, match="byte count differs"):
        bind_financial_extraction_source(
            raw,
            _binding(raw, raw_expected_bytes=len(raw) + 1),
            max_source_bytes=MAX_SOURCE_BYTES,
            source_type="sec_html",
            plain_html_encoding="utf-8",
        )
    with pytest.raises(SourceBindingError, match="hash differs"):
        bind_financial_extraction_source(
            raw,
            _binding(raw, raw_expected_sha256="0" * 64),
            max_source_bytes=MAX_SOURCE_BYTES,
            source_type="sec_html",
            plain_html_encoding="utf-8",
        )
    with pytest.raises(SourceBindingError, match="max_source_bytes"):
        bind_financial_extraction_source(
            raw,
            _binding(raw),
            max_source_bytes=8,
            source_type="sec_html",
            plain_html_encoding="utf-8",
        )
    with pytest.raises(ValueError, match="positive integer"):
        bind_financial_extraction_source(
            raw,
            _binding(raw),
            max_source_bytes=0,
            source_type="sec_html",
            plain_html_encoding="utf-8",
        )


def test_fixture_flag_is_explicit_and_opaque_ids_are_stable_by_snapshot_binding():
    raw = b"<html><body>Synthetic fixture only</body></html>"
    binding = _binding(raw)
    default = bind_financial_extraction_source(
        raw,
        binding,
        max_source_bytes=MAX_SOURCE_BYTES,
        source_type="sec_html",
        plain_html_encoding="utf-8",
    )
    same = bind_financial_extraction_source(
        raw,
        binding,
        max_source_bytes=MAX_SOURCE_BYTES,
        source_type="sec_html",
        plain_html_encoding="utf-8",
    )
    assert default.document is not None and same.document is not None
    assert default.document.fixture_only is False
    assert default.document.source_id == same.document.source_id
    assert default.document.document_id == same.document.document_id
    changed_snapshot = bind_financial_extraction_source(
        raw,
        _binding(raw, snapshot_id="another-snapshot"),
        max_source_bytes=MAX_SOURCE_BYTES,
        source_type="sec_html",
        plain_html_encoding="utf-8",
    )
    assert changed_snapshot.document is not None
    assert changed_snapshot.document.source_id != default.document.source_id
    assert changed_snapshot.document.document_id != default.document.document_id

    synthetic_type = bind_financial_extraction_source(
        raw,
        binding,
        max_source_bytes=MAX_SOURCE_BYTES,
        source_type="synthetic_sec_fixture",
        plain_html_encoding="utf-8",
    )
    declared_fixture = bind_financial_extraction_source(
        raw,
        binding,
        max_source_bytes=MAX_SOURCE_BYTES,
        source_type="sec_html",
        plain_html_encoding="utf-8",
        fixture_host_declaration="synthetic in-memory test input",
    )
    assert synthetic_type.document is not None and synthetic_type.document.fixture_only is True
    assert declared_fixture.document is not None and declared_fixture.document.fixture_only is True


def test_import_is_independent_of_optional_html_ai_and_provider_libraries():
    script = f"""
import builtins, sys
sys.path.insert(0, {str(SOURCE_ROOT)!r})
forbidden = {{"lxml", "arelle", "openai", "anthropic"}}
original = builtins.__import__
def guard(name, *args, **kwargs):
    if name.split('.')[0] in forbidden:
        raise ModuleNotFoundError('blocked optional dependency: ' + name)
    return original(name, *args, **kwargs)
builtins.__import__ = guard
import financial_extraction.domain
import filings.adapters
import filings.adapters.financial_extraction
assert callable(filings.adapters.bind_financial_extraction_source)
assert not any(name in sys.modules for name in forbidden)
"""
    subprocess.run([sys.executable, "-c", script], check=True, capture_output=True, text=True)
