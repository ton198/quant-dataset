from __future__ import annotations

import hashlib
import importlib
import json
import shutil
import socket
import subprocess
import sys
import urllib.request
from pathlib import Path

import pytest

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from arelle.ModelValue import qname  # noqa: E402

from filings import sec_transforms  # noqa: E402
from filings.parse_xbrl import PARSER_VERSION, parse_xbrl  # noqa: E402
from filings.sec_transforms import SecTransformIntegrityError  # noqa: E402

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "filings_parser" / "xbrl"
FILING_ID = "0000123456:000000000000000001"
TRANSFORM_NAMESPACE = "http://www.sec.gov/inlineXBRL/transformation/2015-08-31"
PINNED_COMMIT = "47a372d099168f8669d20a8a6bbe5cb16bbf71ac"


def _git_blob_sha1(content: bytes) -> str:
    header = b"blob " + str(len(content)).encode("ascii") + b"\0"
    return hashlib.sha1(header + content).hexdigest()


def _parse(source: Path, root: Path, *, parse_id: str = "sec-transform-test"):
    return parse_xbrl(
        source,
        filing_id=FILING_ID,
        document_id=source.name,
        parse_id=parse_id,
        allowed_root=root,
    )


def _deny_network(*_args, **_kwargs):
    raise AssertionError("SEC transformation loading or Arelle parsing attempted network access")


def test_vendored_source_is_exact_pinned_upstream_with_preserved_licenses() -> None:
    plugin_root = sec_transforms.sec_transform_plugin_path()
    manifest = json.loads((plugin_root / "UPSTREAM.json").read_text(encoding="utf-8"))

    assert manifest["project"] == "Arelle/EDGAR"
    assert manifest["release"] == "25.2.1.1"
    assert manifest["commit"] == PINNED_COMMIT
    for filename, pin in manifest["source_files"].items():
        content = (plugin_root / filename).read_bytes()
        assert hashlib.sha256(content).hexdigest() == pin["sha256"]
        assert _git_blob_sha1(content) == pin["git_blob_sha1"]
    assert "Copyright (c) 2008 Greg Hewgill" in (plugin_root / "text2num.py").read_text(
        encoding="utf-8"
    )
    assert "COPYRIGHT.md" in (plugin_root / "__init__.py").read_text(encoding="utf-8")
    assert "does not include the referenced `COPYRIGHT.md`" in (
        plugin_root / "NOTICE.md"
    ).read_text(encoding="utf-8")
    assert "Apache License" in (plugin_root / "LICENSE-APACHE-2.0.txt").read_text(encoding="utf-8")
    assert not (plugin_root / "COPYRIGHT.md").exists()
    assert PARSER_VERSION.endswith(f"edgar.25.2.1.1.{PINNED_COMMIT[:12]}")


def test_official_plugin_registers_sec_transform_qnames_and_functions() -> None:
    sec_transforms.sec_transform_plugin_path()
    # Import only after integrity checks, under its installed package name so the
    # upstream relative import resolves to the pinned sibling text2num module.
    plugin = importlib.import_module("filings._vendor.sec_transforms")
    registry = {}
    plugin.__pluginInfo__["ModelManager.LoadCustomTransforms"](registry)

    assert len(registry) == 18
    assert sec_transforms.has_complete_sec_transform_registry(registry)
    cases = {
        "numwordsen": ("four", "4"),
        "durwordsen": ("two years", "P2Y"),
        "datequarterend": ("First quarter 2024", "2024-03-31"),
        "countrynameen": ("United States", "US"),
        "stateprovnameen": ("California", "CA"),
        "edgarprovcountryen": ("Alberta", "A0"),
        "exchnameen": ("Nasdaq", "NASDAQ"),
        "entityfilercategoryen": ("Accelerated Filer", "Accelerated Filer"),
        "boolballotbox": ("☑", "true"),
    }
    for name, (source_value, expected) in cases.items():
        transform = registry[qname(TRANSFORM_NAMESPACE, f"ixt-sec:{name}")]
        assert callable(transform)
        assert transform(source_value) == expected


def test_parser_runs_with_only_pinned_local_sec_plugin_and_keeps_source_lexemes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(socket.socket, "connect", _deny_network)
    monkeypatch.setattr(urllib.request, "urlopen", _deny_network)
    source = FIXTURE_ROOT / "sec-transforms.html"
    original = source.read_bytes()
    source_hash = hashlib.sha256(original).hexdigest()

    result = _parse(source, FIXTURE_ROOT)

    assert result.status == "full"
    assert result.errors == []
    assert result.parser_version == PARSER_VERSION
    assert result.source_hash == source_hash
    assert len(result.facts) == 1
    amount = result.facts[0]
    assert amount["raw_value"] == "four"
    assert amount["transformed_value"] == "4"
    assert amount["normalized_numeric"] == "4"
    assert amount["document_hash"] == source_hash
    assert amount["source_xml"] and "four" in amount["source_xml"]
    assert amount["error_count"] == 0
    provenance = json.loads(amount["provenance"])
    assert provenance["sec_inline_transform_plugin"].endswith(PINNED_COMMIT)
    assert source.read_bytes() == original


def test_invalid_sec_transform_retains_occurrence_as_partial_not_zero(
    tmp_path: Path,
) -> None:
    allowed_root = tmp_path / "input"
    allowed_root.mkdir()
    for name in ("taxonomy.xsd", "xbrli.xsd", "xbrldt.xsd"):
        shutil.copyfile(FIXTURE_ROOT / name, allowed_root / name)
    raw = (
        (FIXTURE_ROOT / "sec-transforms.html")
        .read_bytes()
        .replace(b">four</ix:nonFraction>", b">four bananas</ix:nonFraction>")
    )
    source = allowed_root / "invalid-sec-transform.html"
    source.write_bytes(raw)

    result = _parse(source, allowed_root, parse_id="invalid-sec-transform")

    assert result.status == "partial"
    assert len(result.facts) == 1
    fact = result.facts[0]
    assert fact["raw_value"] == "four bananas"
    assert fact["normalized_numeric"] is None
    assert fact["status"] == "partial"
    assert any(error["code"] == "ix11.10.1.1:transformValueError" for error in result.errors)
    assert source.read_bytes() == raw


def test_tampered_pinned_transform_package_fails_before_arelle_and_has_no_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_root = sec_transforms.sec_transform_plugin_path()
    fake_package = tmp_path / "filings"
    fake_loader = fake_package / "sec_transforms.py"
    fake_plugin = fake_package / "_vendor" / "sec_transforms"
    fake_plugin.mkdir(parents=True)
    fake_loader.write_text("# isolated loader path for integrity regression\n", encoding="utf-8")
    for name in (
        "UPSTREAM.json",
        "NOTICE.md",
        "LICENSE-APACHE-2.0.txt",
        "text2num.py",
    ):
        shutil.copyfile(original_root / name, fake_plugin / name)
    (fake_plugin / "__init__.py").write_bytes(
        (original_root / "__init__.py").read_bytes() + b"\n# tampered\n"
    )
    monkeypatch.setattr(sec_transforms, "__file__", str(fake_loader))
    monkeypatch.setattr(socket.socket, "connect", _deny_network)
    monkeypatch.setattr(urllib.request, "urlopen", _deny_network)

    with pytest.raises(SecTransformIntegrityError, match="checksum"):
        sec_transforms.sec_transform_plugin_path()
    source = FIXTURE_ROOT / "sec-transforms.html"
    original = source.read_bytes()
    result = _parse(source, FIXTURE_ROOT, parse_id="tampered-plugin")

    assert result.status == "failed"
    assert result.validation_scope == "not_performed"
    assert result.facts == []
    assert result.errors[0]["code"] == "sec_transform_integrity_failed"
    assert result.source_hash == hashlib.sha256(original).hexdigest()
    assert source.read_bytes() == original


def test_incomplete_actual_arelle_registry_fails_without_publishing_occurrences(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        sec_transforms, "has_complete_sec_transform_registry", lambda _registry: False
    )
    source = FIXTURE_ROOT / "sec-transforms.html"
    result = _parse(source, FIXTURE_ROOT, parse_id="incomplete-sec-transform-registry")

    assert result.status == "failed"
    assert result.validation_scope == "not_performed"
    assert result.facts == []
    assert result.errors[0]["code"] == "sec_transform_registry_incomplete"
    assert result.source_hash == hashlib.sha256(source.read_bytes()).hexdigest()


def test_parser_has_no_user_supplied_plugin_path_option() -> None:
    import inspect

    parameters = inspect.signature(parse_xbrl).parameters
    assert "plugins" not in parameters
    assert "plugin_path" not in parameters


def test_transform_helper_and_cli_import_remain_lazy_before_an_explicit_parse() -> None:
    code = (
        "import sys; sys.path.insert(0, "
        f"{str(SOURCE_ROOT)!r}); "
        "import filings.sec_transforms; import cli.main; "
        "assert 'filings._vendor.sec_transforms' not in sys.modules; "
        "assert not any(name == 'arelle' or name.startswith('arelle.') "
        "for name in sys.modules)"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
