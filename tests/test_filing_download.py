"""Offline acquisition and conservative SEC document-selection tests."""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import pytest

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from filings.document_selection import (
    DocumentCandidate,
    FilingIndexParseError,
    FilingInventoryError,
    FilingInventoryMismatchError,
    UnsupportedFilingFormError,
    enumerate_documents,
    refine_document_plan,
)
from filings.download import UnselectedDocumentError, fetch_document
from filings.sec_client import FetchResponse, SecClient

FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "filings_download"
VALID_AGENT = "Quant Research analyst@acme-financials.com"


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def monotonic(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds


class FakeResponse:
    def __init__(self, body: bytes, url: str) -> None:
        self.status = 200
        self.headers = {"Content-Type": "application/octet-stream"}
        self.body = body
        self.url = url
        self.position = 0
        self.closed = False

    def geturl(self) -> str:
        return self.url

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = len(self.body) - self.position
        end = min(self.position + size, len(self.body))
        value = self.body[self.position : end]
        self.position = end
        return value

    def close(self) -> None:
        self.closed = True


class FixtureTransport:
    def __init__(self, content_by_url: dict[str, bytes]) -> None:
        self.content_by_url = content_by_url
        self.calls: list[str] = []

    def __call__(self, url: str, headers: Any, timeout: float) -> FakeResponse:
        self.calls.append(url)
        if url not in self.content_by_url:
            raise AssertionError(f"unexpected mocked URL: {url}")
        return FakeResponse(self.content_by_url[url], url)


def _directory_url(cik: str, accession: str) -> str:
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/"


def _client_for_fixture(
    case: str,
    cik: str,
    accession: str,
    *,
    extras: dict[str, bytes] | None = None,
) -> tuple[SecClient, FixtureTransport]:
    fixture = FIXTURE_ROOT / case
    base = _directory_url(cik, accession)
    content = {
        base + "index.json": (fixture / "index.json").read_bytes(),
        base + f"{accession}-index.html": (fixture / "detail.html").read_bytes(),
    }
    content.update(extras or {})
    transport = FixtureTransport(content)
    clock = FakeClock()
    client = SecClient(
        VALID_AGENT,
        transport=transport,
        clock=clock.monotonic,
        sleep=clock.sleep,
    )
    return client, transport


def _candidate(plan: Any, filename: str) -> DocumentCandidate:
    return next(item for item in plan.candidates if item.filename == filename)


def _metadata_only_client(
    cik: str, accession: str, documents: list[tuple[str, str, str]]
) -> tuple[SecClient, FixtureTransport]:
    base = _directory_url(cik, accession)
    index = {
        "directory": {
            "name": urlsplit(base).path,
            "item": [
                {"name": filename, "type": "text.gif", "size": "123"}
                for filename, _description, _document_type in documents
            ],
        }
    }
    detail_rows = "".join(
        "<tr>"
        f"<td>{sequence}</td><td>{description}</td>"
        f'<td><a href="{quote(filename, safe="-._~")}">{filename}</a></td>'
        f"<td>{document_type}</td><td>123</td></tr>"
        for sequence, (filename, description, document_type) in enumerate(documents, start=1)
    )
    detail = (
        '<table class="tableFile"><tr><th>Seq</th><th>Description</th>'
        "<th>Document</th><th>Type</th><th>Size</th></tr>" + detail_rows + "</table>"
    )
    return _client_for_fixture(
        "six_k_contracts",
        cik,
        accession,
        extras={
            base + "index.json": json.dumps(index, ensure_ascii=False).encode("utf-8"),
            base + f"{accession}-index.html": detail.encode("utf-8"),
        },
    )


def _primary_response(plan: Any, filename: str, body: bytes) -> FetchResponse:
    primary = _candidate(plan, filename)
    return FetchResponse(
        body=body,
        request_url=primary.url,
        final_url=primary.url,
        content_type="text/html",
        fetched_at_utc=datetime.now(timezone.utc),
        attempts=1,
        status=200,
    )


def test_domestic_10k_selects_primary_and_xbrl_but_not_all_attachments() -> None:
    cik = "0000320193"
    accession = "0000320193-24-000069"
    client, transport = _client_for_fixture("domestic", cik, accession)

    plan = enumerate_documents(
        cik,
        accession,
        primary_document="aapl-20240928.htm",
        form="10-K",
        client=client,
    )

    assert plan.selection_status == "selected_financial"
    assert [response.request_url for response in plan.inventory_responses] == transport.calls
    assert len(plan.inventory_responses) == 2
    selected = {
        candidate.filename: candidate.role for candidate in plan.candidates if candidate.selected
    }
    assert selected == {
        "aapl-20240928.htm": "primary",
        "aapl-20240928.xsd": "schema",
        "aapl-20240928_ins.xml": "xbrl_instance",
        "aapl-20240928_cal.xml": "linkbase",
        "aapl-20240928_def.xml": "linkbase",
        "aapl-20240928_lab.xml": "linkbase",
        "aapl-20240928_pre.xml": "linkbase",
    }
    assert {item.filename for item in plan.candidates} == {
        "aapl-20240928.htm",
        "aapl-20240928.xsd",
        "aapl-20240928_ins.xml",
        "aapl-20240928_cal.xml",
        "aapl-20240928_def.xml",
        "aapl-20240928_lab.xml",
        "aapl-20240928_pre.xml",
        "logo.jpg",
        "full-submission.txt",
        "press-release.pdf",
    }
    assert not _candidate(plan, "logo.jpg").selected
    assert not _candidate(plan, "full-submission.txt").selected
    assert not _candidate(plan, "press-release.pdf").selected
    assert _candidate(plan, "aapl-20240928.xsd").inventory_metadata["size"] == "44500"
    assert all(len(candidate.document_id) == 64 for candidate in plan.candidates)
    assert all(
        candidate.filing_id == "0000320193:0000320193-24-000069" for candidate in plan.candidates
    )
    assert (
        plan.candidates
        == enumerate_documents(
            cik,
            accession,
            primary_document="aapl-20240928.htm",
            form="10-K",
            client=_client_for_fixture("domestic", cik, accession)[0],
        ).candidates
    )


def test_domestic_primary_scope_is_not_downgraded_by_generic_ex31_attachment() -> None:
    cik = "0000320193"
    accession = "0000320193-24-000069"
    fixture = FIXTURE_ROOT / "domestic"
    index = json.loads((fixture / "index.json").read_text(encoding="utf-8"))
    index["directory"]["item"].append({"name": "ex31.1.htm", "type": "EX-31.1", "size": "1200"})
    detail = (
        (fixture / "detail.html")
        .read_text(encoding="utf-8")
        .replace(
            "</table>",
            "<tr><td>33</td><td>Officer Certification</td>"
            '<td><a href="ex31.1.htm">ex31.1.htm</a></td><td>EX-31.1</td><td>1200</td></tr>\n'
            "</table>",
        )
    )
    base = _directory_url(cik, accession)
    client, _ = _client_for_fixture(
        "domestic",
        cik,
        accession,
        extras={
            base + "index.json": json.dumps(index).encode(),
            base + f"{accession}-index.html": detail.encode(),
        },
    )

    plan = enumerate_documents(
        cik,
        accession,
        primary_document="aapl-20240928.htm",
        form="10-K",
        client=client,
    )

    assert plan.selection_status == "selected_financial"
    exhibit = _candidate(plan, "ex31.1.htm")
    assert not exhibit.selected
    assert exhibit.selection_status == "candidate"


def test_foreign_20f_accepts_agent_accession_prefix_not_equal_to_issuer_cik() -> None:
    cik = "1067983"
    accession = "0000950170-24-012345"
    assert accession[:10] != cik
    client, _ = _client_for_fixture("foreign", cik, accession)

    plan = enumerate_documents(
        cik,
        accession,
        primary_document="nvo-20231231.htm",
        form="20-F",
        client=client,
    )

    assert plan.selection_status == "selected_financial"
    annual_report = _candidate(plan, "annual-report-2023.pdf")
    assert annual_report.selected
    assert annual_report.role == "exhibit"
    assert annual_report.document_type == "EX-99.1"
    assert annual_report.description == "Annual report including financial statements"
    assert annual_report.sequence == "2"


def test_40f_selects_explicit_financial_statements_not_credit_agreement() -> None:
    cik = "1234567"
    accession = "0001234567-24-000001"
    client, _ = _client_for_fixture("forty_f", cik, accession)

    plan = enumerate_documents(
        cik,
        accession,
        primary_document="issuer-2023.htm",
        form="40-F",
        client=client,
    )

    assert plan.selection_status == "selected_financial"
    assert _candidate(plan, "issuer-2023.htm").role == "primary"
    assert _candidate(plan, "financial-statements.pdf").selected
    assert _candidate(plan, "financial-statements.pdf").role == "exhibit"
    assert not _candidate(plan, "credit-agreement.htm").selected
    assert not _candidate(plan, "logo.jpg").selected


def test_6k_mixed_content_selects_explicit_financial_body_and_flags_unknown_exhibit() -> None:
    cik = "7654321"
    accession = "0007654321-24-000004"
    client, _ = _client_for_fixture("six_k_mixed", cik, accession)

    plan = enumerate_documents(
        cik,
        accession,
        primary_document="current-report.htm",
        form="6-K",
        client=client,
    )

    assert plan.selection_status == "needs_review"
    assert _candidate(plan, "current-report.htm").selected
    assert _candidate(plan, "interim-statements.pdf").selected
    assert _candidate(plan, "services-agreement.htm").selected is False
    assert _candidate(plan, "ex99.3.htm").selected is False
    assert any(
        "no explicit financial or non-financial description" in reason for reason in plan.reasons
    )
    assert "financial" in _candidate(plan, "interim-statements.pdf").selection_reason.casefold()


def test_nonfinancial_6k_is_not_mislabeled_complete_financial_filing() -> None:
    cik = "7654321"
    accession = "0007654321-24-000005"
    client, _ = _client_for_fixture("six_k_contracts", cik, accession)

    plan = enumerate_documents(
        cik,
        accession,
        primary_document="current-report.htm",
        form="6-K",
        client=client,
    )

    assert plan.selection_status == "non_financial"
    assert _candidate(plan, "current-report.htm").selected
    assert not _candidate(plan, "credit-agreement.htm").selected
    assert not _candidate(plan, "presentation.pdf").selected
    assert all(
        candidate.selection_status != "required"
        for candidate in plan.candidates
        if not candidate.selected
    )


@pytest.mark.parametrize(
    "generic_description",
    ["Press Release", "Conference Call", "Investor Presentation"],
)
def test_generic_6k_metadata_is_ambiguous_not_proof_of_nonfinancial(
    generic_description: str,
) -> None:
    cik = "7654321"
    accession = "0007654321-24-000005"
    detail = (FIXTURE_ROOT / "six_k_contracts" / "detail.html").read_text(encoding="utf-8")
    detail = detail.replace("Credit Agreement", generic_description).replace(
        "Change in Directors", generic_description
    )
    base = _directory_url(cik, accession)
    client, _ = _client_for_fixture(
        "six_k_contracts",
        cik,
        accession,
        extras={base + f"{accession}-index.html": detail.encode()},
    )

    plan = enumerate_documents(
        cik,
        accession,
        primary_document="current-report.htm",
        form="6-K",
        client=client,
    )

    assert plan.selection_status == "needs_review"
    assert all(candidate.selection_status != "out_of_scope" for candidate in plan.candidates)
    assert not _candidate(plan, "credit-agreement.htm").selected
    assert not _candidate(plan, "presentation.pdf").selected


def test_6k_primary_is_financial_only_when_sec_metadata_explicitly_says_so() -> None:
    cik = "7654321"
    accession = "0007654321-24-000005"
    html = (
        (FIXTURE_ROOT / "six_k_contracts" / "detail.html")
        .read_text(encoding="utf-8")
        .replace(
            '<tr><td>1</td><td>6-K</td><td><a href="current-report.htm">',
            '<tr><td>1</td><td>Interim Financial Statements</td><td><a href="current-report.htm">',
        )
    )
    base = _directory_url(cik, accession)
    client, _ = _client_for_fixture(
        "six_k_contracts",
        cik,
        accession,
        extras={base + f"{accession}-index.html": html.encode()},
    )

    plan = enumerate_documents(
        cik,
        accession,
        primary_document="current-report.htm",
        form="6-K",
        client=client,
    )

    assert plan.selection_status == "selected_financial"
    assert _candidate(plan, "current-report.htm").role == "primary"
    assert (
        "financial statements" in _candidate(plan, "current-report.htm").selection_reason.casefold()
    )


def test_unknown_99_exhibit_is_retained_not_selected_and_marks_review() -> None:
    cik = "7654321"
    accession = "0007654321-24-000004"
    client, _ = _client_for_fixture("six_k_mixed", cik, accession)

    plan = enumerate_documents(
        cik,
        accession,
        primary_document="current-report.htm",
        form="6-K",
        client=client,
    )

    unknown = _candidate(plan, "ex99.3.htm")
    assert unknown.document_type == "EX-99.3"
    assert unknown.selection_status == "candidate"
    assert "retained for review" in unknown.selection_reason


def test_real_tsm_primary_exhibit_title_promotes_only_the_matching_indexed_attachment() -> None:
    cik = "0001046179"
    accession = "0001046179-24-000061"
    client, transport = _client_for_fixture("six_k_primary_exhibit", cik, accession)
    plan = enumerate_documents(
        cik,
        accession,
        primary_document="tsm-fsx20240515x6k.htm",
        form="6-K",
        client=client,
    )
    assert plan.selection_status == "needs_review"
    assert _candidate(plan, "a0515.htm").selection_status == "candidate"

    primary_bytes = (FIXTURE_ROOT / "six_k_primary_exhibit" / "tsm-fsx20240515x6k.htm").read_bytes()
    refined = refine_document_plan(
        plan,
        primary_response=_primary_response(plan, "tsm-fsx20240515x6k.htm", primary_bytes),
    )

    exhibit = _candidate(refined, "a0515.htm")
    evidence = exhibit.inventory_metadata["primary_exhibit_evidence"]
    assert refined.selection_status == "selected_financial"
    assert refined.reasons == ()
    assert exhibit.selected and exhibit.selection_status == "required"
    assert exhibit.role == "exhibit"
    assert exhibit.description == "EX-99.1"
    assert "description explicitly identifies financial statements" in exhibit.selection_reason
    assert evidence["rule_version"] == "primary-exhibit-title-v1"
    assert evidence["exhibit_number"] == "99.1"
    assert evidence["description"].startswith(
        "Consolidated Financial Statements for the Three Months Ended"
    )
    assert (
        evidence["primary_source"]["source_url"] == _candidate(plan, "tsm-fsx20240515x6k.htm").url
    )
    assert evidence["primary_source"]["source_sha256"] == hashlib.sha256(primary_bytes).hexdigest()
    assert evidence["primary_source"]["source_sha256"] == (
        "6f77f2b931c16b1f54a22606439072eb6333a1119dcdec5b91d2b60d0e745820"
    )
    assert (
        evidence["accession_index_source"]["source_sha256"]
        == hashlib.sha256(
            (FIXTURE_ROOT / "six_k_primary_exhibit" / "index.json").read_bytes()
        ).hexdigest()
    )
    assert evidence["accession_index_source"]["locator"] == "$.directory.item[3]"
    assert evidence["linked_href"] == "a0515.htm"
    assert evidence["matched_document"]["source_url"] == (
        "https://www.sec.gov/Archives/edgar/data/1046179/000104617924000061/a0515.htm"
    )
    assert _candidate(refined, "0001046179-24-000061-index.html").selection_status == "out_of_scope"
    assert (
        _candidate(refined, "0001046179-24-000061-index-headers.html").selection_status
        == "out_of_scope"
    )
    assert _candidate(refined, "0001046179-24-000061.txt").selection_status == "out_of_scope"
    assert _candidate(refined, "image.jpg").selection_status == "out_of_scope"
    assert transport.calls == [
        _directory_url(cik, accession) + "index.json",
        _directory_url(cik, accession) + f"{accession}-index.html",
    ]


def test_cnr_quarterly_exhibits_select_by_metadata_and_href() -> None:
    cik = "0000016868"
    accession = "0000016868-24-000049"
    documents = [
        ("cnr-q3.htm", "6-K", "6-K"),
        ("earnings-news.htm", "CN Q3 2024 Earnings News Release", "EX-99.1"),
        (
            "interim-statements.htm",
            "CN Q3 2024 Consolidated Financial Statements and Notes Thereto",
            "EX-99.2",
        ),
        (
            "mda.htm",
            "CN Q3 2024 Management's Discussion and Analysis",
            "EX-99.3",
        ),
        ("certificates.htm", "CN Q3 2024 CEO and CFO Certificates", "EX-99.4"),
        ("press.htm", "Press Release", "EX-99.5"),
        ("deposit.htm", "Certificate of Deposit", "EX-99.6"),
    ]
    client, transport = _metadata_only_client(cik, accession, documents)
    plan = enumerate_documents(
        cik,
        accession,
        primary_document="cnr-q3.htm",
        form="6-K",
        client=client,
    )

    assert plan.selection_status == "needs_review"
    for filename in ("earnings-news.htm", "interim-statements.htm", "mda.htm"):
        candidate = _candidate(plan, filename)
        assert candidate.role == "exhibit"
        assert candidate.selection_status == "required"
        assert candidate.selected is True
    certificate = _candidate(plan, "certificates.htm")
    assert certificate.selection_status == "out_of_scope"
    assert certificate.selected is False
    for filename in ("press.htm", "deposit.htm"):
        assert _candidate(plan, filename).selection_status == "candidate"
        assert _candidate(plan, filename).selected is False

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
        b"<tr><td>Ex 99.5</td><td>Press Release</td>"
        b"<td><a href='press.htm'>Release</a></td></tr>"
        b"<tr><td>Ex 99.6</td><td>Certificate of Deposit</td>"
        b"<td><a href='deposit.htm'>Deposit</a></td></tr>"
        b"</table></body></html>"
    )
    refined = refine_document_plan(
        plan, primary_response=_primary_response(plan, "cnr-q3.htm", primary_body)
    )

    assert refined.selection_status == "needs_review"
    for filename, exhibit_number in (
        ("earnings-news.htm", "99.1"),
        ("interim-statements.htm", "99.2"),
        ("mda.htm", "99.3"),
    ):
        candidate = _candidate(refined, filename)
        proof = candidate.inventory_metadata["primary_exhibit_evidence"]
        assert candidate.selection_status == "required"
        assert proof["exhibit_number"] == exhibit_number
        assert proof["primary_source"]["source_sha256"] == hashlib.sha256(primary_body).hexdigest()
        assert (
            proof["accession_index_source"]["source_sha256"]
            == hashlib.sha256(
                transport.content_by_url[_directory_url(cik, accession) + "index.json"]
            ).hexdigest()
        )
        assert proof["linked_href"] == filename
    assert _candidate(refined, "certificates.htm").selection_status == "out_of_scope"
    assert _candidate(refined, "press.htm").selection_status == "candidate"
    assert _candidate(refined, "deposit.htm").selection_status == "candidate"
    assert len(transport.calls) == 2


def test_financial_review_metadata_and_aif_boundary() -> None:
    cik = "0001000275"
    accession = "0001000275-23-000001"
    docs = [
        ("cover.htm", "40-F", "40-F"),
        ("review.htm", "EX-2 FINANCIAL REVIEW", "EX-2"),
        ("ratios.htm", "EX-5 RETURN ON EQUITY AND ASSETS RATIOS", "EX-5"),
        ("derived-instance.xml", "EXTRACTED XBRL INSTANCE DOCUMENT", "XML"),
        ("aif.htm", "ANNUAL INFORMATION FORM", "EX-1"),
    ]
    client, transport = _metadata_only_client(cik, accession, docs)
    plan = enumerate_documents(
        cik,
        accession,
        primary_document="cover.htm",
        form="40-F",
        client=client,
    )

    review = _candidate(plan, "review.htm")
    aif = _candidate(plan, "aif.htm")
    assert review.role == "exhibit" and review.selected
    assert review.selection_status == "required"
    assert "financial review" in review.selection_reason.casefold()
    ratios = _candidate(plan, "ratios.htm")
    assert ratios.role == "exhibit" and ratios.selected
    assert ratios.selection_status == "required"
    derived_instance = _candidate(plan, "derived-instance.xml")
    assert derived_instance.role == "xbrl_instance" and derived_instance.selected
    assert aif.selected is False and aif.selection_status == "candidate"
    assert plan.selection_status == "needs_review"
    assert transport.calls == [
        _directory_url(cik, accession) + "index.json",
        _directory_url(cik, accession) + f"{accession}-index.html",
    ]

    old_accession = "0001000275-02-000001"
    aif_only_client, _ = _metadata_only_client(
        cik, old_accession, [("aif.htm", "Annual Information Form", "40-F/A")]
    )
    aif_only = enumerate_documents(
        cik,
        old_accession,
        primary_document="aif.htm",
        form="40-F/A",
        client=aif_only_client,
    )
    assert aif_only.selection_status == "needs_review"
    assert "primary document may be a cover" in " ".join(aif_only.reasons).casefold()


def test_explicit_financial_type_wins_over_xbrl_looking_filename() -> None:
    cik = "7654321"
    accession = "0007654321-24-000098"
    docs = [
        ("current-report.htm", "6-K", "6-K"),
        ("financial-statement_ins.xml", "Q3 2024 Consolidated Financial Statements", "EX-99.1"),
    ]
    client, _ = _metadata_only_client(cik, accession, docs)
    plan = enumerate_documents(
        cik,
        accession,
        primary_document="current-report.htm",
        form="6-K",
        client=client,
    )

    financial = _candidate(plan, "financial-statement_ins.xml")
    assert financial.role == "exhibit"
    assert financial.selected is True
    assert financial.selection_status == "required"
    assert "financial statements" in financial.selection_reason.casefold()


def test_mda_context_and_certificate_of_deposit_boundary() -> None:
    cik = "7654321"
    accession = "0007654321-24-000099"
    docs = [
        ("current-report.htm", "6-K", "6-K"),
        ("governance.htm", "MD&A on the Code of Conduct", "EX-99.1"),
        ("deposit.htm", "Certificate of Deposit", "EX-99.2"),
    ]
    client, _ = _metadata_only_client(cik, accession, docs)
    plan = enumerate_documents(
        cik,
        accession,
        primary_document="current-report.htm",
        form="6-K",
        client=client,
    )

    assert plan.selection_status == "needs_review"
    governance = _candidate(plan, "governance.htm")
    deposit = _candidate(plan, "deposit.htm")
    assert governance.selected is False and governance.selection_status == "out_of_scope"
    assert deposit.selected is False and deposit.selection_status == "candidate"


def test_40f_cover_primary_can_select_only_its_financial_report_exhibit() -> None:
    cik = "1234567"
    accession = "0001234567-24-000002"
    client, _ = _client_for_fixture("forty_f_cover_exhibit", cik, accession)
    plan = enumerate_documents(
        cik,
        accession,
        primary_document="issuer-2023.htm",
        form="40-F",
        client=client,
    )
    assert plan.selection_status == "needs_review"
    assert _candidate(plan, "financial-statements.htm").selection_status == "candidate"

    primary_bytes = (FIXTURE_ROOT / "forty_f_cover_exhibit" / "issuer-2023.htm").read_bytes()
    refined = refine_document_plan(
        plan,
        primary_response=_primary_response(plan, "issuer-2023.htm", primary_bytes),
    )

    statements = _candidate(refined, "financial-statements.htm")
    assert refined.selection_status == "selected_financial"
    assert statements.selected and statements.selection_status == "required"
    assert statements.role == "exhibit"
    assert statements.inventory_metadata["primary_exhibit_evidence"]["exhibit_number"] == "99.1"
    assert not _candidate(refined, "credit-agreement.htm").selected
    assert _candidate(refined, "credit-agreement.htm").selection_status == "out_of_scope"
    assert not _candidate(refined, "logo.jpg").selected


def test_primary_exhibit_without_financial_title_or_unique_link_stays_reviewable() -> None:
    cik = "7654321"
    accession = "0007654321-24-000005"
    client, _ = _client_for_fixture("six_k_contracts", cik, accession)
    plan = enumerate_documents(
        cik,
        accession,
        primary_document="current-report.htm",
        form="6-K",
        client=client,
    )
    ambiguous_body = (
        b"<!doctype html SYSTEM 'https://invalid.example/never-load.dtd'>"
        b"<html><body><script>\n<tr><td>99.1</td><td>Financial Statements</td>"
        b"<a href='presentation.pdf'>fake</a></tr>\n</script>"
        b"<table><tr><td>99.1</td><td>Press Release</td>"
        b"<td><a href='presentation.pdf'>presentation.pdf</a></td></tr></table></body></html>"
    )

    refined = refine_document_plan(
        plan,
        primary_response=_primary_response(plan, "current-report.htm", ambiguous_body),
    )

    assert refined.selection_status == "needs_review"
    assert not _candidate(refined, "presentation.pdf").selected
    assert _candidate(refined, "presentation.pdf").selection_status == "out_of_scope"
    assert refined.reasons

    missing_link_body = (
        b"<html><body><table><tr><td>99.1</td>"
        b"<td>Interim Financial Statements</td></tr></table></body></html>"
    )
    missing_link = refine_document_plan(
        plan,
        primary_response=_primary_response(plan, "current-report.htm", missing_link_body),
    )
    assert missing_link.selection_status == "needs_review"
    assert not _candidate(missing_link, "presentation.pdf").selected


def test_primary_financial_exhibit_foreign_or_conflicting_href_fails_closed() -> None:
    cik = "7654321"
    accession = "0007654321-24-000005"
    client, _ = _client_for_fixture("six_k_contracts", cik, accession)
    plan = enumerate_documents(
        cik,
        accession,
        primary_document="current-report.htm",
        form="6-K",
        client=client,
    )
    invalid_hrefs = (
        "https://evil.example/presentation.pdf",
        "/Archives/edgar/data/9999999/000765432124000005/presentation.pdf",
        "../presentation.pdf",
        "%2e%2e%2fpresentation.pdf",
    )
    for href in invalid_hrefs:
        foreign = (
            "<html><body><table><tr><td>99.1</td>"
            "<td>Consolidated Financial Statements and Auditors' Review Report</td>"
            f"<td><a href='{href}'>presentation.pdf</a></td>"
            "</tr></table></body></html>"
        ).encode()
        with pytest.raises(FilingInventoryMismatchError, match="foreign, unsafe"):
            refine_document_plan(
                plan,
                primary_response=_primary_response(plan, "current-report.htm", foreign),
            )

    conflict_accession = "0007654321-24-000004"
    conflict_client, _ = _client_for_fixture("six_k_mixed", cik, conflict_accession)
    conflict_plan = enumerate_documents(
        cik,
        conflict_accession,
        primary_document="current-report.htm",
        form="6-K",
        client=conflict_client,
    )
    conflicting = (
        b"<html><body><table>"
        b"<tr><td>99.1</td><td>Consolidated Financial Statements</td>"
        b"<td><a href='ex99.3.htm'>presentation</a></td></tr>"
        b"<tr><td>99.2</td><td>Audited Annual Report</td>"
        b"<td><a href='ex99.3.htm'>same file</a></td></tr>"
        b"</table></body></html>"
    )
    with pytest.raises(FilingInventoryMismatchError, match="duplicate conflicting"):
        refine_document_plan(
            conflict_plan,
            primary_response=_primary_response(conflict_plan, "current-report.htm", conflicting),
        )


def test_accession_directory_mismatch_fails_closed() -> None:
    cik = "0000320193"
    accession = "0000320193-24-000069"
    fixture = FIXTURE_ROOT / "domestic"
    wrong_index = json.loads((fixture / "index.json").read_text(encoding="utf-8"))
    wrong_index["directory"]["name"] = "/Archives/edgar/data/9999999/000032019324000069/"
    base = _directory_url(cik, accession)
    client, _ = _client_for_fixture(
        "domestic",
        cik,
        accession,
        extras={base + "index.json": json.dumps(wrong_index).encode()},
    )

    with pytest.raises(FilingInventoryMismatchError, match="does not match"):
        enumerate_documents(
            cik,
            accession,
            primary_document="aapl-20240928.htm",
            form="10-K",
            client=client,
        )


def test_accession_folder_mismatch_fails_closed() -> None:
    cik = "0000320193"
    accession = "0000320193-24-000069"
    wrong_index = json.loads((FIXTURE_ROOT / "domestic" / "index.json").read_text())
    wrong_index["directory"]["name"] = "/Archives/edgar/data/320193/000032019324000070/"
    base = _directory_url(cik, accession)
    client, _ = _client_for_fixture(
        "domestic",
        cik,
        accession,
        extras={base + "index.json": json.dumps(wrong_index).encode()},
    )

    with pytest.raises(FilingInventoryMismatchError):
        enumerate_documents(
            cik,
            accession,
            primary_document="aapl-20240928.htm",
            form="10-K",
            client=client,
        )


@pytest.mark.parametrize(
    "bad_name",
    [
        "../secret.xml",
        "%2e%2e%2fsecret.xml",
        "%252e%252e%252fsecret.xml",
        "subdir/file.xml",
        "\\absolute.xml",
        "bad\x00name.xml",
        "bad..name.xml",
        "%zz.xml",
    ],
)
def test_unsafe_or_traversal_inventory_names_are_rejected(bad_name: str) -> None:
    cik = "0000320193"
    accession = "0000320193-24-000069"
    index = json.loads((FIXTURE_ROOT / "domestic" / "index.json").read_text())
    index["directory"]["item"].append({"name": bad_name, "type": "text"})
    base = _directory_url(cik, accession)
    client, _ = _client_for_fixture(
        "domestic",
        cik,
        accession,
        extras={base + "index.json": json.dumps(index).encode()},
    )

    with pytest.raises(FilingInventoryError):
        enumerate_documents(
            cik,
            accession,
            primary_document="aapl-20240928.htm",
            form="10-K",
            client=client,
        )


def test_conflicting_duplicate_index_filename_fails_closed() -> None:
    cik = "0000320193"
    accession = "0000320193-24-000069"
    index = json.loads((FIXTURE_ROOT / "domestic" / "index.json").read_text())
    index["directory"]["item"].append(
        {"name": "aapl-20240928.htm", "type": "text", "size": "different"}
    )
    base = _directory_url(cik, accession)
    client, _ = _client_for_fixture(
        "domestic",
        cik,
        accession,
        extras={base + "index.json": json.dumps(index).encode()},
    )

    with pytest.raises(FilingInventoryMismatchError, match="conflicting metadata"):
        enumerate_documents(
            cik,
            accession,
            primary_document="aapl-20240928.htm",
            form="10-K",
            client=client,
        )


def test_malformed_detail_html_without_sec_table_is_explicit_error() -> None:
    cik = "0000320193"
    accession = "0000320193-24-000069"
    base = _directory_url(cik, accession)
    client, _ = _client_for_fixture(
        "domestic",
        cik,
        accession,
        extras={base + f"{accession}-index.html": b"<html><body>broken table"},
    )

    with pytest.raises(FilingIndexParseError, match="no SEC document table"):
        enumerate_documents(
            cik,
            accession,
            primary_document="aapl-20240928.htm",
            form="10-K",
            client=client,
        )


def test_detail_link_outside_sec_hosts_is_rejected() -> None:
    cik = "0000320193"
    accession = "0000320193-24-000069"
    html = (
        (FIXTURE_ROOT / "domestic" / "detail.html")
        .read_text(encoding="utf-8")
        .replace(
            'href="aapl-20240928.htm"',
            'href="https://evil.example/aapl-20240928.htm"',
        )
    )
    base = _directory_url(cik, accession)
    client, _ = _client_for_fixture(
        "domestic",
        cik,
        accession,
        extras={base + f"{accession}-index.html": html.encode()},
    )

    with pytest.raises(FilingIndexParseError, match="outside SEC hosts"):
        enumerate_documents(
            cik,
            accession,
            primary_document="aapl-20240928.htm",
            form="10-K",
            client=client,
        )


def test_detail_link_that_leaves_accession_directory_is_rejected() -> None:
    cik = "0000320193"
    accession = "0000320193-24-000069"
    html = (
        (FIXTURE_ROOT / "domestic" / "detail.html")
        .read_text(encoding="utf-8")
        .replace(
            'href="aapl-20240928.htm"',
            'href="/Archives/edgar/data/9999999/000032019324000069/aapl-20240928.htm"',
        )
    )
    base = _directory_url(cik, accession)
    client, _ = _client_for_fixture(
        "domestic",
        cik,
        accession,
        extras={base + f"{accession}-index.html": html.encode()},
    )

    with pytest.raises(FilingIndexParseError, match="leaves its accession directory"):
        enumerate_documents(
            cik,
            accession,
            primary_document="aapl-20240928.htm",
            form="10-K",
            client=client,
        )


def test_unicode_and_apostrophe_filenames_are_quoted_once_and_deterministic() -> None:
    cik = "9999999"
    accession = "0009999999-24-000001"
    base = _directory_url(cik, accession)
    filename = "résumé's.htm"
    index = {
        "directory": {
            "name": f"/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/",
            "item": [{"name": filename, "type": "text", "size": "12"}],
        }
    }
    encoded_filename = quote(filename, safe="-._~")
    detail = (
        '<table class="tableFile"><tr><th>Seq</th><th>Description</th>'
        "<th>Document</th><th>Type</th></tr>"
        f'<tr><td>1</td><td>6-K</td><td><a href="{encoded_filename}">{filename}</a></td>'
        "<td>6-K</td></tr></table>"
    ).encode()
    client, transport = _client_for_fixture(
        "six_k_contracts",
        cik,
        accession,
        extras={
            base + "index.json": json.dumps(index, ensure_ascii=False).encode(),
            base + f"{accession}-index.html": detail,
        },
    )

    plan = enumerate_documents(
        cik,
        accession,
        primary_document=filename,
        form="6-K",
        client=client,
    )
    item = plan.candidates[0]
    assert item.url.endswith("r%C3%A9sum%C3%A9%27s.htm")
    assert "%2527" not in item.url
    assert len(item.document_id) == 64
    assert (
        item.document_id
        == _candidate(
            enumerate_documents(
                cik,
                accession,
                primary_document=filename,
                form="6-K",
                client=_client_for_fixture(
                    "six_k_contracts",
                    cik,
                    accession,
                    extras={
                        base + "index.json": json.dumps(index, ensure_ascii=False).encode(),
                        base + f"{accession}-index.html": detail,
                    },
                )[0],
            ),
            filename,
        ).document_id
    )
    assert len(transport.calls) == 2


def test_accession_prefix_is_not_used_as_issuer_cik_validation() -> None:
    cik = "123456789"
    accession = "9999999999-99-999999"
    index = json.loads((FIXTURE_ROOT / "foreign" / "index.json").read_text())
    index["directory"]["name"] = f"/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/"
    item = index["directory"]["item"][0]
    item["name"] = "issuer-report.htm"
    index["directory"]["item"] = [item]
    detail = (
        b'<table class="tableFile"><tr><th>Seq</th><th>Description</th>'
        b"<th>Document</th><th>Type</th></tr>"
        b'<tr><td>1</td><td>20-F</td><td><a href="issuer-report.htm">'
        b"issuer-report.htm</a></td><td>20-F</td></tr>"
        b"</table>"
    )
    base = _directory_url(cik, accession)
    client, _ = _client_for_fixture(
        "foreign",
        cik,
        accession,
        extras={
            base + "index.json": json.dumps(index).encode(),
            base + f"{accession}-index.html": detail,
        },
    )

    plan = enumerate_documents(
        cik,
        accession,
        primary_document="issuer-report.htm",
        form="20-F",
        client=client,
    )
    assert plan.selection_status == "selected_financial"
    assert plan.candidates[0].filing_id == f"{cik.zfill(10)}:{accession}"


def test_unsupported_8k_and_invalid_identifiers_fail_before_transport() -> None:
    client, transport = _client_for_fixture("domestic", "0000320193", "0000320193-24-000069")
    with pytest.raises(UnsupportedFilingFormError):
        enumerate_documents(
            "0000320193",
            "0000320193-24-000069",
            primary_document="aapl-20240928.htm",
            form="8-K",
            client=client,
        )
    with pytest.raises(ValueError, match="CIK"):
        enumerate_documents(
            "12345678901",
            "0000320193-24-000069",
            primary_document="aapl-20240928.htm",
            form="10-K",
            client=client,
        )
    with pytest.raises(ValueError, match="accession_number"):
        enumerate_documents(
            "0000320193",
            "0000320193/24/000069",
            primary_document="aapl-20240928.htm",
            form="10-K",
            client=client,
        )
    assert transport.calls == []


def test_missing_primary_is_not_guessed_from_the_directory() -> None:
    cik = "0000320193"
    accession = "0000320193-24-000069"
    client, _ = _client_for_fixture("domestic", cik, accession)
    with pytest.raises(FilingInventoryMismatchError, match="primary document is missing"):
        enumerate_documents(
            cik,
            accession,
            primary_document="wrong-primary.htm",
            form="10-K",
            client=client,
        )


def test_fetch_document_requests_only_a_selected_candidate_and_writes_nothing(
    tmp_path: Path,
) -> None:
    cik = "0000320193"
    accession = "0000320193-24-000069"
    body_url = _directory_url(cik, accession) + "aapl-20240928.htm"
    raw_body = b"selected filing bytes"
    client, transport = _client_for_fixture("domestic", cik, accession, extras={body_url: raw_body})
    plan = enumerate_documents(
        cik,
        accession,
        primary_document="aapl-20240928.htm",
        form="10-K",
        client=client,
    )
    primary = _candidate(plan, "aapl-20240928.htm")

    fetched = fetch_document(primary, client)

    assert isinstance(fetched, FetchResponse)
    assert fetched.body == raw_body
    assert fetched.status == 200
    assert fetched.fetched_at_utc.tzinfo is timezone.utc
    assert transport.calls.count(body_url) == 1
    assert not list(tmp_path.iterdir())
    assert not _candidate(plan, "logo.jpg").selected
    with pytest.raises(UnselectedDocumentError):
        fetch_document(_candidate(plan, "logo.jpg"), client)
    with pytest.raises(UnselectedDocumentError):
        fetch_document(_candidate(plan, "full-submission.txt"), client)


def test_candidate_url_revalidated_by_fetch_layer() -> None:
    candidate = DocumentCandidate(
        filename="file.htm",
        url="https://evil.example/file.htm",
        document_type=None,
        description=None,
        sequence=None,
        role="primary",
        selected=True,
        selection_reason="Declared primary filing document",
        filing_id="0000000001:0000000001-24-000001",
        document_id="0" * 64,
        selection_status="required",
    )
    client, transport = _client_for_fixture("domestic", "0000320193", "0000320193-24-000069")

    with pytest.raises(ValueError, match="host"):
        fetch_document(candidate, client)
    assert transport.calls == []
