"""Window-local short evidence references with checksum-guarded exact mapping.

Model-facing windows use fixed-width short IDs (``r`` + six uppercase hex index
digits + two base-17 check characters) instead of full-length evidence IDs. The
first check character is the sum of the six hex digit values modulo 17; the
second is the position-weighted (positions 1..6) sum modulo 17 over the alphabet
``0-9A-G``. Any single-character substitution changes at least one check (a body
digit changes both sums by a non-zero amount below 17; a check character no
longer matches the recomputed value), and swapping two adjacent different index
digits changes the weighted sum by their non-zero difference modulo 17. Only an
exact current-window short ID decodes; anything else becomes an uncollidable
sentinel so validation keeps that record unresolved without failing the window.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import replace

from ..domain.contracts import ExtractedRecord, ExtractionWindow, Quote, WindowItem

CHECK_ALPHABET = "0123456789ABCDEFG"
_SHORT_PATTERN = re.compile(r"^r[0-9A-F]{6}[0-9A-G]{2}$")
_MAX_INDEX = 0xFFFFFF

#: Quote ref substituted when a model ref is not an exact current-window short
#: ID. NUL bytes keep it uncollidable with real evidence refs; validation then
#: reports SOURCE_OUTSIDE_WINDOW for that quote's record only.
UNRESOLVED_REF_SENTINEL = "\x00unresolved-source-ref\x00"


def encode_short_ref(index: int) -> str:
    """Encode a 1-based evidence position as a fixed-width checksummed short ID."""
    if not isinstance(index, int) or isinstance(index, bool) or not 1 <= index <= _MAX_INDEX:
        raise ValueError("short-ref index must be an integer in 1..0xFFFFFF")
    body = format(index, "06X")
    digits = [int(char, 16) for char in body]
    first = sum(digits) % 17
    second = sum((position + 1) * digit for position, digit in enumerate(digits)) % 17
    return f"r{body}{CHECK_ALPHABET[first]}{CHECK_ALPHABET[second]}"


def is_wellformed_short_ref(value: object) -> bool:
    """Recompute both check characters; never accept prefixes or near-misses."""
    if not isinstance(value, str) or _SHORT_PATTERN.match(value) is None:
        return False
    index = int(value[1:7], 16)
    if index == 0:
        return False
    return value == encode_short_ref(index)


def _ordered_long_refs(window: ExtractionWindow) -> list[str]:
    if not isinstance(window, ExtractionWindow):
        raise TypeError("window must be an ExtractionWindow")
    ordered: list[str] = []
    seen: set[str] = set()
    for item in (*window.core, *window.context):
        if item.ref not in seen:
            seen.add(item.ref)
            ordered.append(item.ref)
    return ordered


def build_reference_map(window: ExtractionWindow) -> dict[str, str]:
    """Map every deduplicated core/context long ref to a stable short ID.

    Duplicates share one ID; order follows first occurrence across core then
    context, so the mapping is deterministic for a given window.
    """
    mapping = {
        encode_short_ref(position): long_ref
        for position, long_ref in enumerate(_ordered_long_refs(window), start=1)
    }
    if UNRESOLVED_REF_SENTINEL in mapping or UNRESOLVED_REF_SENTINEL in mapping.values():
        raise ValueError("evidence ref collides with the unresolved-ref sentinel")
    return mapping


def render_short_window(window: ExtractionWindow, mapping: Mapping[str, str]) -> str:
    """Render the model-facing window carrying only short IDs, never long IDs."""
    expected = build_reference_map(window)
    if not isinstance(mapping, Mapping) or dict(mapping) != expected:
        raise ValueError("reference map does not match this window")
    shorts = {long_ref: short for short, long_ref in expected.items()}

    def entry(item: WindowItem) -> dict[str, object]:
        return {
            "ref": shorts[item.ref],
            "kind": item.kind,
            "text": item.text,
            "start": item.source.canonical_start,
            "end": item.source.canonical_end,
        }

    return json.dumps(
        {
            "window_id": window.window_id,
            "document_id": window.document_id,
            "core": [entry(item) for item in window.core],
            "context": [entry(item) for item in window.context],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def restore_long_refs(
    records: tuple[ExtractedRecord, ...], mapping: Mapping[str, str]
) -> tuple[ExtractedRecord, ...]:
    """Restore exact original refs for every quote field.

    Only exact current-window short IDs decode. Unknown shorts, original long
    IDs, prefixes and typos become the sentinel, which validation reports as
    outside the window for that record alone.
    """
    if not isinstance(mapping, Mapping):
        raise TypeError("mapping must be a reference map")

    def fix(ref: str) -> str:
        long_ref = mapping.get(ref)
        if not isinstance(long_ref, str) or long_ref == UNRESOLVED_REF_SENTINEL:
            return UNRESOLVED_REF_SENTINEL
        return long_ref

    def fix_quote(quote: Quote) -> Quote:
        return replace(quote, ref=fix(quote.ref))

    restored: list[ExtractedRecord] = []
    for record in records:
        restored.append(
            replace(
                record,
                value_source=fix_quote(record.value_source),
                label_source=fix_quote(record.label_source),
                unit_sources=tuple(fix_quote(quote) for quote in record.unit_sources),
                currency_sources=tuple(fix_quote(quote) for quote in record.currency_sources),
                scale_sources=tuple(fix_quote(quote) for quote in record.scale_sources),
                period=replace(
                    record.period,
                    sources=tuple(fix_quote(quote) for quote in record.period.sources),
                ),
                dimensions=tuple(
                    replace(
                        dimension,
                        sources=tuple(fix_quote(quote) for quote in dimension.sources),
                    )
                    for dimension in record.dimensions
                ),
            )
        )
    return tuple(restored)


__all__ = [
    "CHECK_ALPHABET",
    "UNRESOLVED_REF_SENTINEL",
    "build_reference_map",
    "encode_short_ref",
    "is_wellformed_short_ref",
    "render_short_window",
    "restore_long_refs",
]
