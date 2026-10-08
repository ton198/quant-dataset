"""Offline tests for resumable filing archive acquisition and parser packages."""

from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Mapping
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

import filings.acquisition as acquisition_module
from filings.acquisition import (
    AcquisitionConflictError,
    AcquisitionError,
    download_archive,
    load_archive_runspec,
)
from filings.archive import ArchiveCorruptionError, open_archive, read_snapshot
from filings.models import (
    DOCUMENTS_SCHEMA,
    FILINGS_SCHEMA,
    RunSpec,
    document_id,
)
from filings.packages import (
    PackageConflictError,
    PackagePreparationError,
    materialize_package,
)
from filings.sec_client import SecClient

FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "filings_acquisition"
VALID_AGENT = "Quant Research analyst@acme-financials.com"
SEC_SELF_LISTED_XBRL_INSTANCE = (
    b'<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance">'
    b'<xbrli:context id="fixture-context"/></xbrli:xbrl>'
)


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def monotonic(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds


class FakeResponse:
    def __init__(
        self,
        url: str,
        body: bytes = b"",
        *,
        status: int = 200,
        content_type: str = "application/octet-stream",
    ) -> None:
        self.url = url
        self.status = status
        self.body = body
        self.position = 0
        self.headers = {
            "Content-Type": content_type,
            "Content-Length": str(len(body)),
        }
        self.closed = False

    def geturl(self) -> str:
        return self.url

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = len(self.body) - self.position
        end = min(self.position + size, len(self.body))
        chunk = self.body[self.position : end]
        self.position = end
        return chunk

    def close(self) -> None:
        self.closed = True


class RouteTransport:
    """A strict mock transport: an unknown URL is a test failure, never a network call."""

    def __init__(self, routes: Mapping[str, Any]) -> None:
        self.routes = dict(routes)
        self.calls: list[str] = []

    def __call__(self, url: str, headers: Any, timeout: float) -> FakeResponse:
        self.calls.append(url)
        if url not in self.routes:
            raise AssertionError(f"unexpected mocked URL: {url}")
        value = self.routes[url]
        if isinstance(value, BaseException):
            raise value
        if isinstance(value, tuple):
            status, body = value[:2]
            content_type = value[2] if len(value) > 2 else "application/octet-stream"
            return FakeResponse(url, body, status=status, content_type=content_type)
        return FakeResponse(url, value, content_type=_content_type(url))


def _content_type(url: str) -> str:
    if url.endswith(".json"):
        return "application/json"
    if url.endswith(".html") or url.endswith(".htm"):
        return "text/html"
    if url.endswith(".xsd") or url.endswith(".xml"):
        return "application/xml"
    if url.endswith(".pdf"):
        return "application/pdf"
    return "application/octet-stream"


def _base_url(cik10: str, accession: str) -> str:
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik10)}/{accession.replace('-', '')}/"


def _routes_for_fixture(
    case: str,
    cik10: str,
    accession: str,
    *,
    overrides: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    directory = FIXTURE_ROOT / case
    base = _base_url(cik10, accession)
    index = json.loads((directory / "index.json").read_text(encoding="utf-8"))
    index["directory"]["name"] = f"/Archives/edgar/data/{int(cik10)}/{accession.replace('-', '')}/"
    routes: dict[str, Any] = {
        base + "index.json": json.dumps(index).encode("utf-8"),
        base + f"{accession}-index.html": (directory / "detail.html").read_bytes(),
    }
    for path in directory.iterdir():
        if path.is_file() and path.name not in {"index.json", "detail.html"}:
            routes[base + path.name] = path.read_bytes()
    routes.update(overrides or {})
    return routes


def _client(routes: Mapping[str, Any], *, retries: int = 0) -> tuple[SecClient, RouteTransport]:
    transport = RouteTransport(routes)
    clock = FakeClock()
    client = SecClient(
        VALID_AGENT,
        interval=0.2,
        timeout=3.0,
        retries=retries,
        transport=transport,
        clock=clock.monotonic,
        sleep=clock.sleep,
    )
    return client, transport


def _filing_row(
    cik10: str,
    accession: str,
    form: str,
    primary: str,
    source_ref: Any,
    *,
    filed_date: date,
    scope_status: str | None = None,
) -> dict[str, Any]:
    identity = f"{cik10}:{accession}"
    return {
        "filing_id": identity,
        "cik10": cik10,
        "accession_number": accession,
        "form": form,
        "filed_date": filed_date,
        "report_period_end": None,
        "acceptance_datetime_raw": None,
        "acceptance_datetime_utc": None,
        "effective_visible_session": filed_date + timedelta(days=1),
        "is_amendment": form.endswith("/A"),
        "parent_filing_id": None,
        "parent_link_source": None,
        "primary_document_name": primary,
        "scope_status": scope_status or ("candidate" if form == "6-K" else "included"),
        "scope_evidence_json": None,
        "inventory_status": "not_inspected",
        "raw_coverage_status": "not_attempted",
        "source_submission_logical_key": f"submissions:{cik10}",
        "source_submission_sha256": source_ref.sha256,
        "source_submission_path": source_ref.path,
        "source_submission_locator": "$.filings.recent.accessionNumber[0]",
    }


def _seed_archive(
    tmp_path: Path,
    filing_specs: list[dict[str, Any]],
) -> tuple[Path, Path, RunSpec]:
    root = tmp_path / "archive"
    protected = tmp_path / "protected-inputs"
    protected.mkdir(exist_ok=True)
    spec = RunSpec(
        input_fingerprint="fixture-submission-catalog-v1",
        policy={"selection": "fixture"},
        calendar_provenance={"calendar": "fixture"},
        scope_provenance={"forms": ["10-K", "10-Q", "6-K"]},
    )
    filing_rows: list[dict[str, Any]] = []
    raw_refs: dict[str, Any] = {}
    with open_archive(root, spec, protected_paths=(protected,)) as writer:
        for index, values in enumerate(filing_specs):
            cik10 = values["cik10"]
            accession = values["accession"]
            submission = json.dumps(
                {"fixture_submission": index, "accession": accession},
                sort_keys=True,
            ).encode("utf-8")
            source_ref = writer.put_raw_bytes(submission)
            raw_refs[source_ref.path] = source_ref
            filing_rows.append(
                _filing_row(
                    cik10,
                    accession,
                    values["form"],
                    values["primary"],
                    source_ref,
                    filed_date=values.get("filed_date", date(2024, 1, 1 + index)),
                    scope_status=values.get("scope_status"),
                )
            )
        documents: list[dict[str, Any]] = []
        snapshot = writer.commit(
            tables={
                "filings": pa.Table.from_pylist(filing_rows, schema=FILINGS_SCHEMA),
                "documents": pa.Table.from_pylist(documents, schema=DOCUMENTS_SCHEMA),
            },
            raw_objects=tuple(raw_refs.values()),
            expected_manifest_version=0,
        )
    assert snapshot.manifest_version == 1
    return root, protected, spec


def _single_ten_k(tmp_path: Path, **kwargs: Any) -> tuple[Path, Path, RunSpec, dict[str, Any]]:
    values = {
        "cik10": "0000000001",
        "accession": "0000000001-24-000001",
        "form": "10-K",
        "primary": "annual.htm",
        **kwargs,
    }
    root, protected, spec = _seed_archive(tmp_path, [values])
    return root, protected, spec, values


def _table_rows(snapshot: Any, name: str) -> list[dict[str, Any]]:
    return snapshot.tables[name].to_pylist()


def _doc_for(rows: list[dict[str, Any]], filing_id_value: str, filename: str) -> dict[str, Any]:
    return next(
        row
        for row in rows
        if row["filing_id"] == filing_id_value and row["original_filename"] == filename
    )


def test_default_10k_persists_index_and_only_fetches_primary_and_required_xbrl(
    tmp_path: Path,
) -> None:
    root, protected, spec, filing = _single_ten_k(tmp_path)
    routes = _routes_for_fixture("ten_k", filing["cik10"], filing["accession"])
    client, transport = _client(routes)
    identity = f"{filing['cik10']}:{filing['accession']}"

    result = download_archive(root, client=client, protected_paths=(protected,))

    assert result.processed_filing_ids == (identity,)
    assert result.selection_statuses[identity] == "selected_financial"
    assert result.acquired_document_count == 3
    snapshot = result.snapshot
    filing_row = _table_rows(snapshot, "filings")[0]
    assert filing_row["scope_status"] == "included"
    assert filing_row["inventory_status"] == "known"
    assert filing_row["raw_coverage_status"] == "scoped_complete"

    docs = _table_rows(snapshot, "documents")
    indexed = [row for row in docs if row["role"] == "filing_index"]
    assert {row["original_filename"] for row in indexed} == {
        "index.json",
        f"{filing['accession']}-index.html",
    }
    assert all(row["fetch_status"] == "present" for row in indexed)
    assert _doc_for(docs, identity, "annual.htm")["role"] == "primary"
    assert _doc_for(docs, identity, "issuer.xsd")["role"] == "schema"
    assert _doc_for(docs, identity, "issuer_cal.xml")["role"] == "linkbase"
    assert _doc_for(docs, identity, "logo.gif")["selection_status"] == "out_of_scope"
    assert _doc_for(docs, identity, "logo.gif")["fetch_status"] == "not_requested"
    assert _doc_for(docs, identity, "press-release.pdf")["selection_status"] == "candidate"
    assert _doc_for(docs, identity, "press-release.pdf")["fetch_status"] == "not_requested"
    assert _doc_for(docs, identity, "ex31.htm")["selection_status"] == "candidate"
    assert _doc_for(docs, identity, "ex31.htm")["fetch_status"] == "not_requested"
    assert all(row["fact_extraction_status"] == "not_attempted" for row in docs)
    assert all(row["text_extraction_status"] == "not_attempted" for row in docs)

    base = _base_url(filing["cik10"], filing["accession"])
    assert transport.calls == [
        base + "index.json",
        base + f"{filing['accession']}-index.html",
        base + "annual.htm",
        base + "issuer.xsd",
        base + "issuer_cal.xml",
    ]
    raw_hashes = {ref.sha256 for ref in snapshot.raw_objects}
    for row in docs:
        assert row["source_inventory_sha256"] in raw_hashes
        locator = json.loads(row["source_inventory_locator"])
        for source in locator.get("sources", []):
            assert source["sha256"] in raw_hashes
    annual = _doc_for(docs, identity, "annual.htm")
    assert annual["source_inventory_sha256"] == _doc_for(docs, identity, "index.json")["raw_sha256"]
    assert annual["source_inventory_locator"]
    assert load_archive_runspec(root).canonical_dict() == spec.canonical_dict()


def test_explicit_one_filing_bound_orders_pending_filings_and_advances(tmp_path: Path) -> None:
    filings = [
        {
            "cik10": "0000000001",
            "accession": "0000000001-24-000002",
            "form": "10-K",
            "primary": "annual.htm",
            "filed_date": date(2024, 1, 3),
        },
        {
            "cik10": "0000000002",
            "accession": "0000000002-24-000001",
            "form": "10-Q",
            "primary": "annual.htm",
            "filed_date": date(2024, 1, 1),
        },
        {
            "cik10": "0000000003",
            "accession": "0000000003-24-000001",
            "form": "10-K",
            "primary": "annual.htm",
            "filed_date": date(2024, 1, 2),
            "scope_status": "excluded",
        },
    ]
    root, protected, _ = _seed_archive(tmp_path, filings)
    first, second, excluded = filings
    routes = {}
    routes.update(_routes_for_fixture("ten_k", second["cik10"], second["accession"]))
    routes.update(_routes_for_fixture("ten_k", first["cik10"], first["accession"]))
    client, transport = _client(routes)

    first_run = download_archive(root, client=client, protected_paths=(protected,), max_filings=1)

    assert first_run.processed_filing_ids == (f"{second['cik10']}:{second['accession']}",)
    assert all(excluded["accession"] not in url for url in transport.calls)
    second_client, second_transport = _client(routes)
    second_run = download_archive(
        root, client=second_client, protected_paths=(protected,), max_filings=1
    )
    assert second_run.processed_filing_ids == (f"{first['cik10']}:{first['accession']}",)
    assert len(second_transport.calls) == 5
    assert second_run.snapshot.manifest_version == first_run.snapshot.manifest_version + 1


def test_default_five_limit_does_not_request_a_sixth_filing(tmp_path: Path) -> None:
    filings = [
        {
            "cik10": f"{number:010d}",
            "accession": f"{number:010d}-24-000001",
            "form": "10-K",
            "primary": "annual.htm",
            "filed_date": date(2024, 1, number),
        }
        for number in range(1, 7)
    ]
    root, protected, _ = _seed_archive(tmp_path, filings)
    routes: dict[str, Any] = {}
    for filing in filings[:5]:
        routes.update(_routes_for_fixture("ten_k", filing["cik10"], filing["accession"]))
    client, transport = _client(routes)

    result = download_archive(root, client=client, protected_paths=(protected,))

    expected = tuple(f"{row['cik10']}:{row['accession']}" for row in filings[:5])
    assert result.processed_filing_ids == expected
    assert result.acquired_document_count == 15
    assert len(transport.calls) == 25
    assert all(filings[5]["accession"] not in url for url in transport.calls)
    docs = _table_rows(result.snapshot, "documents")
    assert not any(
        row["filing_id"] == f"{filings[5]['cik10']}:{filings[5]['accession']}" for row in docs
    )


def test_sec_selflisted_detail_response_is_one_audited_document_and_resumes_offline(
    tmp_path: Path,
) -> None:
    filing = {
        "cik10": "0000320193",
        "accession": "0000320193-24-000081",
        "form": "10-Q",
        "primary": "aapl-20240629.htm",
        "filed_date": date(2024, 8, 2),
    }
    root, protected, _ = _seed_archive(tmp_path, [filing])
    base = _base_url(filing["cik10"], filing["accession"])
    xbrl_instance_url = base + "aapl-20240629_htm.xml"
    routes = _routes_for_fixture("sec_selflisted", filing["cik10"], filing["accession"])
    routes[xbrl_instance_url] = SEC_SELF_LISTED_XBRL_INSTANCE
    client, transport = _client(routes)
    identity = f"{filing['cik10']}:{filing['accession']}"
    index_url = base + "index.json"
    detail_name = f"{filing['accession']}-index.html"
    detail_url = base + detail_name
    inventory = json.loads(
        (FIXTURE_ROOT / "sec_selflisted" / "index.json").read_text(encoding="utf-8")
    )
    directory_items = inventory["directory"]["item"]
    directory_filenames = {item["name"] for item in directory_items}
    expected_selected = {
        "aapl-20240629.htm",
        "aapl-20240629.xsd",
        "aapl-20240629_cal.xml",
        "aapl-20240629_def.xml",
        "aapl-20240629_lab.xml",
        "aapl-20240629_pre.xml",
        "aapl-20240629_htm.xml",
    }

    result = download_archive(root, client=client, protected_paths=(protected,))

    filing_row = _table_rows(result.snapshot, "filings")[0]
    docs = _table_rows(result.snapshot, "documents")
    assert result.selection_statuses[identity] == "selected_financial"
    assert result.acquired_document_count == len(expected_selected) == 7
    assert filing_row["inventory_status"] == "known"
    assert filing_row["scope_status"] == "included"
    assert filing_row["raw_coverage_status"] == "scoped_complete"
    expected_document_filenames = directory_filenames | {"index.json"}
    expected_document_urls = {index_url} | {base + filename for filename in directory_filenames}
    assert len(docs) == len(expected_document_filenames) == len(expected_document_urls)
    assert {row["original_filename"] for row in docs} == expected_document_filenames
    assert {row["source_url"] for row in docs} == expected_document_urls
    assert len({row["document_id"] for row in docs}) == len(docs)

    detail_row = _doc_for(docs, identity, detail_name)
    assert detail_row["role"] == "filing_index"
    assert detail_row["selection_status"] == "required"
    assert detail_row["fetch_status"] == "present"
    assert detail_row["source_url"] == detail_url
    detail_raw = (root / detail_row["raw_path"]).read_bytes()
    assert detail_row["raw_sha256"] == hashlib.sha256(detail_raw).hexdigest()
    assert detail_row["source_inventory_sha256"] == detail_row["raw_sha256"]

    item_position = next(
        index for index, item in enumerate(directory_items) if item["name"] == detail_name
    )
    detail_locator = json.loads(detail_row["source_inventory_locator"])
    detail_candidate = detail_locator["directory_item"]
    assert detail_locator["response"]["sha256"] == detail_row["raw_sha256"]
    assert detail_candidate["position"] == item_position
    assert detail_candidate["filename"] == detail_name
    assert detail_candidate["role"] == "other"
    assert detail_candidate["selected"] is False
    assert detail_candidate["description"] is None
    assert detail_candidate["source"] == {
        "sha256": _doc_for(docs, identity, "index.json")["raw_sha256"],
        "locator": f"$.directory.item[{item_position}]",
    }
    raw_hashes = {ref.sha256 for ref in result.snapshot.raw_objects}
    assert all(source["sha256"] in raw_hashes for source in detail_locator["sources"])

    selected_rows = [
        row
        for row in docs
        if row["selection_status"] == "required" and row["role"] != "filing_index"
    ]
    assert {row["original_filename"] for row in selected_rows} == expected_selected
    assert all(row["fetch_status"] == "present" for row in selected_rows)
    extracted_instance = _doc_for(docs, identity, "aapl-20240629_htm.xml")
    assert extracted_instance["role"] == "xbrl_instance"
    assert extracted_instance["selection_status"] == "required"
    assert all(row["fact_extraction_status"] == "not_attempted" for row in docs)
    assert all(row["text_extraction_status"] == "not_attempted" for row in docs)
    expected_calls = {index_url, detail_url} | {base + filename for filename in expected_selected}
    assert len(transport.calls) == len(expected_calls)
    assert set(transport.calls) == expected_calls
    assert all(transport.calls.count(url) == 1 for url in expected_calls)

    repeat_client, repeat_transport = _client({})
    repeated = download_archive(
        root,
        client=repeat_client,
        protected_paths=(protected,),
        filing_ids=(identity,),
    )
    assert repeated.snapshot.snapshot_id == result.snapshot.snapshot_id
    assert repeated.skipped_completed_filing_ids == (identity,)
    assert repeat_transport.calls == []


def test_sec_selflisted_detail_partial_retry_keeps_inventory_audit_and_retries_only_missing_body(
    tmp_path: Path,
) -> None:
    filing = {
        "cik10": "0000320193",
        "accession": "0000320193-24-000081",
        "form": "10-Q",
        "primary": "aapl-20240629.htm",
        "filed_date": date(2024, 8, 2),
    }
    root, protected, _ = _seed_archive(tmp_path, [filing])
    base = _base_url(filing["cik10"], filing["accession"])
    detail_name = f"{filing['accession']}-index.html"
    detail_url = base + detail_name
    primary_url = base + filing["primary"]
    identity = f"{filing['cik10']}:{filing['accession']}"
    first_routes = _routes_for_fixture(
        "sec_selflisted",
        filing["cik10"],
        filing["accession"],
        overrides={primary_url: (404, b"not found", "text/plain")},
    )
    first_routes[base + "aapl-20240629_htm.xml"] = SEC_SELF_LISTED_XBRL_INSTANCE
    first_client, first_transport = _client(first_routes)

    first = download_archive(root, client=first_client, protected_paths=(protected,))

    first_docs = _table_rows(first.snapshot, "documents")
    missing = _doc_for(first_docs, identity, filing["primary"])
    detail_row = _doc_for(first_docs, identity, detail_name)
    extracted_instance = _doc_for(first_docs, identity, "aapl-20240629_htm.xml")
    assert extracted_instance["role"] == "xbrl_instance"
    assert extracted_instance["fetch_status"] == "present"
    assert missing["fetch_status"] == "unavailable"
    assert detail_row["role"] == "filing_index" and detail_row["fetch_status"] == "present"
    assert first_transport.calls.count(detail_url) == 1
    assert len({row["document_id"] for row in first_docs}) == len(first_docs)
    assert _table_rows(first.snapshot, "filings")[0]["raw_coverage_status"] == "partial"

    retry_routes = _routes_for_fixture("sec_selflisted", filing["cik10"], filing["accession"])
    retry_routes[base + "aapl-20240629_htm.xml"] = SEC_SELF_LISTED_XBRL_INSTANCE
    retry_client, retry_transport = _client(retry_routes)
    retried = download_archive(
        root,
        client=retry_client,
        protected_paths=(protected,),
        filing_ids=(identity,),
    )

    retry_docs = _table_rows(retried.snapshot, "documents")
    assert _doc_for(retry_docs, identity, filing["primary"])["fetch_status"] == "present"
    retry_instance = _doc_for(retry_docs, identity, "aapl-20240629_htm.xml")
    assert retry_instance["fetch_status"] == "present"
    assert retry_instance["raw_sha256"] == extracted_instance["raw_sha256"]
    assert _doc_for(retry_docs, identity, detail_name)["role"] == "filing_index"
    assert len({row["document_id"] for row in retry_docs}) == len(retry_docs)
    assert _table_rows(retried.snapshot, "filings")[0]["raw_coverage_status"] == "scoped_complete"
    assert retry_transport.calls == [
        base + "index.json",
        detail_url,
        primary_url,
    ]
    assert first_transport.calls.count(primary_url) == 1


def test_selflisted_selected_candidate_conflicts_instead_of_becoming_an_inventory_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    filing = {
        "cik10": "0000320193",
        "accession": "0000320193-24-000081",
        "form": "10-Q",
        "primary": "aapl-20240629.htm",
        "filed_date": date(2024, 8, 2),
    }
    root, protected, _ = _seed_archive(tmp_path, [filing])
    routes = _routes_for_fixture("sec_selflisted", filing["cik10"], filing["accession"])
    client, transport = _client(routes)
    _identity = f"{filing['cik10']}:{filing['accession']}"
    base = _base_url(filing["cik10"], filing["accession"])
    detail_url = base + f"{filing['accession']}-index.html"
    original_enumerate = acquisition_module.enumerate_documents

    def conflicting_enumerate(*args: Any, **kwargs: Any):
        plan = original_enumerate(*args, **kwargs)
        candidates = list(plan.candidates)
        index = next(
            index for index, candidate in enumerate(candidates) if candidate.url == detail_url
        )
        candidates[index] = replace(
            candidates[index],
            role="primary",
            selected=True,
            selection_status="required",
            selection_reason="conflicting selected primary fixture",
        )
        return replace(plan, candidates=tuple(candidates))

    monkeypatch.setattr(acquisition_module, "enumerate_documents", conflicting_enumerate)
    before = read_snapshot(root)

    with pytest.raises(AcquisitionConflictError, match="selected filing document"):
        download_archive(root, client=client, protected_paths=(protected,))

    assert read_snapshot(root).snapshot_id == before.snapshot_id
    assert transport.calls == [base + "index.json", detail_url]


def test_explicit_missing_or_oversized_filing_selection_fails_without_network(
    tmp_path: Path,
) -> None:
    filings = [
        {
            "cik10": "0000000001",
            "accession": "0000000001-24-000001",
            "form": "10-K",
            "primary": "annual.htm",
        },
        {
            "cik10": "0000000002",
            "accession": "0000000002-24-000001",
            "form": "10-Q",
            "primary": "annual.htm",
        },
    ]
    root, protected, _ = _seed_archive(tmp_path, filings)
    client, transport = _client({})

    with pytest.raises(ValueError, match="exceed max_filings"):
        download_archive(
            root,
            client=client,
            protected_paths=(protected,),
            filing_ids=(
                f"{filings[0]['cik10']}:{filings[0]['accession']}",
                f"{filings[1]['cik10']}:{filings[1]['accession']}",
            ),
            max_filings=1,
        )
    with pytest.raises(AcquisitionError, match="absent from the active snapshot"):
        download_archive(
            root,
            client=client,
            protected_paths=(protected,),
            filing_ids=("0000000099:0000000099-24-000001",),
        )
    assert transport.calls == []
    assert read_snapshot(root).manifest_version == 1


def test_request_requires_existing_archive_protection_and_bounded_max(tmp_path: Path) -> None:
    root, protected, _, _ = _single_ten_k(tmp_path)
    client, transport = _client({})
    with pytest.raises(ValueError, match="max_filings"):
        download_archive(root, client=client, protected_paths=(protected,), max_filings=0)
    with pytest.raises(ValueError, match="max_filings"):
        download_archive(root, client=client, protected_paths=(protected,), max_filings=51)
    with pytest.raises(AcquisitionError, match="protected path"):
        download_archive(root, client=client, protected_paths=())
    absent = tmp_path / "not-initialized"
    with pytest.raises(AcquisitionError, match="existing initialized archive"):
        download_archive(absent, client=client, protected_paths=(protected,))
    assert not absent.exists()
    assert transport.calls == []


def test_explicit_excluded_filing_is_rejected(tmp_path: Path) -> None:
    filing = {
        "cik10": "0000000001",
        "accession": "0000000001-24-000001",
        "form": "10-K",
        "primary": "annual.htm",
        "scope_status": "excluded",
    }
    root, protected, _ = _seed_archive(tmp_path, [filing])
    client, transport = _client({})
    with pytest.raises(AcquisitionError, match="excluded from acquisition scope"):
        download_archive(
            root,
            client=client,
            protected_paths=(protected,),
            filing_ids=(f"{filing['cik10']}:{filing['accession']}",),
        )
    assert transport.calls == []


def test_corrupt_committed_source_bytes_stop_before_any_acquisition_attempt(tmp_path: Path) -> None:
    root, protected, _, _ = _single_ten_k(tmp_path)
    snapshot = read_snapshot(root)
    source_ref = snapshot.raw_objects[0]
    (root / source_ref.path).write_bytes(b"tampered committed bytes")
    client, transport = _client({})

    with pytest.raises(ArchiveCorruptionError):
        download_archive(root, client=client, protected_paths=(protected,))

    assert transport.calls == []


def test_6k_selected_financial_attachment_publishes_evidence_for_chosen_scope(
    tmp_path: Path,
) -> None:
    filing = {
        "cik10": "0000000002",
        "accession": "0000000002-24-000002",
        "form": "6-K",
        "primary": "current-report.htm",
    }
    root, protected, spec = _seed_archive(tmp_path, [filing])
    routes = _routes_for_fixture("six_k", filing["cik10"], filing["accession"])
    client, transport = _client(routes)
    identity = f"{filing['cik10']}:{filing['accession']}"

    result = download_archive(root, client=client, protected_paths=(protected,))

    row = _table_rows(result.snapshot, "filings")[0]
    docs = _table_rows(result.snapshot, "documents")
    assert row["scope_status"] == "included"
    assert row["inventory_status"] == "known"
    assert row["raw_coverage_status"] == "partial"
    assert result.selection_statuses[identity] == "needs_review"
    evidence = json.loads(row["scope_evidence_json"])
    assert evidence["policy"] == "sec-metadata-financial-v1"
    assert evidence["decision"] == "included"
    primary_row = _doc_for(docs, identity, "current-report.htm")
    assert primary_row["fetch_status"] == "present", {
        key: primary_row[key]
        for key in (
            "fetch_status",
            "http_status",
            "diagnostic_code",
            "source_url",
            "source_inventory_locator",
        )
    }
    financial_row = _doc_for(docs, identity, "interim-statements.pdf")
    assert financial_row["fetch_status"] == "present"
    assert evidence["selected_financial_document_ids"] == [financial_row["document_id"]]
    assert evidence["inventory_sha256"] == financial_row["source_inventory_sha256"]
    assert _doc_for(docs, identity, "credit-agreement.htm")["fetch_status"] == "not_requested"
    assert _doc_for(docs, identity, "ex99.3.htm")["selection_status"] == "candidate"
    assert _doc_for(docs, identity, "ex99.3.htm")["fetch_status"] == "not_requested"
    base = _base_url(filing["cik10"], filing["accession"])
    detail_url = base + f"{filing['accession']}-index.html"
    detail_hash = hashlib.sha256(routes[detail_url]).hexdigest()
    raw_hashes = {ref.sha256 for ref in result.snapshot.raw_objects}
    assert evidence["inventory_sha256"] in raw_hashes
    assert detail_hash in raw_hashes
    evidence_sources = evidence["metadata"]["inventory_sources"]
    assert any(
        source["sha256"] == detail_hash and source["source_url"] == detail_url
        for source in evidence_sources
    )
    financial_evidence = next(
        item
        for item in evidence["metadata"]["supporting_documents"]
        if item["document_id"] == financial_row["document_id"]
    )
    assert financial_evidence["description"] == "Interim Condensed Financial Statements"
    assert financial_evidence["document_type"] == "EX-99.1"
    assert financial_evidence["source_url"] == financial_row["source_url"]
    assert any(
        source["sha256"] == detail_hash
        and source["locator"] == "tableFile/document/interim-statements.pdf"
        for source in financial_evidence["source_inventory"]["sources"]
    )
    assert base + "ex99.3.htm" not in transport.calls
    assert base + "credit-agreement.htm" not in transport.calls

    initial_document_sources = {
        item["document_id"]: (item["raw_path"], item["raw_sha256"], item["fetch_status"])
        for item in docs
    }
    initial_raw_objects = {
        (ref.path, ref.sha256, ref.byte_size) for ref in result.snapshot.raw_objects
    }

    # Reproduce a legacy row that was incorrectly marked complete despite the
    # persisted needs_review plan; resume must distrust that stale completion bit.
    legacy_snapshot = read_snapshot(root)
    legacy_filings = _table_rows(legacy_snapshot, "filings")
    legacy_filings[0]["raw_coverage_status"] = "scoped_complete"
    with open_archive(root, spec, protected_paths=(protected,), resume=True) as writer:
        writer.commit(
            tables={"filings": pa.Table.from_pylist(legacy_filings, schema=FILINGS_SCHEMA)},
            raw_objects=legacy_snapshot.raw_objects,
            expected_manifest_version=legacy_snapshot.manifest_version,
        )
    legacy_head = read_snapshot(root)
    assert _table_rows(legacy_head, "filings")[0]["raw_coverage_status"] == "scoped_complete"
    assert (
        acquisition_module._plan_status_from_rows(_table_rows(legacy_head, "documents"))
        == "needs_review"
    )

    second_client, second_transport = _client(routes)
    second = download_archive(
        root,
        client=second_client,
        protected_paths=(protected,),
        filing_ids=(identity,),
    )
    assert second.processed_filing_ids == (identity,)
    assert second.skipped_completed_filing_ids == ()
    assert second.selection_statuses[identity] == "needs_review"
    second_row = _table_rows(second.snapshot, "filings")[0]
    second_docs = _table_rows(second.snapshot, "documents")
    assert second_row["raw_coverage_status"] == "partial"
    assert second_row["scope_evidence_json"] == row["scope_evidence_json"]
    assert {
        item["document_id"]: (item["raw_path"], item["raw_sha256"], item["fetch_status"])
        for item in second_docs
    } == initial_document_sources
    assert {
        (ref.path, ref.sha256, ref.byte_size) for ref in second.snapshot.raw_objects
    } == initial_raw_objects
    selected_body_urls = {
        item["source_url"]
        for item in docs
        if item["selection_status"] == "required" and item["role"] != "filing_index"
    }
    assert not selected_body_urls.intersection(second_transport.calls)
    base = _base_url(filing["cik10"], filing["accession"])
    assert base + "index.json" in second_transport.calls
    assert base + f"{filing['accession']}-index.html" in second_transport.calls


def test_included_6k_scope_evidence_expands_only_additively_on_unchanged_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    filing = {
        "cik10": "0000000002",
        "accession": "0000000002-24-000099",
        "form": "6-K",
        "primary": "cnr-q3.htm",
    }
    root, protected, _ = _seed_archive(tmp_path, [filing])
    identity = f"{filing['cik10']}:{filing['accession']}"
    base = _base_url(filing["cik10"], filing["accession"])
    docs = [
        ("cnr-q3.htm", "6-K", "6-K"),
        ("earnings-news.htm", "CN Q3 2024 Earnings News Release", "EX-99.1"),
        (
            "interim-statements.htm",
            "CN Q3 2024 Consolidated Financial Statements and Notes Thereto",
            "EX-99.2",
        ),
        ("mda.htm", "CN Q3 2024 Management's Discussion and Analysis", "EX-99.3"),
        ("certificates.htm", "CN Q3 2024 CEO and CFO Certificates", "EX-99.4"),
    ]
    directory_name = (
        f"/Archives/edgar/data/{int(filing['cik10'])}/{filing['accession'].replace('-', '')}/"
    )
    index_body = json.dumps(
        {
            "directory": {
                "name": directory_name,
                "item": [{"name": name, "type": "text.gif", "size": "123"} for name, _, _ in docs],
            }
        },
        sort_keys=True,
    ).encode()
    detail_rows = "".join(
        "<tr>"
        f"<td>{sequence}</td><td>{description}</td>"
        f'<td><a href="{filename}">{filename}</a></td>'
        f"<td>{document_type}</td><td>123</td></tr>"
        for sequence, (filename, description, document_type) in enumerate(docs, start=1)
    )
    detail_body = (
        '<table class="tableFile"><tr><th>Seq</th><th>Description</th>'
        "<th>Document</th><th>Type</th><th>Size</th></tr>" + detail_rows + "</table>"
    ).encode()
    primary_body = (
        b"<html><body><table>"
        b"<tr><td>Ex 99.1</td><td>Earnings News Release</td>"
        b"<td><a href='earnings-news.htm'>Earnings</a></td></tr>"
        b"<tr><td>Ex 99.2</td><td>Unaudited Interim Consolidated "
        b"Financial Statements and Notes Thereto</td>"
        b"<td><a href='interim-statements.htm'>Statements</a></td></tr>"
        b"<tr><td>Ex 99.3</td><td>MD&amp;A</td>"
        b"<td><a href='mda.htm'>Management discussion</a></td></tr>"
        b"<tr><td>Ex 99.4</td><td>CEO and CFO Certificates</td>"
        b"<td><a href='certificates.htm'>Certificates</a></td></tr>"
        b"</table></body></html>"
    )
    body_bytes = {
        "cnr-q3.htm": primary_body,
        "earnings-news.htm": b"Earnings News Release",
        "interim-statements.htm": b"Interim Consolidated Financial Statements",
        "mda.htm": b"Management's Discussion and Analysis",
        "certificates.htm": b"CEO and CFO Certificates",
    }
    routes = {
        base + "index.json": index_body,
        base + f"{filing['accession']}-index.html": detail_body,
        **{base + name: payload for name, payload in body_bytes.items()},
    }

    real_enumerator = acquisition_module.enumerate_documents
    real_refiner = acquisition_module.refine_document_plan

    def historical_selector(*args: Any, **kwargs: Any):
        plan = real_enumerator(*args, **kwargs)
        demoted_names = {"earnings-news.htm", "mda.htm"}
        candidates = tuple(
            replace(
                candidate,
                role="other",
                selected=False,
                selection_status="candidate",
                selection_reason=(
                    "Unknown exhibit retained for review; not selected without clear "
                    "financial description"
                ),
            )
            if candidate.filename in demoted_names
            else candidate
            for candidate in plan.candidates
        )
        old_review_reasons = tuple(
            f"{name} has an exhibit type but no explicit financial or non-financial description"
            for name in sorted(demoted_names)
        )
        return replace(
            plan,
            candidates=candidates,
            selection_status="needs_review",
            reasons=tuple(dict.fromkeys((*plan.reasons, *old_review_reasons))),
        )

    monkeypatch.setattr(acquisition_module, "enumerate_documents", historical_selector)
    monkeypatch.setattr(
        acquisition_module, "refine_document_plan", lambda plan, *, primary_response: plan
    )
    first_client, _ = _client(routes)
    first = download_archive(
        root, client=first_client, protected_paths=(protected,), filing_ids=(identity,)
    )
    first_row = _table_rows(first.snapshot, "filings")[0]
    first_docs = _table_rows(first.snapshot, "documents")
    first_evidence = json.loads(first_row["scope_evidence_json"])
    assert first.selection_statuses[identity] == "needs_review"
    assert first_row["scope_status"] == "included"
    assert first_row["raw_coverage_status"] == "partial"
    assert first_evidence["selected_financial_document_ids"] == [
        _doc_for(first_docs, identity, "interim-statements.htm")["document_id"]
    ]
    source_before = {
        row["document_id"]: (
            row["source_url"],
            row["raw_path"],
            row["raw_sha256"],
            row["byte_size"],
        )
        for row in first_docs
    }
    raw_refs_before = {(ref.path, ref.sha256, ref.byte_size) for ref in first.snapshot.raw_objects}

    monkeypatch.setattr(acquisition_module, "enumerate_documents", real_enumerator)
    monkeypatch.setattr(acquisition_module, "refine_document_plan", real_refiner)
    second_client, second_transport = _client(routes)
    second = download_archive(
        root, client=second_client, protected_paths=(protected,), filing_ids=(identity,)
    )
    second_row = _table_rows(second.snapshot, "filings")[0]
    second_docs = _table_rows(second.snapshot, "documents")
    second_evidence = json.loads(second_row["scope_evidence_json"])
    first_ids = set(first_evidence["selected_financial_document_ids"])
    second_ids = set(second_evidence["selected_financial_document_ids"])
    assert second_ids == {
        _doc_for(second_docs, identity, name)["document_id"]
        for name in ("earnings-news.htm", "interim-statements.htm", "mda.htm")
    }
    assert first_ids < second_ids
    assert second.selection_statuses[identity] == "selected_financial"
    assert second_row["scope_status"] == "included"
    assert second_row["raw_coverage_status"] == "scoped_complete"
    assert _doc_for(second_docs, identity, "certificates.htm")["selection_status"] == "out_of_scope"
    supporting = {
        item["filename"]: item for item in second_evidence["metadata"]["supporting_documents"]
    }
    expected_names = {"earnings-news.htm", "interim-statements.htm", "mda.htm"}
    for filename in expected_names:
        proof = supporting[filename]["primary_exhibit_evidence"]
        assert proof["linked_href"] == filename
        assert proof["primary_source"]["source_url"] == base + "cnr-q3.htm"
        assert (
            proof["primary_source"]["source_sha256"]
            == _doc_for(second_docs, identity, "cnr-q3.htm")["raw_sha256"]
        )
        assert (
            proof["accession_index_source"]["source_sha256"]
            == hashlib.sha256(index_body).hexdigest()
        )
        assert proof["phrase"] in {
            "earnings news release",
            "financial statements",
            "management's discussion and analysis",
        }
    for row in second_docs:
        prior = source_before[row["document_id"]]
        assert row["source_url"] == prior[0]
        if prior[1] is not None:
            assert (row["raw_path"], row["raw_sha256"], row["byte_size"]) == prior[1:]
    assert {
        (ref.path, ref.sha256, ref.byte_size) for ref in second.snapshot.raw_objects
    } >= raw_refs_before
    assert second_transport.calls.count(base + "cnr-q3.htm") == 0
    assert second_transport.calls.count(base + "interim-statements.htm") == 0
    assert second_transport.calls.count(base + "earnings-news.htm") == 1
    assert second_transport.calls.count(base + "mda.htm") == 1
    assert first.snapshot.snapshot_id != second.snapshot.snapshot_id
    assert (
        _table_rows(first.snapshot, "filings")[0]["scope_evidence_json"]
        == first_row["scope_evidence_json"]
    )
    assert (root / "snapshots" / first.snapshot.snapshot_id / "manifest.json").is_file()


def test_real_tsm_primary_cross_reference_enriches_6k_scope_and_fetches_only_match(
    tmp_path: Path,
) -> None:
    filing = {
        "cik10": "0001046179",
        "accession": "0001046179-24-000061",
        "form": "6-K",
        "primary": "tsm-fsx20240515x6k.htm",
    }
    root, protected, _ = _seed_archive(tmp_path, [filing])
    routes = _routes_for_fixture("six_k_primary_exhibit", filing["cik10"], filing["accession"])
    client, transport = _client(routes)
    identity = f"{filing['cik10']}:{filing['accession']}"
    base = _base_url(filing["cik10"], filing["accession"])
    primary_url = base + filing["primary"]
    exhibit_url = base + "a0515.htm"

    result = download_archive(
        root,
        client=client,
        protected_paths=(protected,),
        filing_ids=(identity,),
    )

    filing_row = _table_rows(result.snapshot, "filings")[0]
    docs = _table_rows(result.snapshot, "documents")
    primary = _doc_for(docs, identity, filing["primary"])
    exhibit = _doc_for(docs, identity, "a0515.htm")
    assert filing_row["scope_status"] == "included"
    assert filing_row["raw_coverage_status"] == "scoped_complete"
    assert result.selection_statuses[identity] == "selected_financial"
    assert primary["fetch_status"] == "present"
    assert (
        primary["raw_sha256"] == "6f77f2b931c16b1f54a22606439072eb6333a1119dcdec5b91d2b60d0e745820"
    )
    assert exhibit["role"] == "exhibit"
    assert exhibit["selection_status"] == "required"
    assert exhibit["fetch_status"] == "present"
    detail_index = _doc_for(docs, identity, "0001046179-24-000061-index.html")
    assert detail_index["role"] == "filing_index"
    assert detail_index["selection_status"] == "required"
    detail_inventory = json.loads(detail_index["source_inventory_locator"])
    assert detail_inventory["directory_item"]["selection_status"] == "out_of_scope"
    assert (
        _doc_for(docs, identity, "0001046179-24-000061-index-headers.html")["selection_status"]
        == "out_of_scope"
    )
    assert (
        _doc_for(docs, identity, "0001046179-24-000061.txt")["selection_status"] == "out_of_scope"
    )
    assert _doc_for(docs, identity, "image.jpg")["selection_status"] == "out_of_scope"
    evidence = json.loads(filing_row["scope_evidence_json"])
    assert evidence["policy"] == "sec-metadata-financial-v1"
    assert evidence["selected_financial_document_ids"] == [exhibit["document_id"]]
    financial = evidence["metadata"]["supporting_documents"][0]
    primary_proof = financial["primary_exhibit_evidence"]
    assert primary_proof["rule_version"] == "primary-exhibit-title-v1"
    assert primary_proof["primary_source"]["source_url"] == primary_url
    assert primary_proof["primary_source"]["source_sha256"] == primary["raw_sha256"]
    assert primary_proof["description"].startswith("Consolidated Financial Statements")
    assert any(
        item["source_url"] == primary_url and item["sha256"] == primary["raw_sha256"]
        for item in evidence["metadata"]["inventory_sources"]
    )
    assert transport.calls == [
        base + "index.json",
        base + f"{filing['accession']}-index.html",
        primary_url,
        exhibit_url,
    ]
    assert transport.calls.count(primary_url) == 1
    assert result.acquired_document_count == 2


def test_real_tsm_selected_exhibit_404_stays_required_partial_without_fake_hash(
    tmp_path: Path,
) -> None:
    filing = {
        "cik10": "0001046179",
        "accession": "0001046179-24-000061",
        "form": "6-K",
        "primary": "tsm-fsx20240515x6k.htm",
    }
    root, protected, _ = _seed_archive(tmp_path, [filing])
    base = _base_url(filing["cik10"], filing["accession"])
    exhibit_url = base + "a0515.htm"
    routes = _routes_for_fixture(
        "six_k_primary_exhibit",
        filing["cik10"],
        filing["accession"],
        overrides={exhibit_url: (404, b"not found", "text/plain")},
    )
    client, transport = _client(routes)
    identity = f"{filing['cik10']}:{filing['accession']}"

    result = download_archive(
        root,
        client=client,
        protected_paths=(protected,),
        filing_ids=(identity,),
    )

    filing_row = _table_rows(result.snapshot, "filings")[0]
    docs = _table_rows(result.snapshot, "documents")
    exhibit = _doc_for(docs, identity, "a0515.htm")
    primary = _doc_for(docs, identity, filing["primary"])
    assert filing_row["scope_status"] == "included"
    assert filing_row["scope_evidence_json"] is not None
    assert filing_row["raw_coverage_status"] == "partial"
    assert exhibit["selection_status"] == "required"
    assert exhibit["fetch_status"] == "unavailable"
    assert exhibit["http_status"] == 404
    assert (exhibit["raw_sha256"], exhibit["raw_path"], exhibit["byte_size"]) == (
        None,
        None,
        None,
    )
    assert primary["fetch_status"] == "present"
    assert transport.calls.count(exhibit_url) == 1


def test_40f_cover_primary_selects_audited_financial_exhibit_and_reaches_raw_scope_complete(
    tmp_path: Path,
) -> None:
    filing = {
        "cik10": "0001234567",
        "accession": "0001234567-24-000002",
        "form": "40-F",
        "primary": "issuer-2023.htm",
    }
    root, protected, _ = _seed_archive(tmp_path, [filing])
    routes = _routes_for_fixture("forty_f_cover_exhibit", filing["cik10"], filing["accession"])
    client, transport = _client(routes)
    identity = f"{filing['cik10']}:{filing['accession']}"
    base = _base_url(filing["cik10"], filing["accession"])

    result = download_archive(
        root,
        client=client,
        protected_paths=(protected,),
        filing_ids=(identity,),
    )

    filing_row = _table_rows(result.snapshot, "filings")[0]
    docs = _table_rows(result.snapshot, "documents")
    assert result.selection_statuses[identity] == "selected_financial"
    assert filing_row["raw_coverage_status"] == "scoped_complete"
    assert _doc_for(docs, identity, filing["primary"])["fetch_status"] == "present"
    statements = _doc_for(docs, identity, "financial-statements.htm")
    assert statements["role"] == "exhibit"
    assert statements["selection_status"] == "required"
    assert statements["fetch_status"] == "present"
    assert _doc_for(docs, identity, "credit-agreement.htm")["selection_status"] == "out_of_scope"
    assert transport.calls == [
        base + "index.json",
        base + f"{filing['accession']}-index.html",
        base + filing["primary"],
        base + "financial-statements.htm",
    ]


def test_existing_6k_candidate_reuses_primary_cas_bytes_on_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    filing = {
        "cik10": "0001046179",
        "accession": "0001046179-24-000061",
        "form": "6-K",
        "primary": "tsm-fsx20240515x6k.htm",
    }
    root, protected, _ = _seed_archive(tmp_path, [filing])
    identity = f"{filing['cik10']}:{filing['accession']}"
    base = _base_url(filing["cik10"], filing["accession"])
    exhibit_url = base + "a0515.htm"
    first_routes = _routes_for_fixture(
        "six_k_primary_exhibit",
        filing["cik10"],
        filing["accession"],
        overrides={exhibit_url: (404, b"not found", "text/plain")},
    )
    original_refiner = acquisition_module.refine_document_plan
    monkeypatch.setattr(
        acquisition_module, "refine_document_plan", lambda plan, *, primary_response: plan
    )
    first_client, first_transport = _client(first_routes)
    first = download_archive(
        root,
        client=first_client,
        protected_paths=(protected,),
        filing_ids=(identity,),
    )
    first_row = _table_rows(first.snapshot, "filings")[0]
    first_docs = _table_rows(first.snapshot, "documents")
    primary_before = _doc_for(first_docs, identity, filing["primary"])
    exhibit_before = _doc_for(first_docs, identity, "a0515.htm")
    assert first_row["scope_status"] == "candidate"
    assert first_row["raw_coverage_status"] == "partial"
    assert primary_before["fetch_status"] == "present"
    assert exhibit_before["selection_status"] == "candidate"
    assert exhibit_before["fetch_status"] == "not_requested"
    assert exhibit_before["raw_sha256"] is None
    assert exhibit_before["byte_size"] is None
    assert first_transport.calls.count(base + filing["primary"]) == 1
    assert exhibit_url not in first_transport.calls

    monkeypatch.setattr(acquisition_module, "refine_document_plan", original_refiner)
    retry_routes = _routes_for_fixture(
        "six_k_primary_exhibit", filing["cik10"], filing["accession"]
    )
    retry_client, retry_transport = _client(retry_routes)
    retried = download_archive(
        root,
        client=retry_client,
        protected_paths=(protected,),
        filing_ids=(identity,),
    )
    retry_row = _table_rows(retried.snapshot, "filings")[0]
    retry_docs = _table_rows(retried.snapshot, "documents")
    primary_after = _doc_for(retry_docs, identity, filing["primary"])
    exhibit_after = _doc_for(retry_docs, identity, "a0515.htm")
    assert retry_row["scope_status"] == "included"
    assert retry_row["raw_coverage_status"] == "scoped_complete"
    assert primary_after["raw_sha256"] == primary_before["raw_sha256"]
    assert exhibit_after["fetch_status"] == "present"
    assert exhibit_after["raw_sha256"] is not None
    assert exhibit_after["byte_size"] == len(retry_routes[exhibit_url])
    assert retry_transport.calls == [
        base + "index.json",
        base + f"{filing['accession']}-index.html",
        exhibit_url,
    ]
    assert base + filing["primary"] not in retry_transport.calls
    assert retried.acquired_document_count == 1


def test_6k_primary_failure_does_not_claim_financial_scope_or_successful_bytes(
    tmp_path: Path,
) -> None:
    filing = {
        "cik10": "0001046179",
        "accession": "0001046179-24-000061",
        "form": "6-K",
        "primary": "tsm-fsx20240515x6k.htm",
    }
    root, protected, _ = _seed_archive(tmp_path, [filing])
    identity = f"{filing['cik10']}:{filing['accession']}"
    base = _base_url(filing["cik10"], filing["accession"])
    primary_url = base + filing["primary"]
    routes = _routes_for_fixture(
        "six_k_primary_exhibit",
        filing["cik10"],
        filing["accession"],
        overrides={primary_url: (404, b"not found", "text/plain")},
    )
    client, transport = _client(routes)

    result = download_archive(
        root,
        client=client,
        protected_paths=(protected,),
        filing_ids=(identity,),
    )

    filing_row = _table_rows(result.snapshot, "filings")[0]
    docs = _table_rows(result.snapshot, "documents")
    primary = _doc_for(docs, identity, filing["primary"])
    exhibit = _doc_for(docs, identity, "a0515.htm")
    assert result.selection_statuses[identity] == "needs_review"
    assert filing_row["scope_status"] == "candidate"
    assert filing_row["scope_evidence_json"] is None
    assert filing_row["raw_coverage_status"] == "partial"
    assert primary["fetch_status"] == "unavailable"
    assert primary["http_status"] == 404
    assert (primary["raw_sha256"], primary["raw_path"], primary["byte_size"]) == (
        None,
        None,
        None,
    )
    assert exhibit["selection_status"] == "candidate"
    assert exhibit["fetch_status"] == "not_requested"
    assert primary_url in transport.calls
    assert base + "a0515.htm" not in transport.calls


def test_included_6k_payload_fetch_error_keeps_partial_scope_evidence(tmp_path: Path) -> None:
    filing = {
        "cik10": "0000000002",
        "accession": "0000000002-24-000002",
        "form": "6-K",
        "primary": "current-report.htm",
    }
    root, protected, _ = _seed_archive(tmp_path, [filing])
    base = _base_url(filing["cik10"], filing["accession"])
    routes = _routes_for_fixture(
        "six_k",
        filing["cik10"],
        filing["accession"],
        overrides={base + "interim-statements.pdf": (404, b"not found", "text/plain")},
    )
    client, transport = _client(routes)
    identity = f"{filing['cik10']}:{filing['accession']}"

    result = download_archive(root, client=client, protected_paths=(protected,))

    row = _table_rows(result.snapshot, "filings")[0]
    docs = _table_rows(result.snapshot, "documents")
    financial = _doc_for(docs, identity, "interim-statements.pdf")
    evidence = json.loads(row["scope_evidence_json"])
    assert row["scope_status"] == "included"
    assert row["raw_coverage_status"] == "partial"
    assert evidence["selected_financial_document_ids"] == [financial["document_id"]]
    assert evidence["inventory_sha256"] == financial["source_inventory_sha256"]
    assert financial["selection_status"] == "required"
    assert financial["fetch_status"] == "unavailable"
    assert (financial["raw_sha256"], financial["raw_path"], financial["byte_size"]) == (
        None,
        None,
        None,
    )
    assert _doc_for(docs, identity, "current-report.htm")["fetch_status"] == "present"
    assert base + "credit-agreement.htm" not in transport.calls
    assert base + "ex99.3.htm" not in transport.calls


def test_included_6k_scope_evidence_metadata_drift_conflicts_without_rewriting_head(
    tmp_path: Path,
) -> None:
    filing = {
        "cik10": "0000000002",
        "accession": "0000000002-24-000002",
        "form": "6-K",
        "primary": "current-report.htm",
    }
    root, protected, _ = _seed_archive(tmp_path, [filing])
    base = _base_url(filing["cik10"], filing["accession"])
    first_routes = _routes_for_fixture(
        "six_k",
        filing["cik10"],
        filing["accession"],
        overrides={base + "interim-statements.pdf": (404, b"not found", "text/plain")},
    )
    first_client, _ = _client(first_routes)
    identity = f"{filing['cik10']}:{filing['accession']}"
    first = download_archive(root, client=first_client, protected_paths=(protected,))
    original_row = _table_rows(first.snapshot, "filings")[0]
    original_evidence = original_row["scope_evidence_json"]

    changed_detail = (
        (FIXTURE_ROOT / "six_k" / "detail.html")
        .read_text(encoding="utf-8")
        .replace("Interim Condensed Financial Statements", "Annual Financial Report")
    )
    detail_url = base + f"{filing['accession']}-index.html"
    changed_routes = _routes_for_fixture(
        "six_k",
        filing["cik10"],
        filing["accession"],
        overrides={
            detail_url: changed_detail.encode(),
            base + "interim-statements.pdf": (404, b"not found", "text/plain"),
        },
    )
    changed_client, _ = _client(changed_routes)
    with pytest.raises(AcquisitionConflictError, match="scope evidence conflicts"):
        download_archive(
            root,
            client=changed_client,
            protected_paths=(protected,),
            filing_ids=(identity,),
        )

    unchanged = read_snapshot(root)
    assert unchanged.snapshot_id == first.snapshot.snapshot_id
    assert _table_rows(unchanged, "filings")[0]["scope_evidence_json"] == original_evidence


def test_included_6k_additive_scope_rejects_tampered_prior_source_hash_without_commit(
    tmp_path: Path,
) -> None:
    filing = {
        "cik10": "0000000002",
        "accession": "0000000002-24-000002",
        "form": "6-K",
        "primary": "current-report.htm",
    }
    root, protected, spec = _seed_archive(tmp_path, [filing])
    routes = _routes_for_fixture("six_k", filing["cik10"], filing["accession"])
    client, _ = _client(routes)
    identity = f"{filing['cik10']}:{filing['accession']}"
    first = download_archive(root, client=client, protected_paths=(protected,))

    head = read_snapshot(root)
    filing_rows = _table_rows(head, "filings")
    original_evidence = json.loads(filing_rows[0]["scope_evidence_json"])
    original_evidence["metadata"]["inventory_sources"][0]["sha256"] = "0" * 64
    tampered_evidence = json.dumps(
        original_evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    filing_rows[0]["scope_evidence_json"] = tampered_evidence
    with open_archive(root, spec, protected_paths=(protected,), resume=True) as writer:
        tampered = writer.commit(
            tables={"filings": pa.Table.from_pylist(filing_rows, schema=FILINGS_SCHEMA)},
            raw_objects=head.raw_objects,
            expected_manifest_version=head.manifest_version,
        )

    retry_client, _ = _client(routes)
    with pytest.raises(AcquisitionConflictError, match="source-identical additive refinement"):
        download_archive(
            root,
            client=retry_client,
            protected_paths=(protected,),
            filing_ids=(identity,),
        )
    after = read_snapshot(root)
    assert after.snapshot_id == tampered.snapshot_id != first.snapshot.snapshot_id
    assert _table_rows(after, "filings")[0]["scope_evidence_json"] == tampered_evidence


def test_nonfinancial_6k_preserves_index_and_candidates_without_fetching_body(
    tmp_path: Path,
) -> None:
    filing = {
        "cik10": "0000000002",
        "accession": "0000000002-24-000003",
        "form": "6-K",
        "primary": "current-report.htm",
    }
    root, protected, _ = _seed_archive(tmp_path, [filing])
    routes = _routes_for_fixture("six_k_nonfinancial", filing["cik10"], filing["accession"])
    client, transport = _client(routes)
    identity = f"{filing['cik10']}:{filing['accession']}"

    result = download_archive(root, client=client, protected_paths=(protected,))

    filing_row = _table_rows(result.snapshot, "filings")[0]
    docs = _table_rows(result.snapshot, "documents")
    assert result.selection_statuses[identity] == "non_financial"
    assert filing_row["scope_status"] == "excluded"
    assert filing_row["inventory_status"] == "known"
    assert filing_row["raw_coverage_status"] == "not_attempted"
    evidence = json.loads(filing_row["scope_evidence_json"])
    assert evidence["decision"] == "excluded"
    assert evidence["selected_financial_document_ids"] == []
    assert evidence["reasons"]
    detail_url = (
        _base_url(filing["cik10"], filing["accession"]) + f"{filing['accession']}-index.html"
    )
    detail_hash = hashlib.sha256(routes[detail_url]).hexdigest()
    raw_hashes = {ref.sha256 for ref in result.snapshot.raw_objects}
    assert evidence["inventory_sha256"] in raw_hashes
    assert detail_hash in raw_hashes
    assert any(
        source["sha256"] == detail_hash and source["source_url"] == detail_url
        for source in evidence["metadata"]["inventory_sources"]
    )
    assert _doc_for(docs, identity, "current-report.htm")["selection_status"] == "out_of_scope"
    assert _doc_for(docs, identity, "current-report.htm")["fetch_status"] == "not_requested"
    assert len([row for row in docs if row["role"] == "filing_index"]) == 2
    assert len(transport.calls) == 2


def test_generic_press_release_and_conference_call_6k_remains_candidate_without_fetching_exhibits(
    tmp_path: Path,
) -> None:
    filing = {
        "cik10": "0000000002",
        "accession": "0000000002-24-000003",
        "form": "6-K",
        "primary": "current-report.htm",
    }
    root, protected, _ = _seed_archive(tmp_path, [filing])
    base = _base_url(filing["cik10"], filing["accession"])
    detail = (FIXTURE_ROOT / "six_k_nonfinancial" / "detail.html").read_text(encoding="utf-8")
    detail = detail.replace("Credit Agreement", "Press Release").replace(
        "Change in Directors", "Conference Call"
    )
    routes = _routes_for_fixture(
        "six_k_nonfinancial",
        filing["cik10"],
        filing["accession"],
        overrides={
            base + f"{filing['accession']}-index.html": detail.encode(),
            base + "current-report.htm": b"primary current report",
        },
    )
    client, transport = _client(routes)
    identity = f"{filing['cik10']}:{filing['accession']}"

    result = download_archive(root, client=client, protected_paths=(protected,))

    row = _table_rows(result.snapshot, "filings")[0]
    docs = _table_rows(result.snapshot, "documents")
    assert result.selection_statuses[identity] == "needs_review"
    assert row["scope_status"] == "candidate"
    assert row["scope_evidence_json"] is None
    assert row["raw_coverage_status"] == "partial"
    assert _doc_for(docs, identity, "current-report.htm")["fetch_status"] == "present"
    assert _doc_for(docs, identity, "credit-agreement.htm")["selection_status"] == "candidate"
    assert _doc_for(docs, identity, "presentation.pdf")["selection_status"] == "candidate"
    assert base + "credit-agreement.htm" not in transport.calls
    assert base + "presentation.pdf" not in transport.calls
    assert transport.calls == [
        base + "index.json",
        base + f"{filing['accession']}-index.html",
        base + "current-report.htm",
    ]


def test_404_selected_document_persists_unavailable_then_retries_only_that_url(
    tmp_path: Path,
) -> None:
    root, protected, _, filing = _single_ten_k(tmp_path)
    base = _base_url(filing["cik10"], filing["accession"])
    first_routes = _routes_for_fixture(
        "ten_k",
        filing["cik10"],
        filing["accession"],
        overrides={base + "annual.htm": (404, b"not found", "text/plain")},
    )
    first_client, first_transport = _client(first_routes)
    identity = f"{filing['cik10']}:{filing['accession']}"

    first = download_archive(root, client=first_client, protected_paths=(protected,))

    first_docs = _table_rows(first.snapshot, "documents")
    missing = _doc_for(first_docs, identity, "annual.htm")
    assert missing["fetch_status"] == "unavailable"
    assert missing["http_status"] == 404
    assert missing["diagnostic_code"] == "sec_not_found"
    assert (missing["raw_sha256"], missing["raw_path"], missing["byte_size"]) == (None, None, None)
    assert _doc_for(first_docs, identity, "issuer.xsd")["fetch_status"] == "present"
    assert _table_rows(first.snapshot, "filings")[0]["raw_coverage_status"] == "partial"

    retry_routes = _routes_for_fixture(
        "ten_k",
        filing["cik10"],
        filing["accession"],
        overrides={base + "annual.htm": b"annual body restored"},
    )
    retry_client, retry_transport = _client(retry_routes)
    retry = download_archive(
        root,
        client=retry_client,
        protected_paths=(protected,),
        filing_ids=(identity,),
    )
    retried_docs = _table_rows(retry.snapshot, "documents")
    assert _doc_for(retried_docs, identity, "annual.htm")["fetch_status"] == "present"
    assert (
        _doc_for(retried_docs, identity, "issuer.xsd")["raw_sha256"]
        == _doc_for(first_docs, identity, "issuer.xsd")["raw_sha256"]
    )
    assert retry_transport.calls == [
        base + "index.json",
        base + f"{filing['accession']}-index.html",
        base + "annual.htm",
    ]
    assert base + "issuer.xsd" not in retry_transport.calls
    assert base + "issuer_cal.xml" not in retry_transport.calls
    assert _table_rows(retry.snapshot, "filings")[0]["raw_coverage_status"] == "scoped_complete"
    assert first_transport.calls.count(base + "annual.htm") == 1


def test_partial_resume_rejects_changed_xbrl_type_without_unselecting_committed_row(
    tmp_path: Path,
) -> None:
    root, protected, _, filing = _single_ten_k(tmp_path)
    base = _base_url(filing["cik10"], filing["accession"])
    initial_routes = _routes_for_fixture(
        "ten_k",
        filing["cik10"],
        filing["accession"],
        overrides={base + "annual.htm": (404, b"not found", "text/plain")},
    )
    initial_client, _ = _client(initial_routes)
    first = download_archive(root, client=initial_client, protected_paths=(protected,))
    detail = (
        (FIXTURE_ROOT / "ten_k" / "detail.html")
        .read_text(encoding="utf-8")
        .replace("<td>EX-101.SCH</td>", "<td>EX-99.9</td>")
    )
    refreshed_routes = _routes_for_fixture(
        "ten_k",
        filing["cik10"],
        filing["accession"],
        overrides={base + f"{filing['accession']}-index.html": detail.encode()},
    )
    refreshed_client, refreshed_transport = _client(refreshed_routes)
    identity = f"{filing['cik10']}:{filing['accession']}"

    with pytest.raises(AcquisitionConflictError, match="document type conflicts"):
        download_archive(
            root,
            client=refreshed_client,
            protected_paths=(protected,),
            filing_ids=(identity,),
        )

    assert read_snapshot(root).snapshot_id == first.snapshot.snapshot_id
    assert refreshed_transport.calls == [
        base + "index.json",
        base + f"{filing['accession']}-index.html",
    ]


def test_503_selected_document_persists_sanitized_error_without_success_fields(
    tmp_path: Path,
) -> None:
    root, protected, _, filing = _single_ten_k(tmp_path)
    base = _base_url(filing["cik10"], filing["accession"])
    routes = _routes_for_fixture(
        "ten_k",
        filing["cik10"],
        filing["accession"],
        overrides={base + "annual.htm": (503, b"secret upstream body", "text/plain")},
    )
    client, _ = _client(routes)
    identity = f"{filing['cik10']}:{filing['accession']}"

    result = download_archive(root, client=client, protected_paths=(protected,))

    row = _doc_for(_table_rows(result.snapshot, "documents"), identity, "annual.htm")
    assert row["fetch_status"] == "error"
    assert row["http_status"] == 503
    assert row["diagnostic_code"] == "http_503"
    assert (row["raw_sha256"], row["raw_path"], row["byte_size"]) == (None, None, None)
    assert b"secret upstream body" not in (root / "ledger.jsonl").read_bytes()


def test_repeated_explicit_acquisition_is_idempotent_after_all_required_bytes_verified(
    tmp_path: Path,
) -> None:
    root, protected, _, filing = _single_ten_k(tmp_path)
    routes = _routes_for_fixture("ten_k", filing["cik10"], filing["accession"])
    first_client, first_transport = _client(routes)
    identity = f"{filing['cik10']}:{filing['accession']}"
    first = download_archive(
        root,
        client=first_client,
        protected_paths=(protected,),
        filing_ids=(identity,),
    )
    old_ledger = (root / "ledger.jsonl").read_bytes()

    second_client, second_transport = _client({})
    second = download_archive(
        root,
        client=second_client,
        protected_paths=(protected,),
        filing_ids=(identity,),
    )

    assert second.snapshot.snapshot_id == first.snapshot.snapshot_id
    assert second.snapshot.manifest_version == first.snapshot.manifest_version
    assert second.skipped_completed_filing_ids == (identity,)
    assert second_client is not None and not second_transport.calls
    assert first_transport.calls
    assert (root / "ledger.jsonl").read_bytes() == old_ledger


def test_materialize_package_copies_relative_xbrl_files_and_resumes_owned_workspace(
    tmp_path: Path,
) -> None:
    root, protected, _, filing = _single_ten_k(tmp_path)
    client, _ = _client(_routes_for_fixture("ten_k", filing["cik10"], filing["accession"]))
    result = download_archive(root, client=client, protected_paths=(protected,))
    identity = f"{filing['cik10']}:{filing['accession']}"
    before = read_snapshot(root)
    workspace = tmp_path / "parser-workspace"

    package = materialize_package(root, identity, workspace_root=workspace)

    assert package.scope_status == "included"
    assert package.raw_coverage_status == "scoped_complete"
    assert set(entry.original_filename for entry in package.entrypoints.values()) == {
        "annual.htm",
        "issuer.xsd",
        "issuer_cal.xml",
    }
    assert 'href="issuer.xsd"' in (workspace / "annual.htm").read_text(encoding="utf-8")
    assert 'schemaLocation="issuer_cal.xml"' in (workspace / "issuer.xsd").read_text(
        encoding="utf-8"
    )
    docs = _table_rows(before, "documents")
    for source_url, entry in package.entrypoints.items():
        row = next(row for row in docs if row["source_url"] == source_url)
        raw = (root / row["raw_path"]).read_bytes()
        assert entry.local_path.read_bytes() == raw
        assert hashlib.sha256(raw).hexdigest() == entry.raw_sha256
        assert not entry.local_path.samefile(root / row["raw_path"])
    second = materialize_package(root, identity, workspace_root=workspace)
    assert second.entrypoints == package.entrypoints
    assert second.snapshot_id == result.snapshot.snapshot_id
    after = read_snapshot(root)
    assert after.snapshot_id == before.snapshot_id


def test_materialize_package_rejects_foreign_nonempty_or_symlink_workspace(tmp_path: Path) -> None:
    root, protected, _, filing = _single_ten_k(tmp_path)
    client, _ = _client(_routes_for_fixture("ten_k", filing["cik10"], filing["accession"]))
    download_archive(root, client=client, protected_paths=(protected,))
    identity = f"{filing['cik10']}:{filing['accession']}"

    foreign = tmp_path / "foreign-workspace"
    foreign.mkdir()
    (foreign / "keep.txt").write_text("do not overwrite", encoding="utf-8")
    with pytest.raises(PackageConflictError, match="ownership marker"):
        materialize_package(root, identity, workspace_root=foreign)
    assert (foreign / "keep.txt").read_text(encoding="utf-8") == "do not overwrite"

    target = tmp_path / "target-workspace"
    target.mkdir()
    symlink = tmp_path / "workspace-link"
    symlink.symlink_to(target, target_is_directory=True)
    with pytest.raises(PackagePreparationError, match="symlink"):
        materialize_package(root, identity, workspace_root=symlink)


def _malformed_package_archive(
    tmp_path: Path,
    *,
    filenames: tuple[str, ...],
) -> tuple[Path, str]:
    root = tmp_path / "archive"
    protected = tmp_path / "protected-inputs"
    protected.mkdir(exist_ok=True)
    spec = RunSpec(input_fingerprint="malformed-package-fixture")
    cik10 = "0000000001"
    accession = "0000000001-24-000001"
    identity = f"{cik10}:{accession}"
    submission_ref = None
    document_rows: list[dict[str, Any]] = []
    all_refs: dict[str, Any] = {}
    with open_archive(root, spec, protected_paths=(protected,)) as writer:
        submission_ref = writer.put_raw_bytes(b"submission")
        all_refs[submission_ref.path] = submission_ref
        inventory_ref = writer.put_raw_bytes(b"{}")
        all_refs[inventory_ref.path] = inventory_ref
        raw_values = [writer.put_raw_bytes(f"content-{i}".encode()) for i in range(len(filenames))]
        all_refs.update({ref.path: ref for ref in raw_values})
        filing = _filing_row(
            cik10,
            accession,
            "10-K",
            filenames[0],
            submission_ref,
            filed_date=date(2024, 1, 1),
        )
        filing["inventory_status"] = "known"
        filing["raw_coverage_status"] = "scoped_complete"
        for index, filename in enumerate(filenames):
            url = f"https://www.sec.gov/Archives/edgar/data/1/000000000124000001/file{index}.htm"
            role = "primary" if index == 0 else "schema"
            document_rows.append(
                {
                    "document_id": document_id(identity, url),
                    "filing_id": identity,
                    "original_filename": filename,
                    "source_url": url,
                    "role": role,
                    "selection_status": "required",
                    "fetch_status": "present",
                    "source_inventory_sha256": inventory_ref.sha256,
                    "source_inventory_locator": "{}",
                    "raw_sha256": raw_values[index].sha256,
                    "raw_path": raw_values[index].path,
                    "byte_size": raw_values[index].byte_size,
                    "media_type": "text/html",
                    "fetched_at_utc": datetime.now(timezone.utc),
                    "http_status": 200,
                    "diagnostic_code": None,
                    "fact_extraction_status": "not_attempted",
                    "text_extraction_status": "not_attempted",
                }
            )
        writer.commit(
            tables={
                "filings": pa.Table.from_pylist([filing], schema=FILINGS_SCHEMA),
                "documents": pa.Table.from_pylist(document_rows, schema=DOCUMENTS_SCHEMA),
            },
            raw_objects=tuple(all_refs.values()),
            expected_manifest_version=0,
        )
    return root, identity


def test_materialize_package_rejects_path_traversal_and_filename_conflicts(tmp_path: Path) -> None:
    root, identity = _malformed_package_archive(tmp_path, filenames=("../escape.htm",))
    with pytest.raises(PackagePreparationError, match="unsafe original filename"):
        materialize_package(root, identity, workspace_root=tmp_path / "bad-name")

    duplicate_root = tmp_path / "duplicate-case"
    duplicate_root.mkdir()
    # Use a distinct helper parent so the archive's root path and protected path are fresh.
    root2, identity2 = _malformed_package_archive(
        duplicate_root, filenames=("same.htm", "same.htm")
    )
    with pytest.raises(PackageConflictError, match="conflict on one original filename"):
        materialize_package(root2, identity2, workspace_root=duplicate_root / "duplicate-package")
