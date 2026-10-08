"""Parser-neutral helpers for identifying and validating filing source bytes."""

from .sec_envelope import (
    extract_sec_envelope,
    is_complete_sec_pdf_envelope,
    sec_envelope_encoding,
)

__all__ = [
    "extract_sec_envelope",
    "is_complete_sec_pdf_envelope",
    "sec_envelope_encoding",
]
