"""Archive integration tests for bounded, source-authorized IXDS work units."""

from __future__ import annotations

import hashlib
import json
import socket
import sys
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

import pyarrow as pa
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TESTS_ROOT = REPO_ROOT / "tests"
FIXTURES = TESTS_ROOT / "fixtures" / "filings_ixds_processing"
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))
if str(TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(TESTS_ROOT))

import test_filing_processing as processing_fixtures  # noqa: E402

import filings.processing as processing_module  # noqa: E402
from filings.archive import open_archive, read_snapshot  # noqa: E402
from filings.dependencies import DependencyPreparationError  # noqa: E402
from filings.models import DOCUMENTS_SCHEMA  # noqa: E402
from filings.parsing_models import ParseResult  # noqa: E402
from filings.processing import (  # noqa: E402
    ProcessingCorruptionError,
    ProcessingError,
    parse_archive,
)

IDENTITY = processing_fixtures.IDENTITY


def _process(archive: Path, workspace: Path, protected: tuple[Path, ...], **kwargs: Any):
    return parse_archive(
        archive,
        protected_paths=protected,
        workspace_root=workspace,
        filing_ids=(IDENTITY,),
        max_filings=1,
        **kwargs,
    )


def _replace_document_bytes(
    archive: Path,
    protected: tuple[Path, ...],
    document_id: str,
    content: bytes,
) -> None:
    snapshot = read_snapshot(archive)
    rows = snapshot.tables["documents"].to_pylist()
    selected = next(row for row in rows if row["document_id"] == document_id)
    with open_archive(
        archive,
        processing_fixtures.load_archive_runspec(archive),
        protected_paths=protected,
        resume=True,
    ) as writer:
        reference = writer.put_raw_bytes(content)
        selected.update(
            {
                "raw_sha256": reference.sha256,
                "raw_path": reference.path,
                "byte_size": reference.byte_size,
                "fetch_status": "present",
            }
        )
        writer.commit(
            tables={"documents": pa.Table.from_pylist(rows, schema=DOCUMENTS_SCHEMA)},
            raw_objects=(reference,),
            expected_manifest_version=snapshot.manifest_version,
        )


def _group_archive(tmp_path: Path):
    archive, _cache, protected, _transport, _ = processing_fixtures._catalog_then_download(
        tmp_path, primary="annual_inline_100.htm"
    )
    snapshot = read_snapshot(archive)
    documents = snapshot.tables["documents"].to_pylist()
    primary = next(
        row for row in documents if row["filing_id"] == IDENTITY and row["role"] == "primary"
    )
    _replace_document_bytes(
        archive,
        protected,
        primary["document_id"],
        (FIXTURES / "a-primary.html").read_bytes(),
    )

    secondary = processing_fixtures._add_archive_document(
        archive,
        protected,
        filename="b-exhibit.html",
        body=(FIXTURES / "b-exhibit.html").read_bytes(),
        role="exhibit",
    )
    for filename in ("taxonomy.xsd", "xbrli.xsd", "xbrldt.xsd"):
        processing_fixtures._add_archive_document(
            archive,
            protected,
            filename=filename,
            body=(FIXTURES / filename).read_bytes(),
            role="schema",
        )
    current = read_snapshot(archive)
    primary = next(
        row
        for row in current.tables["documents"].to_pylist()
        if row["document_id"] == primary["document_id"]
    )
    secondary = next(
        row
        for row in current.tables["documents"].to_pylist()
        if row["document_id"] == secondary["document_id"]
    )
    return archive, protected, primary, secondary


def _deny_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def deny(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("IXDS archive processing attempted external network access")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(urllib.request, "urlopen", deny)


def _seed_solo_attempts(
    archive: Path,
    workspace: Path,
    protected: tuple[Path, ...],
    monkeypatch: pytest.MonkeyPatch,
):
    original = processing_module._resolve_inline_source_group
    monkeypatch.setattr(
        processing_module, "_resolve_inline_source_group", lambda *args, **kwargs: None
    )
    try:
        solo = _process(archive, workspace, protected)
    finally:
        monkeypatch.setattr(processing_module, "_resolve_inline_source_group", original)
    xbrl = [
        row
        for row in solo.snapshot.tables["parses"].to_pylist()
        if row["parser_name"] == processing_module.xbrl_parser.PARSER_NAME
    ]
    return solo, {row["document_id"]: row for row in xbrl}


def test_solo_attempts_become_one_real_group_parse_and_resume_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _deny_network(monkeypatch)
    archive, protected, primary, secondary = _group_archive(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    solo, old_parses = _seed_solo_attempts(archive, workspace, protected, monkeypatch)
    assert set(old_parses) == {primary["document_id"], secondary["document_id"]}
    prior_text_ids = {
        row["parse_id"]
        for row in solo.snapshot.tables["parses"].to_pylist()
        if row["parser_name"] == processing_module.text_parser.PARSER_NAME
    }

    def reject_fetch(url: str) -> Any:
        raise AssertionError(f"fixture should resolve local dependency without fetching: {url}")

    grouped = _process(
        archive,
        workspace,
        protected,
        fetch_dependencies=reject_fetch,
    )
    xbrl = [
        row
        for row in grouped.snapshot.tables["parses"].to_pylist()
        if row["parser_name"] == processing_module.xbrl_parser.PARSER_NAME
    ]
    assert len(xbrl) == 1
    assert xbrl[0]["document_id"] == primary["document_id"]
    assert xbrl[0]["status"] == "full"
    assert xbrl[0]["fact_count"] == 6
    assert xbrl[0]["parse_id"] != old_parses[primary["document_id"]]["parse_id"]
    notes = json.loads(xbrl[0]["notes_json"])
    group = notes["source_group"]
    assert notes["strategy"] == "inline_document_set"
    assert notes["source_group_fingerprint"] == processing_module._digest(group)
    assert [member["document_id"] for member in group["members"]] == [
        primary["document_id"],
        secondary["document_id"],
    ]
    assert set(notes["retired_parse_ids"]) == {
        old_parses[primary["document_id"]]["parse_id"],
        old_parses[secondary["document_id"]]["parse_id"],
    }
    assert {
        row["parse_id"]
        for row in grouped.snapshot.tables["parses"].to_pylist()
        if row["parser_name"] == processing_module.text_parser.PARSER_NAME
    } == prior_text_ids

    members_by_url = {member["source_url"]: member for member in group["members"]}
    actual_facts = grouped.snapshot.tables["facts"].to_pylist()
    assert len(actual_facts) == 6
    assert {row["source_uri"] for row in actual_facts} == set(members_by_url)
    assert all(row["source_hash"] == primary["raw_sha256"] for row in actual_facts)
    assert all(
        row["document_hash"] == members_by_url[row["source_uri"]]["raw_sha256"]
        for row in actual_facts
    )
    assert all(
        json.loads(row["provenance"])["source_document_id"]
        == members_by_url[row["source_uri"]]["document_id"]
        for row in actual_facts
    )
    current_docs = {
        row["document_id"]: row for row in grouped.snapshot.tables["documents"].to_pylist()
    }
    assert current_docs[primary["document_id"]]["fact_extraction_status"] == "full"
    assert current_docs[secondary["document_id"]]["fact_extraction_status"] == "full"

    repeated = _process(archive, workspace, protected)
    assert repeated.snapshot.snapshot_id == grouped.snapshot.snapshot_id
    assert repeated.skipped_filing_ids == (IDENTITY,)
    assert (
        repeated.snapshot.tables["parses"].to_pylist()
        == grouped.snapshot.tables["parses"].to_pylist()
    )
    resumed = parse_archive(
        archive,
        protected_paths=protected,
        workspace_root=workspace,
    )
    assert resumed.snapshot.snapshot_id == grouped.snapshot.snapshot_id
    assert resumed.selected_filing_ids == ()


def test_valid_group_failure_retires_old_solo_facts_and_records_membership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive, protected, primary, secondary = _group_archive(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _solo_snapshot, old_parses = _seed_solo_attempts(archive, workspace, protected, monkeypatch)
    calls: list[tuple[Path, tuple[Path, ...]]] = []

    def failed_group(entrypoint: Path, **kwargs: Any) -> ParseResult:
        member_paths = tuple(kwargs.get("inline_document_set", ()))
        calls.append((Path(entrypoint), member_paths))
        return ParseResult(
            filing_id=kwargs["filing_id"],
            document_id=kwargs["document_id"],
            parse_id=kwargs["parse_id"],
            parser_name=processing_module.xbrl_parser.PARSER_NAME,
            parser_version=processing_module.xbrl_parser.PARSER_VERSION,
            status="failed",
            source_hash=hashlib.sha256(Path(entrypoint).read_bytes()).hexdigest(),
            errors=[{"code": "arelle_failed", "message": "fixture failure", "severity": "error"}],
        )

    monkeypatch.setattr(processing_module.xbrl_parser, "parse_xbrl", failed_group)
    failed = _process(
        archive,
        workspace,
        protected,
        inline_document_sets={IDENTITY: (primary["document_id"], secondary["document_id"])},
    )
    assert len(calls) == 1
    assert len(calls[0][1]) == 2
    xbrl = [
        row
        for row in failed.snapshot.tables["parses"].to_pylist()
        if row["parser_name"] == processing_module.xbrl_parser.PARSER_NAME
    ]
    assert len(xbrl) == 1
    assert xbrl[0]["document_id"] == primary["document_id"]
    assert xbrl[0]["status"] == "failed"
    assert xbrl[0]["fact_count"] == 0
    assert failed.snapshot.tables["facts"].num_rows == 0
    notes = json.loads(xbrl[0]["notes_json"])
    assert set(notes["retired_parse_ids"]) == {
        old_parses[primary["document_id"]]["parse_id"],
        old_parses[secondary["document_id"]]["parse_id"],
    }
    assert {
        row["document_id"]: row["fact_extraction_status"]
        for row in failed.snapshot.tables["documents"].to_pylist()
        if row["document_id"] in {primary["document_id"], secondary["document_id"]}
    } == {primary["document_id"]: "failed", secondary["document_id"]: "failed"}


def test_dependency_preparation_failure_publishes_retryable_group_and_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _deny_network(monkeypatch)
    archive, protected, primary, secondary = _group_archive(tmp_path)
    independent = processing_fixtures._add_archive_document(
        archive,
        protected,
        filename="c-independent.html",
        body=(FIXTURES / "c-independent.html").read_bytes(),
        role="exhibit",
    )
    remote_taxonomy_url = "https://taxonomy.example.test/taxonomy.xsd"
    remote_primary = (
        (FIXTURES / "a-primary.html")
        .read_bytes()
        .replace(
            b'xlink:href="taxonomy.xsd"',
            f'xlink:href="{remote_taxonomy_url}"'.encode(),
        )
    )
    remote_exhibit = (
        (FIXTURES / "b-exhibit.html")
        .read_bytes()
        .replace(
            b'xlink:href="taxonomy.xsd"',
            f'xlink:href="{remote_taxonomy_url}"'.encode(),
        )
    )
    _replace_document_bytes(archive, protected, primary["document_id"], remote_primary)
    _replace_document_bytes(archive, protected, secondary["document_id"], remote_exhibit)
    before_seed = read_snapshot(archive)
    primary = next(
        row
        for row in before_seed.tables["documents"].to_pylist()
        if row["document_id"] == primary["document_id"]
    )

    def good_cached_fetch(url: str) -> Any:
        filename = Path(urlsplit(url).path).name
        body_path = FIXTURES / filename
        if not body_path.is_file():
            raise AssertionError(f"unexpected fixture dependency URL: {url}")
        return SimpleNamespace(
            body=body_path.read_bytes(),
            request_url=url,
            final_url=url,
            transport_url=url,
            redirect_chain=(),
        )

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    original_group_resolver = processing_module._resolve_inline_source_group
    monkeypatch.setattr(
        processing_module, "_resolve_inline_source_group", lambda *args, **kwargs: None
    )
    try:
        solo = _process(
            archive,
            workspace,
            protected,
            fetch_dependencies=good_cached_fetch,
            allowed_taxonomy_hosts={"taxonomy.example.test"},
        )
    finally:
        monkeypatch.setattr(
            processing_module, "_resolve_inline_source_group", original_group_resolver
        )

    old_numeric = {
        row["document_id"]: row
        for row in solo.snapshot.tables["parses"].to_pylist()
        if row["filing_id"] == IDENTITY
        and row["parser_name"] == processing_module.xbrl_parser.PARSER_NAME
    }
    assert set(old_numeric) == {
        primary["document_id"],
        secondary["document_id"],
        independent["document_id"],
    }
    independent_solo = old_numeric[independent["document_id"]]
    independent_facts = [
        row
        for row in solo.snapshot.tables["facts"].to_pylist()
        if row["parse_id"] == independent_solo["parse_id"]
    ]
    assert independent_solo["status"] == "full"
    assert independent_facts
    old_dependency_fingerprint = independent_solo["dependencies_fingerprint"]
    old_independent_dependencies = [
        row
        for row in solo.snapshot.tables["dependencies"].to_pylist()
        if row["filing_id"] == IDENTITY
        and row["dependency_fingerprint"] == old_dependency_fingerprint
    ]
    assert old_independent_dependencies
    old_text_ids = {
        row["parse_id"]
        for row in solo.snapshot.tables["parses"].to_pylist()
        if row["filing_id"] == IDENTITY
        and row["parser_name"] == processing_module.text_parser.PARSER_NAME
    }
    raw_objects_before = {
        (item.path, item.sha256, item.byte_size) for item in solo.snapshot.raw_objects
    }
    old_solo_ids = {
        old_numeric[primary["document_id"]]["parse_id"],
        old_numeric[secondary["document_id"]]["parse_id"],
    }
    group_ids = (primary["document_id"], secondary["document_id"])

    failed_urls: list[str] = []

    def fail_cached_dependency(url: str) -> Any:
        failed_urls.append(url)
        raise DependencyPreparationError(
            "fixture_dependency_unavailable",
            "a locally bound dependency was intentionally unavailable for this attempt",
            url=url,
        )

    original_error_row = processing_module._dependency_error_row
    failed_head = read_snapshot(archive)
    for corruption in ("fingerprint", "foreign_source_group"):
        with monkeypatch.context() as attempt_patch:

            def corrupt_error_row(
                *args: Any, _corruption: str = corruption, **kwargs: Any
            ) -> dict[str, Any]:
                row = original_error_row(*args, **kwargs)
                provenance = json.loads(row["provenance_json"])
                if provenance.get("kind") == "dependency_preparation_failed":
                    if _corruption == "fingerprint":
                        row["dependency_fingerprint"] = "f" * 64
                    else:
                        provenance["source_group_fingerprint"] = "e" * 64
                        row["provenance_json"] = json.dumps(
                            provenance, sort_keys=True, separators=(",", ":")
                        )
                return row

            attempt_patch.setattr(processing_module, "_dependency_error_row", corrupt_error_row)
            with pytest.raises(ProcessingCorruptionError):
                _process(
                    archive,
                    workspace,
                    protected,
                    fetch_dependencies=fail_cached_dependency,
                    allowed_taxonomy_hosts={"taxonomy.example.test"},
                    inline_document_sets={IDENTITY: group_ids},
                )
        assert read_snapshot(archive).snapshot_id == failed_head.snapshot_id
    assert failed_urls

    fail_calls_before = len(failed_urls)
    failed = _process(
        archive,
        workspace,
        protected,
        fetch_dependencies=fail_cached_dependency,
        allowed_taxonomy_hosts={"taxonomy.example.test"},
        inline_document_sets={IDENTITY: group_ids},
    )
    assert len(failed_urls) > fail_calls_before
    group_parse = next(
        row
        for row in failed.snapshot.tables["parses"].to_pylist()
        if row["filing_id"] == IDENTITY
        and row["parser_name"] == processing_module.xbrl_parser.PARSER_NAME
        and row["document_id"] == primary["document_id"]
    )
    assert group_parse["status"] == "failed"
    assert group_parse["fact_count"] == 0
    notes = json.loads(group_parse["notes_json"])
    assert notes["dependency_mode"] == "preparation_failed"
    assert set(notes["retired_parse_ids"]) == old_solo_ids
    assert notes["dependency_preparation"]["diagnostic_code"] == "fixture_dependency_unavailable"
    failure_fingerprint = group_parse["dependencies_fingerprint"]
    error_rows = [
        row
        for row in failed.snapshot.tables["dependencies"].to_pylist()
        if row["filing_id"] == IDENTITY
        and row["dependency_fingerprint"] == failure_fingerprint
        and row["status"] == "error"
        and json.loads(row["provenance_json"]).get("kind") == "dependency_preparation_failed"
    ]
    assert len(error_rows) == 1
    error_provenance = json.loads(error_rows[0]["provenance_json"])
    assert error_provenance["filing_id"] == IDENTITY
    assert error_provenance["source_group_fingerprint"] == notes["source_group_fingerprint"]
    assert error_provenance["dependency_fingerprint"] == failure_fingerprint
    assert {entry["document_id"] for entry in error_provenance["entrypoints"]} == set(group_ids)
    assert all(
        entry["document_id"] != independent["document_id"]
        for entry in error_provenance["entrypoints"]
    )

    current_parses = [
        row
        for row in failed.snapshot.tables["parses"].to_pylist()
        if row["filing_id"] == IDENTITY
        and row["parser_name"] == processing_module.xbrl_parser.PARSER_NAME
    ]
    current_by_doc = {row["document_id"]: row for row in current_parses}
    assert set(current_by_doc) == {primary["document_id"], independent["document_id"]}
    assert current_by_doc[independent["document_id"]]["parse_id"] == independent_solo["parse_id"]
    current_facts = [
        row for row in failed.snapshot.tables["facts"].to_pylist() if row["filing_id"] == IDENTITY
    ]
    assert not any(row["parse_id"] in old_solo_ids for row in current_facts)
    assert any(row["parse_id"] in old_solo_ids for row in solo.snapshot.tables["facts"].to_pylist())
    assert {row["occurrence_id"] for row in current_facts} == {
        row["occurrence_id"] for row in independent_facts
    }
    assert failed.snapshot.tables["facts"].num_rows == len(independent_facts)
    docs_after_failure = {
        row["document_id"]: row for row in failed.snapshot.tables["documents"].to_pylist()
    }
    assert docs_after_failure[primary["document_id"]]["fact_extraction_status"] == "failed"
    assert docs_after_failure[secondary["document_id"]]["fact_extraction_status"] == "failed"
    assert docs_after_failure[independent["document_id"]]["fact_extraction_status"] == "full"
    assert {
        row["parse_id"]
        for row in failed.snapshot.tables["parses"].to_pylist()
        if row["filing_id"] == IDENTITY
        and row["parser_name"] == processing_module.text_parser.PARSER_NAME
    } == old_text_ids
    assert {
        (item.path, item.sha256, item.byte_size) for item in failed.snapshot.raw_objects
    } == raw_objects_before
    assert [
        row
        for row in failed.snapshot.tables["dependencies"].to_pylist()
        if row["filing_id"] == IDENTITY
        and row["dependency_fingerprint"] == old_dependency_fingerprint
    ] == old_independent_dependencies

    failed_head = failed.snapshot
    repeat_failed = _process(
        archive,
        workspace,
        protected,
        fetch_dependencies=fail_cached_dependency,
        allowed_taxonomy_hosts={"taxonomy.example.test"},
    )
    assert repeat_failed.snapshot.snapshot_id == failed_head.snapshot_id
    assert repeat_failed.processed_filing_ids == (IDENTITY,)
    assert repeat_failed.skipped_filing_ids == ()
    assert repeat_failed.failed_parse_count == 1

    recovered = _process(
        archive,
        workspace,
        protected,
        fetch_dependencies=good_cached_fetch,
        allowed_taxonomy_hosts={"taxonomy.example.test"},
    )
    recovered_xbrl = [
        row
        for row in recovered.snapshot.tables["parses"].to_pylist()
        if row["filing_id"] == IDENTITY
        and row["parser_name"] == processing_module.xbrl_parser.PARSER_NAME
    ]
    recovered_by_doc = {row["document_id"]: row for row in recovered_xbrl}
    recovered_group = recovered_by_doc[primary["document_id"]]
    assert recovered_group["status"] == "full"
    assert recovered_group["fact_count"] == 6
    assert recovered_group["dependencies_fingerprint"] != failure_fingerprint
    assert set(recovered_by_doc) == {primary["document_id"], independent["document_id"]}
    assert recovered_by_doc[independent["document_id"]]["parse_id"] == independent_solo["parse_id"]
    recovered_notes = json.loads(recovered_group["notes_json"])
    assert set(recovered_notes["retired_parse_ids"]) >= old_solo_ids | {group_parse["parse_id"]}
    recovered_dependencies = recovered.snapshot.tables["dependencies"].to_pylist()
    assert not any(
        row["filing_id"] == IDENTITY and row["dependency_fingerprint"] == failure_fingerprint
        for row in recovered_dependencies
    )
    assert any(
        row["filing_id"] == IDENTITY and row["dependency_fingerprint"] == old_dependency_fingerprint
        for row in recovered_dependencies
    )
    assert sum(
        row["filing_id"] == IDENTITY for row in recovered.snapshot.tables["facts"].to_pylist()
    ) == 6 + len(independent_facts)


def test_changed_non_anchor_member_invalidates_whole_persisted_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _deny_network(monkeypatch)
    archive, protected, primary, secondary = _group_archive(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    first = _process(
        archive,
        workspace,
        protected,
        inline_document_sets={IDENTITY: (primary["document_id"], secondary["document_id"])},
    )
    old_parse = next(
        row
        for row in first.snapshot.tables["parses"].to_pylist()
        if row["parser_name"] == processing_module.xbrl_parser.PARSER_NAME
    )
    updated_body = (
        FIXTURES / "b-exhibit.html"
    ).read_bytes() + b"\n<!-- revised source binding -->\n"
    _replace_document_bytes(archive, protected, secondary["document_id"], updated_body)

    reprocessed_workspace = tmp_path / "workspace-reprocessed"
    reprocessed_workspace.mkdir()
    reprocessed = _process(archive, reprocessed_workspace, protected)
    xbrl = [
        row
        for row in reprocessed.snapshot.tables["parses"].to_pylist()
        if row["parser_name"] == processing_module.xbrl_parser.PARSER_NAME
    ]
    assert len(xbrl) == 1
    assert xbrl[0]["document_id"] == primary["document_id"]
    assert xbrl[0]["status"] == "full"
    assert xbrl[0]["parse_id"] != old_parse["parse_id"]
    current_group = json.loads(xbrl[0]["notes_json"])["source_group"]
    current_member = next(
        member
        for member in current_group["members"]
        if member["document_id"] == secondary["document_id"]
    )
    assert current_member["raw_sha256"] == hashlib.sha256(updated_body).hexdigest()
    assert len(reprocessed.snapshot.tables["facts"].to_pylist()) == 6


def test_saved_group_membership_expands_for_a_new_cross_referenced_member(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _deny_network(monkeypatch)
    archive, protected, primary, secondary = _group_archive(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    first = _process(
        archive,
        workspace,
        protected,
        inline_document_sets={IDENTITY: (primary["document_id"], secondary["document_id"])},
    )
    old_group_parse = next(
        row
        for row in first.snapshot.tables["parses"].to_pylist()
        if row["parser_name"] == processing_module.xbrl_parser.PARSER_NAME
    )
    added = processing_fixtures._add_archive_document(
        archive,
        protected,
        filename="d-cross-file.html",
        body=(FIXTURES / "d-cross-file.html").read_bytes(),
        role="exhibit",
    )
    next_workspace = tmp_path / "workspace-with-new-member"
    next_workspace.mkdir()

    expanded = _process(archive, next_workspace, protected)
    parses = [
        row
        for row in expanded.snapshot.tables["parses"].to_pylist()
        if row["parser_name"] == processing_module.xbrl_parser.PARSER_NAME
    ]
    assert len(parses) == 1
    assert parses[0]["status"] == "full"
    notes = json.loads(parses[0]["notes_json"])
    assert [member["document_id"] for member in notes["source_group"]["members"]] == [
        primary["document_id"],
        secondary["document_id"],
        added["document_id"],
    ]
    assert old_group_parse["parse_id"] in notes["retired_parse_ids"]
    assert len(expanded.snapshot.tables["facts"].to_pylist()) == 7
    added_facts = [
        row
        for row in expanded.snapshot.tables["facts"].to_pylist()
        if row["document_hash"] == added["raw_sha256"]
    ]
    assert len(added_facts) == 1
    assert json.loads(added_facts[0]["provenance"])["source_document_id"] == added["document_id"]


def test_shared_ids_or_namespaces_do_not_join_independent_inline_reports(
    tmp_path: Path,
) -> None:
    primary_path = tmp_path / "primary.xhtml"
    independent_path = tmp_path / "independent.xhtml"
    primary_path.write_bytes((FIXTURES / "a-primary.html").read_bytes())
    independent = (FIXTURES / "c-independent.html").read_text(encoding="utf-8")
    independent = independent.replace("own-context", "shared-context").replace(
        "own-usd", "shared-usd"
    )
    independent_path.write_text(independent, encoding="utf-8")

    def row(identity: str, url: str, path: Path, role: str) -> dict[str, Any]:
        body = path.read_bytes()
        return {
            "document_id": identity,
            "source_url": url,
            "original_filename": path.name,
            "role": role,
            "selection_status": "required",
            "fetch_status": "present",
            "raw_sha256": hashlib.sha256(body).hexdigest(),
            "byte_size": len(body),
        }

    primary = row("a" * 64, "https://example.test/primary.xhtml", primary_path, "primary")
    independent_row = row(
        "b" * 64, "https://example.test/independent.xhtml", independent_path, "exhibit"
    )
    rows = [primary, independent_row]
    entrypoints = {
        primary["source_url"]: primary_path,
        independent_row["source_url"]: independent_path,
    }
    assert processing_module._resolve_inline_source_group(rows, entrypoints) is None
    with pytest.raises(ProcessingError, match="explicit group"):
        processing_module._resolve_inline_source_group(
            rows,
            entrypoints,
            explicit_member_ids=(primary["document_id"], independent_row["document_id"]),
            explicit=True,
        )


def test_explicit_named_target_is_rejected_before_parser_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive, protected, primary, secondary = _group_archive(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    before = read_snapshot(archive)
    targeted = (
        (FIXTURES / "a-primary.html")
        .read_bytes()
        .replace(b"<ix:references>", b'<ix:references target="named-target">')
    )
    _replace_document_bytes(archive, protected, primary["document_id"], targeted)

    def parser_must_not_run(*_args: Any, **_kwargs: Any) -> ParseResult:
        raise AssertionError("unsupported target reached the public parser")

    monkeypatch.setattr(processing_module.xbrl_parser, "parse_xbrl", parser_must_not_run)
    with pytest.raises(ProcessingError, match="only the default target"):
        _process(
            archive,
            workspace,
            protected,
            inline_document_sets={IDENTITY: (primary["document_id"], secondary["document_id"])},
        )
    assert read_snapshot(archive).manifest_version == before.manifest_version + 1


def test_unknown_or_scalar_group_members_are_rejected_without_publication(
    tmp_path: Path,
) -> None:
    archive, protected, primary, secondary = _group_archive(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    before = read_snapshot(archive)
    with pytest.raises(ProcessingError, match="absent or non-inline"):
        _process(
            archive,
            workspace,
            protected,
            inline_document_sets={IDENTITY: (primary["document_id"], "f" * 64)},
        )
    assert read_snapshot(archive).snapshot_id == before.snapshot_id
    with pytest.raises(ProcessingError, match="explicit document-ID sequences"):
        parse_archive(
            archive,
            protected_paths=protected,
            workspace_root=workspace,
            filing_ids=(IDENTITY,),
            inline_document_sets={IDENTITY: primary["document_id"]},  # type: ignore[dict-item]
        )
