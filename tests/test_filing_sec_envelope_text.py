from __future__ import annotations

import hashlib
import json
import socket
import sys
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from filings.parse_text import PARSER_VERSION, _extract_sec_envelope, parse_text
from filings.parsing_models import SECTION_SCHEMA

FIXTURES = Path(__file__).parent / "fixtures" / "filings_parser" / "sec_envelopes"
FILING_ID = "0000123456:000000000000000001"


def _parse(source: Path, *, expected_hash: str | None = None):
    return parse_text(
        source,
        filing_id=FILING_ID,
        document_id=source.name,
        parse_id="sec-envelope-test",
        allowed_root=source.parent,
        expected_hash=expected_hash,
    )


def _fulltext(result):
    return next(row for row in result.sections if row["section_kind"] == "fulltext")


def test_single_document_envelope_matches_payload_and_records_original_byte_span() -> None:
    wrapper = FIXTURES / "single-document-wrapper.txt"
    payload_path = FIXTURES / "single-document-payload.html"
    original = wrapper.read_bytes()
    expected_hash = hashlib.sha256(original).hexdigest()

    result = _parse(wrapper, expected_hash=expected_hash)
    payload_result = _parse(payload_path)

    assert result.status == "full"
    assert result.errors == []
    assert result.parser_version == PARSER_VERSION == "1.0.3"
    assert result.source_hash == expected_hash
    assert result.sections
    assert {row["status"] for row in result.sections} == {"full"}
    assert _fulltext(result)["document_hash"] == expected_hash
    assert _fulltext(result)["source_relpath"] == wrapper.name
    assert _fulltext(result)["content_text"] == _fulltext(payload_result)["content_text"]
    assert all(row["source_hash"] == expected_hash for row in result.sections)

    content = _fulltext(result)["content_text"]
    for retained in (
        "Financial Highlights",
        "Operating revenue for the year was $1,234 million.",
        "Revenue $1,234 million",
        "Footnote retained after table",
        "€12 million from München",
        "Visible tail after hidden metadata.",
    ):
        assert retained in content
    for excluded in (
        "AMENDMENT NO. 1 TO FORM 10-K",
        "annual-amendment.htm",
        "MachineOnlyScriptSentinel",
        "MachineOnlyHiddenSentinel",
        "Protocol Header Must Not Leak",
    ):
        assert excluded not in content

    heading = next(row for row in result.sections if row["section_kind"] == "heading")
    assert heading["heading"] == "Financial Highlights"
    assert (
        heading["content_text"]
        == next(row for row in payload_result.sections if row["section_kind"] == "heading")[
            "content_text"
        ]
    )
    assert "TEXT payload DOM" in heading["provenance"]
    provenance = json.loads(
        _fulltext(result)["provenance"].split("SEC SGML envelope provenance=", 1)[1]
    )
    assert provenance["format"] == "sec_sgml_single_document"
    assert provenance["metadata"] == {
        "description": "AMENDMENT NO. 1 TO FORM 10-K",
        "filename": "annual-amendment.htm",
        "sequence": "1",
        "type": "10-K/A",
    }
    start = provenance["text_payload_start_byte"]
    end = provenance["text_payload_end_byte"]
    assert original[start:end] == payload_path.read_bytes()

    from lxml import etree

    payload_parser = etree.HTMLParser(
        encoding="utf-8", recover=True, no_network=True, remove_comments=True
    )
    payload_root = etree.fromstring(payload_path.read_bytes(), payload_parser)
    payload_tree = payload_root.getroottree()
    fulltext_root = payload_tree.xpath(_fulltext(result)["source_xpath"])
    assert fulltext_root == [payload_root]
    assert payload_tree.getpath(fulltext_root[0]) == _fulltext(result)["source_xpath"]
    matched_heading = payload_tree.xpath(heading["source_xpath"])
    assert len(matched_heading) == 1
    assert str(matched_heading[0].tag).casefold() == "h1"
    assert payload_tree.getpath(matched_heading[0]) == heading["source_xpath"]
    assert result.to_arrow_tables()[1].schema == SECTION_SCHEMA
    assert wrapper.read_bytes() == original


def test_utf16_and_utf32_envelope_offsets_use_original_bytes(tmp_path: Path) -> None:
    html = "<html><body><h1>Résumé</h1><p>€12</p></body></html>\n"
    for encoding, codec in (("utf-16", "utf-16-le"), ("utf-32", "utf-32-le")):
        wrapper_text = (
            "<DOCUMENT>\n<TYPE>10-K\n<SEQUENCE>1\n<FILENAME>report.htm\n"
            "<TEXT>\n" + html + "</TEXT>\n</DOCUMENT>\n"
        )
        source = tmp_path / f"wrapped-{encoding}.txt"
        original = wrapper_text.encode(encoding)
        source.write_bytes(original)

        result = _parse(source)
        assert result.status == "full", result.errors
        assert "Résumé" in _fulltext(result)["content_text"]
        assert "€12" in _fulltext(result)["content_text"]
        provenance = json.loads(
            _fulltext(result)["provenance"].split("SEC SGML envelope provenance=", 1)[1]
        )
        assert original[
            provenance["text_payload_start_byte"] : provenance["text_payload_end_byte"]
        ] == html.encode(codec)
        assert result.source_hash == hashlib.sha256(original).hexdigest()
        assert source.read_bytes() == original


def test_invalid_or_ambiguous_envelopes_fail_closed(tmp_path: Path) -> None:
    good_payload = (FIXTURES / "single-document-payload.html").read_text(encoding="utf-8")
    cases = {
        "missing-text": "<DOCUMENT>\n<TYPE>10-K\n</DOCUMENT>\n",
        "missing-text-end": ("<DOCUMENT>\n<TYPE>10-K\n<TEXT>\n" + good_payload + "</DOCUMENT>\n"),
        "multiple-documents": (
            "<DOCUMENT>\n<TYPE>10-K\n<TEXT>\n"
            + good_payload
            + "</TEXT>\n</DOCUMENT>\n<DOCUMENT>\n<TYPE>EX-1\n<TEXT>\n"
            + good_payload
            + "</TEXT>\n</DOCUMENT>\n"
        ),
        "unknown-after-text": (
            "<DOCUMENT>\n<TYPE>10-K\n<TEXT>\n"
            + good_payload
            + "</TEXT>\n<UNEXPECTED>extra</UNEXPECTED>\n</DOCUMENT>\n"
        ),
        "unknown-header": (
            "<DOCUMENT>\n<TYPE>10-K\n<PRIVATE-HEADER>not approved\n<TEXT>\n"
            + good_payload
            + "</TEXT>\n</DOCUMENT>\n"
        ),
    }
    for name, text in cases.items():
        source = tmp_path / f"{name}.htm"
        source.write_text(text, encoding="utf-8")
        result = _parse(source)
        assert result.status == "failed", name
        assert result.sections == [], name
        assert result.errors[0]["code"] == "unsupported_envelope", name


def test_truncated_utf8_in_payload_is_retained_as_partial_with_diagnostic(tmp_path: Path) -> None:
    payload = (FIXTURES / "single-document-payload.html").read_bytes()
    wrapper = (
        b"<DOCUMENT>\n<TYPE>10-K\n<SEQUENCE>1\n<FILENAME>bad.htm\n<TEXT>\n"
        + payload[:-1]
        + b"\xc3\n</TEXT>\n</DOCUMENT>\n"
    )
    source = tmp_path / "truncated-utf8.htm"
    source.write_bytes(wrapper)

    result = _parse(source)
    assert result.status == "partial"
    assert _fulltext(result)["status"] == "partial"
    assert any(error["code"] == "encoding_fallback" for error in result.errors)
    assert result.source_hash == hashlib.sha256(wrapper).hexdigest()
    assert source.read_bytes() == wrapper


def test_html_recovery_warnings_inside_a_valid_envelope_remain_partial(tmp_path: Path) -> None:
    source = tmp_path / "malformed-body.htm"
    source.write_text(
        "<DOCUMENT>\n<TYPE>10-K\n<SEQUENCE>1\n<FILENAME>body.htm\n<TEXT>\n"
        "<html><body><p><div>broken</p></div></body></html>\n"
        "</TEXT>\n</DOCUMENT>\n",
        encoding="utf-8",
    )

    result = _parse(source)
    assert result.status == "partial"
    assert _fulltext(result)["status"] == "partial"
    assert any(error["code"] == "html_recovery" for error in result.errors)


def test_envelope_html_parse_never_attempts_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts: list[str] = []

    def deny(*args, **kwargs):
        attempts.append("network")
        raise AssertionError("SEC envelope HTML parsing must not load external content")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(urllib.request, "urlopen", deny)
    html = (
        '<!DOCTYPE html SYSTEM "https://example.invalid/external.dtd">\n'
        "<html><body><h1>Local only</h1><p>Visible text</p></body></html>\n"
    )
    source = tmp_path / "external-doctype.txt"
    source.write_text(
        "<DOCUMENT>\n<TYPE>10-K\n<SEQUENCE>1\n<FILENAME>local.htm\n<TEXT>\n"
        + html
        + "</TEXT>\n</DOCUMENT>\n",
        encoding="utf-8",
    )

    result = _parse(source)
    assert result.status == "full", result.errors
    assert "Visible text" in _fulltext(result)["content_text"]
    assert attempts == []


def test_envelope_extractor_preserves_original_hash_and_rejects_truncated_utf16() -> None:
    raw = (FIXTURES / "single-document-wrapper.txt").read_bytes()
    envelope = _extract_sec_envelope(raw)
    assert envelope is not None
    assert raw[envelope["start_byte"] : envelope["end_byte"]].startswith(b"<!DOCTYPE html>")

    broken = "<DOCUMENT>\n<TYPE>10-K\n<TEXT>\n<html></html>\n</TEXT>\n</DOCUMENT>\n".encode(
        "utf-16"
    )[:-1]
    with pytest.raises(ValueError, match="cannot be decoded safely"):
        _extract_sec_envelope(broken)
