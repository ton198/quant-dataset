from __future__ import annotations

import hashlib
import shutil
import socket
import sys
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from filings.parse_xbrl import PARSER_VERSION, parse_xbrl
from filings.parsing_models import FACT_SCHEMA

FIXTURES = Path(__file__).parent / "fixtures" / "filings_ixds"
FILING_ID = "0000123456:000000000000000001"
_SOURCE_BASE = "https://ixds.example.test/filing/"


def _copy_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = tmp_path / "workspace"
    root.mkdir()
    for source in FIXTURES.iterdir():
        if source.is_file():
            shutil.copyfile(source, root / source.name)
    return root, root / "a-primary.html", root / "b-exhibit.html"


def _bindings(root: Path) -> tuple[dict[str, Path], dict[Path, str], dict[Path, str]]:
    paths = sorted(path for path in root.iterdir() if path.is_file())
    uri_map = {_SOURCE_BASE + path.name: path for path in paths}
    origins = {path: uri for uri, path in uri_map.items()}
    hashes = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
    return uri_map, origins, hashes


def _parse_group(
    primary: Path,
    exhibit: Path,
    root: Path,
    *,
    parse_id: str = "ixds-test-1",
    uri_map: dict[str, Path] | None = None,
    origins: dict[Path, str] | None = None,
    hashes: dict[Path, str] | None = None,
):
    if uri_map is None or origins is None or hashes is None:
        uri_map, origins, hashes = _bindings(root)
    return parse_xbrl(
        primary,
        filing_id=FILING_ID,
        document_id="primary-document-id",
        parse_id=parse_id,
        allowed_root=root,
        cache_dir=root,
        uri_map=uri_map,
        source_origins=origins,
        expected_hashes=hashes,
        inline_document_set=[primary, exhibit],
    )


def test_two_member_ixds_loads_one_offline_default_model_losslessly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, primary, exhibit = _copy_fixture(tmp_path)
    original_bytes = {path: path.read_bytes() for path in root.iterdir() if path.is_file()}
    expected_hashes = {
        path: hashlib.sha256(raw).hexdigest() for path, raw in original_bytes.items()
    }
    expected_origins = {path: _SOURCE_BASE + path.name for path in original_bytes}
    uri_map = {uri: path for path, uri in expected_origins.items()}
    network_attempts: list[str] = []

    def deny_network(*args, **kwargs):
        network_attempts.append("network")
        raise AssertionError("IXDS parsing must be offline")

    monkeypatch.setattr(socket.socket, "connect", deny_network)
    monkeypatch.setattr(urllib.request, "urlopen", deny_network)

    result = _parse_group(
        primary,
        exhibit,
        root,
        parse_id="ixds-full",
        uri_map=uri_map,
        origins=expected_origins,
        hashes=expected_hashes,
    )

    assert result.parser_version == PARSER_VERSION
    assert result.status == "full"
    assert result.validation_scope == "arelle_structural_xbrl"
    assert result.source_hash == expected_hashes[primary]
    assert result.errors == []
    assert len(result.facts) == 6
    assert all(row["document_id"] == "primary-document-id" for row in result.facts)
    assert all(row["source_hash"] == expected_hashes[primary] for row in result.facts)

    amounts = [row for row in result.facts if row["concept_local_name"] == "Amount"]
    assert [row["raw_value"] for row in amounts] == ["1,234", "1,234", "567"]
    assert len({row["occurrence_id"] for row in amounts}) == 3
    assert [row["normalized_numeric"] for row in amounts] == ["1234", "1234", "567"]
    assert [row["source_uri"] for row in amounts] == [
        expected_origins[primary],
        expected_origins[primary],
        expected_origins[exhibit],
    ]
    assert [row["document_hash"] for row in amounts] == [
        expected_hashes[primary],
        expected_hashes[primary],
        expected_hashes[exhibit],
    ]

    continued = next(row for row in result.facts if row["continued_at"] == "cross-file-tail")
    assert continued["source_uri"] == expected_origins[primary]
    assert continued["raw_value"] == "Cross-file Résumé continuation"
    tuple_fact = next(row for row in result.facts if row["is_tuple"])
    tuple_child = next(row for row in result.facts if row["raw_value"] == "Tuple child")
    assert tuple_child["parent_occurrence_id"] == tuple_fact["occurrence_id"]
    # A full result also verifies the cross-member ix:relationship endpoints resolved.
    assert result.facts_to_arrow().schema == FACT_SCHEMA
    assert network_attempts == []
    assert {path: path.read_bytes() for path in original_bytes} == original_bytes

    # The same SDK Session cannot leak the grouped target into a later standalone run.
    standalone = parse_xbrl(
        primary,
        filing_id=FILING_ID,
        document_id="primary-document-id",
        parse_id="ixds-standalone-after-group",
        allowed_root=root,
        expected_hashes=expected_hashes,
    )
    assert standalone.status in {"partial", "full"}
    assert len(standalone.facts) == 5
    assert all(row["source_relpath"] == primary.name for row in standalone.facts)
    assert standalone.source_hash == expected_hashes[primary]
    assert network_attempts == []


def test_ixds_source_guards_block_missing_outside_and_wrong_hash_members(tmp_path: Path) -> None:
    root, primary, exhibit = _copy_fixture(tmp_path)
    uri_map, origins, hashes = _bindings(root)

    bad_hashes = dict(hashes)
    bad_hashes[exhibit] = "0" * 64
    wrong_hash = _parse_group(
        primary,
        exhibit,
        root,
        parse_id="ixds-bad-member-hash",
        uri_map=uri_map,
        origins=origins,
        hashes=bad_hashes,
    )
    assert wrong_hash.status == "failed"
    assert wrong_hash.facts == []
    assert wrong_hash.errors[0]["code"] == "source_integrity_failed"

    missing = _parse_group(
        primary,
        root / "missing-member.html",
        root,
        parse_id="ixds-missing-member",
        uri_map=uri_map,
        origins=origins,
        hashes=hashes,
    )
    assert missing.status == "failed"
    assert missing.facts == []

    outside = tmp_path / "outside.html"
    shutil.copyfile(exhibit, outside)
    escaped = _parse_group(
        primary,
        outside,
        root,
        parse_id="ixds-outside-member",
        uri_map=uri_map,
        origins=origins,
        hashes=hashes,
    )
    assert escaped.status == "failed"
    assert escaped.facts == []


def test_ixds_unknown_fact_owner_and_post_parse_mutation_return_no_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, primary, exhibit = _copy_fixture(tmp_path)
    uri_map, origins, hashes = _bindings(root)

    import filings.parse_xbrl as parser

    outside = tmp_path / "unexpected-owner.html"
    outside.write_text("not a declared source", encoding="utf-8")
    original_source_details = parser._fact_source_details
    changed_one_owner = False

    def report_unknown_owner(*args, **kwargs):
        nonlocal changed_one_owner
        info = original_source_details(*args, **kwargs)
        if not changed_one_owner:
            changed_one_owner = True
            info["source_path"] = outside
            info["source_uri"] = "https://untrusted.example.test/fact.html"
            info["document_hash"] = hashlib.sha256(outside.read_bytes()).hexdigest()
            info["source_verified"] = True
        return info

    monkeypatch.setattr(parser, "_fact_source_details", report_unknown_owner)
    untrusted = _parse_group(
        primary,
        exhibit,
        root,
        parse_id="ixds-untrusted-owner",
        uri_map=uri_map,
        origins=origins,
        hashes=hashes,
    )
    assert changed_one_owner
    assert untrusted.status == "failed"
    assert untrusted.facts == []
    assert untrusted.errors[0]["code"] == "ixds_untrusted_fact_source"

    monkeypatch.setattr(parser, "_fact_source_details", original_source_details)
    from arelle.api.Session import Session

    original_run = Session.run

    def mutate_member_after_load(session, *args, **kwargs):
        succeeded = original_run(session, *args, **kwargs)
        exhibit.write_bytes(exhibit.read_bytes() + b"<!-- changed during parse -->")
        return succeeded

    monkeypatch.setattr(Session, "run", mutate_member_after_load)
    mutated = _parse_group(
        primary,
        exhibit,
        root,
        parse_id="ixds-mutated-member",
        uri_map=uri_map,
        origins=origins,
        hashes=hashes,
    )
    assert mutated.status == "failed"
    assert mutated.facts == []
    assert any(error["code"] == "source_integrity_changed" for error in mutated.errors)


def test_ixds_named_targets_and_missing_context_are_not_silently_accepted(tmp_path: Path) -> None:
    root, primary, exhibit = _copy_fixture(tmp_path)
    original_exhibit = exhibit.read_text(encoding="utf-8")
    exhibit.write_text(
        original_exhibit.replace("<ix:references>", '<ix:references target="named-target">'),
        encoding="utf-8",
    )
    uri_map, origins, hashes = _bindings(root)
    named = _parse_group(
        primary,
        exhibit,
        root,
        parse_id="ixds-named-target",
        uri_map=uri_map,
        origins=origins,
        hashes=hashes,
    )
    assert named.status == "unsupported"
    assert named.facts == []
    assert named.errors[0]["code"] == "unsupported_inline_document_set"

    exhibit.write_text(
        original_exhibit.replace('contextRef="shared-context"', 'contextRef="missing-context"'),
        encoding="utf-8",
    )
    uri_map, origins, hashes = _bindings(root)
    missing_context = _parse_group(
        primary,
        exhibit,
        root,
        parse_id="ixds-missing-context",
        uri_map=uri_map,
        origins=origins,
        hashes=hashes,
    )
    assert missing_context.status == "partial"
    assert len(missing_context.facts) == 6
    assert any(
        error["code"] in {"xbrl.4.6.1:itemContextRef", "invalid_xbrl_value"}
        for error in missing_context.errors
    )
    invalid = next(row for row in missing_context.facts if row["context_id"] == "missing-context")
    assert invalid["context_id"] == "missing-context"


def test_ixds_preflight_only_allows_bare_html_doctype_for_declared_members(tmp_path: Path) -> None:
    root, primary, exhibit = _copy_fixture(tmp_path)
    exhibit.write_text(
        exhibit.read_text(encoding="utf-8").replace(
            "<!DOCTYPE html>",
            '<!DOCTYPE html [<!ENTITY secret "must-not-expand">]>',
        ),
        encoding="utf-8",
    )
    uri_map, origins, hashes = _bindings(root)
    unsafe = _parse_group(
        primary,
        exhibit,
        root,
        parse_id="ixds-doctype-attack",
        uri_map=uri_map,
        origins=origins,
        hashes=hashes,
    )
    assert unsafe.status == "failed"
    assert unsafe.facts == []
    assert unsafe.errors[0]["code"] == "xbrl_preflight_failed"
    assert "DOCTYPE" in unsafe.errors[0]["message"]
