"""Offline tests for SEC submissions catalog identity and cache validation."""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

import pytest

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from filings import (  # noqa: E402
    DOCUMENTS_SCHEMA,
    FILINGS_SCHEMA,
    CatalogConflictError,
    CatalogError,
    document_id,
    filing_id,
    read_cached_catalog,
    submission_rows,
)

FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "filings_core"
CIK = "0000000123"
PAGE_NAME = "CIK0000000123-submissions-001.json"
SESSIONS = (
    date(2021, 2, 24),
    date(2021, 2, 26),
    date(2024, 1, 12),
    date(2024, 1, 16),
    date(2024, 1, 17),
)


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURE_ROOT / name).read_text(encoding="utf-8"))


def _write_cache(
    root: Path,
    *,
    main_payload: dict[str, Any] | None = None,
    page_payload: dict[str, Any] | None = None,
    include_page: bool = True,
    resource_layout: str = "mapping",
    page_url: str | None = None,
    main_url: str | None = None,
) -> tuple[Path, dict[str, list[dict[str, Any]]]]:
    root.mkdir(parents=True, exist_ok=True)
    main = main_payload if main_payload is not None else _fixture("submissions.json")
    page = page_payload if page_payload is not None else _fixture("submissions-001.json")
    items: dict[str, list[dict[str, Any]]] = {}

    def add(logical_key: str, payload: dict[str, Any], url: str) -> dict[str, Any]:
        content = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        digest = hashlib.sha256(content).hexdigest()
        relative = f"{digest}.json"
        (root / relative).write_bytes(content)
        return {
            "path": relative,
            "sha256": digest,
            "byte_size": len(content),
            "url": url,
            "status": "done",
        }

    items["submissions:0000000123"] = [
        add(
            "submissions:0000000123",
            main,
            main_url or "https://data.sec.gov/submissions/CIK0000000123.json",
        )
    ]
    if include_page:
        items[f"submissions-page:{CIK}:{PAGE_NAME}"] = [
            add(
                f"submissions-page:{CIK}:{PAGE_NAME}",
                page,
                page_url or f"https://data.sec.gov/submissions/{PAGE_NAME}",
            )
        ]

    if resource_layout == "mapping":
        resources: Any = items
    elif resource_layout == "list":
        resources = [
            {"logical_key": key, **record} for key, versions in items.items() for record in versions
        ]
    else:
        raise AssertionError(resource_layout)
    (root / "manifest.json").write_text(
        json.dumps({"resources": resources}, sort_keys=True), encoding="utf-8"
    )
    return root, items


def _catalog(root: Path, **kwargs: Any):
    return read_cached_catalog(root, CIK, xnys_sessions=SESSIONS, **kwargs)


def test_literal_ids_and_agent_accession_prefix() -> None:
    accession = "0000999999-24-000001"
    assert filing_id(CIK, accession) == f"{CIK}:{accession}"
    with pytest.raises(ValueError):
        filing_id("123", accession)
    # The accession prefix is the submitting agent, not an issuer identity check.
    assert filing_id(CIK, "0000000123-24-000001") == f"{CIK}:0000000123-24-000001"

    expected = hashlib.sha256(
        json.dumps(
            ["document-v1", f"{CIK}:{accession}", "https://example.test/a.htm"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    assert document_id(f"{CIK}:{accession}", "https://example.test/a.htm") == expected


def test_submission_rows_supported_shapes_and_rejects_ragged_arrays() -> None:
    main = _fixture("submissions.json")
    page = _fixture("submissions-001.json")
    assert len(submission_rows(main, kind="submissions")) == 3
    assert len(submission_rows(page, kind="submissions-page")) == 2
    assert (
        submission_rows(
            {
                "fields": ["accessionNumber", "filingDate"],
                "data": [["0000999999-24-000001", "2024-01-12"]],
            },
            kind="submissions-page",
        )[0]["filingDate"]
        == "2024-01-12"
    )

    ragged = _fixture("submissions.json")
    ragged["filings"]["recent"]["form"].pop()
    with pytest.raises(CatalogError, match="ragged"):
        submission_rows(ragged, kind="submissions")
    with pytest.raises(CatalogError, match="duplicate field"):
        submission_rows(
            {"fields": ["form", "form"], "data": [["10-K", "10-K"]]},
            kind="submissions-page",
        )
    with pytest.raises(CatalogError, match="width"):
        submission_rows(
            {"fields": ["form", "filingDate"], "data": [["10-K"]]},
            kind="submissions-page",
        )


def test_cached_catalog_hashes_owned_main_and_referenced_page(tmp_path: Path) -> None:
    root, _ = _write_cache(tmp_path / "cache", resource_layout="list")
    result = _catalog(root)
    assert result.input_status == "complete"
    assert len(result.resources) == 2
    assert len(result.filings) == 4
    assert {row["accession_number"] for row in result.filings} == {
        "0000999999-24-000000",
        "0000999999-24-000001",
        "0000999999-24-000002",
        "0000007777-21-000003",
    }
    duplicate = next(row for row in result.filings if row["form"] == "10-Q/A")
    assert duplicate["primary_document_name"] == "quarter-amended.htm"
    assert duplicate["source_submission_logical_key"] == "submissions:0000000123"
    assert duplicate["effective_visible_session"] == date(2024, 1, 16)
    assert result.input_fingerprint == _catalog(root).input_fingerprint


def test_normalized_owner_tokens_and_legacy_cache_prefix_paths(tmp_path: Path) -> None:
    root, _ = _write_cache(tmp_path / "cache")
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    resources = manifest["resources"]
    page_key = f"submissions-page:{CIK}:{PAGE_NAME}"
    page_record = resources.pop(page_key)
    resources[f"submissions-page:123:{PAGE_NAME}"] = page_record
    main_record = resources["submissions:0000000123"][0]
    main_record["path"] = f"data/raw/sec/financials/{main_record['path']}"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = _catalog(root)
    historical = next(row for row in result.filings if row["form"] == "10-K")
    assert historical["source_submission_logical_key"] == f"submissions-page:123:{PAGE_NAME}"
    assert result.resources[0].source_path.parent == root.resolve()


def test_embedded_logical_key_must_match_authoritative_manifest_key(tmp_path: Path) -> None:
    root, _ = _write_cache(tmp_path / "cache")
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["resources"]["submissions:0000000123"][0]["logical_key"] = "submissions:0000000124"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(CatalogError, match="logical_key does not match"):
        _catalog(root)


def test_exact_owner_and_filename_are_required(tmp_path: Path) -> None:
    root, _ = _write_cache(tmp_path / "cache")
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    # This similarly named foreign key cannot satisfy the issuer's reference.
    record = manifest["resources"].pop(f"submissions-page:{CIK}:{PAGE_NAME}")
    manifest["resources"][f"submissions-page:0000000124:{PAGE_NAME}"] = record
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(CatalogError, match="missing historical page"):
        _catalog(root)

    foreign_reference = _fixture("submissions.json")
    foreign_reference["filings"]["files"][0]["name"] = "CIK0000000124-submissions-001.json"
    root2, _ = _write_cache(tmp_path / "cache2", main_payload=foreign_reference)
    with pytest.raises(CatalogError, match="not owned"):
        _catalog(root2, allow_partial=True)


def test_unreferenced_owned_page_is_not_loaded_by_filename_guess(tmp_path: Path) -> None:
    root, _ = _write_cache(tmp_path / "cache")
    extra_name = "CIK0000000123-submissions-002.json"
    extra_payload = {
        "accessionNumber": ["0000555555-20-000001"],
        "filingDate": ["2020-06-01"],
        "reportDate": ["2019-12-31"],
        "form": ["40-F"],
    }
    extra_content = json.dumps(extra_payload, sort_keys=True, separators=(",", ":")).encode()
    extra_hash = hashlib.sha256(extra_content).hexdigest()
    extra_path = f"{extra_hash}.json"
    (root / extra_path).write_bytes(extra_content)
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["resources"][f"submissions-page:{CIK}:{extra_name}"] = [
        {
            "path": extra_path,
            "sha256": extra_hash,
            "byte_size": len(extra_content),
            "url": f"https://data.sec.gov/submissions/{extra_name}",
            "status": "done",
        }
    ]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = _catalog(root)
    assert len(result.resources) == 2
    assert "0000555555-20-000001" not in {row["accession_number"] for row in result.filings}


@pytest.mark.parametrize(
    "main_url",
    [
        "https://example.test/submissions/CIK0000000123.json",
        "https://data.sec.gov/submissions/CIK0000000124.json",
    ],
)
def test_main_submission_url_must_be_exact_sec_owner_url(tmp_path: Path, main_url: str) -> None:
    root, _ = _write_cache(tmp_path / "cache", main_url=main_url)
    with pytest.raises(CatalogError, match="unexpected SEC URL"):
        _catalog(root)


def test_main_cik_must_match_request(tmp_path: Path) -> None:
    payload = _fixture("submissions.json")
    payload["cik"] = "0000000124"
    root, _ = _write_cache(tmp_path / "cache", main_payload=payload)
    with pytest.raises(CatalogError, match="does not match"):
        _catalog(root)


def test_every_referenced_page_required_unless_partial_is_explicit(tmp_path: Path) -> None:
    payload = _fixture("submissions.json")
    payload["filings"]["files"].append({"name": "CIK0000000123-submissions-002.json"})
    root, _ = _write_cache(tmp_path / "cache", main_payload=payload)
    with pytest.raises(CatalogError, match="missing historical page"):
        _catalog(root)

    partial = _catalog(root, allow_partial=True)
    assert partial.input_status == "partial"
    assert partial.diagnostics == ("missing_historical_page:CIK0000000123-submissions-002.json",)
    assert len(partial.resources) == 2


def test_corrupt_latest_resource_never_falls_back_to_older_version(tmp_path: Path) -> None:
    root, items = _write_cache(tmp_path / "cache")
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    key = "submissions:0000000123"
    valid = manifest["resources"][key][0]
    latest = dict(valid, sha256="0" * 64)
    manifest["resources"][key] = [valid, latest]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(CatalogError, match="latest resource bytes"):
        _catalog(root)
    assert items[key][0]["sha256"] == valid["sha256"]


def test_historical_sec_url_and_symlink_escape_are_rejected(tmp_path: Path) -> None:
    root, _ = _write_cache(tmp_path / "cache", page_url="https://example.test/page.json")
    with pytest.raises(CatalogError, match="unexpected SEC URL"):
        _catalog(root)

    root2, _ = _write_cache(tmp_path / "cache2")
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    manifest_path = root2 / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    main_key = "submissions:0000000123"
    original = manifest["resources"][main_key][0]
    link = root2 / "outside-link.json"
    link.symlink_to(outside)
    original["path"] = link.name
    original["byte_size"] = outside.stat().st_size
    original["sha256"] = hashlib.sha256(outside.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(CatalogError, match="escapes cache_root"):
        _catalog(root2)


def test_duplicate_accessions_coalesce_missing_values_and_conflicts_fail(tmp_path: Path) -> None:
    root, _ = _write_cache(tmp_path / "cache")
    rows = _catalog(root).filings
    assert sum(row["accession_number"] == "0000999999-24-000001" for row in rows) == 1

    conflicting_page = _fixture("submissions-001.json")
    conflicting_page["reportDate"][0] = "2023-06-30"
    root2, _ = _write_cache(tmp_path / "cache2", page_payload=conflicting_page)
    with pytest.raises(CatalogConflictError, match="reportDate"):
        _catalog(root2)


def test_amendments_6k_candidate_and_strict_next_xnys_session(tmp_path: Path) -> None:
    root, _ = _write_cache(tmp_path / "cache")
    result = _catalog(root)
    regular = next(row for row in result.filings if row["form"] == "10-Q")
    amended = next(row for row in result.filings if row["form"] == "10-Q/A")
    six_k = next(row for row in result.filings if row["form"] == "6-K")
    assert regular["filing_id"] != amended["filing_id"]
    assert amended["is_amendment"] is True
    assert amended["parent_filing_id"] is None
    assert amended["parent_link_source"] is None
    assert amended["effective_visible_session"] == date(2024, 1, 16)  # Fri -> Tue after MLK Day
    assert amended["acceptance_datetime_raw"] == "2024-01-12T20:00:00Z"
    assert amended["acceptance_datetime_utc"].isoformat() == "2024-01-12T20:00:00+00:00"
    assert six_k["acceptance_datetime_raw"] == "2024-01-16T15:00:00"
    assert six_k["acceptance_datetime_utc"] is None  # no assumed timezone for naive input
    assert six_k["scope_status"] == "candidate"
    assert six_k["scope_evidence_json"] is None
    assert six_k["raw_coverage_status"] == "not_attempted"
    assert six_k["effective_visible_session"] == date(2024, 1, 17)
    historical = next(row for row in result.filings if row["form"] == "10-K")
    assert historical["effective_visible_session"] == date(2021, 2, 26)

    with pytest.raises(CatalogError, match="strictly after"):
        read_cached_catalog(
            root,
            CIK,
            xnys_sessions=(
                date(2021, 2, 24),
                date(2021, 2, 26),
                date(2024, 1, 12),
                date(2024, 1, 16),
            ),
        )


def test_calendar_must_cover_filing_dates_before_assigning_future_sessions(tmp_path: Path) -> None:
    root, _ = _write_cache(tmp_path / "cache")
    with pytest.raises(CatalogError, match="session on or before 2021-02-25"):
        read_cached_catalog(
            root,
            CIK,
            xnys_sessions=(
                date(2024, 1, 10),
                date(2024, 1, 12),
                date(2024, 1, 16),
                date(2024, 1, 17),
            ),
        )


def test_forms_override_only_narrows_approved_forms(tmp_path: Path) -> None:
    root, _ = _write_cache(tmp_path / "cache")
    result = _catalog(root, forms={"10-K"})
    assert {row["form"] for row in result.filings} == {"10-K"}
    with pytest.raises(CatalogError, match="approved forms"):
        _catalog(root, forms={"8-K"})


def test_core_arrow_schemas_match_contract_columns() -> None:
    assert FILINGS_SCHEMA.names == [
        "filing_id",
        "cik10",
        "accession_number",
        "form",
        "filed_date",
        "report_period_end",
        "acceptance_datetime_raw",
        "acceptance_datetime_utc",
        "effective_visible_session",
        "is_amendment",
        "parent_filing_id",
        "parent_link_source",
        "primary_document_name",
        "scope_status",
        "scope_evidence_json",
        "inventory_status",
        "raw_coverage_status",
        "source_submission_logical_key",
        "source_submission_sha256",
        "source_submission_path",
        "source_submission_locator",
    ]
    assert str(FILINGS_SCHEMA.field("filed_date").type) == "date32[day]"
    assert str(FILINGS_SCHEMA.field("acceptance_datetime_utc").type) == "timestamp[us, tz=UTC]"
    assert DOCUMENTS_SCHEMA.names == [
        "document_id",
        "filing_id",
        "original_filename",
        "source_url",
        "role",
        "selection_status",
        "fetch_status",
        "source_inventory_sha256",
        "source_inventory_locator",
        "raw_sha256",
        "raw_path",
        "byte_size",
        "media_type",
        "fetched_at_utc",
        "http_status",
        "diagnostic_code",
        "fact_extraction_status",
        "text_extraction_status",
    ]
