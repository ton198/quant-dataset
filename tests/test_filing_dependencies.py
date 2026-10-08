from __future__ import annotations

import hashlib
import shutil
import socket
import sys
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from filings.dependencies import (
    DependencyPreparationError,
    FetchResponse,
    arelle_cache_path,
    offline_runtime_options,
    prepare_dependencies,
)
from filings.parse_xbrl import parse_xbrl

FIXTURES = Path(__file__).parent / "fixtures" / "filings_dependencies"
PARSER_FIXTURES = Path(__file__).parent / "fixtures" / "filings_parser" / "xbrl"


def _copy_workspace(tmp_path: Path, fixture: Path) -> tuple[Path, Path]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    input_path = workspace / fixture.name
    shutil.copyfile(fixture, input_path)
    return workspace, input_path


def _response(
    body: bytes, url: str, final_url: str | None = None, chain: tuple[str, ...] = ()
) -> FetchResponse:
    return FetchResponse(body=body, final_url=final_url or url, redirect_chain=chain)


def test_prepare_graph_deduplicates_cycles_and_preserves_original_bytes(tmp_path: Path) -> None:
    workspace, entrypoint = _copy_workspace(tmp_path, FIXTURES / "graph" / "instance.xml")
    source_url = "https://filings.example.invalid/archive/instance.xml"
    sources = {
        "https://filings.example.invalid/taxonomy/root.xsd": (
            FIXTURES / "graph" / "root.xsd"
        ).read_bytes(),
        "https://filings.example.invalid/taxonomy/common/leaf.xsd": (
            FIXTURES / "graph" / "leaf.xsd"
        ).read_bytes(),
        "https://taxonomy.example.invalid/base.xsd": (FIXTURES / "graph" / "base.xsd").read_bytes(),
        "https://filings.example.invalid/archive/labels.xml": (
            FIXTURES / "graph" / "labels.xml"
        ).read_bytes(),
    }
    original_bytes = entrypoint.read_bytes()
    original_hash = hashlib.sha256(original_bytes).hexdigest()
    calls: list[str] = []

    def fetch(url: str):
        calls.append(url)
        # Compatible with the SEC client's response by shape, without importing it.
        return SimpleNamespace(body=sources[url], final_url=url)

    prepared = prepare_dependencies(
        {source_url: entrypoint},
        workspace_root=workspace,
        cache_dir=tmp_path / "private-cache",
        fetch=fetch,
        allowed_hosts={"filings.example.invalid", "taxonomy.example.invalid"},
    )
    assert set(calls) == set(sources)
    assert len(calls) == 4  # schemaRef/schemaLocation duplicate and two graph cycles.
    assert len(prepared.records) == 5
    assert prepared.entrypoint_path(source_url).read_bytes() == original_bytes
    assert prepared.records[0].sha256 == original_hash
    assert prepared.records[0].size == len(original_bytes)
    assert set(prepared.expected_hashes) == set(prepared.origin_map)
    assert all(path.is_relative_to(prepared.cache_dir) for path in prepared.expected_hashes)
    assert entrypoint.read_bytes() == original_bytes
    assert hashlib.sha256(entrypoint.read_bytes()).hexdigest() == original_hash
    with pytest.raises(TypeError):
        prepared.url_map["https://other.example.invalid/a.xsd"] = entrypoint  # type: ignore[index]


def test_local_companions_and_symlink_dependencies_are_handled_safely(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "archive").mkdir(parents=True)
    (workspace / "taxonomy").mkdir()
    entrypoint = workspace / "archive" / "instance.xml"
    entrypoint.write_text(
        '<root xmlns:xlink="http://www.w3.org/1999/xlink">'
        '<schemaRef xlink:href="../taxonomy/root.xsd"/></root>',
        encoding="utf-8",
    )
    root_schema = workspace / "taxonomy" / "root.xsd"
    root_schema.write_text(
        '<xsd:schema xmlns:xsd="http://www.w3.org/2001/XMLSchema">'
        '<xsd:include schemaLocation="leaf.xsd"/></xsd:schema>',
        encoding="utf-8",
    )
    leaf_schema = workspace / "taxonomy" / "leaf.xsd"
    leaf_schema.write_text(
        '<xsd:schema xmlns:xsd="http://www.w3.org/2001/XMLSchema"/>', encoding="utf-8"
    )
    source_url = "https://filings.example.invalid/archive/instance.xml"

    def deny_fetch(url: str) -> FetchResponse:
        raise AssertionError(f"local companion unexpectedly fetched: {url}")

    prepared = prepare_dependencies(
        {source_url: entrypoint},
        workspace_root=workspace,
        cache_dir=tmp_path / "cache-local",
        fetch=deny_fetch,
        allowed_hosts={"filings.example.invalid"},
    )
    assert (
        prepared.url_map["https://filings.example.invalid/taxonomy/root.xsd"].read_bytes()
        == root_schema.read_bytes()
    )
    assert (
        prepared.url_map["https://filings.example.invalid/taxonomy/leaf.xsd"].read_bytes()
        == leaf_schema.read_bytes()
    )
    assert len(prepared.records) == 3

    (workspace / "taxonomy" / "root.xsd").unlink()
    (workspace / "taxonomy" / "root.xsd").symlink_to(leaf_schema)
    with pytest.raises(DependencyPreparationError) as caught:
        prepare_dependencies(
            {source_url: entrypoint},
            workspace_root=workspace,
            cache_dir=tmp_path / "cache-symlink-dependency",
            fetch=deny_fetch,
            allowed_hosts={"filings.example.invalid"},
        )
    assert caught.value.code == "unsafe_path"
    assert not (tmp_path / "cache-symlink-dependency").exists()


def test_redirects_are_cached_as_aliases_and_checked_against_allowlist(tmp_path: Path) -> None:
    workspace, entrypoint = _copy_workspace(tmp_path, FIXTURES / "graph" / "instance.xml")
    source_url = "https://filings.example.invalid/archive/instance.xml"
    requested = "https://filings.example.invalid/taxonomy/root.xsd"
    final = "https://taxonomy.example.invalid/v2/root.xsd"
    bodies = {
        "https://taxonomy.example.invalid/v2/common/leaf.xsd": (
            FIXTURES / "graph" / "leaf.xsd"
        ).read_bytes(),
        "https://taxonomy.example.invalid/base.xsd": (FIXTURES / "graph" / "base.xsd").read_bytes(),
        "https://filings.example.invalid/archive/labels.xml": (
            FIXTURES / "graph" / "labels.xml"
        ).read_bytes(),
        final: (
            b'<xsd:schema xmlns:xsd="http://www.w3.org/2001/XMLSchema" '
            b'targetNamespace="https://example.test/filings"/>'
        ),
    }

    def fetch(url: str) -> FetchResponse:
        if url == requested:
            return _response(bodies[final], url, final, (final,))
        return _response(bodies[url], url)

    prepared = prepare_dependencies(
        {source_url: entrypoint},
        workspace_root=workspace,
        cache_dir=tmp_path / "cache-redirect",
        fetch=fetch,
        allowed_hosts={"filings.example.invalid", "taxonomy.example.invalid"},
    )
    assert prepared.url_map[requested].read_bytes() == bodies[final]
    assert prepared.url_map[final].read_bytes() == bodies[final]
    redirect_record = next(
        record for record in prepared.records if record.requested_url == requested
    )
    assert redirect_record.final_url == final
    assert redirect_record.redirect_chain == (final,)
    assert redirect_record.sha256 == hashlib.sha256(bodies[final]).hexdigest()

    bad_cases = [
        (
            lambda url: _response(b"<schema/>", url, "https://evil.example.invalid/x.xsd"),
            {"filings.example.invalid"},
            "untrusted_host",
        ),
        (
            lambda url: _response(
                b"<schema/>", url, url, ("https://evil.example.invalid/redirect.xsd",)
            ),
            {"filings.example.invalid"},
            "untrusted_host",
        ),
        (
            lambda url: SimpleNamespace(
                body=b"<schema/>",
                request_url="https://taxonomy.example.invalid/not-requested.xsd",
                final_url=requested,
            ),
            {"filings.example.invalid", "taxonomy.example.invalid"},
            "fetch_request_mismatch",
        ),
    ]
    for index, (callback, hosts, code) in enumerate(bad_cases):
        output = tmp_path / f"bad-redirect-{index}"
        with pytest.raises(DependencyPreparationError) as caught:
            prepare_dependencies(
                {source_url: entrypoint},
                workspace_root=workspace,
                cache_dir=output,
                fetch=callback,
                allowed_hosts=hosts,
            )
        assert caught.value.code == code
        assert not output.exists()


def test_multiple_entrypoints_keep_each_original_alias_and_source_bytes(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source_a = workspace / "a.xml"
    source_b = workspace / "b.xml"
    shutil.copyfile(FIXTURES / "entrypoints" / "a.xml", source_a)
    shutil.copyfile(FIXTURES / "entrypoints" / "b.xml", source_b)
    original_a = source_a.read_bytes()
    original_b = source_b.read_bytes()

    alias_a = "HTTPS://FILINGS.EXAMPLE.INVALID/archive/a.xml#first"
    second_alias_a = "https://filings.example.invalid/archive/a.xml#second"
    alias_b = "https://filings.example.invalid/archive/b.xml#source-b"
    canonical_a = "https://filings.example.invalid/archive/a.xml"
    canonical_b = "https://filings.example.invalid/archive/b.xml"

    prepared = prepare_dependencies(
        {alias_b: source_b, alias_a: source_a, second_alias_a: source_a},
        workspace_root=workspace,
        cache_dir=tmp_path / "cache-multiple-entrypoints",
        fetch=lambda url: pytest.fail(f"entrypoint without references unexpectedly fetched: {url}"),
        allowed_hosts={"filings.example.invalid"},
    )

    for alias in (alias_a, second_alias_a, canonical_a):
        assert prepared.url_map[alias].read_bytes() == original_a
    for alias in (alias_b, canonical_b):
        assert prepared.url_map[alias].read_bytes() == original_b

    records = {record.requested_url: record for record in prepared.records}
    assert records[canonical_a].sha256 == hashlib.sha256(original_a).hexdigest()
    assert records[canonical_b].sha256 == hashlib.sha256(original_b).hexdigest()
    assert records[canonical_a].local_path == prepared.url_map[alias_a]
    assert records[canonical_b].local_path == prepared.url_map[alias_b]
    for path, original, canonical in (
        (prepared.url_map[alias_a], original_a, canonical_a),
        (prepared.url_map[alias_b], original_b, canonical_b),
    ):
        assert prepared.origin_map[path] == canonical
        assert prepared.expected_hashes[path] == hashlib.sha256(original).hexdigest()


def test_redirected_relative_dependency_graph_fails_before_cache_publish(
    tmp_path: Path,
) -> None:
    workspace, entrypoint = _copy_workspace(tmp_path, FIXTURES / "graph" / "instance.xml")
    source_url = "https://filings.example.invalid/archive/instance.xml"
    requested = "https://filings.example.invalid/taxonomy/root.xsd"
    final = "https://taxonomy.example.invalid/v2/root.xsd"
    calls: list[str] = []

    def fetch(url: str) -> FetchResponse:
        calls.append(url)
        assert url == requested
        return _response(
            (FIXTURES / "graph" / "root.xsd").read_bytes(),
            url,
            final,
            (final,),
        )

    output = tmp_path / "cache-redirect-relative"
    with pytest.raises(DependencyPreparationError) as caught:
        prepare_dependencies(
            {source_url: entrypoint},
            workspace_root=workspace,
            cache_dir=output,
            fetch=fetch,
            allowed_hosts={"filings.example.invalid", "taxonomy.example.invalid"},
        )

    assert caught.value.code == "unsupported_redirect_relative_dependency"
    assert caught.value.url == requested
    assert "changed host or directory" in str(caught.value)
    assert calls == [requested]
    assert not output.exists()


def test_redirected_scheme_relative_dependency_fails_but_absolute_import_is_supported(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "instance.xml"
    requested = "http://filings.example.invalid/taxonomy/root.xsd"
    final = "https://filings.example.invalid/taxonomy/root.xsd"
    absolute_url = "https://taxonomy.example.invalid/leaf.xsd"
    source_url = "https://filings.example.invalid/archive/instance.xml"
    source.write_text(
        '<root xmlns:xlink="http://www.w3.org/1999/xlink">'
        f'<schemaRef xlink:href="{requested}"/></root>',
        encoding="utf-8",
    )
    leaf_body = b'<xsd:schema xmlns:xsd="http://www.w3.org/2001/XMLSchema"/>'
    scheme_relative_body = (
        b'<xsd:schema xmlns:xsd="http://www.w3.org/2001/XMLSchema">'
        b'<xsd:import schemaLocation="//taxonomy.example.invalid/leaf.xsd"/>'
        b"</xsd:schema>"
    )
    calls: list[str] = []

    def fetch_scheme_relative(url: str) -> FetchResponse:
        calls.append(url)
        assert url == requested
        return _response(scheme_relative_body, url, final, (final,))

    output = tmp_path / "cache-scheme-relative-redirect"
    with pytest.raises(DependencyPreparationError) as caught:
        prepare_dependencies(
            {source_url: source},
            workspace_root=workspace,
            cache_dir=output,
            fetch=fetch_scheme_relative,
            allowed_hosts={"filings.example.invalid", "taxonomy.example.invalid"},
        )

    assert caught.value.code == "unsupported_scheme_relative_resource"
    assert calls == [requested]
    assert not output.exists()
    assert not list(tmp_path.glob(f".{output.name}.stage-*"))
    calls.clear()

    absolute_body = (
        b'<xsd:schema xmlns:xsd="http://www.w3.org/2001/XMLSchema">'
        b'<xsd:import schemaLocation="https://taxonomy.example.invalid/leaf.xsd"/>'
        b'<xsd:include schemaLocation="#id"/>'
        b"</xsd:schema>"
    )

    def fetch_absolute(url: str) -> FetchResponse:
        calls.append(url)
        if url == requested:
            return _response(absolute_body, url, final, (final,))
        assert url == absolute_url
        return _response(leaf_body, url)

    prepared = prepare_dependencies(
        {source_url: source},
        workspace_root=workspace,
        cache_dir=tmp_path / "cache-absolute-import-redirect",
        fetch=fetch_absolute,
        allowed_hosts={"filings.example.invalid", "taxonomy.example.invalid"},
    )

    assert calls == [requested, absolute_url]
    assert prepared.url_map[requested].read_bytes() == absolute_body
    assert prepared.url_map[final].read_bytes() == absolute_body
    assert prepared.url_map[absolute_url].read_bytes() == leaf_body


def test_scheme_relative_entrypoint_reference_fails_before_any_child_fetch(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "instance.xml"
    source.write_text(
        '<root xmlns:xlink="http://www.w3.org/1999/xlink">'
        '<schemaRef xlink:href="//taxonomy.example.invalid/root.xsd"/></root>',
        encoding="utf-8",
    )
    source_url = "https://filings.example.invalid/archive/instance.xml"
    output = tmp_path / "cache-scheme-relative-entrypoint"
    calls: list[str] = []

    def deny_fetch(url: str) -> FetchResponse:
        calls.append(url)
        pytest.fail(f"scheme-relative reference unexpectedly fetched: {url}")

    with pytest.raises(DependencyPreparationError) as caught:
        prepare_dependencies(
            {source_url: source},
            workspace_root=workspace,
            cache_dir=output,
            fetch=deny_fetch,
            allowed_hosts={"filings.example.invalid", "taxonomy.example.invalid"},
        )

    assert caught.value.code == "unsupported_scheme_relative_resource"
    assert caught.value.url == source_url
    assert calls == []
    assert not output.exists()
    assert not list(tmp_path.glob(f".{output.name}.stage-*"))


def test_same_host_https_upgrade_with_relative_imports_fails_closed(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "instance.xml"
    requested = "http://filings.example.invalid/taxonomy/root.xsd"
    final = "https://filings.example.invalid/taxonomy/root.xsd"
    source_url = "https://filings.example.invalid/archive/instance.xml"
    source.write_text(
        '<root xmlns:xlink="http://www.w3.org/1999/xlink">'
        f'<schemaRef xlink:href="{requested}"/></root>',
        encoding="utf-8",
    )

    output = tmp_path / "cache-https-upgrade-relative"
    with pytest.raises(DependencyPreparationError) as caught:
        prepare_dependencies(
            {source_url: source},
            workspace_root=workspace,
            cache_dir=output,
            fetch=lambda url: _response(
                (FIXTURES / "graph" / "root.xsd").read_bytes(), url, final, (final,)
            ),
            allowed_hosts={"filings.example.invalid"},
        )

    requested_parts = urlsplit(requested)
    final_parts = urlsplit(final)
    assert requested_parts.hostname == final_parts.hostname
    assert requested_parts.path == final_parts.path
    assert (requested_parts.scheme, final_parts.scheme) == ("http", "https")
    assert caught.value.code == "unsupported_redirect_relative_dependency"
    assert "requested-URL origin" in str(caught.value)
    assert not output.exists()


@pytest.mark.parametrize("schema_scheme", ["http", "https"])
def test_public_offline_parse_uses_verified_path_relative_taxonomy_aliases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, schema_scheme: str
) -> None:
    run_root = tmp_path / "runroot"
    run_root.mkdir()
    workspace = run_root / "package"
    workspace.mkdir()
    source = workspace / "instance.xml"
    original_instance = (PARSER_FIXTURES / "instance.xml").read_bytes()
    parent_url = f"{schema_scheme}://taxonomy.example.invalid/base/taxonomy.xsd"
    assert original_instance.count(b"taxonomy.xsd") == 2
    source_bytes = original_instance.replace(b"taxonomy.xsd", parent_url.encode("ascii"))
    source.write_bytes(source_bytes)
    source_url = "https://filings.example.invalid/archive/instance.xml"

    parent_body = (PARSER_FIXTURES / "taxonomy.xsd").read_bytes()
    include = b'<xsd:include schemaLocation="common/leaf.xsd"/>'
    insert_before = b'  <xsd:element name="Amount"'
    assert insert_before in parent_body
    parent_body = parent_body.replace(insert_before, include + insert_before, 1)
    leaf_body = (
        b'<xsd:schema xmlns:xsd="http://www.w3.org/2001/XMLSchema" '
        b'targetNamespace="https://example.test/filings"/>'
    )
    resources = {
        parent_url: parent_body,
        f"{schema_scheme}://taxonomy.example.invalid/base/xbrli.xsd": (
            PARSER_FIXTURES / "xbrli.xsd"
        ).read_bytes(),
        f"{schema_scheme}://taxonomy.example.invalid/base/xbrldt.xsd": (
            PARSER_FIXTURES / "xbrldt.xsd"
        ).read_bytes(),
        f"{schema_scheme}://taxonomy.example.invalid/base/common/leaf.xsd": leaf_body,
    }
    calls: list[str] = []

    def fetch(url: str) -> SimpleNamespace:
        calls.append(url)
        https_url = url.replace("http://", "https://", 1)
        return SimpleNamespace(
            body=resources[url],
            request_url=url,
            transport_url=https_url,
            final_url=https_url,
            redirect_chain=(),
        )

    prepared = prepare_dependencies(
        {source_url: source},
        workspace_root=workspace,
        cache_dir=run_root / "cache",
        fetch=fetch,
        allowed_hosts={"filings.example.invalid", "taxonomy.example.invalid"},
    )
    assert set(calls) == set(resources)
    assert len(calls) == len(resources)

    for url, body in resources.items():
        transport_url = url.replace("http://", "https://", 1)
        expected_aliases = (url, transport_url) if url.startswith("http://") else (url,)
        digest = hashlib.sha256(body).hexdigest()
        alias_paths = {alias: prepared.url_map[alias] for alias in expected_aliases}
        for _alias, path in alias_paths.items():
            assert path.read_bytes() == body
            assert prepared.expected_hashes[path] == digest
            assert prepared.origin_map[path] in set(expected_aliases)
            assert prepared.url_map[prepared.origin_map[path]] == path
        if len(alias_paths) == 2 and len(set(alias_paths.values())) == 2:
            assert prepared.origin_map[alias_paths[url]] == url
            assert prepared.origin_map[alias_paths[transport_url]] == transport_url
        parent_cache_path = prepared.url_map[parent_url]
        if url == parent_url:
            for relative_name in ("xbrli.xsd", "xbrldt.xsd", "common/leaf.xsd"):
                relative_cache_path = parent_cache_path.parent / relative_name
                child_url = f"{schema_scheme}://taxonomy.example.invalid/base/{relative_name}"
                assert relative_cache_path.is_file()
                assert prepared.url_map[child_url] == relative_cache_path
        record = next(item for item in prepared.records if item.requested_url == url)
        assert record.final_url == transport_url
        assert record.transport_url == transport_url
        assert record.relative_base_url == url
        assert record.sha256 == digest

    primary_entry = prepared.entrypoint_path(source_url)
    primary_hash = hashlib.sha256(source_bytes).hexdigest()
    assert primary_entry.read_bytes() == source_bytes
    network_attempts: list[str] = []

    def deny_network(*args, **kwargs):
        network_attempts.append("blocked")
        raise AssertionError("offline Arelle attempted network access")

    with monkeypatch.context() as guard:
        guard.setattr(socket.socket, "connect", deny_network)
        guard.setattr(urllib.request, "urlopen", deny_network)
        guard.setattr(urllib.request.OpenerDirector, "open", deny_network)
        parsed = parse_xbrl(
            primary_entry,
            filing_id="0000000001:0000000001-24-000001",
            document_id="path-relative-taxonomy-proof",
            parse_id=f"path-relative-taxonomy-proof-{schema_scheme}",
            allowed_root=prepared.cache_dir,
            cache_dir=prepared.cache_dir,
            uri_map=prepared.url_map,
            source_origins=prepared.origin_map,
            expected_hashes=prepared.expected_hashes,
        )

    assert network_attempts == []
    assert parsed.status == "full"
    assert parsed.validation_scope == "arelle_structural_xbrl"
    assert parsed.source_hash == primary_hash
    assert len(parsed.facts) == 11
    amounts = [row for row in parsed.facts if row["concept_local_name"] == "Amount"]
    assert len(amounts) == 2
    assert {row["raw_value"] for row in amounts} == {"100.00"}
    assert {row["normalized_numeric"] for row in amounts} == {"100.00"}
    default_fact = next(row for row in parsed.facts if row["concept_local_name"] == "DefaultAmount")
    assert (default_fact["raw_value"], default_fact["normalized_numeric"]) == ("", "12")
    fraction = next(row for row in parsed.facts if row["concept_local_name"] == "Rate")
    assert (fraction["fraction_numerator"], fraction["fraction_denominator"]) == ("1", "3")
    assert all(row["document_hash"] == primary_hash for row in parsed.facts)
    assert source.read_bytes() == source_bytes
    assert all(
        hashlib.sha256(path.read_bytes()).hexdigest() == digest
        for path, digest in prepared.expected_hashes.items()
    )


def test_transport_alias_proof_rejects_crosshost_path_query_port_and_extra_hops(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "instance.xml"
    requested = "http://filings.example.invalid/taxonomy/root.xsd"
    transport = "https://filings.example.invalid/taxonomy/root.xsd"
    source_url = "https://filings.example.invalid/archive/instance.xml"
    source.write_text(
        '<root xmlns:xlink="http://www.w3.org/1999/xlink">'
        f'<schemaRef xlink:href="{requested}"/></root>',
        encoding="utf-8",
    )
    body = (FIXTURES / "graph" / "root.xsd").read_bytes()
    bad_responses = (
        (
            "https://taxonomy.example.invalid/taxonomy/root.xsd",
            transport,
            (),
            "unsupported_redirect_relative_dependency",
        ),
        (
            "https://filings.example.invalid/v2/root.xsd",
            transport,
            (),
            "unsupported_redirect_relative_dependency",
        ),
        (
            "https://filings.example.invalid/taxonomy/root.xsd?version=2",
            transport,
            (),
            "unsafe_url",
        ),
        (
            "https://filings.example.invalid:444/taxonomy/root.xsd",
            transport,
            (),
            "unsafe_url",
        ),
        (
            transport,
            transport,
            ("https://filings.example.invalid/v2/root.xsd",),
            "unsupported_redirect_relative_dependency",
        ),
    )
    for index, (final_url, transport_url, redirects, expected_code) in enumerate(bad_responses):
        output = tmp_path / f"cache-bad-transport-alias-{index}"
        calls: list[str] = []

        def fetch(
            url: str,
            _calls: list[str] = calls,
            _transport_url: str = transport_url,
            _final_url: str = final_url,
            _redirects: tuple[str, ...] = redirects,
        ) -> SimpleNamespace:
            _calls.append(url)
            return SimpleNamespace(
                body=body,
                request_url=url,
                transport_url=_transport_url,
                final_url=_final_url,
                redirect_chain=_redirects,
            )

        with pytest.raises(DependencyPreparationError) as caught:
            prepare_dependencies(
                {source_url: source},
                workspace_root=workspace,
                cache_dir=output,
                fetch=fetch,
                allowed_hosts={"filings.example.invalid", "taxonomy.example.invalid"},
            )
        assert caught.value.code == expected_code
        assert calls == [requested]
        assert not output.exists()
        assert not list(tmp_path.glob(f".{output.name}.stage-*"))


def test_same_host_https_upgrade_maps_both_urls_for_nonrelative_resource(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "instance.xml"
    requested = "http://filings.example.invalid/taxonomy/root.xsd"
    final = "https://filings.example.invalid/taxonomy/root.xsd"
    source_url = "https://filings.example.invalid/archive/instance.xml"
    source.write_text(
        '<root xmlns:xlink="http://www.w3.org/1999/xlink">'
        f'<schemaRef xlink:href="{requested}"/></root>',
        encoding="utf-8",
    )
    resource_body = b'<xsd:schema xmlns:xsd="http://www.w3.org/2001/XMLSchema"/>'

    prepared = prepare_dependencies(
        {source_url: source},
        workspace_root=workspace,
        cache_dir=tmp_path / "cache-https-upgrade-no-relative",
        fetch=lambda url: _response(resource_body, url, final, (final,)),
        allowed_hosts={"filings.example.invalid"},
    )

    for alias in (requested, final):
        path = prepared.url_map[alias]
        assert path.read_bytes() == resource_body
        assert prepared.expected_hashes[path] == hashlib.sha256(resource_body).hexdigest()
        assert prepared.origin_map[path] in {requested, final}
        assert prepared.url_map[prepared.origin_map[path]] == path
    record = next(record for record in prepared.records if record.requested_url == requested)
    assert record.final_url == final
    assert record.sha256 == hashlib.sha256(resource_body).hexdigest()


def test_rejects_entities_xml_base_legacy_html_and_unsafe_urls(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source_url = "https://filings.example.invalid/entry.xml"
    cases = {
        "entity.xml": b'<!DOCTYPE root [<!ENTITY secret SYSTEM "file:///etc/passwd">]><root>&secret;</root>',
        "xml-base.xml": (
            b'<root xmlns:xml="http://www.w3.org/XML/1998/namespace" '
            b'xml:base="https://elsewhere.example.invalid/"></root>'
        ),
        "legacy.html": b"<!doctype html><html><body>old filing</body></html>",
        "external-dtd.xml": (
            b'<!DOCTYPE schema SYSTEM "https://evil.example.invalid/schema.dtd"><schema/>'
        ),
    }
    expected = {
        "entity.xml": "xml_declaration_forbidden",
        "xml-base.xml": "unsupported_xml_base",
        "legacy.html": "unsupported_non_xml",
        "external-dtd.xml": "xml_declaration_forbidden",
    }
    for name, body in cases.items():
        source = workspace / name
        source.write_bytes(body)
        with pytest.raises(DependencyPreparationError) as caught:
            prepare_dependencies(
                {source_url: source},
                workspace_root=workspace,
                cache_dir=tmp_path / f"cache-{name}",
                fetch=lambda url: pytest.fail(f"unsafe XML triggered fetch: {url}"),
                allowed_hosts={"filings.example.invalid"},
            )
        assert caught.value.code == expected[name]
        assert source.read_bytes() == body

    for unsafe in (
        "file:///etc/passwd",
        "https://user:secret@filings.example.invalid/private.xsd",
        "https://evil.example.invalid/external.xsd",
        "https://filings.example.invalid:9443/nonstandard.xsd",
        "https://filings.example.invalid/%2e%2e/outside.xsd",
        "https://filings.example.invalid/taxonomy.xsd?cache=2",
    ):
        source = workspace / "reference.xml"
        source.write_text(
            '<root xmlns:xlink="http://www.w3.org/1999/xlink">'
            f'<schemaRef xlink:href="{unsafe}"/></root>',
            encoding="utf-8",
        )
        with pytest.raises(DependencyPreparationError):
            prepare_dependencies(
                {source_url: source},
                workspace_root=workspace,
                cache_dir=tmp_path / f"unsafe-{hashlib.sha256(unsafe.encode()).hexdigest()[:8]}",
                fetch=lambda url: pytest.fail(f"unsafe URL triggered fetch: {url}"),
                allowed_hosts={"filings.example.invalid"},
            )


def test_budget_missing_dependency_and_symlink_cache_fail_without_publish(tmp_path: Path) -> None:
    workspace, entrypoint = _copy_workspace(tmp_path, FIXTURES / "graph" / "instance.xml")
    source_url = "https://filings.example.invalid/archive/instance.xml"
    allowed = {"filings.example.invalid", "taxonomy.example.invalid"}

    cases = [
        (
            "missing",
            lambda url: (_ for _ in ()).throw(FileNotFoundError(url)),
            {},
            "missing_dependency",
        ),
        (
            "limit",
            lambda url: _response(b"<schema/>", url),
            {"max_dependencies": 1},
            "dependency_limit",
        ),
        (
            "size",
            lambda url: _response(b"<schema/>", url),
            {"max_total_bytes": 20},
            "total_size_limit",
        ),
    ]
    for name, fetch, options, code in cases:
        output = tmp_path / f"cache-{name}"
        with pytest.raises(DependencyPreparationError) as caught:
            prepare_dependencies(
                {source_url: entrypoint},
                workspace_root=workspace,
                cache_dir=output,
                fetch=fetch,
                allowed_hosts=allowed,
                **options,
            )
        assert caught.value.code == code
        assert not output.exists()

    with pytest.raises(DependencyPreparationError) as in_workspace:
        prepare_dependencies(
            {source_url: entrypoint},
            workspace_root=workspace,
            cache_dir=workspace / "cache",
            fetch=lambda url: pytest.fail("workspace cache should reject before fetching"),
            allowed_hosts=allowed,
        )
    assert in_workspace.value.code == "unsafe_cache_path"

    symlink_parent = tmp_path / "cache-parent"
    symlink_parent.mkdir()
    symlink = tmp_path / "cache-link"
    symlink.symlink_to(symlink_parent, target_is_directory=True)
    with pytest.raises(DependencyPreparationError) as cache_link:
        prepare_dependencies(
            {source_url: entrypoint},
            workspace_root=workspace,
            cache_dir=symlink / "private",
            fetch=lambda url: pytest.fail("symlink cache should reject before fetching"),
            allowed_hosts=allowed,
        )
    assert cache_link.value.code == "unsafe_path"


def test_runtime_cache_helper_and_public_parser_accept_only_verified_mappings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    entrypoint = workspace / "instance.xml"
    source_fixture = PARSER_FIXTURES / "instance.xml"
    original = source_fixture.read_bytes()
    entrypoint.write_bytes(original)
    source_url = "https://filings.example.invalid/archive/instance.xml"
    sources = {
        "taxonomy.xsd": (PARSER_FIXTURES / "taxonomy.xsd").read_bytes(),
        "xbrli.xsd": (PARSER_FIXTURES / "xbrli.xsd").read_bytes(),
        "xbrldt.xsd": (PARSER_FIXTURES / "xbrldt.xsd").read_bytes(),
    }

    def fetch(url: str) -> FetchResponse:
        return _response(sources[Path(urlsplit(url).path).name], url)

    prepared = prepare_dependencies(
        {source_url: entrypoint},
        workspace_root=workspace,
        cache_dir=tmp_path / "arelle-private-cache",
        fetch=fetch,
        allowed_hosts={"filings.example.invalid"},
    )
    cached_entrypoint = prepared.entrypoint_path(source_url)
    assert cached_entrypoint == arelle_cache_path(source_url, prepared.cache_dir)
    options = offline_runtime_options(cached_entrypoint, prepared.cache_dir)
    assert options.entrypointFile == str(cached_entrypoint)
    assert options.cacheDirectory == str(prepared.cache_dir)
    assert options.internetConnectivity == "offline"
    assert options.disablePersistentConfig is True

    attempts: list[str] = []

    def deny(*args, **kwargs):
        attempts.append("network")
        raise AssertionError("offline parser attempted network access")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(urllib.request, "urlopen", deny)

    from arelle.api.Session import Session

    with Session() as session:
        assert session.run(options=options) is True
        assert session._cntlr is not None
        assert session._cntlr.webCache.cacheDir == str(prepared.cache_dir)
        assert session._cntlr.disablePersistentConfig is True

    parsed = parse_xbrl(
        cached_entrypoint,
        filing_id="0000123456:000000000000000001",
        document_id="instance.xml",
        parse_id="offline-cache-parse-1",
        allowed_root=prepared.cache_dir,
        cache_dir=prepared.cache_dir,
        uri_map=prepared.url_map,
        source_origins=prepared.origin_map,
        expected_hashes=prepared.expected_hashes,
    )
    assert parsed.status == "full"
    assert parsed.validation_scope == "arelle_structural_xbrl"
    assert len(parsed.facts) == 11
    assert parsed.source_hash == hashlib.sha256(original).hexdigest()
    assert {row["source_uri"] for row in parsed.facts} == {source_url}
    assert all(row["document_hash"] == parsed.source_hash for row in parsed.facts)
    assert attempts == []
    assert entrypoint.read_bytes() == original
    assert all(
        hashlib.sha256(path.read_bytes()).hexdigest() == digest
        for path, digest in prepared.expected_hashes.items()
    )

    forged_hashes = dict(prepared.expected_hashes)
    forged_hashes[cached_entrypoint] = "0" * 64
    forged_hash = parse_xbrl(
        cached_entrypoint,
        filing_id="0000123456:000000000000000001",
        document_id="instance.xml",
        parse_id="forged-hash",
        allowed_root=prepared.cache_dir,
        uri_map=prepared.url_map,
        source_origins=prepared.origin_map,
        expected_hashes=forged_hashes,
    )
    assert forged_hash.status == "failed"
    assert forged_hash.errors[0]["code"] == "source_integrity_failed"

    missing_map = dict(prepared.url_map)
    missing_map.pop("https://filings.example.invalid/archive/taxonomy.xsd")
    missing = parse_xbrl(
        cached_entrypoint,
        filing_id="0000123456:000000000000000001",
        document_id="instance.xml",
        parse_id="missing-map",
        allowed_root=prepared.cache_dir,
        uri_map=missing_map,
        source_origins=prepared.origin_map,
        expected_hashes=prepared.expected_hashes,
    )
    assert missing.status == "failed" and not missing.facts

    outside = tmp_path / "outside.xsd"
    outside.write_text("<schema/>", encoding="utf-8")
    outside_map = dict(prepared.url_map)
    outside_map["https://filings.example.invalid/outside.xsd"] = outside
    forged_path = parse_xbrl(
        cached_entrypoint,
        filing_id="0000123456:000000000000000001",
        document_id="instance.xml",
        parse_id="outside-map",
        allowed_root=prepared.cache_dir,
        uri_map=outside_map,
        source_origins=prepared.origin_map,
        expected_hashes=prepared.expected_hashes,
    )
    assert forged_path.status == "failed" and not forged_path.facts


def test_postload_hash_check_prevents_full_result_after_input_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "instance.xml"
    original = (PARSER_FIXTURES / "instance.xml").read_bytes()
    source.write_bytes(original)
    source_url = "https://filings.example.invalid/archive/instance.xml"
    sources = {
        "taxonomy.xsd": (PARSER_FIXTURES / "taxonomy.xsd").read_bytes(),
        "xbrli.xsd": (PARSER_FIXTURES / "xbrli.xsd").read_bytes(),
        "xbrldt.xsd": (PARSER_FIXTURES / "xbrldt.xsd").read_bytes(),
    }
    prepared = prepare_dependencies(
        {source_url: source},
        workspace_root=workspace,
        cache_dir=tmp_path / "cache-post-hash",
        fetch=lambda url: _response(sources[Path(urlsplit(url).path).name], url),
        allowed_hosts={"filings.example.invalid"},
    )
    from arelle.api.Session import Session

    original_run = Session.run
    cached_entrypoint = prepared.entrypoint_path(source_url)

    def mutate_after_load(session, options, *args, **kwargs):
        result = original_run(session, options, *args, **kwargs)
        Path(options.entrypointFile).write_bytes(original + b"<!-- changed after load -->")
        return result

    monkeypatch.setattr(Session, "run", mutate_after_load)
    result = parse_xbrl(
        cached_entrypoint,
        filing_id="0000123456:000000000000000001",
        document_id="instance.xml",
        parse_id="mutated-after-load",
        allowed_root=prepared.cache_dir,
        uri_map=prepared.url_map,
        source_origins=prepared.origin_map,
        expected_hashes=prepared.expected_hashes,
    )
    assert result.status == "partial"
    assert len(result.facts) == 11
    assert any(error["code"] == "source_integrity_changed" for error in result.errors)
    assert source.read_bytes() == original


def test_preparer_allows_only_bare_html_doctype_on_inline_primary(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "inline.html"
    original = (PARSER_FIXTURES / "inline.html").read_bytes()
    source.write_bytes(original)
    source_url = "https://filings.example.invalid/archive/inline.html"
    sources = {
        "taxonomy.xsd": (PARSER_FIXTURES / "taxonomy.xsd").read_bytes(),
        "xbrli.xsd": (PARSER_FIXTURES / "xbrli.xsd").read_bytes(),
        "xbrldt.xsd": (PARSER_FIXTURES / "xbrldt.xsd").read_bytes(),
    }
    prepared = prepare_dependencies(
        {source_url: source},
        workspace_root=workspace,
        cache_dir=tmp_path / "cache-bare-doctype",
        fetch=lambda url: _response(sources[Path(urlsplit(url).path).name], url),
        allowed_hosts={"filings.example.invalid"},
    )
    assert prepared.entrypoint_path(source_url).read_bytes() == original
    result = parse_xbrl(
        prepared.entrypoint_path(source_url),
        filing_id="0000123456:000000000000000001",
        document_id="inline.html",
        parse_id="doctype-inline-1",
        allowed_root=prepared.cache_dir,
        uri_map=prepared.url_map,
        source_origins=prepared.origin_map,
        expected_hashes=prepared.expected_hashes,
    )
    assert result.status == "full"
    assert len(result.facts) == 3
    assert source.read_bytes() == original


def urlsplit_path(url: str):
    return urlsplit(url)
