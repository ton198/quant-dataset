from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pyarrow.parquet as pq

from filings.content_parse import ContentResult
from filings.report_samples import _filing_worker, validate_report_samples
from filings.sec_client import FetchResponse, SecNotFoundError


def test_successful_mock_filing_commits_sample_and_removes_raw(tmp_path: Path, monkeypatch) -> None:
    identity = "0000000001:0000000001-24-000001"
    filing = SimpleNamespace(
        cik10="0000000001",
        accession_number="0000000001-24-000001",
        form="10-K",
        filed_date="2024-01-01",
        report_date=None,
        primary_document="report.htm",
    )
    body = b"<html><body><h1>Item 1. Business</h1><p>Revenue was $1,234.</p></body></html>"
    url = "https://www.sec.gov/Archives/edgar/data/1/000000000124000001/report.htm"
    response = FetchResponse(body, url, url, "text/html", datetime.now(timezone.utc), 1, 200)
    candidate = SimpleNamespace(
        role="primary", filename="report.htm", selected=True, selection_status="required", url=url
    )
    plan = SimpleNamespace(
        candidates=(candidate,), selection_status="selected_financial", reasons=()
    )
    monkeypatch.setattr("filings.report_samples.enumerate_documents", lambda *args, **kwargs: plan)
    monkeypatch.setattr("filings.report_samples.fetch_document", lambda *args, **kwargs: response)
    monkeypatch.setattr(
        "filings.report_samples.parse_content",
        lambda *args, **kwargs: ContentResult(
            documents=[{"status": "full", "table_count": 0}],
            blocks=[{"text": "Revenue was $1,234.", "block_kind": "text"}],
        ),
    )
    work = tmp_path / "work"
    work.mkdir()
    committed = tmp_path / "committed"
    sample, diagnostic = _filing_worker(
        filing, "Research contact@acme.com", work, client=object(), committed_root=committed
    )
    assert sample and sample["sample_id"] == identity
    assert sample["blocks"][0]["text"] == "Revenue was $1,234."
    assert diagnostic["status"] == "emitted"
    assert not list(work.glob("filing-*"))


def test_missing_primary_falls_back_to_sec_full_submission(tmp_path: Path, monkeypatch) -> None:
    filing = SimpleNamespace(
        cik10="0000000001",
        accession_number="0000000001-24-000001",
        form="10-K",
        filed_date="2024-01-01",
        report_date=None,
        primary_document="report.htm",
    )
    body = (
        b"<DOCUMENT>\n<TYPE>10-K\n<FILENAME>report.htm\n<TEXT>\n"
        b"<html><body>Report body.</body></html>\n</TEXT>\n</DOCUMENT>\n"
    )
    url = "https://www.sec.gov/Archives/edgar/data/1/000000000124000001/0000000001-24-000001.txt"
    response = FetchResponse(body, url, url, "text/plain", datetime.now(timezone.utc), 1, 200)
    candidate = SimpleNamespace(
        role="primary", filename="report.htm", selected=True, selection_status="required", url=url
    )
    plan = SimpleNamespace(
        candidates=(candidate,), selection_status="selected_financial", reasons=()
    )
    monkeypatch.setattr("filings.report_samples.enumerate_documents", lambda *a, **k: plan)
    monkeypatch.setattr(
        "filings.report_samples.fetch_document",
        lambda *a, **k: (_ for _ in ()).throw(SecNotFoundError(1)),
    )
    monkeypatch.setattr(
        "filings.report_samples.parse_content",
        lambda raw, **kwargs: ContentResult(
            documents=[{"status": "full", "table_count": 0}],
            blocks=[{"text": "Report body.", "block_kind": "text"}],
        ),
    )

    class FallbackClient:
        def get(self, requested_url):
            assert requested_url == url
            return response

    work = tmp_path / "work"
    work.mkdir()
    sample, diagnostic = _filing_worker(
        filing,
        "Research analyst@acme.com",
        work,
        client=FallbackClient(),
        committed_root=tmp_path / "committed",
    )
    assert sample is not None
    assert diagnostic["sources"][0]["role"] == "full_submission"
    assert diagnostic["status"] == "emitted"
    assert not list(work.glob("filing-*"))


def test_transport_drives_primary_404_fallback_build_and_resume(
    tmp_path: Path, monkeypatch
) -> None:
    from filings.report_samples import build_report_samples
    from filings.sec_client import SecClient

    accession = "0000000001-24-000001"
    accn_path = accession.replace("-", "")
    index_url = f"https://www.sec.gov/Archives/edgar/data/1/{accn_path}/index.json"
    detail_url = f"https://www.sec.gov/Archives/edgar/data/1/{accn_path}/{accession}-index.html"
    primary_url = f"https://www.sec.gov/Archives/edgar/data/1/{accn_path}/report.htm"
    fallback_url = f"https://www.sec.gov/Archives/edgar/data/1/{accn_path}/{accession}.txt"
    primary_body = (
        b"<DOCUMENT>\n<TYPE>10-K\n<FILENAME>report.htm\n<TEXT>\n"
        b"<html><body><p>Revenue $1,234</p></body></html>\n</TEXT>\n</DOCUMENT>\n"
    )
    responses = {
        index_url: (
            json.dumps(
                {
                    "directory": {
                        "name": f"/Archives/edgar/data/1/{accn_path}",
                        "item": [{"name": "report.htm", "type": ""}],
                    }
                }
            ).encode(),
            200,
        ),
        detail_url: (
            b'<table class="tableFile"><tr><td>1</td><td>10-K</td>'
            b'<td><a href="report.htm">report.htm</a></td>'
            b"<td>Annual Report</td></tr></table>",
            200,
        ),
        primary_url: (b"not found", 404),
        fallback_url: (primary_body, 200),
    }
    requests = []

    class Response:
        headers = {"Content-Type": "application/octet-stream"}

        def __init__(self, body, status):
            self.body, self.offset, self.status = body, 0, status

        def read(self, size=-1):
            chunk = (
                self.body[self.offset : self.offset + size]
                if size >= 0
                else self.body[self.offset :]
            )
            self.offset += len(chunk)
            return chunk

        def close(self):
            pass

    def transport(url, headers, timeout):
        requests.append(url)
        if url not in responses:
            raise AssertionError(f"unexpected offline request: {url}")
        body, status = responses[url]
        return Response(body, status)

    cache = tmp_path / "cache"
    catalog = cache / "data/raw/sec/financials/submissions/CIK0000000001-submissions-1.json"
    catalog.parent.mkdir(parents=True)
    catalog.write_text(
        json.dumps(
            {
                "cik": 1,
                "filings": {
                    "recent": {
                        "accessionNumber": [accession],
                        "filingDate": ["2024-01-01"],
                        "form": ["10-K"],
                        "reportDate": ["2023-12-31"],
                        "primaryDocument": ["report.htm"],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    (cache / "manifest.json").write_text("{}", encoding="utf-8")
    identity = "0000000001:0000000001-24-000001"
    monkeypatch.setattr(
        "filings.report_samples.scan_cached_filings",
        lambda *a, **k: SimpleNamespace(
            filings=(
                SimpleNamespace(
                    cik10="0000000001",
                    accession_number=accession,
                    form="10-K",
                    filed_date="2024-01-01",
                    report_date="2023-12-31",
                    primary_document="report.htm",
                ),
            ),
        ),
    )
    out, work = tmp_path / "out", tmp_path / "work"
    client = SecClient("Research analyst@acme.com", transport=transport, interval=0.2)
    result = build_report_samples(
        cache, out, work_dir=work, user_agent="Research analyst@acme.com", client=client
    )
    assert result["filings"] == 1 and result["samples"] == 1, (
        out / "diagnostics.jsonl"
    ).read_text() + repr(requests)
    assert requests == [index_url, detail_url, primary_url, fallback_url]
    assert requests.count(primary_url) == 1
    validated = validate_report_samples(out, expected_ids={identity})
    assert validated["status_counts"]["emitted"] == 1
    assert json.loads((out / "diagnostics.jsonl").read_text())["coverage_status"] == "partial"
    sample = pq.read_table(next(out.rglob("*.parquet"))).to_pylist()[0]
    assert sample["sample_id"] == identity and sample["blocks"][0]["text"] == "Revenue $1,234"
    assert not list(work.glob("filing-*"))
    assert hashlib.sha256(primary_body).hexdigest() not in json.dumps(sample, default=str)

    resumed = build_report_samples(
        cache,
        out,
        work_dir=work,
        resume=True,
        user_agent="Research analyst@acme.com",
        client=client,
    )
    assert resumed["samples"] == 1
    assert requests == [index_url, detail_url, primary_url, fallback_url]


def test_concurrent_real_workers_use_shared_mock_sec_client(tmp_path: Path, monkeypatch) -> None:
    from filings.report_samples import build_report_samples
    from filings.sec_client import SecClient

    filings = tuple(
        SimpleNamespace(
            cik10=f"{index:010d}",
            accession_number=f"{index:010d}-24-000001",
            form="10-K",
            filed_date="2024-01-01",
            report_date=None,
            primary_document=f"report{index}.htm",
        )
        for index in range(1, 3)
    )
    monkeypatch.setattr(
        "filings.report_samples.scan_cached_filings",
        lambda *a, **k: SimpleNamespace(filings=filings),
    )
    requested = []

    class Response:
        headers = {"Content-Type": "application/octet-stream"}
        status = 200

        def __init__(self, body):
            self.body, self.offset = body, 0

        def read(self, size=-1):
            chunk = (
                self.body[self.offset : self.offset + size]
                if size >= 0
                else self.body[self.offset :]
            )
            self.offset += len(chunk)
            return chunk

        def close(self):
            pass

    def transport(url, headers, timeout):
        requested.append(url)
        if url.endswith("index.json"):
            accession_path = url.split("/data/")[1].split("/index.json")[0]
            cik, accession_digits = accession_path.split("/")
            index = int(cik)
            name = f"report{index}.htm"
            body = json.dumps(
                {
                    "directory": {
                        "name": f"/Archives/edgar/data/{cik}/{accession_digits}",
                        "item": [{"name": name, "type": ""}],
                    }
                }
            ).encode()
        elif url.endswith("-index.html"):
            cik = url.split("/data/")[1].split("/")[0]
            name = f"report{int(cik)}.htm"
            body = (
                f'<table class="tableFile"><tr><td>1</td><td>10-K</td><td><a href="{name}">'
                f"{name}</a></td><td>Annual Report</td></tr></table>"
            ).encode()
        else:
            body = b"<html><body><p>Annual report contents.</p></body></html>"
        return Response(body)

    client = SecClient("Research analyst@acme.com", transport=transport, interval=0.2)
    result = build_report_samples(
        cache_root=tmp_path / "cache",
        out=tmp_path / "out",
        work_dir=tmp_path / "work",
        user_agent="Research analyst@acme.com",
        client=client,
        workers=2,
    )
    assert result["filings"] == 2 and result["samples"] == 2, (
        tmp_path / "out" / "diagnostics.jsonl"
    ).read_text()
    assert len([url for url in requested if url.endswith("index.json")]) == 2
    assert len(list((tmp_path / "out").rglob("*.parquet"))) == 1


def test_build_respects_worker_bound_and_rejects_protected_output(
    tmp_path: Path, monkeypatch
) -> None:
    import threading
    from datetime import date

    from filings.report_samples import ReportSamplesError, build_report_samples

    filings = tuple(
        SimpleNamespace(
            cik10=f"{index:010d}",
            accession_number=f"{index:010d}-24-000001",
            form="10-K",
            filed_date="2024-01-01",
            report_date="2023-12-31",
            primary_document="report.htm",
        )
        for index in range(1, 5)
    )
    monkeypatch.setattr(
        "filings.report_samples.scan_cached_filings",
        lambda *a, **k: SimpleNamespace(filings=filings),
    )
    barrier = threading.Barrier(2)
    active = maximum = 0
    lock = threading.Lock()

    def worker(filing, user_agent, work_root, *, client, committed_root):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        barrier.wait(timeout=3)
        identity = f"{filing.cik10}:{filing.accession_number}"
        sample = {
            "sample_id": identity,
            "entity_id": filing.cik10,
            "filed_date": date(2024, 1, 1),
            "blocks": [{"order_index": 0, "block_type": "unknown", "text": "Revenue"}],
        }
        diagnostic = {"filing_id": identity, "status": "emitted", "block_count": 1}
        with lock:
            active -= 1
        return sample, diagnostic

    monkeypatch.setattr("filings.report_samples._filing_worker", worker)
    result = build_report_samples(
        cache_root=tmp_path / "cache",
        out=tmp_path / "out",
        work_dir=tmp_path / "work",
        user_agent="Research analyst@acme.com",
        workers=2,
    )
    assert result["filings"] == 4 and result["samples"] == 4
    assert maximum == 2
    try:
        build_report_samples(
            cache_root=tmp_path / "cache",
            out=Path("baselines"),
            work_dir=tmp_path / "work2",
            user_agent="Research analyst@acme.com",
        )
    except ReportSamplesError as exc:
        assert "protected" in str(exc)
    else:
        raise AssertionError("protected baseline output must be rejected")


def test_build_rejects_nonempty_and_symlink_output(tmp_path: Path, monkeypatch) -> None:
    from filings.report_samples import ReportSamplesError, build_report_samples

    monkeypatch.setattr(
        "filings.report_samples.scan_cached_filings", lambda *a, **k: SimpleNamespace(filings=())
    )
    existing = tmp_path / "existing"
    existing.mkdir()
    (existing / "user.txt").write_text("preserve", encoding="utf-8")
    try:
        build_report_samples(
            cache_root=tmp_path / "cache",
            out=existing,
            work_dir=tmp_path / "work",
            user_agent="Research analyst@acme.com",
        )
    except ReportSamplesError as exc:
        assert "absent or empty" in str(exc)
    else:
        raise AssertionError("nonempty output must not be replaced")
    try:
        build_report_samples(
            cache_root=tmp_path / "cache",
            out=Path("data/raw/financial_reports"),
            work_dir=tmp_path / "protected-work",
            user_agent="Research analyst@acme.com",
        )
    except ReportSamplesError as exc:
        assert "protected" in str(exc)
    else:
        raise AssertionError("raw-tree output must be rejected")
    monkeypatch.chdir(tmp_path)
    safe_data_output = Path("data/financial_reports")
    result = build_report_samples(
        cache_root=tmp_path / "cache",
        out=safe_data_output,
        work_dir=Path("data/.financial_reports.work"),
        user_agent="Research analyst@acme.com",
    )
    assert result["filings"] == 0
    assert safe_data_output.is_dir()
    try:
        build_report_samples(
            cache_root=tmp_path / "cache",
            out=existing,
            work_dir=tmp_path / "resume-work",
            user_agent="Research analyst@acme.com",
            resume=True,
        )
    except ReportSamplesError as exc:
        assert "resume output lacks owned" in str(exc)
    else:
        raise AssertionError("unrelated nonempty resume output must be rejected")
    assert (existing / "user.txt").read_text(encoding="utf-8") == "preserve"
    try:
        build_report_samples(
            cache_root=tmp_path / "cache",
            out=tmp_path / "negative-limit",
            work_dir=tmp_path / "negative-limit-work",
            limit=-1,
            user_agent="Research analyst@acme.com",
        )
    except ReportSamplesError as exc:
        assert "positive integer" in str(exc)
    else:
        raise AssertionError("negative limit must be rejected")
    assert (existing / "user.txt").read_text(encoding="utf-8") == "preserve"
    target = tmp_path / "target"
    target.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(target, target_is_directory=True)
    try:
        build_report_samples(
            cache_root=tmp_path / "cache",
            out=alias,
            work_dir=tmp_path / "work2",
            user_agent="Research analyst@acme.com",
        )
    except ReportSamplesError as exc:
        assert "symlink" in str(exc)
    else:
        raise AssertionError("symlink output must be rejected")


def test_resume_filters_prior_checkpoints_to_current_limit(tmp_path: Path, monkeypatch) -> None:
    from datetime import date

    from filings.report_samples import build_report_samples

    filings = tuple(
        SimpleNamespace(
            cik10=f"{index:010d}",
            accession_number=f"{index:010d}-24-000001",
            form="10-K",
            filed_date="2024-01-01",
            report_date=None,
            primary_document=None,
        )
        for index in range(1, 3)
    )
    monkeypatch.setattr(
        "filings.report_samples.scan_cached_filings",
        lambda *a, **k: SimpleNamespace(filings=filings),
    )
    calls = []

    def worker(filing, user_agent, work_root, *, client, committed_root):
        identity = f"{filing.cik10}:{filing.accession_number}"
        calls.append(identity)
        sample = {
            "sample_id": identity,
            "entity_id": filing.cik10,
            "filed_date": date(2024, 1, 1),
            "blocks": [{"order_index": 0, "block_type": "unknown", "text": "Revenue"}],
        }
        diagnostic = {"filing_id": identity, "status": "emitted", "block_count": 1}
        from filings.report_samples import _commit_filing

        _commit_filing(committed_root, identity, sample, diagnostic)
        return sample, diagnostic

    monkeypatch.setattr("filings.report_samples._filing_worker", worker)
    out, work = tmp_path / "out", tmp_path / "work"
    first = build_report_samples(
        cache_root=tmp_path / "cache",
        out=out,
        work_dir=work,
        limit=2,
        user_agent="Research analyst@acme.com",
    )
    assert first["samples"] == 2
    assert len(list(out.rglob("*.parquet"))) >= 1
    calls.clear()
    second = build_report_samples(
        cache_root=tmp_path / "cache",
        out=out,
        work_dir=work,
        limit=1,
        resume=True,
        user_agent="Research analyst@acme.com",
    )
    assert second["filings"] == 1 and second["samples"] == 1
    assert calls == []
    assert validate_report_samples(out)["filings"] == 1


def test_resume_discards_corrupt_checkpoint_and_retries_identity(
    tmp_path: Path, monkeypatch
) -> None:
    from datetime import date

    from filings.report_samples import build_report_samples, filing_id

    filing = SimpleNamespace(
        cik10="0000000001",
        accession_number="0000000001-24-000001",
        form="10-K",
        filed_date="2024-01-01",
        report_date=None,
        primary_document=None,
    )
    monkeypatch.setattr(
        "filings.report_samples.scan_cached_filings",
        lambda *a, **k: SimpleNamespace(filings=(filing,)),
    )
    work, out = tmp_path / "work", tmp_path / "out"
    committed = work / "committed"
    committed.mkdir(parents=True)
    identity = filing_id(filing.cik10, filing.accession_number)
    (committed / "corrupt.json").write_text("{", encoding="utf-8")
    (committed / "stale.json").write_text(
        json.dumps({"diagnostic": {"filing_id": identity, "status": "emitted"}}), encoding="utf-8"
    )
    calls = []

    def worker(item, user_agent, work_root, *, client, committed_root):
        calls.append(identity)
        sample = {
            "sample_id": identity,
            "entity_id": filing.cik10,
            "filed_date": date(2024, 1, 1),
            "blocks": [{"order_index": 0, "block_type": "unknown", "text": "Revenue"}],
        }
        diagnostic = {"filing_id": identity, "status": "emitted", "block_count": 1}
        from filings.report_samples import _commit_filing

        _commit_filing(committed_root, identity, sample, diagnostic)
        return sample, diagnostic

    monkeypatch.setattr("filings.report_samples._filing_worker", worker)
    result = build_report_samples(
        cache_root=tmp_path / "cache",
        out=out,
        work_dir=work,
        resume=True,
        user_agent="Research analyst@acme.com",
    )
    assert result["samples"] == 1 and calls == [identity]
    assert not (committed / "corrupt.json").exists()


def test_streaming_writer_preserves_batches_and_year_buckets(tmp_path: Path, monkeypatch) -> None:
    from datetime import date

    from filings.report_samples import build_report_samples

    filings = tuple(
        SimpleNamespace(
            cik10=f"{index:010d}",
            accession_number=f"{index:010d}-24-000001",
            form="10-K",
            filed_date=f"{2023 + index % 2}-01-01",
            report_date=None,
            primary_document=None,
        )
        for index in range(1, 1002)
    )
    monkeypatch.setattr(
        "filings.report_samples.scan_cached_filings",
        lambda *a, **k: SimpleNamespace(filings=filings),
    )

    def worker(filing, user_agent, work_root, *, client, committed_root):
        identity = f"{filing.cik10}:{filing.accession_number}"
        sample = {
            "sample_id": identity,
            "entity_id": filing.cik10,
            "filed_date": date.fromisoformat(filing.filed_date),
            "blocks": [{"order_index": 0, "block_type": "unknown", "text": "Revenue"}],
        }
        return sample, {"filing_id": identity, "status": "emitted", "block_count": 1}

    monkeypatch.setattr("filings.report_samples._filing_worker", worker)
    out = tmp_path / "out"
    result = build_report_samples(
        cache_root=tmp_path / "cache",
        out=out,
        work_dir=tmp_path / "work",
        user_agent="Research analyst@acme.com",
        workers=4,
    )
    shards = sorted(out.rglob("*.parquet"))
    assert result["samples"] == 1001
    assert len(shards) == 5
    assert sum(pq.read_table(path).num_rows for path in shards) == 1001
    assert {path.parent.name for path in shards} == {"year=2023", "year=2024"}


def test_pdf_primary_is_skipped_with_explicit_diagnostic(tmp_path: Path, monkeypatch) -> None:
    from filings.report_samples import build_report_samples

    filing = SimpleNamespace(
        cik10="0000000001",
        accession_number="0000000001-24-000001",
        form="10-K",
        filed_date="2024-01-01",
        report_date=None,
        primary_document="report.pdf",
    )
    body = b"%PDF-1.7\n"
    url = "https://www.sec.gov/Archives/edgar/data/1/000000000124000001/report.pdf"
    response = FetchResponse(body, url, url, "application/pdf", datetime.now(timezone.utc), 1, 200)
    candidate = SimpleNamespace(
        role="primary", filename="report.pdf", selected=True, selection_status="required", url=url
    )
    plan = SimpleNamespace(
        candidates=(candidate,), selection_status="selected_financial", reasons=()
    )
    monkeypatch.setattr(
        "filings.report_samples.scan_cached_filings",
        lambda *a, **k: SimpleNamespace(filings=(filing,)),
    )
    monkeypatch.setattr("filings.report_samples.enumerate_documents", lambda *a, **k: plan)
    monkeypatch.setattr("filings.report_samples.fetch_document", lambda *a, **k: response)
    out = tmp_path / "out"
    result = build_report_samples(
        cache_root=tmp_path / "cache",
        out=out,
        work_dir=tmp_path / "work",
        user_agent="Research analyst@acme.com",
        client=object(),
    )
    assert result["samples"] == 0 and result["filings"] == 1
    diagnostic = json.loads((out / "diagnostics.jsonl").read_text().strip())
    assert diagnostic["status"] == "skipped"
    assert diagnostic["parser_status"] == ["unsupported"]
    assert diagnostic["sources"][0]["byte_size"] == len(body)
    assert not list((tmp_path / "work").glob("filing-*"))


def test_clean_block_preserves_spaced_numeric_tokens() -> None:
    from filings.report_samples import _collect_blocks

    parsed = ContentResult(
        documents=[{"status": "full"}],
        blocks=[{"text": "Revenue $   1,200   (300)", "block_kind": "text"}],
    )
    blocks, _, _ = _collect_blocks(parsed)
    assert blocks[0]["text"] == "Revenue $ 1,200 (300)"


def test_clean_block_rejects_numeric_token_mutation() -> None:
    from filings.report_samples import ReportSamplesError, _collect_blocks

    parsed = ContentResult(
        documents=[{"status": "full"}],
        blocks=[{"text": "Revenue 1,234", "block_kind": "text"}],
    )
    assert _collect_blocks(parsed)[0][0]["text"] == "Revenue 1,234"
    parsed.blocks[0]["text"] = "Revenue 1\u200b234"
    try:
        _collect_blocks(parsed)
    except ReportSamplesError as exc:
        assert "numeric token" in str(exc)
    else:
        raise AssertionError("numeric token sequence changes must fail cleaning")


def test_download_failure_commits_failed_diagnostic_without_raw_bytes(
    tmp_path: Path, monkeypatch
) -> None:
    filing = SimpleNamespace(
        cik10="0000000001",
        accession_number="0000000001-24-000001",
        form="10-K",
        filed_date="2024-01-01",
        report_date=None,
        primary_document="report.htm",
    )
    candidate = SimpleNamespace(
        role="primary",
        filename="report.htm",
        selected=True,
        selection_status="required",
        url="https://www.sec.gov/report.htm",
    )
    plan = SimpleNamespace(
        candidates=(candidate,), selection_status="selected_financial", reasons=()
    )
    monkeypatch.setattr("filings.report_samples.enumerate_documents", lambda *a, **k: plan)
    monkeypatch.setattr(
        "filings.report_samples.fetch_document",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    work = tmp_path / "work"
    work.mkdir()
    sample, diagnostic = _filing_worker(
        filing,
        "Research analyst@acme.com",
        work,
        client=object(),
        committed_root=tmp_path / "committed",
    )
    assert sample is None and diagnostic["status"] == "failed"
    assert not list(work.glob("filing-*"))
    checkpoint = next((tmp_path / "committed").glob("*.json"))
    assert json.loads(checkpoint.read_text())["diagnostic"]["status"] == "failed"


def test_validator_rejects_duplicate_sample_ids(tmp_path: Path) -> None:
    import pyarrow as pa

    from filings.report_samples import _SCHEMA

    sample_id = "0000000001:0000000001-24-000001"
    (tmp_path / "diagnostics.jsonl").write_text(
        json.dumps({"filing_id": sample_id, "status": "emitted"}) + "\n", encoding="utf-8"
    )
    (tmp_path / "identity.json").write_text(json.dumps([sample_id]), encoding="utf-8")
    row = {
        "sample_id": sample_id,
        "entity_id": "0000000001",
        "filed_date": None,
        "blocks": [{"order_index": 0, "block_type": "unknown", "text": "Revenue"}],
    }
    pq.write_table(pa.Table.from_pylist([row, row], schema=_SCHEMA), tmp_path / "part.parquet")
    try:
        validate_report_samples(tmp_path)
    except ValueError as exc:
        assert "duplicate sample_id" in str(exc)
    else:
        raise AssertionError("duplicate sample IDs must fail validation")


def test_validator_rejects_bad_block_type_and_index(tmp_path: Path) -> None:
    import pyarrow as pa

    from filings.report_samples import _SCHEMA

    sample_id = "0000000001:0000000001-24-000001"
    (tmp_path / "diagnostics.jsonl").write_text(
        json.dumps({"filing_id": sample_id, "status": "emitted"}) + "\n", encoding="utf-8"
    )
    (tmp_path / "identity.json").write_text(json.dumps([sample_id]), encoding="utf-8")
    row = {
        "sample_id": sample_id,
        "entity_id": "0000000001",
        "filed_date": None,
        "blocks": [{"order_index": 1, "block_type": "bad", "text": "Revenue"}],
    }
    pq.write_table(pa.Table.from_pylist([row], schema=_SCHEMA), tmp_path / "part.parquet")
    try:
        validate_report_samples(tmp_path)
    except ValueError as exc:
        assert "invalid block" in str(exc)
    else:
        raise AssertionError("bad block metadata must fail validation")


def test_validator_rejects_emitted_count_mismatch_and_missing_shard(tmp_path: Path) -> None:
    sample_id = "0000000001:0000000001-24-000001"
    (tmp_path / "diagnostics.jsonl").write_text(
        json.dumps({"filing_id": sample_id, "status": "emitted"}) + "\n", encoding="utf-8"
    )
    (tmp_path / "identity.json").write_text(json.dumps([sample_id]), encoding="utf-8")
    try:
        validate_report_samples(tmp_path)
    except ValueError as exc:
        assert "no Parquet shards" in str(exc)
    else:
        raise AssertionError("emitted diagnostics without shards must fail validation")


def test_validator_rejects_wrong_nested_schema(tmp_path: Path) -> None:
    (tmp_path / "diagnostics.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "identity.json").write_text("[]", encoding="utf-8")
    pq.write_table(__import__("pyarrow").table({"sample_id": ["x"]}), tmp_path / "bad.parquet")
    try:
        validate_report_samples(tmp_path, expected_ids=[])
    except ValueError as exc:
        assert "schema mismatch" in str(exc)
    else:
        raise AssertionError("validator must reject the wrong schema")
