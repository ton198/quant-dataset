from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from filings.parse_text import (
    _extract_sec_envelope,
    _is_complete_sec_pdf_envelope,
    _sec_envelope_encoding,
)
from filings.source import (
    extract_sec_envelope,
    is_complete_sec_pdf_envelope,
    sec_envelope_encoding,
)


def test_source_envelope_public_api_preserves_legacy_return_shape() -> None:
    payload = b"<html><body>source payload</body></html>\n"
    raw = (
        b"<DOCUMENT>\n<TYPE>10-K\n<SEQUENCE>1\n<FILENAME>report.htm\n"
        b"<TEXT>\n" + payload + b"</TEXT>\n</DOCUMENT>\n"
    )

    assert sec_envelope_encoding(raw) == ("utf-8", 0)
    envelope = extract_sec_envelope(raw)

    assert envelope == {
        "payload": payload,
        "payload_kind": "html",
        "encoding": "utf-8",
        "used_fallback": False,
        "metadata": {"type": "10-K", "sequence": "1", "filename": "report.htm"},
        "start_byte": raw.index(payload),
        "end_byte": raw.index(payload) + len(payload),
    }
    assert raw[envelope["start_byte"] : envelope["end_byte"]] == envelope["payload"]
    assert extract_sec_envelope(b"<html></html>") is None


def test_parse_text_retains_private_sec_envelope_import_aliases() -> None:
    assert _extract_sec_envelope is extract_sec_envelope
    assert _is_complete_sec_pdf_envelope is is_complete_sec_pdf_envelope
    assert _sec_envelope_encoding is sec_envelope_encoding


def test_public_pdf_envelope_helper_requires_complete_single_wrapper() -> None:
    complete = (
        b"<DOCUMENT>\n<TYPE>EX-1\n<SEQUENCE>1\n<FILENAME>report.pdf\n"
        b"<TEXT>\n%PDF-1.7\n\x00\xff\n</TEXT>\n</DOCUMENT>\n"
    )
    incomplete = complete.replace(b"</DOCUMENT>\n", b"")

    assert is_complete_sec_pdf_envelope(complete)
    assert not is_complete_sec_pdf_envelope(incomplete)
