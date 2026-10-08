from __future__ import annotations

import hashlib
import socket
import sys
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from filings.parse_xbrl import parse_xbrl

FILING_ID = "0000016868:0000950103-02-000522"
LEGACY_TEXT = (
    "FORM 40-F/A\n"
    "<PAGE>\n"
    "<TABLE>\n"
    "<S>Registrant<C>Incorporation<C>Financial statements\n"
    "INDEPENDENT AUDITORS' REPORT\n"
    "</TABLE>\n"
    "SIGNATURES\n"
)


def _envelope(payload: str, encoding: str = "utf-8") -> bytes:
    text = (
        "<DOCUMENT>\n<TYPE>40-F/A\n<SEQUENCE>1\n<FILENAME>cnr.txt\n"
        "<TEXT>\n"
        f"{payload}"
        "</TEXT>\n</DOCUMENT>\n"
    )
    return text.encode(encoding)


def _parse(source: Path, *, root: Path | None = None, expected_hash: str | None = None):
    allowed_root = root or source.parent
    expected_hashes = {source: expected_hash} if expected_hash is not None else None
    return parse_xbrl(
        source,
        filing_id=FILING_ID,
        document_id=source.name,
        parse_id="legacy-non-xbrl-final-format-test",
        allowed_root=allowed_root,
        expected_hashes=expected_hashes,
    )


def test_complete_verified_sec_legacy_text_is_unsupported_not_xml_damage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "may3002_40fa.txt"
    raw = _envelope(LEGACY_TEXT)
    source.write_bytes(raw)
    expected_hash = hashlib.sha256(raw).hexdigest()
    network_attempts: list[str] = []

    def deny(*args, **kwargs):
        network_attempts.append("network")
        raise AssertionError("format classification must not use the network")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(urllib.request, "urlopen", deny)

    result = _parse(source, expected_hash=expected_hash)

    assert result.status == "unsupported"
    assert result.source_hash == expected_hash
    assert result.facts == []
    assert result.errors[0]["code"] == "unsupported_non_xbrl"
    assert "legacy-text payload" in result.errors[0]["message"]
    assert source.read_bytes() == raw
    assert network_attempts == []


def test_sec_legacy_payload_classification_respects_supported_source_encodings(
    tmp_path: Path,
) -> None:
    for encoding in ("utf-8", "utf-16", "utf-32"):
        source = tmp_path / f"legacy-{encoding}.txt"
        raw = _envelope("Résumé\n" + LEGACY_TEXT, encoding)
        source.write_bytes(raw)
        result = _parse(source, expected_hash=hashlib.sha256(raw).hexdigest())
        assert result.status == "unsupported", (encoding, result.errors)
        assert result.source_hash == hashlib.sha256(raw).hexdigest()
        assert result.facts == []
        assert result.errors[0]["code"] == "unsupported_non_xbrl"
        assert source.read_bytes() == raw


def test_incomplete_or_multiple_document_envelopes_are_never_unsupported_non_xbrl(
    tmp_path: Path,
) -> None:
    truncated = tmp_path / "truncated.txt"
    truncated.write_bytes(b"<DOCUMENT>\n<TYPE>40-F/A\n<TEXT>\n<PAGE>\n<TABLE>\n<S>Body<C>Value\n")
    result = _parse(truncated, expected_hash=hashlib.sha256(truncated.read_bytes()).hexdigest())
    assert result.status == "failed"
    assert result.facts == []
    assert result.errors[0]["code"] != "unsupported_non_xbrl"

    multiple = tmp_path / "multiple.txt"
    multiple.write_bytes(_envelope(LEGACY_TEXT) + _envelope("Second submitted document\n"))
    result = _parse(multiple, expected_hash=hashlib.sha256(multiple.read_bytes()).hexdigest())
    assert result.status == "failed"
    assert result.facts == []
    assert result.errors[0]["code"] != "unsupported_non_xbrl"


def test_pdf_doctype_entities_and_wrapped_xbrl_are_not_plain_legacy_text(
    tmp_path: Path,
) -> None:
    pdf = tmp_path / "attachment.txt"
    pdf.write_bytes(
        b"<DOCUMENT>\n<TYPE>EX-1\n<SEQUENCE>1\n<FILENAME>attachment.pdf\n<TEXT>\n"
        b"%PDF-1.7\n% cached PDF payload\n</TEXT>\n</DOCUMENT>\n"
    )
    pdf_result = _parse(pdf, expected_hash=hashlib.sha256(pdf.read_bytes()).hexdigest())
    assert pdf_result.status == "failed"
    assert pdf_result.facts == []
    assert pdf_result.errors[0]["code"] != "unsupported_non_xbrl"

    doctype = tmp_path / "doctype.txt"
    doctype.write_bytes(_envelope('<!DOCTYPE html [<!ENTITY blocked "no">]>\n' + LEGACY_TEXT))
    doctype_result = _parse(doctype, expected_hash=hashlib.sha256(doctype.read_bytes()).hexdigest())
    assert doctype_result.status == "failed"
    assert doctype_result.facts == []
    assert doctype_result.errors[0]["code"] != "unsupported_non_xbrl"

    wrapped_xbrl = tmp_path / "wrapped-inline.txt"
    wrapped_xbrl.write_bytes(
        _envelope(
            '<html xmlns="http://www.w3.org/1999/xhtml" '
            'xmlns:ix="http://www.xbrl.org/2013/inlineXBRL">'
            "<body><ix:nonNumeric name='ex:Text' contextRef='c'>Fact</ix:nonNumeric></body></html>"
        )
    )
    wrapped_result = _parse(
        wrapped_xbrl, expected_hash=hashlib.sha256(wrapped_xbrl.read_bytes()).hexdigest()
    )
    assert wrapped_result.status == "failed"
    assert wrapped_result.facts == []
    assert wrapped_result.errors[0]["code"] != "unsupported_non_xbrl"


def test_bad_expected_hash_and_unsafe_source_paths_cannot_be_masked(
    tmp_path: Path,
) -> None:
    root = tmp_path / "allowed"
    root.mkdir()
    source = root / "legacy.txt"
    raw = _envelope(LEGACY_TEXT)
    source.write_bytes(raw)

    mismatch = _parse(source, expected_hash="0" * 64)
    assert mismatch.status == "failed"
    assert mismatch.source_hash == hashlib.sha256(raw).hexdigest()
    assert mismatch.facts == []
    assert mismatch.errors[0]["code"] == "source_integrity_failed"

    outside = tmp_path / "outside.txt"
    outside.write_bytes(raw)
    escaped = _parse(outside, root=root, expected_hash=hashlib.sha256(raw).hexdigest())
    assert escaped.status == "failed"
    assert escaped.facts == []
    assert escaped.errors[0]["code"] != "unsupported_non_xbrl"

    symlink = root / "symlink.txt"
    symlink.symlink_to(source)
    rejected = _parse(symlink, root=root, expected_hash=hashlib.sha256(raw).hexdigest())
    assert rejected.status == "failed"
    assert rejected.facts == []
    assert rejected.errors[0]["code"] != "unsupported_non_xbrl"


def test_classic_xbrl_entrypoint_still_parses_normally() -> None:
    fixture_root = Path(__file__).parent / "fixtures" / "filings_parser" / "xbrl"
    source = fixture_root / "instance.xml"
    raw = source.read_bytes()
    result = parse_xbrl(
        source,
        filing_id=FILING_ID,
        document_id=source.name,
        parse_id="legacy-non-xbrl-valid-xbrl-regression",
        allowed_root=fixture_root,
        expected_hashes={source: hashlib.sha256(raw).hexdigest()},
    )
    assert result.status == "full"
    assert len(result.facts) == 11
    assert result.source_hash == hashlib.sha256(raw).hexdigest()
