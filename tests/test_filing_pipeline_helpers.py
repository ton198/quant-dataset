"""Focused checks for the pure filing-selection and parser-source helpers."""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

import pytest

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from filings.packages import PackageEntry, PreparedPackage  # noqa: E402
from filings.parsing_models import ParseResult  # noqa: E402
from filings.pipeline.planning import select_filings  # noqa: E402
from filings.pipeline.validation import (  # noqa: E402
    annotate_group_fact_owners,
    fact_sources_verified,
)


class SelectionError(ValueError):
    pass


def test_selection_injects_pending_predicate_filters_scope_and_orders_bound() -> None:
    filings = [
        {"filing_id": "late", "filed_date": date(2024, 1, 3), "scope_status": "included"},
        {"filing_id": "early-b", "filed_date": date(2024, 1, 1), "scope_status": "candidate"},
        {"filing_id": "excluded", "filed_date": date(2024, 1, 2), "scope_status": "excluded"},
        {"filing_id": "early-a", "filed_date": date(2024, 1, 1), "scope_status": "included"},
    ]
    documents = [{"filing_id": "late", "document_id": "doc-late"}]
    pending_calls: list[str] = []

    def is_pending(identity: str, *_args: Any, **_kwargs: Any) -> bool:
        pending_calls.append(identity)
        return True

    selected, docs_by_filing = select_filings(
        filings,
        documents,
        None,
        2,
        {},
        Path("/archive"),
        {},
        has_pending_work=is_pending,
        error_type=SelectionError,
    )

    assert [row["filing_id"] for row in selected] == ["early-a", "early-b"]
    assert pending_calls == ["late", "early-b", "early-a"]
    assert docs_by_filing == {"late": documents}


def test_selection_explicit_ids_reject_missing_and_excluded() -> None:
    filings = [
        {"filing_id": "included", "filed_date": date(2024, 1, 1), "scope_status": "included"},
        {"filing_id": "excluded", "filed_date": date(2024, 1, 2), "scope_status": "excluded"},
    ]
    common = (filings, [], None, 5, {}, Path("/archive"), {})
    kwargs = {"has_pending_work": lambda *_args, **_kwargs: False, "error_type": SelectionError}
    with pytest.raises(SelectionError, match="absent from the active snapshot"):
        select_filings(*common[:2], ("missing",), *common[3:], **kwargs)
    with pytest.raises(SelectionError, match="excluded from processing scope"):
        select_filings(*common[:2], ("excluded",), *common[3:], **kwargs)

    selected, _ = select_filings(*common[:2], ("included",), *common[3:], **kwargs)
    assert [row["filing_id"] for row in selected] == ["included"]


def _package(root: Path) -> tuple[PreparedPackage, str, str]:
    source = root / "annual.htm"
    source.write_bytes(b"verified filing bytes")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    source_url = "https://sec.example/filing/annual.htm"
    package = PreparedPackage(
        filing_id="0000000123:0000999999-24-000001",
        workspace_root=root,
        entrypoints={
            source_url: PackageEntry(
                local_path=source,
                document_id="doc-1",
                raw_sha256=digest,
                byte_size=source.stat().st_size,
                role="primary",
                original_filename="annual.htm",
            )
        },
        scope_status="included",
        raw_coverage_status="complete",
        snapshot_id="snapshot-1",
    )
    return package, source_url, digest


def _parse_result(source_url: str, digest: str, relative: str = "annual.htm") -> ParseResult:
    return ParseResult(
        filing_id="0000000123:0000999999-24-000001",
        document_id="doc-1",
        parse_id="parse-1",
        parser_name="filings.xbrl",
        parser_version="test",
        status="full",
        source_hash=digest,
        facts=[
            {
                "source_uri": source_url,
                "source_relpath": relative,
                "document_hash": digest,
                "provenance": '{"source":"inline"}',
            }
        ],
    )


def test_fact_source_verification_rejects_forged_path_and_digest(tmp_path: Path) -> None:
    package, source_url, digest = _package(tmp_path)
    result = _parse_result(source_url, digest)
    assert fact_sources_verified(result, package, None)

    forged_path = _parse_result(source_url, digest, "../outside.htm")
    assert not fact_sources_verified(forged_path, package, None)

    forged_digest = _parse_result(source_url, "f" * 64)
    assert not fact_sources_verified(forged_digest, package, None)


def test_inline_owner_annotation_requires_unique_owner_and_is_canonical(tmp_path: Path) -> None:
    package, source_url, digest = _package(tmp_path)
    result = _parse_result(source_url, digest)
    source_group = {
        "members": [
            {"source_url": source_url, "raw_sha256": digest, "document_id": "doc-1"}
        ]
    }
    assert annotate_group_fact_owners(result, source_group, canonical_json=lambda value: json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8"))
    assert result.facts[0]["provenance"] == (
        '{"source":"inline","source_document_id":"doc-1"}'
    )
    assert fact_sources_verified(result, package, None, source_group)

    duplicate_owner = {
        "members": [
            {"source_url": source_url, "raw_sha256": digest, "document_id": "doc-1"},
            {"source_url": source_url, "raw_sha256": digest, "document_id": "doc-2"},
        ]
    }
    untouched = _parse_result(source_url, digest)
    assert not annotate_group_fact_owners(
        untouched,
        duplicate_owner,
        canonical_json=lambda value: json.dumps(value, sort_keys=True).encode("utf-8"),
    )

    prior_conflict = _parse_result(source_url, digest)
    prior_conflict.facts[0]["provenance"] = '{"source_document_id":"foreign"}'
    assert not annotate_group_fact_owners(
        prior_conflict,
        source_group,
        canonical_json=lambda value: json.dumps(value, sort_keys=True).encode("utf-8"),
    )
