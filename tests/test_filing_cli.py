"""Offline CLI coverage for catalog, query, acquisition, and processing workflows."""

from __future__ import annotations

import builtins
import csv
import hashlib
import importlib.util
import json
import os
import socket
import subprocess
import sys
import urllib.request
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pyarrow as pa
import pytest

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from cli.main import main  # noqa: E402
from filings import (  # noqa: E402
    DOCUMENTS_SCHEMA,
    FILINGS_SCHEMA,
    RunSpec,
    document_id,
    filing_id,
    open_archive,
    read_snapshot,
)
from filings.workflow import FilingWorkflowError, _calendar_sessions  # noqa: E402

FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "filings_core"
ACQUISITION_FIXTURES = REPO_ROOT / "tests" / "fixtures" / "filings_acquisition"
PROCESSING_FIXTURES = REPO_ROOT / "tests" / "fixtures" / "filings_processing"
CIK = "0000000123"
PAGE_NAME = "CIK0000000123-submissions-001.json"
REQUIRES_DUCKDB = pytest.mark.skipif(
    importlib.util.find_spec("duckdb") is None,
    reason="DuckDB is optional; install the query extra to run this CLI test",
)


def _payload(name: str) -> tuple[dict[str, Any], bytes]:
    content = (FIXTURE_ROOT / name).read_bytes()
    return json.loads(content), content


def _write_cache(
    root: Path,
    *,
    include_page: bool = True,
    main_payload: dict[str, Any] | None = None,
) -> Path:
    root.mkdir(parents=True)
    default_main, main_bytes = _payload("submissions.json")
    main = main_payload if main_payload is not None else default_main
    if main_payload is not None:
        main_bytes = json.dumps(main, sort_keys=True, separators=(",", ":")).encode("utf-8")
    _, page_bytes = _payload("submissions-001.json")

    def add(payload_bytes: bytes, url: str) -> dict[str, Any]:
        digest = hashlib.sha256(payload_bytes).hexdigest()
        relative = f"{digest}.json"
        (root / relative).write_bytes(payload_bytes)
        return {
            "path": relative,
            "sha256": digest,
            "byte_size": len(payload_bytes),
            "url": url,
            "status": "done",
        }

    resources: dict[str, list[dict[str, Any]]] = {
        f"submissions:{CIK}": [add(main_bytes, f"https://data.sec.gov/submissions/CIK{CIK}.json")]
    }
    if include_page:
        resources[f"submissions-page:{CIK}:{PAGE_NAME}"] = [
            add(page_bytes, f"https://data.sec.gov/submissions/{PAGE_NAME}")
        ]
    (root / "manifest.json").write_text(
        json.dumps({"resources": resources}, sort_keys=True), encoding="utf-8"
    )
    return root


def _catalog_args(archive: Path, cache: Path, *extra: str) -> list[str]:
    return [
        "filings",
        "catalog",
        "--archive",
        str(archive),
        "--cache-root",
        str(cache),
        "--cik",
        CIK,
        "--start",
        "2024-01-10",
        "--end",
        "2024-01-16",
        *extra,
    ]


def _run_cli(*arguments: str | Path) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    current = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(SOURCE_ROOT), current) if part
    )
    return subprocess.run(
        [sys.executable, "-m", "cli.main", *(str(value) for value in arguments)],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _run_python(script: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    current = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(SOURCE_ROOT), current) if part
    )
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _write_query_archive(root: Path) -> Path:
    protected = root.parent / "protected-input"
    protected.mkdir(parents=True, exist_ok=True)
    accessions = ("0000999999-24-000001", "0000999999-24-000002")
    filed_dates = (date(2024, 1, 10), date(2024, 2, 15))
    visible_dates = (date(2024, 1, 11), date(2024, 2, 16))
    forms = ("10-Q", "10-K")
    filenames = ("quarterly.htm", "annual.htm")
    with open_archive(root, RunSpec(), protected_paths=(protected,)) as writer:
        submission = writer.put_raw_bytes(b"tiny local submissions fixture")
        inventory = writer.put_raw_bytes(b"tiny local directory fixture")
        body = writer.put_raw_bytes(
            b"<html><body><h1>Fixture report</h1><p>Filing text fixture.</p></body></html>"
        )
        filing_rows: list[dict[str, Any]] = []
        document_rows: list[dict[str, Any]] = []
        for accession, filed, visible, form, filename in zip(
            accessions, filed_dates, visible_dates, forms, filenames, strict=True
        ):
            identity = filing_id(CIK, accession)
            source_url = f"https://www.sec.gov/Archives/{accession}/{filename}"
            filing_rows.append(
                {
                    "filing_id": identity,
                    "cik10": CIK,
                    "accession_number": accession,
                    "form": form,
                    "filed_date": filed,
                    "report_period_end": date(2023, 12, 31),
                    "acceptance_datetime_raw": None,
                    "acceptance_datetime_utc": None,
                    "effective_visible_session": visible,
                    "is_amendment": False,
                    "parent_filing_id": None,
                    "parent_link_source": None,
                    "primary_document_name": filename,
                    "scope_status": "included",
                    "scope_evidence_json": None,
                    "inventory_status": "known",
                    "raw_coverage_status": "partial",
                    "source_submission_logical_key": f"submissions:{CIK}",
                    "source_submission_sha256": submission.sha256,
                    "source_submission_path": submission.path,
                    "source_submission_locator": "filings.recent[0]",
                }
            )
            document_rows.append(
                {
                    "document_id": document_id(identity, source_url),
                    "filing_id": identity,
                    "original_filename": filename,
                    "source_url": source_url,
                    "role": "primary",
                    "selection_status": "required",
                    "fetch_status": "present",
                    "source_inventory_sha256": inventory.sha256,
                    "source_inventory_locator": "directory[0]",
                    "raw_sha256": body.sha256,
                    "raw_path": body.path,
                    "byte_size": body.byte_size,
                    "media_type": "text/html",
                    "fetched_at_utc": None,
                    "http_status": 200,
                    "diagnostic_code": None,
                    "fact_extraction_status": "not_attempted",
                    "text_extraction_status": "not_attempted",
                }
            )
        writer.commit(
            tables={
                "filings": pa.Table.from_pylist(filing_rows, schema=FILINGS_SCHEMA),
                "documents": pa.Table.from_pylist(document_rows, schema=DOCUMENTS_SCHEMA),
            },
            raw_objects=(submission, inventory, body),
            expected_manifest_version=0,
        )
    return root


def _archive_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _write_download_cache(root: Path, *, form: str = "10-K", primary: str = "annual.htm") -> Path:
    main_payload, _ = _payload("submissions.json")
    recent = main_payload["filings"]["recent"]
    for name, values in tuple(recent.items()):
        if isinstance(values, list):
            recent[name] = [values[1]]
    recent["form"] = [form]
    recent["primaryDocument"] = [primary]
    main_payload["filings"]["files"] = []
    return _write_cache(root, include_page=False, main_payload=main_payload)


def _seed_download_archive(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    *,
    form: str = "10-K",
    primary: str = "annual.htm",
) -> tuple[Path, Path, str]:
    cache = _write_download_cache(tmp_path / "cache", form=form, primary=primary)
    archive = tmp_path / "archive"
    report = main(_catalog_args(archive, cache, "--form", form))
    captured = capsys.readouterr()
    assert report == 0, captured.err
    return archive, cache, f"{CIK}:0000999999-24-000001"


def _download_fixture_routes(*, form: str = "10-K") -> dict[str, bytes]:
    case = "six_k" if form == "6-K" else "ten_k"
    directory = ACQUISITION_FIXTURES / case
    accession = "0000999999-24-000001"
    base = f"https://www.sec.gov/Archives/edgar/data/{int(CIK)}/{accession.replace('-', '')}/"
    index = json.loads((directory / "index.json").read_text(encoding="utf-8"))
    index["directory"]["name"] = f"/Archives/edgar/data/{int(CIK)}/{accession.replace('-', '')}/"
    routes = {
        base + "index.json": json.dumps(index).encode("utf-8"),
        base + f"{accession}-index.html": (directory / "detail.html").read_bytes(),
    }
    routes.update(
        {
            base + path.name: path.read_bytes()
            for path in directory.iterdir()
            if path.is_file() and path.name not in {"index.json", "detail.html"}
        }
    )
    return routes


class _OfflineHttpResponse:
    def __init__(self, url: str, body: bytes, status: int = 200) -> None:
        self.url = url
        self.body = body
        self.status = status
        self.position = 0
        self.headers = {
            "Content-Type": "application/json" if url.endswith(".json") else "text/html",
            "Content-Length": str(len(body)),
        }

    def geturl(self) -> str:
        return self.url

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = len(self.body) - self.position
        stop = min(self.position + size, len(self.body))
        value = self.body[self.position : stop]
        self.position = stop
        return value

    def close(self) -> None:
        return None


class _OfflineSecTransport:
    def __init__(self, routes: dict[str, bytes], *, status: int = 200) -> None:
        self.routes = routes
        self.status = status
        self.calls: list[str] = []

    def __call__(self, url: str, headers: Any, timeout: float) -> _OfflineHttpResponse:
        self.calls.append(url)
        if url not in self.routes:
            raise AssertionError(f"unexpected mocked SEC URL: {url}")
        return _OfflineHttpResponse(url, self.routes[url], self.status)


def _install_offline_sec_transport(
    monkeypatch: pytest.MonkeyPatch, *, form: str = "10-K", status: int = 200
) -> _OfflineSecTransport:
    from filings import sec_client

    transport = _OfflineSecTransport(_download_fixture_routes(form=form), status=status)
    monkeypatch.setattr(sec_client, "_stdlib_transport", transport)
    return transport


def _write_sec_secrets(
    path: Path, user_agent: str = "Research contact analyst@acme-financials.com"
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "[secrets]\nsec_user_agent = " + json.dumps(user_agent) + "\n", encoding="utf-8"
    )
    return path


def test_calendar_loader_uses_xnys_holidays_and_rejects_out_of_range() -> None:
    sessions, provenance = _calendar_sessions(date(2024, 1, 10), date(2024, 1, 12))
    assert date(2024, 1, 15) not in sessions
    assert next(session for session in sessions if session > date(2024, 1, 12)) == date(2024, 1, 16)
    assert provenance["availability_horizon"] == "2024-01-22"
    with pytest.raises(FilingWorkflowError, match="10-day horizon"):
        _calendar_sessions(date(2027, 9, 25), date(2027, 9, 25))


def test_catalog_cli_copies_exact_cached_bytes_and_publishes_core_tables(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = _write_cache(tmp_path / "cache")
    archive = tmp_path / "archive"
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("network")),
    )
    code = main(_catalog_args(archive, cache, "--cik", CIK))
    captured = capsys.readouterr()
    assert code == 0, captured.err
    report = json.loads(captured.out)
    assert report["catalog_status"] == "catalog_only"
    assert report["input_status"] == "complete"
    assert report["requested_ciks"] == [CIK]
    assert report["filing_count"] == 3
    assert report["verified_resource_count"] == 2
    assert report["active_raw_object_count"] == 2
    snapshot = read_snapshot(archive)
    assert snapshot.tables["filings"].schema.equals(FILINGS_SCHEMA, check_metadata=True)
    assert snapshot.tables["documents"].schema.equals(DOCUMENTS_SCHEMA, check_metadata=True)
    assert snapshot.tables["documents"].num_rows == 0
    assert "facts" not in snapshot.tables
    assert snapshot.manifest["calendar_provenance"]["calendar"] == "XNYS"
    assert len(snapshot.manifest["input_fingerprint"]) == 64
    spec_policy = snapshot.manifest["run_spec"]["policy"]
    assert list(spec_policy["requested_ciks"]) == [CIK]
    assert spec_policy["filed_date_start_inclusive"] == "2024-01-10"
    assert spec_policy["filed_date_end_inclusive"] == "2024-01-16"
    approved = snapshot.manifest["run_spec"]["approved_forms"]
    assert list(approved) == sorted(approved)
    rows = snapshot.tables["filings"].to_pylist()
    assert [row["filed_date"] for row in rows] == [
        date(2024, 1, 10),
        date(2024, 1, 12),
        date(2024, 1, 16),
    ]
    by_form = {row["form"]: row for row in rows}
    assert by_form["10-Q/A"]["effective_visible_session"] == date(2024, 1, 16)
    assert by_form["10-Q/A"]["is_amendment"] is True
    assert by_form["10-Q/A"]["parent_filing_id"] is None
    assert by_form["6-K"]["scope_status"] == "candidate"
    assert by_form["6-K"]["raw_coverage_status"] == "not_attempted"
    assert by_form["6-K"]["acceptance_datetime_utc"] is None
    assert by_form["10-Q/A"]["filing_id"] == f"{CIK}:0000999999-24-000001"
    assert all(row["source_submission_path"].startswith("raw/sha256/") for row in rows)
    for ref in snapshot.raw_objects:
        content = (archive / ref.path).read_bytes()
        assert hashlib.sha256(content).hexdigest() == ref.sha256
        assert content in {
            (FIXTURE_ROOT / "submissions.json").read_bytes(),
            (FIXTURE_ROOT / "submissions-001.json").read_bytes(),
        }


def test_resume_is_idempotent_and_run_spec_binds_dates(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cache = _write_cache(tmp_path / "cache")
    archive = tmp_path / "archive"
    assert main(_catalog_args(archive, cache)) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["manifest_version"] == 1
    assert main(_catalog_args(archive, cache, "--resume", "--cik", "123")) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["manifest_version"] == 1
    assert second["snapshot_id"] == first["snapshot_id"]
    assert second["idempotent"] is True
    changed = _catalog_args(archive, cache, "--resume")
    changed[changed.index("2024-01-10")] = "2024-01-11"
    assert main(changed) == 1
    assert "RunSpec" in capsys.readouterr().err
    assert read_snapshot(archive).manifest_version == 1


def test_resume_rejects_changed_cached_resource_fingerprint(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cache = _write_cache(tmp_path / "cache")
    archive = tmp_path / "archive"
    assert main(_catalog_args(archive, cache)) == 0
    initial = json.loads(capsys.readouterr().out)
    manifest_path = cache / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload, _ = _payload("submissions.json")
    payload["name"] = "Changed metadata without filing-row changes"
    content = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(content).hexdigest()
    name = f"{digest}.json"
    (cache / name).write_bytes(content)
    resources = manifest["resources"][f"submissions:{CIK}"]
    resources.append(dict(resources[-1], path=name, sha256=digest, byte_size=len(content)))
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    assert main(_catalog_args(archive, cache, "--resume")) == 1
    assert "RunSpec" in capsys.readouterr().err
    snapshot = read_snapshot(archive)
    assert snapshot.manifest_version == 1
    assert snapshot.manifest["input_fingerprint"] == initial["input_fingerprint"]


def test_partial_history_requires_flag_and_is_provenanced(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cache = _write_cache(tmp_path / "cache", include_page=False)
    archive = tmp_path / "archive"
    assert main(_catalog_args(archive, cache)) == 1
    assert "missing historical page" in capsys.readouterr().err
    assert not archive.exists()
    assert main(_catalog_args(archive, cache, "--allow-partial")) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["catalog_status"] == "partial_catalog_only"
    assert report["input_status"] == "partial"
    assert report["diagnostics"] == [f"{CIK}:missing_historical_page:{PAGE_NAME}"]
    assert read_snapshot(archive).manifest["coverage_provenance"]["input_status"] == "partial"


def test_empty_selection_still_commits_exact_empty_core_schemas(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cache = _write_cache(tmp_path / "cache")
    archive = tmp_path / "archive"
    args = _catalog_args(archive, cache)
    args[args.index("2024-01-10")] = "2025-01-01"
    args[args.index("2024-01-16")] = "2025-01-03"
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)["filing_count"] == 0
    snapshot = read_snapshot(archive)
    assert snapshot.tables["filings"].schema.equals(FILINGS_SCHEMA, check_metadata=True)
    assert snapshot.tables["filings"].num_rows == 0
    assert snapshot.tables["documents"].schema.equals(DOCUMENTS_SCHEMA, check_metadata=True)
    assert snapshot.tables["documents"].num_rows == 0
    assert snapshot.manifest["scope_provenance"]["empty_selection"] is True


def test_archive_cannot_overlap_cache_or_known_protected_roots(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cache = _write_cache(tmp_path / "cache")
    archive = cache / "nested-archive"
    assert main(_catalog_args(archive, cache)) == 1
    assert "overlaps a protected path" in capsys.readouterr().err
    assert not archive.exists()


def test_catalog_form_choices_are_approved_only(tmp_path: Path) -> None:
    cache = _write_cache(tmp_path / "cache")
    archive = tmp_path / "archive"
    result = _run_cli(*_catalog_args(archive, cache, "--form", "8-K"))
    assert result.returncode == 2
    assert "invalid choice" in result.stderr
    assert not archive.exists()


def test_verify_reports_integrity_not_financial_coverage(tmp_path: Path) -> None:
    cache = _write_cache(tmp_path / "cache")
    archive = tmp_path / "archive"
    assert _run_cli(*_catalog_args(archive, cache)).returncode == 0
    verified = _run_cli("filings", "verify", "--archive", archive)
    assert verified.returncode == 0, verified.stderr
    report = json.loads(verified.stdout)
    assert report["integrity_status"] == "verified"
    assert report["coverage_claim"] == "not_assessed"
    assert report["table_row_counts"] == {"documents": 0, "filings": 3}
    batch = read_snapshot(archive).manifest["tables"]["filings"]["batches"][0]
    (archive / batch["path"]).write_bytes(b"corrupt")
    corrupted = _run_cli("filings", "verify", "--archive", archive)
    assert corrupted.returncode != 0
    assert "archive verification failed" in corrupted.stderr


def test_unrelated_cli_help_and_optional_parser_imports_remain_lazy() -> None:
    root_help = _run_cli("--help")
    download_help = _run_cli("download", "--help")
    catalog_help = _run_cli("filings", "catalog", "--help")
    parse_help = _run_cli("filings", "parse", "--help")
    assert root_help.returncode == 0 and "filings" in root_help.stdout
    assert download_help.returncode == 0 and "--stage" in download_help.stdout
    assert catalog_help.returncode == 0 and "--allow-partial" in catalog_help.stdout
    assert parse_help.returncode == 0 and "--workspace-root" in parse_help.stdout
    probe = _run_python(
        "import sys; from cli.main import main; "
        "main(['filings', 'verify', '--archive', '/definitely/not/an/archive']); "
        "print('ARELLE_IMPORTED', any(n == 'arelle' or n.startswith('arelle.') "
        "for n in sys.modules))"
    )
    assert probe.returncode == 0
    assert "ARELLE_IMPORTED False" in probe.stdout


@REQUIRES_DUCKDB
def test_filings_query_cli_emits_csv_and_archive_metadata_separately(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    archive = _write_query_archive(tmp_path / "archive")
    before = _archive_hashes(archive)
    code = main(
        [
            "filings",
            "query",
            "--archive",
            str(archive),
            "--sql",
            "SELECT f.form AS filing, d.original_filename AS filing "
            "FROM filings AS f JOIN documents AS d USING (filing_id) "
            "WHERE f.filed_date >= DATE '2024-02-01' ORDER BY f.filed_date",
            "--limit",
            "1",
        ]
    )
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert list(csv.reader(captured.out.splitlines())) == [
        ["filing", "filing"],
        ["10-K", "annual.htm"],
    ]
    assert "format=filings-core-archive" in captured.err
    assert "manifest_version=1" in captured.err
    assert "rows=1" in captured.err
    assert _archive_hashes(archive) == before

    assert (
        main(
            [
                "filings",
                "query",
                "--archive",
                str(archive),
                "--sql",
                "SELECT COUNT(*) AS n FROM filings",
            ]
        )
        == 0
    )
    assert list(csv.reader(capsys.readouterr().out.splitlines())) == [["n"], ["2"]]


@pytest.mark.parametrize(
    ("extra", "expected"),
    [
        ([], "--archive"),
        (["--archive", "/tmp/archive"], "--sql"),
        (["--sql", "SELECT 1"], "--archive"),
    ],
)
def test_filings_query_requires_explicit_archive_and_sql(
    extra: list[str], expected: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as error:
        main(["filings", "query", *extra])
    assert error.value.code == 2
    assert expected in capsys.readouterr().err


@pytest.mark.parametrize("value", ["bad", "0", "1001"])
def test_filings_query_cli_rejects_invalid_limits(
    value: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as error:
        main(
            ["filings", "query", "--archive", "/tmp/archive", "--sql", "SELECT 1", "--limit", value]
        )
    assert error.value.code == 2
    assert "limit" in capsys.readouterr().err


@REQUIRES_DUCKDB
def test_filings_query_cli_rejects_write_sql_without_storage_changes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    archive = _write_query_archive(tmp_path / "archive")
    before = _archive_hashes(archive)
    code = main(["filings", "query", "--archive", str(archive), "--sql", "DELETE FROM filings"])
    captured = capsys.readouterr()
    assert code == 1
    assert "Only a single SELECT" in captured.err
    assert captured.out == ""
    assert _archive_hashes(archive) == before


def test_query_help_stays_lazy_without_optional_dependencies(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    original = builtins.__import__

    def no_optional_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "duckdb" or name == "arelle" or name.startswith("arelle."):
            raise AssertionError(f"optional module imported during help: {name}")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_optional_import)
    with pytest.raises(SystemExit) as result:
        main(["filings", "query", "--help"])
    assert result.value.code == 0
    assert "--archive" in capsys.readouterr().out


@REQUIRES_DUCKDB
def test_query_cli_reports_actionable_missing_duckdb_extra(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    archive = _write_query_archive(tmp_path / "archive")
    original = builtins.__import__

    def without_duckdb(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "duckdb":
            raise ImportError("simulated missing optional dependency")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_duckdb)
    code = main(["filings", "query", "--archive", str(archive), "--sql", "SELECT 1"])
    captured = capsys.readouterr()
    assert code == 1
    assert "uv sync --extra query" in captured.err
    assert captured.out == ""


@pytest.mark.parametrize("value", ["bad", "0", "51"])
def test_filings_download_cli_rejects_invalid_max_before_archive_access(
    value: str, capsys: pytest.CaptureFixture[str]
) -> None:
    root = Path("/tmp/nonexistent-filings-download-root")
    with pytest.raises(SystemExit) as error:
        main(["filings", "download", "--archive", str(root), "--max-filings", value])
    assert error.value.code == 2
    assert "max-filings" in capsys.readouterr().err
    assert not root.exists()


@pytest.mark.parametrize("extra", [[], ["--max-filings", "2"]])
def test_filings_download_requires_archive(
    extra: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as error:
        main(["filings", "download", *extra])
    assert error.value.code == 2
    assert "--archive" in capsys.readouterr().err


def test_download_help_stays_lazy_without_parser_or_query_extras(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    original = builtins.__import__

    def no_optional_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "duckdb" or name == "arelle" or name.startswith("arelle."):
            raise AssertionError(f"optional module imported during help: {name}")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_optional_import)
    with pytest.raises(SystemExit) as result:
        main(["filings", "download", "--help"])
    assert result.value.code == 0
    help_text = capsys.readouterr().out
    assert all(
        flag in help_text for flag in ("--archive", "--max-filings", "--filing-id", "--secrets")
    )


def test_filings_download_cli_uses_offline_sec_transport_and_reports_snapshot(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    archive, cache, identity = _seed_download_archive(tmp_path, capsys)
    secrets = _write_sec_secrets(tmp_path / "secrets.toml")
    transport = _install_offline_sec_transport(monkeypatch)
    cache_before = _archive_hashes(cache)
    code = main(
        [
            "filings",
            "download",
            "--archive",
            str(archive),
            "--filing-id",
            identity,
            "--secrets",
            str(secrets),
        ]
    )
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert code == 0, captured.err
    assert report["command"] == "filings download"
    assert report["status"] == "completed"
    assert report["manifest_version"] == 2
    assert report["requested_filing_ids"] == [identity]
    assert report["processed_filing_ids"] == [identity]
    assert report["acquired_document_count"] == 3
    assert report["blocked_filing_ids"] == []
    assert len(transport.calls) == 5
    assert "analyst@acme-financials.com" not in captured.out + captured.err
    assert _archive_hashes(cache) == cache_before


def test_download_needs_review_is_informational_and_blocked_result_is_partial(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    archive, _, identity = _seed_download_archive(
        tmp_path, capsys, form="6-K", primary="current-report.htm"
    )
    secrets = _write_sec_secrets(tmp_path / "secrets.toml")
    transport = _install_offline_sec_transport(monkeypatch, form="6-K")
    code = main(["filings", "download", "--archive", str(archive), "--secrets", str(secrets)])
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert code == 0
    assert report["status"] == "needs_review"
    assert report["needs_review_filing_ids"] == [identity]
    assert transport.calls

    blocked_archive, _, blocked_identity = _seed_download_archive(tmp_path / "blocked", capsys)
    blocked_secrets = _write_sec_secrets(tmp_path / "blocked-secrets.toml")
    blocked_transport = _install_offline_sec_transport(monkeypatch, status=403)
    code = main(
        [
            "filings",
            "download",
            "--archive",
            str(blocked_archive),
            "--secrets",
            str(blocked_secrets),
        ]
    )
    report = json.loads(capsys.readouterr().out)
    assert code == 1
    assert report["status"] == "partial"
    assert report["blocked_filing_ids"] == [blocked_identity]
    assert blocked_transport.calls


def test_download_unknown_id_placeholder_contact_and_protected_archive_are_safe(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    archive, cache, _identity = _seed_download_archive(tmp_path, capsys)
    secrets = _write_sec_secrets(tmp_path / "secrets.toml")
    transport = _install_offline_sec_transport(monkeypatch)
    before_archive, before_cache = _archive_hashes(archive), _archive_hashes(cache)
    code = main(
        [
            "filings",
            "download",
            "--archive",
            str(archive),
            "--secrets",
            str(secrets),
            "--filing-id",
            f"{CIK}:0000000001-24-999999",
        ]
    )
    captured = capsys.readouterr()
    assert code == 1 and "absent from the active snapshot" in captured.err
    assert transport.calls == []
    assert _archive_hashes(archive) == before_archive
    assert _archive_hashes(cache) == before_cache

    placeholder = _write_sec_secrets(
        tmp_path / "placeholder.toml", "Placeholder contact@invalid.example"
    )
    code = main(["filings", "download", "--archive", str(archive), "--secrets", str(placeholder)])
    captured = capsys.readouterr()
    assert code == 1
    assert "contact@invalid.example" not in captured.err
    assert transport.calls == []

    from filings import workflow

    monkeypatch.setattr(workflow, "protected_archive_paths", lambda _cache: (archive,))
    code = main(["filings", "download", "--archive", str(archive), "--secrets", str(secrets)])
    assert code == 1
    assert transport.calls == []


def test_download_missing_archive_fails_before_secret_read_or_creation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from filings import config

    monkeypatch.setattr(
        config,
        "load_sec_user_agent",
        lambda _path: (_ for _ in ()).throw(
            AssertionError("secret read before archive validation")
        ),
    )
    archive = tmp_path / "not-created"
    code = main(
        ["filings", "download", "--archive", str(archive), "--secrets", str(tmp_path / "none.toml")]
    )
    assert code == 1
    assert "existing directory" in capsys.readouterr().err
    assert not archive.exists()


def test_filings_parse_cli_offline_by_default_does_not_read_secrets(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _write_query_archive(tmp_path / "archive")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    def deny_network(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("offline parse attempted network access")

    monkeypatch.setattr(socket.socket, "connect", deny_network)
    monkeypatch.setattr(urllib.request, "urlopen", deny_network)
    from filings import config

    monkeypatch.setattr(
        config,
        "load_sec_user_agent",
        lambda _path: (_ for _ in ()).throw(AssertionError("offline parse must not read secrets")),
    )
    code = main(["filings", "parse", "--archive", str(archive), "--workspace-root", str(workspace)])
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert code == 0, captured.err
    assert report["command"] == "filings parse"
    assert report["status"] == "completed_with_unsupported"
    assert report["manifest_version"] == 2
    assert report["dependencies_prepared"] is False
    assert report["full_parse_count"] == 2
    assert report["unsupported_parse_count"] == 2
    assert report["fact_rows_written"] == 0
    assert report["section_rows_written"] >= 2
    assert "not financial-statement correctness" in report["coverage_claim"]
    snapshot = read_snapshot(archive)
    assert snapshot.tables["parses"].num_rows == 4
    assert snapshot.tables["facts"].num_rows == 0
    assert snapshot.tables["sections"].num_rows >= 2


def test_filings_parse_requires_archive_workspace_and_valid_limit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for args, expected in [
        (["filings", "parse"], "--archive"),
        (["filings", "parse", "--archive", "/tmp/a"], "--workspace-root"),
        (["filings", "parse", "--workspace-root", "/tmp/w"], "--archive"),
    ]:
        with pytest.raises(SystemExit) as error:
            main(args)
        assert error.value.code == 2
        assert expected in capsys.readouterr().err
    with pytest.raises(SystemExit) as error:
        main(
            [
                "filings",
                "parse",
                "--archive",
                "/tmp/a",
                "--workspace-root",
                "/tmp/w",
                "--max-filings",
                "51",
            ]
        )
    assert error.value.code == 2
    assert "max-filings" in capsys.readouterr().err


def test_filings_parse_rejects_invalid_mode_combinations_and_unknown_hosts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    archive = tmp_path / "missing"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for options, expected in (
        (["--taxonomy-host", "www.xbrl.org"], "--taxonomy-host requires"),
        (["--secrets", str(tmp_path / "secrets.toml")], "--secrets is only used"),
    ):
        code = main(
            [
                "filings",
                "parse",
                "--archive",
                str(archive),
                "--workspace-root",
                str(workspace),
                *options,
            ]
        )
        assert code == 2
        assert expected in capsys.readouterr().err
    for unknown_host in (
        "attacker.example",
        "sub.taxonomies.xbrl.us",
        "taxonomies.xbrl.us.attacker.example",
        "*.xbrl.us",
        "192.0.2.1",
    ):
        with pytest.raises(SystemExit) as error:
            main(
                [
                    "filings",
                    "parse",
                    "--archive",
                    str(archive),
                    "--workspace-root",
                    str(workspace),
                    "--prepare-dependencies",
                    "--taxonomy-host",
                    unknown_host,
                ]
            )
        assert error.value.code == 2
        assert "invalid choice" in capsys.readouterr().err


def test_filings_parse_help_is_lazy_without_optional_parser_or_query_imports(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    original = builtins.__import__

    def no_optional_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "duckdb" or name == "arelle" or name.startswith("arelle."):
            raise AssertionError(f"optional dependency imported during help: {name}")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_optional_import)
    with pytest.raises(SystemExit) as result:
        main(["filings", "parse", "--help"])
    assert result.value.code == 0
    help_text = capsys.readouterr().out
    assert all(
        flag in help_text
        for flag in (
            "--archive",
            "--workspace-root",
            "--filing-id",
            "--max-filings",
            "--prepare-dependencies",
            "--taxonomy-host",
            "--secrets",
        )
    )


@pytest.mark.parametrize(
    ("taxonomy_options", "expected_hosts"),
    [
        (
            ["--taxonomy-host", "www.xbrl.org"],
            {"www.sec.gov", "data.sec.gov", "xbrl.sec.gov", "www.xbrl.org"},
        ),
        (
            ["--taxonomy-host", "xbrl.sec.gov"],
            {"www.sec.gov", "data.sec.gov", "xbrl.sec.gov"},
        ),
        (
            ["--taxonomy-host", "taxonomies.xbrl.us"],
            {"www.sec.gov", "data.sec.gov", "xbrl.sec.gov", "taxonomies.xbrl.us"},
        ),
        (
            [],
            {
                "www.sec.gov",
                "data.sec.gov",
                "xbrl.sec.gov",
                "xbrl.fasb.org",
                "www.xbrl.org",
                "xbrl.ifrs.org",
                "www.w3.org",
                "taxonomies.xbrl.us",
            },
        ),
    ],
)
def test_parse_preparation_opt_in_uses_secure_taxonomy_client_and_redacts_contact(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    taxonomy_options: list[str],
    expected_hosts: set[str],
) -> None:
    archive = _write_query_archive(tmp_path / "archive")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    secrets = _write_sec_secrets(tmp_path / "sec-only-secrets.toml")
    expected_identity = f"{CIK}:0000999999-24-000001"
    observed: dict[str, Any] = {}

    class FakeTaxonomyClient:
        def __init__(self, *, user_agent: str, allowed_hosts: tuple[str, ...]) -> None:
            observed["user_agent"] = user_agent
            observed["allowed_hosts"] = allowed_hosts

        def fetch_taxonomy(self, url: str) -> Any:
            observed["url"] = url
            return SimpleNamespace(
                body=b"fixture", request_url=url, final_url=url, transport_url=url
            )

    from filings import processing, sec_client

    monkeypatch.setattr(sec_client, "TaxonomyClient", FakeTaxonomyClient)

    def fake_parse_archive(
        archive_path: Path,
        *,
        protected_paths: Any,
        workspace_root: Path,
        filing_ids: Any,
        max_filings: int,
        fetch_dependencies: Any,
        allowed_taxonomy_hosts: Any,
    ) -> Any:
        observed["protected_paths"] = protected_paths
        observed["workspace_root"] = workspace_root
        observed["filing_ids"] = filing_ids
        observed["max_filings"] = max_filings
        observed["allowed_taxonomy_hosts"] = allowed_taxonomy_hosts
        fetch_dependencies("https://www.xbrl.org/taxonomy.xsd")
        snapshot = read_snapshot(archive_path)
        return SimpleNamespace(
            snapshot=snapshot,
            selected_filing_ids=(expected_identity,),
            processed_filing_ids=(expected_identity,),
            skipped_filing_ids=(),
            filing_statuses={expected_identity: "processed"},
            parse_statuses={},
            fact_rows_written=0,
            section_rows_written=0,
            dependency_rows_written=0,
            full_parse_count=0,
            partial_parse_count=0,
            unsupported_parse_count=0,
            failed_parse_count=0,
        )

    monkeypatch.setattr(processing, "parse_archive", fake_parse_archive)
    code = main(
        [
            "filings",
            "parse",
            "--archive",
            str(archive),
            "--workspace-root",
            str(workspace),
            "--filing-id",
            expected_identity,
            "--max-filings",
            "1",
            "--prepare-dependencies",
            *taxonomy_options,
            "--secrets",
            str(secrets),
        ]
    )
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert code == 0, captured.err
    assert observed["user_agent"] == "Research contact analyst@acme-financials.com"
    assert observed["url"] == "https://www.xbrl.org/taxonomy.xsd"
    assert set(observed["allowed_hosts"]) == expected_hosts
    assert observed["allowed_taxonomy_hosts"] == observed["allowed_hosts"]
    assert observed["filing_ids"] == [expected_identity]
    assert observed["max_filings"] == 1
    assert secrets.resolve() in observed["protected_paths"]
    assert report["dependencies_prepared"] is True
    assert "analyst@acme-financials.com" not in captured.out + captured.err


def test_parse_bad_archive_fails_before_reading_secrets_or_importing_taxonomy_client(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    original = builtins.__import__

    def no_secret_modules(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "filings.config":
            raise AssertionError("secrets loader imported before archive guard")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_secret_modules)
    archive = tmp_path / "missing-archive"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    code = main(
        [
            "filings",
            "parse",
            "--archive",
            str(archive),
            "--workspace-root",
            str(workspace),
            "--prepare-dependencies",
            "--secrets",
            str(tmp_path / "missing-secrets.toml"),
        ]
    )
    captured = capsys.readouterr()
    assert code == 1
    assert "existing verified archive" in captured.err
    assert not archive.exists()


def test_parse_protected_workspace_fails_before_secret_read_or_client_creation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _write_query_archive(tmp_path / "archive")
    workspace = tmp_path / "protected-workspace"
    workspace.mkdir()
    secrets = tmp_path / "missing-secrets.toml"
    from filings import config, workflow

    monkeypatch.setattr(workflow, "protected_archive_paths", lambda _cache: (workspace,))
    monkeypatch.setattr(
        config,
        "load_sec_user_agent",
        lambda _path: (_ for _ in ()).throw(AssertionError("protected workspace read secrets")),
    )
    code = main(
        [
            "filings",
            "parse",
            "--archive",
            str(archive),
            "--workspace-root",
            str(workspace),
            "--prepare-dependencies",
            "--taxonomy-host",
            "www.xbrl.org",
            "--secrets",
            str(secrets),
        ]
    )
    assert code == 1
    assert "workspace_root overlaps a protected path" in capsys.readouterr().err


def test_parse_unknown_id_fails_before_fetch_or_workspace_mutation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _write_query_archive(tmp_path / "archive")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    secrets = _write_sec_secrets(tmp_path / "secrets.toml")
    from filings import config

    monkeypatch.setattr(
        config,
        "load_sec_user_agent",
        lambda _path: (_ for _ in ()).throw(AssertionError("unknown ID read SEC credentials")),
    )
    code = main(
        [
            "filings",
            "parse",
            "--archive",
            str(archive),
            "--workspace-root",
            str(workspace),
            "--prepare-dependencies",
            "--taxonomy-host",
            "www.xbrl.org",
            "--secrets",
            str(secrets),
            "--filing-id",
            f"{CIK}:0000000001-24-999999",
        ]
    )
    captured = capsys.readouterr()
    assert code == 1
    assert "absent from the active snapshot" in captured.err
    assert not list(workspace.iterdir())
    assert read_snapshot(archive).manifest_version == 1


def test_parse_reports_arelle_extra_actionable_diagnostic_without_error_body(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _write_query_archive(tmp_path / "archive")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    from filings import processing

    snapshot = read_snapshot(archive)

    class Rows:
        def to_pylist(self) -> list[dict[str, Any]]:
            return [
                {
                    "document_id": "d" * 64,
                    "parser_name": "filings.xbrl",
                    "errors_json": json.dumps(
                        [{"code": "arelle_dependency_missing", "message": "private traceback"}]
                    ),
                }
            ]

    snapshot_view = SimpleNamespace(
        root=snapshot.root,
        snapshot_id=snapshot.snapshot_id,
        manifest_version=snapshot.manifest_version,
        tables={"parses": Rows()},
    )
    monkeypatch.setattr(
        processing,
        "parse_archive",
        lambda *_args, **_kwargs: SimpleNamespace(
            snapshot=snapshot_view,
            selected_filing_ids=(),
            processed_filing_ids=(),
            skipped_filing_ids=(),
            filing_statuses={},
            parse_statuses={f"{'d' * 64}:filings.xbrl": "failed"},
            fact_rows_written=0,
            section_rows_written=0,
            dependency_rows_written=0,
            full_parse_count=0,
            partial_parse_count=0,
            unsupported_parse_count=0,
            failed_parse_count=1,
        ),
    )
    code = main(["filings", "parse", "--archive", str(archive), "--workspace-root", str(workspace)])
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert code == 1
    assert report["status"] == "partial"
    assert report["diagnostic_codes"] == ["arelle_dependency_missing"]
    assert "uv sync --frozen --extra filings" in report["install_hint"]
    assert "private traceback" not in captured.out + captured.err
