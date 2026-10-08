"""Pure, bounded filing selection for the processing integration layer."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from ..models import RawObjectRef


def select_filings(
    filing_rows: Sequence[Mapping[str, Any]],
    documents: Sequence[Mapping[str, Any]],
    requested_ids: tuple[str, ...] | None,
    max_filings: int,
    active_parses: Mapping[tuple[str, str], Mapping[str, Any]],
    archive_root: Path,
    refs: Mapping[str, RawObjectRef],
    *,
    dependency_rows: Sequence[Mapping[str, Any]] = (),
    has_pending_work: Callable[..., bool],
    error_type: type[Exception],
) -> tuple[list[Mapping[str, Any]], dict[str, list[Mapping[str, Any]]]]:
    """Select explicitly requested or pending filings without mutating archive state."""
    filings_by_id = {row["filing_id"]: row for row in filing_rows}
    docs_by_filing: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in documents:
        docs_by_filing[row["filing_id"]].append(row)
    if requested_ids is not None:
        missing = sorted(set(requested_ids) - set(filings_by_id))
        if missing:
            raise error_type(
                f"explicit filing_ids are absent from the active snapshot: {missing}"
            )
        excluded = sorted(
            identity
            for identity in requested_ids
            if filings_by_id[identity]["scope_status"] == "excluded"
        )
        if excluded:
            raise error_type(
                f"explicit filing_ids are excluded from processing scope: {excluded}"
            )
        return [filings_by_id[value] for value in requested_ids], dict(docs_by_filing)

    pending: list[Mapping[str, Any]] = []
    for row in filing_rows:
        identity = row["filing_id"]
        if row["scope_status"] not in {"included", "candidate"}:
            continue
        if has_pending_work(
            identity,
            docs_by_filing.get(identity, ()),
            active_parses,
            archive_root,
            refs,
            dependency_rows=dependency_rows,
        ):
            pending.append(row)
    pending.sort(key=lambda row: (row["filed_date"], row["filing_id"]))
    return pending[:max_filings], dict(docs_by_filing)
