from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from filings.parse_text import PARSER_VERSION, _extract_sec_envelope, parse_text

FIXTURES = Path(__file__).parent / "fixtures" / "filings_parser" / "legacy_sgml_text"
FILING_ID = "0000016868:0000950103-02-000522"


def _parse(source: Path, *, expected_hash: str | None = None):
    return parse_text(
        source,
        filing_id=FILING_ID,
        document_id=source.name,
        parse_id="legacy-sgml-text-test",
        allowed_root=source.parent,
        expected_hash=expected_hash,
    )


def _fulltext(result):
    return next(row for row in result.sections if row["section_kind"] == "fulltext")


def test_complete_40f_amendment_envelope_preserves_plain_sgml_and_original_byte_span() -> None:
    source = FIXTURES / "complete-40f-a.txt"
    original = source.read_bytes()
    expected_hash = hashlib.sha256(original).hexdigest()
    envelope = _extract_sec_envelope(original)

    assert envelope is not None
    assert envelope["payload_kind"] == "legacy_text"
    assert envelope["metadata"]["type"] == "40-F/A"
    payload = original[envelope["start_byte"] : envelope["end_byte"]]
    assert payload == envelope["payload"]

    result = _parse(source, expected_hash=expected_hash)

    assert PARSER_VERSION == "1.0.3"
    assert result.status == "full", result.errors
    assert result.errors == []
    assert result.source_hash == expected_hash
    assert len(result.sections) == 1
    row = _fulltext(result)
    assert row["status"] == "full"
    assert row["content_text"] == payload.decode("utf-8").strip()
    assert row["source_hash"] == expected_hash
    assert row["document_hash"] == expected_hash
    assert row["source_relpath"] == source.name
    assert row["source_xpath"] is None
    assert row["source_ordinal"] == 0
    assert row["heading"] is None
    assert row["heading_level"] is None
    assert row["confidence"] == "document"
    assert row["raw_encoding"] == "utf-8"

    content = row["content_text"]
    for retained in (
        "FORM 40-F/A",
        "<PAGE>",
        "<TABLE>",
        "<S>Registrant<C>Incorporation<C>Financial statements",
        "INDEPENDENT AUDITORS' REPORT",
        "See note 7",
        "SIGNATURES",
    ):
        assert retained in content
    assert content.index("FORM 40-F/A") < content.index("<PAGE>")
    assert content.index("<PAGE>") < content.index("<TABLE>")
    assert content.index("<TABLE>") < content.index("INDEPENDENT AUDITORS' REPORT")

    provenance = json.loads(row["provenance"].split("SEC SGML envelope provenance=", 1)[1])
    assert provenance["text_payload_kind"] == "legacy_text"
    assert provenance["source_xpath_scope"] is None
    assert "markers are retained" in provenance["legacy_text_representation"]
    assert provenance["metadata"]["filename"] == "may3002_40fa.txt"
    start = provenance["text_payload_start_byte"]
    end = provenance["text_payload_end_byte"]
    assert original[start:end] == payload
    assert source.read_bytes() == original


def test_legacy_payload_byte_spans_and_encoding_are_original_source_relative(
    tmp_path: Path,
) -> None:
    payload_text = "Résumé financial presentation\n<PAGE>\n<TABLE>\n<S>Value<C>One\n</TABLE>\n"
    encodings = (
        ("utf-8", "utf-8"),
        ("utf-16", "utf-16-le"),
        ("utf-32", "utf-32-le"),
    )
    for encoding, payload_codec in encodings:
        wrapper_text = (
            "<DOCUMENT>\n<TYPE>40-F/A\n<SEQUENCE>1\n<FILENAME>legacy.txt\n"
            "<TEXT>\n" + payload_text + "</TEXT>\n</DOCUMENT>\n"
        )
        source = tmp_path / f"legacy-{encoding}.txt"
        original = wrapper_text.encode(encoding)
        source.write_bytes(original)

        envelope = _extract_sec_envelope(original)
        result = _parse(source, expected_hash=hashlib.sha256(original).hexdigest())

        assert envelope is not None
        assert envelope["payload_kind"] == "legacy_text"
        assert original[envelope["start_byte"] : envelope["end_byte"]] == payload_text.encode(
            payload_codec
        )
        assert result.status == "full", (encoding, result.errors)
        assert len(result.sections) == 1
        row = _fulltext(result)
        assert row["content_text"] == payload_text.strip()
        assert row["source_xpath"] is None
        assert row["raw_encoding"] == envelope["encoding"]
        assert row["source_hash"] == hashlib.sha256(original).hexdigest()
        assert source.read_bytes() == original


def test_complete_pdf_inside_sec_text_is_unsupported_not_plain_text(tmp_path: Path) -> None:
    pdf_payload = b"%PDF-1.7\n% binary PDF payload \x00\xff\x10\n"
    original = (
        b"<DOCUMENT>\n<TYPE>EX-1\n<SEQUENCE>1\n<FILENAME>attachment.pdf\n<TEXT>\n"
        + pdf_payload
        + b"\n</TEXT>\n</DOCUMENT>\n"
    )
    source = tmp_path / "pdf-envelope.txt"
    source.write_bytes(original)

    result = _parse(source, expected_hash=hashlib.sha256(original).hexdigest())

    assert result.status == "unsupported"
    assert result.source_hash == hashlib.sha256(original).hexdigest()
    assert result.sections == []
    assert result.errors[0]["code"] == "unsupported_pdf"

    bom_source = tmp_path / "pdf-envelope-utf8-bom.txt"
    bom_source.write_bytes(b"\xef\xbb\xbf" + original)
    bom_result = _parse(bom_source)
    assert bom_result.status == "unsupported"
    assert bom_result.source_hash == hashlib.sha256(bom_source.read_bytes()).hexdigest()
    assert bom_result.sections == []
    assert bom_result.errors[0]["code"] == "unsupported_pdf"


def test_binary_or_hash_mismatched_payload_never_returns_a_full_section(tmp_path: Path) -> None:
    binary = tmp_path / "binary-envelope.txt"
    binary.write_bytes(
        b"<DOCUMENT>\n<TYPE>40-F/A\n<TEXT>\nnot plain text\x00binary\n</TEXT>\n</DOCUMENT>\n"
    )
    result = _parse(binary)
    assert result.status == "unsupported"
    assert result.sections == []
    assert result.errors[0]["code"] == "unsupported_binary"

    source = FIXTURES / "complete-40f-a.txt"
    mismatch = _parse(source, expected_hash="0" * 64)
    assert mismatch.status == "failed"
    assert mismatch.source_hash == hashlib.sha256(source.read_bytes()).hexdigest()
    assert mismatch.sections == []
    assert mismatch.errors[0]["code"] == "source_hash_mismatch"


def test_pdf_with_invalid_envelope_footer_is_not_claimed_unsupported_complete_or_full(
    tmp_path: Path,
) -> None:
    source = tmp_path / "trailing-content-pdf.txt"
    source.write_bytes(b"<DOCUMENT>\n<TYPE>EX-1\n<TEXT>\n%PDF-1.7\n</TEXT>\n</DOCUMENT>\nextra\n")

    result = _parse(source)

    assert result.status == "failed"
    assert result.sections == []
    assert result.errors[0]["code"] == "unsupported_envelope"

    interior = tmp_path / "interior-content-pdf.txt"
    interior.write_bytes(
        b"<DOCUMENT>\n<TYPE>EX-1\n<TEXT>\n%PDF-1.7\n</TEXT>\nunexpected\n</DOCUMENT>\n"
    )
    interior_result = _parse(interior)
    assert interior_result.status == "failed"
    assert interior_result.sections == []
    assert interior_result.errors[0]["code"] == "unsupported_envelope"
