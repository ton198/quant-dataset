"""Offline integration tests for bounded archive parsing and persistence."""

from __future__ import annotations

import hashlib
import json
import socket
import sys
import urllib.request
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pyarrow as pa
import pytest

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures" / "filings_processing"
CORE_FIXTURES = Path(__file__).parent / "fixtures" / "filings_core"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

import filings.processing as processing_module  # noqa: E402
from filings.acquisition import download_archive, load_archive_runspec  # noqa: E402
from filings.archive import (  # noqa: E402
    ArchiveCorruptionError,
    ArchiveWriter,
    open_archive,
    read_snapshot,
)
from filings.models import DOCUMENTS_SCHEMA, document_id  # noqa: E402
from filings.packages import PackageConflictError, PackageEntry, PreparedPackage  # noqa: E402
from filings.parsing_models import FACT_SCHEMA, SECTION_SCHEMA, ParseResult  # noqa: E402
from filings.processing import (  # noqa: E402
    DEPENDENCIES_SCHEMA,
    PARSES_SCHEMA,
    ProcessingError,
    parse_archive,
)
from filings.sec_client import SecClient  # noqa: E402
from filings.workflow import protected_archive_paths, run_catalog  # noqa: E402

CIK = "0000000123"
ACCESSION = "0000999999-24-000001"
IDENTITY = f"{CIK}:{ACCESSION}"
VALID_AGENT = "Processing fixture analyst@acme-financials.com"
CLASSIC_INSTANCE_100 = (
    b'<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" '
    b'xmlns:link="http://www.xbrl.org/2003/linkbase" '
    b'xmlns:xlink="http://www.w3.org/1999/xlink" '
    b'xmlns:iso4217="http://www.xbrl.org/2003/iso4217" '
    b'xmlns:ex="https://example.test/filings">'
    b'<link:schemaRef xlink:type="simple" xlink:href="issuer.xsd"/>'
    b'<xbrli:context id="duration"><xbrli:entity>'
    b'<xbrli:identifier scheme="https://example.test/entity">0000123456</xbrli:identifier>'
    b"</xbrli:entity><xbrli:period><xbrli:startDate>2024-01-01</xbrli:startDate>"
    b"<xbrli:endDate>2024-01-02</xbrli:endDate></xbrli:period></xbrli:context>"
    b'<xbrli:unit id="USD"><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unit>'
    b'<ex:Amount contextRef="duration" unitRef="USD" decimals="0">100</ex:Amount>'
    b"</xbrli:xbrl>"
)


class FakeClock:
    def __init__(self) -> None:
        self.current = 0.0

    def monotonic(self) -> float:
        return self.current

    def sleep(self, seconds: float) -> None:
        self.current += seconds


class FakeHttpResponse:
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
        result = self.body[self.position : stop]
        self.position = stop
        return result

    def close(self) -> None:
        return None


class FixtureSecTransport:
    """Exact route map: unexpected URL attempts fail locally, never reach network."""

    def __init__(self, routes: Mapping[str, bytes]) -> None:
        self.routes = dict(routes)
        self.calls: list[str] = []

    def __call__(self, url: str, headers: Mapping[str, str], timeout: float) -> FakeHttpResponse:
        self.calls.append(url)
        if url not in self.routes:
            raise AssertionError(f"unexpected SEC fixture URL: {url}")
        return FakeHttpResponse(url, self.routes[url])


def _write_catalog_cache(cache_root: Path, *, primary: str = "annual_inline.htm") -> Path:
    cache_root.mkdir(parents=True)
    payload = json.loads((CORE_FIXTURES / "submissions.json").read_text(encoding="utf-8"))
    recent = payload["filings"]["recent"]
    source_index = recent["accessionNumber"].index(ACCESSION)
    for key, values in tuple(recent.items()):
        if isinstance(values, list):
            recent[key] = [values[source_index]]
    recent["form"] = ["10-K"]
    recent["primaryDocument"] = [primary]
    payload["filings"]["files"] = []
    content = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(content).hexdigest()
    (cache_root / f"{digest}.json").write_bytes(content)
    resource = {
        "path": f"{digest}.json",
        "sha256": digest,
        "byte_size": len(content),
        "url": f"https://data.sec.gov/submissions/CIK{CIK}.json",
        "status": "done",
    }
    (cache_root / "manifest.json").write_text(
        json.dumps({"resources": {f"submissions:{CIK}": [resource]}}, sort_keys=True),
        encoding="utf-8",
    )
    return cache_root


def _sec_fixture_routes(*, primary: str = "annual_inline.htm") -> dict[str, bytes]:
    fixture_root = FIXTURES / "acquisition"
    base = f"https://www.sec.gov/Archives/edgar/data/{int(CIK)}/{ACCESSION.replace('-', '')}/"
    directory = json.loads((fixture_root / "index.json").read_text(encoding="utf-8"))
    directory["directory"]["name"] = (
        f"/Archives/edgar/data/{int(CIK)}/{ACCESSION.replace('-', '')}/"
    )
    directory["directory"]["item"][0]["name"] = primary
    if primary.lower().endswith(".pdf"):
        directory["directory"]["item"][0]["type"] = "application/pdf"
    detail = (fixture_root / "detail.html").read_text(encoding="utf-8")
    detail = detail.replace("annual_inline.htm", primary)
    primary_bytes = (fixture_root / primary).read_bytes()
    return {
        base + "index.json": json.dumps(directory).encode("utf-8"),
        base + f"{ACCESSION}-index.html": detail.encode("utf-8"),
        base + primary: primary_bytes,
        base + "issuer.xsd": (fixture_root / "issuer.xsd").read_bytes(),
    }


def _catalog_then_download(tmp_path: Path, *, primary: str = "annual_inline.htm"):
    cache = _write_catalog_cache(tmp_path / "cache", primary=primary)
    archive = tmp_path / "archive"
    protected = protected_archive_paths(cache)
    catalog = run_catalog(
        archive=archive,
        cache_root=cache,
        ciks=(CIK,),
        start_date=date(2024, 1, 1),
        end_date=date(2024, 1, 31),
        forms={"10-K"},
    )
    assert catalog.snapshot.tables["filings"].num_rows == 1
    clock = FakeClock()
    transport = FixtureSecTransport(_sec_fixture_routes(primary=primary))
    client = SecClient(
        VALID_AGENT,
        interval=0.2,
        timeout=3,
        retries=0,
        transport=transport,
        clock=clock.monotonic,
        sleep=clock.sleep,
    )
    acquired = download_archive(
        archive,
        client=client,
        protected_paths=protected,
        filing_ids=(IDENTITY,),
        max_filings=1,
    )
    assert acquired.processed_filing_ids == (IDENTITY,)
    return archive, cache, protected, transport, acquired.snapshot


def _digest_tree(root: Path) -> dict[str, str]:
    return {
        item.relative_to(root).as_posix(): hashlib.sha256(item.read_bytes()).hexdigest()
        for item in sorted(root.rglob("*"))
        if item.is_file()
    }


def _dependency_response(url: str) -> SimpleNamespace:
    filename = Path(urlsplit_path(url)).name
    source = FIXTURES / "xbrl" / filename
    body = source.read_bytes()
    return SimpleNamespace(
        body=body,
        request_url=url,
        final_url=url,
        transport_url=url.replace("https://", "http://"),
        redirect_chain=(),
    )


def urlsplit_path(value: str) -> str:
    from urllib.parse import urlsplit

    return urlsplit(value).path


def _assert_no_external_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def deny(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("processing/parser attempted HTTP or socket access")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(urllib.request, "urlopen", deny)


def _parse(
    archive: Path,
    workspace: Path,
    protected: tuple[Path, ...],
    **kwargs: Any,
):
    return parse_archive(
        archive,
        protected_paths=protected,
        workspace_root=workspace,
        filing_ids=(IDENTITY,),
        max_filings=1,
        **kwargs,
    )


def _add_archive_document(
    archive: Path,
    protected: tuple[Path, ...],
    *,
    filename: str,
    body: bytes,
    role: str,
) -> dict[str, Any]:
    snapshot = read_snapshot(archive)
    document_rows = snapshot.tables["documents"].to_pylist()
    template = next(row for row in document_rows if row["filing_id"] == IDENTITY)
    source_url = f"{template['source_url'].rsplit('/', 1)[0]}/{filename}"
    new_id = document_id(IDENTITY, source_url)
    row = dict(template)
    with open_archive(
        archive,
        load_archive_runspec(archive),
        protected_paths=protected,
        resume=True,
    ) as writer:
        raw_ref = writer.put_raw_bytes(body)
        row.update(
            {
                "document_id": new_id,
                "original_filename": filename,
                "source_url": source_url,
                "role": role,
                "selection_status": "required",
                "fetch_status": "present",
                "raw_sha256": raw_ref.sha256,
                "raw_path": raw_ref.path,
                "byte_size": raw_ref.byte_size,
                "media_type": "application/xml",
                "http_status": 200,
                "diagnostic_code": None,
                "fact_extraction_status": "not_attempted",
                "text_extraction_status": "not_attempted",
            }
        )
        document_rows.append(row)
        writer.commit(
            tables={
                "documents": pa.Table.from_pylist(document_rows, schema=DOCUMENTS_SCHEMA),
            },
            raw_objects=(raw_ref,),
            expected_manifest_version=snapshot.manifest_version,
        )
    return row


@pytest.mark.parametrize(
    ("encoding", "namespace"),
    [
        ("utf-8", "http://www.xbrl.org/2013/inlineXBRL"),
        ("utf-16", "http://www.xbrl.org/2008/inlineXBRL"),
        ("utf-32", "http://www.xbrl.org/2013/inlineXBRL"),
    ],
)
def test_inline_detection_is_encoding_aware_and_classic_fallback_remains_available(
    tmp_path: Path, encoding: str, namespace: str
) -> None:
    source_url = "https://filings.example.invalid/annual.xhtml"
    inline_path = tmp_path / f"inline-{encoding}.xhtml"
    inline_path.write_bytes(
        (
            '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:ix="'
            f'{namespace}"><body><ix:nonFraction name="ex:Amount"/></body></html>'
        ).encode(encoding)
    )
    primary = {
        "document_id": "inline-doc",
        "source_url": source_url,
        "original_filename": inline_path.name,
        "role": "primary",
        "selection_status": "required",
        "fetch_status": "present",
    }
    entrypoints = {source_url: SimpleNamespace(local_path=inline_path)}

    assert processing_module._inline_xbrl(inline_path)
    inline_docs, _, inline_strategy = processing_module._xbrl_targets([primary], entrypoints)
    assert [row["document_id"] for row in inline_docs] == ["inline-doc"]
    assert inline_strategy == "inline_preferred"

    plain_path = tmp_path / "plain.xhtml"
    plain_path.write_text(
        '<html xmlns="http://www.w3.org/1999/xhtml"><body>no inline facts</body></html>',
        encoding="utf-8",
    )
    classic_path = tmp_path / "instance.xml"
    classic_path.write_text(
        '<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance"/>',
        encoding="utf-8",
    )
    plain_doc = dict(
        primary,
        document_id="plain-doc",
        source_url="https://filings.example.invalid/plain.xhtml",
        original_filename=plain_path.name,
    )
    classic_doc = dict(
        primary,
        document_id="classic-doc",
        source_url="https://filings.example.invalid/instance.xml",
        original_filename=classic_path.name,
        role="xbrl_instance",
    )
    classic_docs, _, classic_strategy = processing_module._xbrl_targets(
        [plain_doc, classic_doc],
        {
            plain_doc["source_url"]: SimpleNamespace(local_path=plain_path),
            classic_doc["source_url"]: SimpleNamespace(local_path=classic_path),
        },
    )
    assert [row["document_id"] for row in classic_docs] == ["classic-doc"]
    assert classic_strategy == "classic_instance_fallback"


def test_catalog_download_process_persists_xbrl_text_dependencies_and_pit_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _assert_no_external_network(monkeypatch)
    archive, cache, protected, sec_transport, acquired = _catalog_then_download(tmp_path)
    original_archive_docs = {
        row["document_id"]: row["raw_sha256"]
        for row in acquired.tables["documents"].to_pylist()
        if row["fetch_status"] == "present"
    }
    original_source_hashes = _digest_tree(archive)
    cache_hashes = _digest_tree(cache)
    workspace = tmp_path / "processing-workspace"
    workspace.mkdir()
    taxonomy_calls: list[str] = []

    def fetch_taxonomy(url: str) -> Any:
        taxonomy_calls.append(url)
        return _dependency_response(url)

    result = _parse(
        archive,
        workspace,
        protected,
        fetch_dependencies=fetch_taxonomy,
        allowed_taxonomy_hosts={"taxonomy.example.test"},
    )

    assert result.snapshot.manifest_version == acquired.manifest_version + 1
    assert result.selected_filing_ids == (IDENTITY,)
    assert result.processed_filing_ids == (IDENTITY,)
    assert result.full_parse_count == 2
    assert result.partial_parse_count == 0
    assert result.failed_parse_count == 0
    assert result.fact_rows_written == 3
    assert result.section_rows_written >= 2
    assert result.dependency_rows_written >= 3
    assert len(taxonomy_calls) == 2
    assert set(taxonomy_calls) == {
        "https://taxonomy.example.test/xbrli.xsd",
        "https://taxonomy.example.test/xbrldt.xsd",
    }
    assert sec_transport.calls  # Catalog/acquisition fixture route seam only.

    snapshot = read_snapshot(archive)
    assert snapshot.tables["facts"].schema.equals(FACT_SCHEMA, check_metadata=True)
    assert snapshot.tables["sections"].schema.equals(SECTION_SCHEMA, check_metadata=True)
    assert snapshot.tables["parses"].schema.equals(PARSES_SCHEMA, check_metadata=True)
    assert snapshot.tables["dependencies"].schema.equals(DEPENDENCIES_SCHEMA, check_metadata=True)
    assert snapshot.tables["facts"].num_rows == 3
    assert snapshot.tables["sections"].num_rows >= 2
    assert snapshot.tables["parses"].num_rows == 2
    assert snapshot.tables["dependencies"].num_rows >= 3
    dependency = next(
        row
        for row in snapshot.tables["dependencies"].to_pylist()
        if row["requested_url"] == "https://taxonomy.example.test/xbrli.xsd"
    )
    assert dependency["final_url"] == "https://taxonomy.example.test/xbrli.xsd"
    assert dependency["transport_url"] == "http://taxonomy.example.test/xbrli.xsd"
    assert dependency["status"] == "present"
    assert dependency["raw_path"] and dependency["sha256"]
    xbrl_parse = next(
        row for row in snapshot.tables["parses"].to_pylist() if row["parser_name"] == "filings.xbrl"
    )
    text_parse = next(
        row for row in snapshot.tables["parses"].to_pylist() if row["parser_name"] == "filings.text"
    )
    assert xbrl_parse["status"] == text_parse["status"] == "full"
    assert xbrl_parse["fact_count"] == 3
    assert text_parse["section_count"] >= 2
    processed_documents = {
        row["original_filename"]: row
        for row in snapshot.tables["documents"].to_pylist()
        if row["filing_id"] == IDENTITY
    }
    assert processed_documents["annual_inline.htm"]["fact_extraction_status"] == "full"
    assert processed_documents["annual_inline.htm"]["text_extraction_status"] == "full"
    assert processed_documents["issuer.xsd"]["fact_extraction_status"] == "not_attempted"
    assert processed_documents["issuer.xsd"]["text_extraction_status"] == "not_attempted"
    assert all(
        row["document_id"] == processed_documents["annual_inline.htm"]["document_id"]
        for row in snapshot.tables["facts"].to_pylist()
    )
    assert all(
        row["source_hash"] == processed_documents["annual_inline.htm"]["raw_sha256"]
        for row in snapshot.tables["facts"].to_pylist()
    )
    docs_after = {
        row["document_id"]: row["raw_sha256"]
        for row in snapshot.tables["documents"].to_pylist()
        if row["fetch_status"] == "present"
    }
    assert original_archive_docs == docs_after
    assert _digest_tree(cache) == cache_hashes
    assert set(original_source_hashes).issubset(_digest_tree(archive))
    assert all(
        ref.sha256 == hashlib.sha256((archive / ref.path).read_bytes()).hexdigest()
        for ref in snapshot.raw_objects
    )
    # Query semantics stay user-authored: a parser stage does not rewrite the filing's
    # catalog date or visibility session.
    original_filing = acquired.tables["filings"].to_pylist()[0]
    processed_filing = snapshot.tables["filings"].to_pylist()[0]
    assert original_filing["filed_date"] == processed_filing["filed_date"] == date(2024, 1, 12)
    assert (
        original_filing["effective_visible_session"]
        == processed_filing["effective_visible_session"]
        == date(2024, 1, 16)
    )
    # User-selected workspace parent remains free of accidental archive-relative files.
    assert (workspace / IDENTITY / "annual_inline.htm").is_file()


@pytest.mark.parametrize("error_code", ["source_integrity_changed", "unverified_fact_source", None])
def test_untrusted_partial_xbrl_occurrences_are_recorded_failed_without_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_code: str | None,
) -> None:
    _assert_no_external_network(monkeypatch)
    archive, _cache, protected, _transport, _ = _catalog_then_download(
        tmp_path, primary="annual_inline_100.htm"
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    def partial_with_bad_source(entrypoint: Path, **kwargs: Any) -> ParseResult:
        source_hash = hashlib.sha256(Path(entrypoint).read_bytes()).hexdigest()
        row = {name: None for name in FACT_SCHEMA.names}
        row.update(
            {
                "filing_id": kwargs["filing_id"],
                "document_id": kwargs["document_id"],
                "parse_id": kwargs["parse_id"],
                "parser_name": processing_module.xbrl_parser.PARSER_NAME,
                "parser_version": processing_module.xbrl_parser.PARSER_VERSION,
                "status": "partial",
                "source_hash": source_hash,
                "source_uri": Path(entrypoint).as_uri(),
                "source_relpath": Path(entrypoint).name,
                "document_hash": "f" * 64,
                "is_numeric": True,
                "raw_value": "999",
                "normalized_numeric": "999",
                "error_codes_json": json.dumps([error_code])
                if error_code == "unverified_fact_source"
                else "[]",
            }
        )
        errors = (
            [
                {
                    "code": error_code,
                    "message": "simulated input/cache integrity fault",
                    "severity": "error",
                }
            ]
            if error_code == "source_integrity_changed"
            else []
        )
        return ParseResult(
            filing_id=kwargs["filing_id"],
            document_id=kwargs["document_id"],
            parse_id=kwargs["parse_id"],
            parser_name=processing_module.xbrl_parser.PARSER_NAME,
            parser_version=processing_module.xbrl_parser.PARSER_VERSION,
            status="partial",
            source_hash=source_hash,
            validation_scope="arelle_structural_xbrl",
            facts=[row],
            errors=errors,
        )

    monkeypatch.setattr(processing_module.xbrl_parser, "parse_xbrl", partial_with_bad_source)
    result = _parse(archive, workspace, protected)

    xbrl = next(
        row
        for row in result.snapshot.tables["parses"].to_pylist()
        if row["parser_name"] == processing_module.xbrl_parser.PARSER_NAME
    )
    assert xbrl["status"] == "failed"
    assert xbrl["fact_count"] == 0
    assert xbrl["validation_scope"] == "not_performed"
    assert result.snapshot.tables["facts"].num_rows == 0
    errors = json.loads(xbrl["errors_json"])
    assert errors
    assert errors[0]["code"] == (error_code or "unverified_fact_source")


def test_text_sections_require_verified_document_hash_and_anchor_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _assert_no_external_network(monkeypatch)
    archive, _cache, protected, _transport, _ = _catalog_then_download(
        tmp_path, primary="annual_inline_100.htm"
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    original_parse_text = processing_module.text_parser.parse_text

    def text_with_foreign_anchor(*args: Any, **kwargs: Any) -> ParseResult:
        result = original_parse_text(*args, **kwargs)
        result.status = "partial"
        for section in result.sections:
            section["status"] = "partial"
            section["document_hash"] = "e" * 64
            section["source_relpath"] = "foreign-filing/annual.htm"
        return result

    def no_xbrl_facts(entrypoint: Path, **kwargs: Any) -> ParseResult:
        source_hash = hashlib.sha256(Path(entrypoint).read_bytes()).hexdigest()
        return ParseResult(
            filing_id=kwargs["filing_id"],
            document_id=kwargs["document_id"],
            parse_id=kwargs["parse_id"],
            parser_name=processing_module.xbrl_parser.PARSER_NAME,
            parser_version=processing_module.xbrl_parser.PARSER_VERSION,
            status="unsupported",
            source_hash=source_hash,
            validation_scope="not_performed",
        )

    monkeypatch.setattr(processing_module.text_parser, "parse_text", text_with_foreign_anchor)
    monkeypatch.setattr(processing_module.xbrl_parser, "parse_xbrl", no_xbrl_facts)
    result = _parse(archive, workspace, protected)

    text = next(
        row
        for row in result.snapshot.tables["parses"].to_pylist()
        if row["parser_name"] == processing_module.text_parser.PARSER_NAME
    )
    assert text["status"] == "failed"
    assert text["section_count"] == 0
    assert result.snapshot.tables["sections"].num_rows == 0
    assert "unverified_fact_source" in text["errors_json"]


def test_fact_source_authority_is_scoped_to_its_filing(tmp_path: Path) -> None:
    current_root = tmp_path / "current"
    foreign_root = tmp_path / "foreign"
    current_root.mkdir()
    foreign_root.mkdir()
    current_path = current_root / "annual.htm"
    foreign_path = foreign_root / "annual.htm"
    current_path.write_bytes(b"current filing source")
    foreign_path.write_bytes(b"foreign filing source")
    current_hash = hashlib.sha256(current_path.read_bytes()).hexdigest()
    foreign_hash = hashlib.sha256(foreign_path.read_bytes()).hexdigest()
    current_package = PreparedPackage(
        filing_id="current-filing",
        workspace_root=current_root,
        entrypoints={
            "https://sec.example/current/annual.htm": PackageEntry(
                local_path=current_path,
                document_id="current-doc",
                raw_sha256=current_hash,
                byte_size=current_path.stat().st_size,
                role="primary",
                original_filename="annual.htm",
            )
        },
        scope_status="included",
        raw_coverage_status="complete",
        snapshot_id="current-snapshot",
    )
    foreign_package = PreparedPackage(
        filing_id="foreign-filing",
        workspace_root=foreign_root,
        entrypoints={
            "https://sec.example/foreign/annual.htm": PackageEntry(
                local_path=foreign_path,
                document_id="foreign-doc",
                raw_sha256=foreign_hash,
                byte_size=foreign_path.stat().st_size,
                role="primary",
                original_filename="annual.htm",
            )
        },
        scope_status="included",
        raw_coverage_status="complete",
        snapshot_id="foreign-snapshot",
    )
    result = ParseResult(
        filing_id="current-filing",
        document_id="current-doc",
        parse_id="parse",
        parser_name=processing_module.xbrl_parser.PARSER_NAME,
        parser_version=processing_module.xbrl_parser.PARSER_VERSION,
        status="partial",
        source_hash=current_hash,
        facts=[
            {
                "source_uri": "https://sec.example/foreign/annual.htm",
                "source_relpath": "annual.htm",
                "document_hash": foreign_hash,
            }
        ],
    )

    assert not processing_module._fact_sources_verified(result, current_package, None)
    assert processing_module._fact_sources_verified(result, foreign_package, None)


def test_offline_missing_taxonomy_is_partial_not_zero_and_retry_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _assert_no_external_network(monkeypatch)
    archive, _cache, protected, _transport, _ = _catalog_then_download(tmp_path)
    workspace = tmp_path / "processing-workspace"
    workspace.mkdir()
    missing = _parse(archive, workspace, protected)
    assert missing.snapshot.manifest_version == 3
    statuses = {row["status"] for row in missing.snapshot.tables["parses"].to_pylist()}
    assert "full" in statuses  # Local text extraction still completed.
    xbrl_rows = missing.snapshot.tables["parses"].to_pylist()
    xbrl = next(row for row in xbrl_rows if row["parser_name"] == "filings.xbrl")
    assert xbrl["status"] in {"failed", "partial"}
    assert xbrl["fact_count"] == 0
    assert xbrl["errors_json"] != "[]"
    assert missing.snapshot.tables["dependencies"].num_rows >= 1

    calls: list[str] = []

    def fetch_taxonomy(url: str) -> Any:
        calls.append(url)
        return _dependency_response(url)

    retried = _parse(
        archive,
        workspace,
        protected,
        fetch_dependencies=fetch_taxonomy,
        allowed_taxonomy_hosts={"taxonomy.example.test"},
    )
    assert retried.snapshot.manifest_version == 4
    xbrl = next(
        row
        for row in retried.snapshot.tables["parses"].to_pylist()
        if row["parser_name"] == "filings.xbrl"
    )
    assert xbrl["status"] == "full"
    assert xbrl["fact_count"] == 3
    assert calls


@pytest.mark.parametrize(
    ("primary", "expected_status", "expected_fact_count"),
    [
        ("annual_inline_invalid.htm", "partial", 1),
        ("annual_inline.htm", "full", 3),
        ("legacy.htm", "unsupported", 0),
        ("annual.pdf", "unsupported", 0),
    ],
)
def test_reprocessing_is_idempotent_and_preserves_unmodified_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    primary: str,
    expected_status: str,
    expected_fact_count: int,
) -> None:
    _assert_no_external_network(monkeypatch)
    archive, _cache, protected, _transport, acquired = _catalog_then_download(
        tmp_path, primary=primary
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    original_hashes = _digest_tree(archive)
    callback_calls: list[str] = []

    def fetch_taxonomy(url: str) -> Any:
        callback_calls.append(url)
        return _dependency_response(url)

    first = _parse(
        archive,
        workspace,
        protected,
        fetch_dependencies=fetch_taxonomy,
        allowed_taxonomy_hosts={"taxonomy.example.test"},
    )
    xbrl = next(
        row
        for row in first.snapshot.tables["parses"].to_pylist()
        if row["parser_name"] == "filings.xbrl"
    )
    assert xbrl["status"] == expected_status
    assert xbrl["fact_count"] == expected_fact_count
    expected_invalid = (
        next(
            row
            for row in first.snapshot.tables["facts"].to_pylist()
            if row["concept_local_name"] == "Amount" and row["raw_value"] == "not-a-number"
        )
        if primary.endswith("invalid.htm")
        else None
    )
    if expected_invalid is not None:
        assert expected_invalid["is_valid"] is False
        assert expected_invalid["normalized_numeric"] is None
        assert expected_invalid["raw_value"] == "not-a-number"
        xbrl_record = next(
            row
            for row in first.snapshot.tables["parses"].to_pylist()
            if row["parser_name"] == "filings.xbrl"
        )
        assert "ix11.10.1.1:transformValueError" in xbrl_record["errors_json"]
    after_first = _digest_tree(archive)
    assert after_first != original_hashes

    rerun = _parse(
        archive,
        workspace,
        protected,
        fetch_dependencies=fetch_taxonomy,
        allowed_taxonomy_hosts={"taxonomy.example.test"},
    )
    assert rerun.snapshot.manifest_version == first.snapshot.manifest_version
    assert rerun.snapshot.snapshot_id == first.snapshot.snapshot_id
    if primary.endswith("invalid.htm"):
        # Partial parses remain retryable. The repeated deterministic partial attempt
        # has identical active bytes/metadata and therefore does not publish a new head.
        assert rerun.processed_filing_ids == (IDENTITY,)
        assert rerun.partial_parse_count == 1
    else:
        assert rerun.processed_filing_ids == ()
        assert rerun.skipped_filing_ids == (IDENTITY,)
    assert {
        key: value for key, value in _digest_tree(archive).items() if key != "ledger.jsonl"
    } == {key: value for key, value in after_first.items() if key != "ledger.jsonl"}
    if primary in {"annual_inline_invalid.htm", "annual_inline.htm"}:
        assert callback_calls  # Prepared remote taxonomy fetches happened once.
    else:
        assert callback_calls == []  # Legacy HTML/PDF do not need remote taxonomy.
    assert acquired.manifest_version == 2


def test_parser_version_change_replaces_only_active_version_and_preserves_old_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive, _cache, protected, _transport, _ = _catalog_then_download(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    first = _parse(
        archive,
        workspace,
        protected,
        fetch_dependencies=lambda url: _dependency_response(url),
        allowed_taxonomy_hosts={"taxonomy.example.test"},
    )
    old_xbrl = next(
        row
        for row in first.snapshot.tables["parses"].to_pylist()
        if row["parser_name"] == "filings.xbrl"
    )
    old_text = next(
        row
        for row in first.snapshot.tables["parses"].to_pylist()
        if row["parser_name"] == "filings.text"
    )

    monkeypatch.setattr("filings.parse_xbrl.PARSER_VERSION", "test-version+arelle-2.46.0")
    versioned = _parse(
        archive,
        workspace,
        protected,
        fetch_dependencies=lambda url: _dependency_response(url),
        allowed_taxonomy_hosts={"taxonomy.example.test"},
    )

    assert versioned.snapshot.manifest_version == first.snapshot.manifest_version + 1
    active = versioned.snapshot.tables["parses"].to_pylist()
    assert len(active) == 2
    current_xbrl = next(row for row in active if row["parser_name"] == "filings.xbrl")
    current_text = next(row for row in active if row["parser_name"] == "filings.text")
    assert current_xbrl["parser_version"] == "test-version+arelle-2.46.0"
    assert current_xbrl["parse_id"] != old_xbrl["parse_id"]
    assert current_xbrl["status"] == "full"
    assert current_text["parse_id"] == old_text["parse_id"]
    # The old immutable snapshot still has its original parser version and rows.
    assert (
        next(
            row
            for row in read_snapshot(archive).tables["parses"].to_pylist()
            if row["parser_name"] == "filings.xbrl"
        )["parser_version"]
        == "test-version+arelle-2.46.0"
    )
    assert (
        next(
            row
            for row in first.snapshot.tables["parses"].to_pylist()
            if row["parser_name"] == "filings.xbrl"
        )["parser_version"]
        == old_xbrl["parser_version"]
    )


def test_explicit_dependency_refresh_replaces_only_active_parser_rows(tmp_path: Path) -> None:
    archive, _cache, protected, _transport, _ = _catalog_then_download(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    offline = _parse(archive, workspace, protected)
    assert offline.snapshot.manifest_version == 3
    old_xbrl = next(
        row
        for row in offline.snapshot.tables["parses"].to_pylist()
        if row["parser_name"] == "filings.xbrl"
    )
    old_text = next(
        row
        for row in offline.snapshot.tables["parses"].to_pylist()
        if row["parser_name"] == "filings.text"
    )

    refreshed = _parse(
        archive,
        workspace,
        protected,
        fetch_dependencies=lambda url: _dependency_response(url),
        allowed_taxonomy_hosts={"taxonomy.example.test"},
    )
    assert refreshed.snapshot.manifest_version == 4
    current = refreshed.snapshot.tables["parses"].to_pylist()
    assert len(current) == 2
    new_xbrl = next(row for row in current if row["parser_name"] == "filings.xbrl")
    new_text = next(row for row in current if row["parser_name"] == "filings.text")
    assert new_xbrl["parse_id"] != old_xbrl["parse_id"]
    assert new_xbrl["status"] == "full"
    assert new_text["parse_id"] == old_text["parse_id"]


def test_dependency_graph_snapshots_follow_the_active_parse_per_source(tmp_path: Path) -> None:
    archive, _cache, protected, _transport, _ = _catalog_then_download(tmp_path)
    secondary = _add_archive_document(
        archive,
        protected,
        filename="annual_secondary_invalid.htm",
        body=(FIXTURES / "acquisition" / "annual_inline_invalid.htm").read_bytes(),
        role="exhibit",
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    first = _parse(
        archive,
        workspace,
        protected,
        fetch_dependencies=lambda url: _dependency_response(url),
        allowed_taxonomy_hosts={"taxonomy.example.test"},
    )
    first_parses = {
        row["document_id"]: row
        for row in first.snapshot.tables["parses"].to_pylist()
        if row["parser_name"] == processing_module.xbrl_parser.PARSER_NAME
    }
    primary = next(
        row
        for row in first.snapshot.tables["documents"].to_pylist()
        if row["original_filename"] == "annual_inline.htm"
    )
    parse_a = first_parses[primary["document_id"]]
    parse_b = first_parses[secondary["document_id"]]
    assert parse_a["status"] == "full"
    assert parse_b["status"] == "partial"
    graph_x = parse_a["dependencies_fingerprint"]
    assert parse_b["dependencies_fingerprint"] == graph_x
    facts_a = [
        row
        for row in first.snapshot.tables["facts"].to_pylist()
        if row["document_id"] == primary["document_id"]
    ]
    assert facts_a

    def missing_taxonomy(url: str) -> Any:
        if url.endswith("/xbrli.xsd"):
            raise OSError("fixture dependency unavailable")
        return _dependency_response(url)

    second = _parse(
        archive,
        workspace,
        protected,
        fetch_dependencies=missing_taxonomy,
        allowed_taxonomy_hosts={"taxonomy.example.test"},
    )
    second_parses = {
        row["document_id"]: row
        for row in second.snapshot.tables["parses"].to_pylist()
        if row["parser_name"] == processing_module.xbrl_parser.PARSER_NAME
    }
    assert second_parses[primary["document_id"]]["parse_id"] == parse_a["parse_id"]
    assert second_parses[primary["document_id"]]["status"] == "full"
    parse_b_y = second_parses[secondary["document_id"]]
    assert parse_b_y["status"] == "failed"
    assert parse_b_y["fact_count"] == 0
    graph_y = parse_b_y["dependencies_fingerprint"]
    assert graph_y != graph_x
    assert [
        row["occurrence_id"]
        for row in second.snapshot.tables["facts"].to_pylist()
        if row["document_id"] == primary["document_id"]
    ] == [row["occurrence_id"] for row in facts_a]
    second_dependency_rows = second.snapshot.tables["dependencies"].to_pylist()
    assert any(
        row["dependency_fingerprint"] == graph_x and row["status"] == "present"
        for row in second_dependency_rows
    )
    assert any(
        row["dependency_fingerprint"] == graph_y
        and row["status"] == "error"
        and row["diagnostic_code"] == "missing_dependency"
        for row in second_dependency_rows
    )

    def changed_taxonomy(url: str) -> Any:
        response = _dependency_response(url)
        if url.endswith("/xbrli.xsd"):
            return SimpleNamespace(
                body=response.body + b"\n<!-- refreshed fixture schema -->",
                request_url=url,
                final_url=url,
                transport_url=getattr(response, "transport_url", None),
                redirect_chain=(),
            )
        return response

    third = _parse(
        archive,
        workspace,
        protected,
        fetch_dependencies=changed_taxonomy,
        allowed_taxonomy_hosts={"taxonomy.example.test"},
    )
    third_parses = {
        row["document_id"]: row
        for row in third.snapshot.tables["parses"].to_pylist()
        if row["parser_name"] == processing_module.xbrl_parser.PARSER_NAME
    }
    parse_b_z = third_parses[secondary["document_id"]]
    assert parse_b_z["status"] == "partial"
    graph_z = parse_b_z["dependencies_fingerprint"]
    assert graph_z not in {graph_x, graph_y}
    assert third_parses[primary["document_id"]]["dependencies_fingerprint"] == graph_x
    xbrli_rows = [
        row
        for row in third.snapshot.tables["dependencies"].to_pylist()
        if row["requested_url"] == "https://taxonomy.example.test/xbrli.xsd"
    ]
    assert {row["dependency_fingerprint"] for row in xbrli_rows} == {graph_x, graph_z}
    assert len({row["sha256"] for row in xbrli_rows}) == 2
    assert not any(
        row["dependency_fingerprint"] == graph_y
        for row in third.snapshot.tables["dependencies"].to_pylist()
    )

    repeated = _parse(
        archive,
        workspace,
        protected,
        fetch_dependencies=changed_taxonomy,
        allowed_taxonomy_hosts={"taxonomy.example.test"},
    )
    assert repeated.snapshot.snapshot_id == third.snapshot.snapshot_id
    identities = [
        (
            row["filing_id"],
            row["dependency_fingerprint"],
            row["requested_url"],
            row["final_url"],
            row["sha256"],
            row["status"],
        )
        for row in repeated.snapshot.tables["dependencies"].to_pylist()
    ]
    assert len(identities) == len(set(identities))


def test_inline_strategy_retires_only_superseded_classic_instance_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive, _cache, protected, _transport, _ = _catalog_then_download(
        tmp_path, primary="annual_inline_100.htm"
    )
    classic_instance = CLASSIC_INSTANCE_100
    classic = _add_archive_document(
        archive,
        protected,
        filename="generated-instance.xml",
        body=classic_instance,
        role="xbrl_instance",
    )
    exhibit = _add_archive_document(
        archive,
        protected,
        filename="notes-exhibit.htm",
        body=b"<html><body><h1>Independent exhibit</h1><p>Kept as text.</p></body></html>",
        role="exhibit",
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    original_selector = processing_module._xbrl_targets

    def select_classic(rows: Any, entrypoints: Any):
        return original_selector(
            [row for row in rows if row["role"] == "xbrl_instance"], entrypoints
        )

    monkeypatch.setattr(processing_module, "_xbrl_targets", select_classic)
    first = _parse(
        archive,
        workspace,
        protected,
        fetch_dependencies=lambda url: _dependency_response(url),
        allowed_taxonomy_hosts={"taxonomy.example.test"},
    )
    first_xbrl = next(
        row
        for row in first.snapshot.tables["parses"].to_pylist()
        if row["parser_name"] == processing_module.xbrl_parser.PARSER_NAME
    )
    assert first_xbrl["document_id"] == classic["document_id"]
    assert first_xbrl["status"] == "full"
    assert first.snapshot.tables["facts"].num_rows == 1
    assert first.snapshot.tables["facts"].to_pylist()[0]["raw_value"] == "100"

    monkeypatch.setattr(processing_module, "_xbrl_targets", original_selector)
    second = _parse(
        archive,
        workspace,
        protected,
        fetch_dependencies=lambda url: _dependency_response(url),
        allowed_taxonomy_hosts={"taxonomy.example.test"},
    )
    active_xbrl = [
        row
        for row in second.snapshot.tables["parses"].to_pylist()
        if row["parser_name"] == processing_module.xbrl_parser.PARSER_NAME
    ]
    primary = next(
        row
        for row in second.snapshot.tables["documents"].to_pylist()
        if row["original_filename"] == "annual_inline_100.htm"
    )
    assert [row["document_id"] for row in active_xbrl] == [primary["document_id"]]
    active_facts = second.snapshot.tables["facts"].to_pylist()
    assert len(active_facts) == 1
    assert active_facts[0]["document_id"] == primary["document_id"]
    assert active_facts[0]["raw_value"] == "100"
    classic_current = next(
        row
        for row in second.snapshot.tables["documents"].to_pylist()
        if row["document_id"] == classic["document_id"]
    )
    assert classic_current["fact_extraction_status"] == "not_attempted"
    current_parse = active_xbrl[0]
    current_notes = json.loads(current_parse["notes_json"])
    assert current_notes["retirement_reason"] == "superseded_derived_instance"
    assert current_notes["retired_parse_ids"] == [first_xbrl["parse_id"]]
    assert any(
        row["document_id"] == exhibit["document_id"] and row["section_kind"] == "fulltext"
        for row in second.snapshot.tables["sections"].to_pylist()
    )
    assert first.snapshot.tables["facts"].to_pylist()[0]["document_id"] == classic["document_id"]
    assert any(ref.path == classic["raw_path"] for ref in second.snapshot.raw_objects)
    assert (archive / classic["raw_path"]).read_bytes() == classic_instance


def test_failed_inline_attempt_does_not_retain_superseded_classic_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive, _cache, protected, _transport, _ = _catalog_then_download(
        tmp_path, primary="annual_inline_100.htm"
    )
    classic = _add_archive_document(
        archive,
        protected,
        filename="generated-instance.xml",
        body=CLASSIC_INSTANCE_100,
        role="xbrl_instance",
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    original_selector = processing_module._xbrl_targets

    def select_classic(rows: Any, entrypoints: Any):
        return original_selector(
            [row for row in rows if row["role"] == "xbrl_instance"], entrypoints
        )

    monkeypatch.setattr(processing_module, "_xbrl_targets", select_classic)
    old_head = _parse(
        archive,
        workspace,
        protected,
        fetch_dependencies=lambda url: _dependency_response(url),
        allowed_taxonomy_hosts={"taxonomy.example.test"},
    )
    old_xbrl = next(
        row
        for row in old_head.snapshot.tables["parses"].to_pylist()
        if row["parser_name"] == processing_module.xbrl_parser.PARSER_NAME
    )
    assert old_xbrl["document_id"] == classic["document_id"]
    assert old_head.snapshot.tables["facts"].num_rows == 1

    monkeypatch.setattr(processing_module, "_xbrl_targets", original_selector)

    def failed_inline(entrypoint: Path, **kwargs: Any) -> ParseResult:
        source_hash = hashlib.sha256(Path(entrypoint).read_bytes()).hexdigest()
        return ParseResult(
            filing_id=kwargs["filing_id"],
            document_id=kwargs["document_id"],
            parse_id=kwargs["parse_id"],
            parser_name=processing_module.xbrl_parser.PARSER_NAME,
            parser_version=processing_module.xbrl_parser.PARSER_VERSION,
            status="failed",
            source_hash=source_hash,
            validation_scope="not_performed",
            errors=[
                {
                    "code": "source_integrity_changed",
                    "message": "simulated inline cache integrity failure",
                    "severity": "error",
                }
            ],
        )

    monkeypatch.setattr(processing_module.xbrl_parser, "parse_xbrl", failed_inline)
    current = _parse(
        archive,
        workspace,
        protected,
        fetch_dependencies=lambda url: _dependency_response(url),
        allowed_taxonomy_hosts={"taxonomy.example.test"},
    )
    current_xbrl = [
        row
        for row in current.snapshot.tables["parses"].to_pylist()
        if row["parser_name"] == processing_module.xbrl_parser.PARSER_NAME
    ]
    assert len(current_xbrl) == 1
    assert current_xbrl[0]["status"] == "failed"
    assert current_xbrl[0]["fact_count"] == 0
    assert current.snapshot.tables["facts"].num_rows == 0
    assert old_head.snapshot.tables["facts"].to_pylist()[0]["document_id"] == classic["document_id"]


@pytest.mark.parametrize("max_filings", [0, 51, True, 1.5])
def test_limits_and_missing_archive_are_rejected_without_creation(
    tmp_path: Path, max_filings: Any
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    absent = tmp_path / "absent-archive"
    with pytest.raises(ProcessingError, match="max_filings"):
        parse_archive(
            absent,
            protected_paths=(tmp_path / "protected",),
            workspace_root=workspace,
            max_filings=max_filings,
        )
    assert not absent.exists()


def test_missing_and_unknown_filings_are_explicit_and_non_destructive(tmp_path: Path) -> None:
    archive, _cache, protected, _transport, _ = _catalog_then_download(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    before = _digest_tree(archive)
    with pytest.raises(ProcessingError, match="absent from the active snapshot"):
        parse_archive(
            archive,
            protected_paths=protected,
            workspace_root=workspace,
            filing_ids=("0000000123:0000999999-24-999999",),
        )
    assert _digest_tree(archive) == before
    assert not list(workspace.iterdir())


def test_protected_archive_or_workspace_is_rejected(tmp_path: Path) -> None:
    archive, _cache, protected, _transport, _ = _catalog_then_download(tmp_path)
    with pytest.raises(ProcessingError, match="workspace_root must be separate"):
        parse_archive(
            archive,
            protected_paths=protected,
            workspace_root=archive,
            filing_ids=(IDENTITY,),
        )
    protected_workspace = tmp_path / "protected-workspace"
    protected_workspace.mkdir()
    with pytest.raises(ProcessingError, match="workspace_root overlaps"):
        parse_archive(
            archive,
            protected_paths=(*protected, protected_workspace),
            workspace_root=protected_workspace,
            filing_ids=(IDENTITY,),
        )


def test_processing_reconciles_exception_after_published_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive, _cache, protected, _transport, acquired = _catalog_then_download(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    original_commit = ArchiveWriter.commit

    def commit_then_raise(writer: Any, **kwargs: Any) -> Any:
        published = original_commit(writer, **kwargs)
        if "parses" in kwargs.get("tables", {}):
            raise RuntimeError("simulated crash after manifest publication")
        return published

    monkeypatch.setattr(ArchiveWriter, "commit", commit_then_raise)
    result = _parse(
        archive,
        workspace,
        protected,
        fetch_dependencies=lambda url: _dependency_response(url),
        allowed_taxonomy_hosts={"taxonomy.example.test"},
    )
    assert result.snapshot.manifest_version == acquired.manifest_version + 1
    assert result.snapshot.tables["facts"].num_rows > 0
    assert read_snapshot(archive).snapshot_id == result.snapshot.snapshot_id


def test_modified_owned_workspace_or_committed_source_fails_closed(tmp_path: Path) -> None:
    archive, _cache, protected, _transport, acquired = _catalog_then_download(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    doc = next(
        row
        for row in acquired.tables["documents"].to_pylist()
        if row["original_filename"] == "annual_inline.htm"
    )
    raw_path = archive / doc["raw_path"]
    original = raw_path.read_bytes()
    raw_path.write_bytes(original + b"tampered")
    with pytest.raises(ArchiveCorruptionError):
        parse_archive(
            archive,
            protected_paths=protected,
            workspace_root=workspace,
            filing_ids=(IDENTITY,),
        )
    raw_path.write_bytes(original)
    read_snapshot(archive)

    package_workspace = workspace / IDENTITY
    package_workspace.mkdir()
    (package_workspace / "foreign.txt").write_text("foreign", encoding="utf-8")
    before = _digest_tree(archive)
    with pytest.raises(PackageConflictError):
        parse_archive(
            archive,
            protected_paths=protected,
            workspace_root=workspace,
            filing_ids=(IDENTITY,),
        )
    assert _digest_tree(archive) == before


def test_unapproved_taxonomy_host_is_rejected_before_callback(tmp_path: Path) -> None:
    archive, _cache, protected, _transport, _ = _catalog_then_download(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    calls: list[str] = []

    def fetch(url: str) -> Any:
        calls.append(url)
        return _dependency_response(url)

    before = _digest_tree(archive)
    result = _parse(archive, workspace, protected, fetch_dependencies=fetch)
    assert calls == []  # No caller-approved taxonomy host: callback never sees it.
    assert result.snapshot.manifest_version == 3
    xbrl = next(
        row
        for row in result.snapshot.tables["parses"].to_pylist()
        if row["parser_name"] == "filings.xbrl"
    )
    assert xbrl["status"] in {"failed", "partial"}
    assert result.snapshot.tables["dependencies"].num_rows >= 1
    assert _digest_tree(archive) != before


def test_cached_source_catalog_identity_and_original_dates_are_not_rewritten(
    tmp_path: Path,
) -> None:
    archive, _cache, protected, _transport, acquired = _catalog_then_download(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    original_filing = acquired.tables["filings"].to_pylist()[0]
    original_documents = {
        row["document_id"]: row["raw_sha256"]
        for row in acquired.tables["documents"].to_pylist()
        if row["fetch_status"] == "present"
    }
    result = _parse(
        archive,
        workspace,
        protected,
        fetch_dependencies=lambda url: _dependency_response(url),
        allowed_taxonomy_hosts={"taxonomy.example.test"},
    )
    updated = result.snapshot.tables["filings"].to_pylist()[0]
    docs = {
        row["document_id"]: row["raw_sha256"]
        for row in result.snapshot.tables["documents"].to_pylist()
        if row["fetch_status"] == "present"
    }
    assert updated["filing_id"] == original_filing["filing_id"] == IDENTITY
    assert updated["filed_date"] == original_filing["filed_date"]
    assert updated["effective_visible_session"] == original_filing["effective_visible_session"]
    assert docs == original_documents
    assert result.snapshot.tables["facts"].schema.equals(FACT_SCHEMA, check_metadata=True)
    assert result.snapshot.tables["sections"].schema.equals(SECTION_SCHEMA, check_metadata=True)
