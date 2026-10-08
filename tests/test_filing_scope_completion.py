from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from filings.acquisition import _coverage_after_plan, _is_complete, _plan_status_from_rows


def _doc(
    role: str,
    *,
    plan_status: Any = "selected_financial",
    filename: str | None = None,
    selection_status: str = "required",
    fetch_status: str = "present",
    locator_override: str | None = None,
) -> dict[str, Any]:
    return {
        "role": role,
        "original_filename": filename or f"{role}.htm",
        "selection_status": selection_status,
        "fetch_status": fetch_status,
        "source_inventory_locator": (
            locator_override
            if locator_override is not None
            else json.dumps({"plan_status": plan_status}, sort_keys=True)
        ),
    }


def _complete_docs(plan_status: str = "selected_financial") -> list[dict[str, Any]]:
    return [
        _doc("filing_index", filename="index.json", plan_status=plan_status),
        _doc("primary", filename="primary.htm", plan_status=plan_status),
        _doc("exhibit", filename="financial-statements.pdf", plan_status=plan_status),
    ]


def _complete_filing_row(**updates: Any) -> dict[str, Any]:
    return {
        "inventory_status": "known",
        "scope_status": "included",
        "raw_coverage_status": "scoped_complete",
        "primary_document_name": "primary.htm",
        **updates,
    }


def test_included_6k_scope_with_needs_review_stays_partial_when_all_known_files_are_present() -> (
    None
):
    filing = {
        "form": "6-K",
        "scope_status": "included",
        "scope_evidence_json": '{"decision":"included"}',
    }
    docs = _complete_docs("needs_review")

    assert _coverage_after_plan(filing, "needs_review", docs, blocked=False) == "partial"
    assert not _is_complete(_complete_filing_row(), docs)
    assert _plan_status_from_rows(docs) == "needs_review"


def test_foreign_40f_needs_review_is_partial_even_with_present_required_documents() -> None:
    filing = {"form": "40-F", "scope_status": "included"}
    docs = _complete_docs("needs_review")

    assert _coverage_after_plan(filing, "needs_review", docs, blocked=False) == "partial"
    assert not _is_complete(_complete_filing_row(), docs)


def test_selected_financial_is_complete_only_with_index_primary_and_nonempty_required_set() -> None:
    filing = {"form": "10-K", "scope_status": "included"}
    docs = _complete_docs()

    assert (
        _coverage_after_plan(filing, "selected_financial", docs, blocked=False) == "scoped_complete"
    )
    assert _is_complete(_complete_filing_row(), docs)

    assert _coverage_after_plan(filing, "selected_financial", docs[:1], blocked=False) == "partial"
    assert not _is_complete(_complete_filing_row(), docs[:1])

    without_primary = [docs[0], docs[2]]
    assert (
        _coverage_after_plan(filing, "selected_financial", without_primary, blocked=False)
        == "partial"
    )
    assert not _is_complete(_complete_filing_row(), without_primary)

    missing_index = [docs[1], docs[2]]
    assert (
        _coverage_after_plan(filing, "selected_financial", missing_index, blocked=False)
        == "partial"
    )
    assert not _is_complete(_complete_filing_row(), missing_index)


def test_unknown_missing_or_conflicting_persisted_plan_status_fails_closed() -> None:
    for docs in (
        [],
        [_doc("filing_index", locator_override="{}")],
        [_doc("filing_index", plan_status="unexpected")],
        [_doc("filing_index", locator_override="not-json")],
        [
            _doc("filing_index", plan_status="selected_financial"),
            _doc("primary", plan_status="needs_review"),
        ],
    ):
        assert _plan_status_from_rows(docs) == "needs_review"
        assert not _is_complete(_complete_filing_row(), docs)


def test_nonfinancial_and_blocked_coverage_keep_their_existing_terminal_states() -> None:
    filing = {"form": "6-K", "scope_status": "included"}
    docs = _complete_docs("non_financial")

    assert _coverage_after_plan(filing, "non_financial", docs, blocked=False) == "not_attempted"
    assert _coverage_after_plan(filing, "selected_financial", docs, blocked=True) == "blocked"
