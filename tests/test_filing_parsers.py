from __future__ import annotations

import hashlib
import json
import socket
import sys
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from filings.parse_text import parse_text
from filings.parse_xbrl import parse_xbrl
from filings.parsing_models import FACT_SCHEMA, SECTION_SCHEMA

FIXTURES = Path(__file__).parent / "fixtures" / "filings_parser"
FILING_ID = "0000123456:000000000000000001"


def _parse_xbrl(source: Path, root: Path, **kwargs):
    return parse_xbrl(
        source,
        filing_id=FILING_ID,
        document_id=source.name,
        parse_id="parse-test-1",
        allowed_root=root,
        **kwargs,
    )


def test_local_xbrl_preserves_occurrences_context_units_and_source_bytes() -> None:
    root = FIXTURES / "xbrl"
    source = root / "instance.xml"
    before = {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.iterdir()
        if path.is_file()
    }

    result = _parse_xbrl(source, root)

    assert result.status == "full"
    assert result.validation_scope == "arelle_structural_xbrl"
    assert result.source_hash == hashlib.sha256(source.read_bytes()).hexdigest()
    assert len(result.facts) == 11
    occurrence_ids = {row["occurrence_id"] for row in result.facts}
    assert len(occurrence_ids) == len(result.facts)
    assert all(
        row["parent_occurrence_id"] is None or row["parent_occurrence_id"] in occurrence_ids
        for row in result.facts
    )
    amounts = [row for row in result.facts if row["concept_local_name"] == "Amount"]
    assert len(amounts) == 2
    assert amounts[0]["occurrence_id"] != amounts[1]["occurrence_id"]
    assert [row["object_index"] for row in result.facts] == sorted(
        row["object_index"] for row in result.facts
    )
    assert {row["normalized_numeric"] for row in amounts} == {"100.00"}
    assert all(row["source_xml"] and row["source_line"] for row in result.facts)
    assert all(row["document_hash"] == result.source_hash for row in result.facts)

    nil_fact = next(row for row in result.facts if row["concept_local_name"] == "NilAmount")
    assert nil_fact["is_nil"] is True
    default_fact = next(row for row in result.facts if row["concept_local_name"] == "DefaultAmount")
    assert default_fact["raw_value"] == ""
    assert default_fact["transformed_value"] == "12"
    assert default_fact["normalized_numeric"] == "12"
    whitespace_fact = [
        row
        for row in result.facts
        if row["concept_local_name"] == "TextFact" and row["raw_value"] == "   "
    ]
    assert len(whitespace_fact) == 1
    fraction = next(row for row in result.facts if row["concept_local_name"] == "Rate")
    assert (fraction["fraction_numerator"], fraction["fraction_denominator"]) == ("1", "3")
    assert (
        fraction["validated_fraction_numerator"],
        fraction["validated_fraction_denominator"],
    ) == ("1", "3")
    assert json.loads(fraction["x_value_json"]) == {"denominator": "3", "numerator": "1"}
    assert fraction["normalized_numeric"] is None
    tuple_fact = next(row for row in result.facts if row["is_tuple"])
    tuple_child = next(row for row in result.facts if row["raw_value"] == "Tuple child")
    assert tuple_child["parent_occurrence_id"] == tuple_fact["occurrence_id"]

    context = json.loads(amounts[0]["context_json"])
    assert "2023-01-01" in context["context_xml"]
    assert "2023-12-31" in context["context_xml"]
    assert "RegionAxis" in context["context_xml"] and "NorthMember" in context["context_xml"]
    assert "ProductAxis" in context["context_xml"] and "Widget" in context["context_xml"]
    assert "segment metadata" in context["context_xml"]
    assert "scenario metadata" in context["context_xml"]
    unit = json.loads(amounts[0]["unit_json"])
    assert unit["numerator_measures"] == ["{http://www.xbrl.org/2003/iso4217}USD"]
    assert (unit["numerator_measures"], unit["denominator_measures"]) == (
        ["{http://www.xbrl.org/2003/iso4217}USD"],
        [],
    )
    ratio = json.loads(fraction["unit_json"])
    assert ratio["denominator_measures"]

    facts_table, sections_table = result.to_arrow_tables()
    assert facts_table.schema == FACT_SCHEMA
    assert sections_table.schema == SECTION_SCHEMA
    assert sections_table.num_rows == 0
    assert {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in before} == before

    repeated = _parse_xbrl(source, root)
    assert [row["occurrence_id"] for row in repeated.facts] == [
        row["occurrence_id"] for row in result.facts
    ]
    other_parse = parse_xbrl(
        source,
        filing_id=FILING_ID,
        document_id=source.name,
        parse_id="parse-test-2",
        allowed_root=root,
    )
    other_filing = parse_xbrl(
        source,
        filing_id="0000654321:000000000000000002",
        document_id=source.name,
        parse_id="parse-test-1",
        allowed_root=root,
    )
    other_document = parse_xbrl(
        source,
        filing_id=FILING_ID,
        document_id="different-document-id",
        parse_id="parse-test-1",
        allowed_root=root,
    )
    assert occurrence_ids.isdisjoint(row["occurrence_id"] for row in other_parse.facts)
    assert occurrence_ids.isdisjoint(row["occurrence_id"] for row in other_filing.facts)
    assert occurrence_ids.isdisjoint(row["occurrence_id"] for row in other_document.facts)


def test_inline_xbrl_keeps_raw_transformed_hidden_and_continued_values() -> None:
    root = FIXTURES / "xbrl"
    result = _parse_xbrl(root / "inline.html", root)

    assert result.status == "full"
    assert result.errors == []
    assert len(result.facts) == 3
    amount = next(row for row in result.facts if row["concept_local_name"] == "Amount")
    assert amount["raw_value"] == "1,234"
    assert amount["transformed_value"] == "-1234000"
    assert amount["normalized_numeric"] == "-1234000"
    assert amount["inline_format"] == "ixt:num-dot-decimal"
    assert amount["inline_sign"] == "-" and amount["inline_scale"] == "3"
    hidden = next(row for row in result.facts if row["raw_value"] == "hidden value")
    assert hidden["inline_hidden"] is True
    continued = next(row for row in result.facts if row["continued_at"] == "part-2")
    assert continued["raw_value"] == "Résumé continued text"


def test_inline_fraction_transforms_keep_exact_validated_rational() -> None:
    root = FIXTURES / "xbrl"
    result = _parse_xbrl(root / "inline-fraction.html", root)

    assert result.status == "full"
    assert len(result.facts) == 1
    fact = result.facts[0]
    assert (fact["fraction_numerator"], fact["fraction_denominator"]) == (
        "1,234",
        "200",
    )
    assert (fact["validated_fraction_numerator"], fact["validated_fraction_denominator"]) == (
        "-617",
        "10",
    )
    assert json.loads(fact["x_value_json"]) == {"numerator": "-617", "denominator": "10"}
    assert fact["fraction_numerator_format"] == "ixt:num-dot-decimal"
    assert fact["fraction_numerator_sign"] == "-"
    assert fact["fraction_numerator_scale"] == "1"
    assert fact["fraction_denominator_format"] == "ixt:num-dot-decimal"
    assert fact["normalized_numeric"] is None


def test_inline_tuple_parent_uses_arelle_semantics_across_wrappers_and_tuple_ref() -> None:
    root = FIXTURES / "xbrl"
    result = _parse_xbrl(root / "inline-tuples.html", root)

    assert result.status == "full"
    outer = next(row for row in result.facts if row["concept_local_name"] == "Record")
    nested = next(row for row in result.facts if row["concept_local_name"] == "NestedRecord")
    text_facts = {row["raw_value"]: row for row in result.facts if not row["is_tuple"]}
    assert nested["parent_occurrence_id"] == outer["occurrence_id"]
    assert (
        text_facts["Nested inline tuple child"]["parent_occurrence_id"] == nested["occurrence_id"]
    )
    assert text_facts["Out-of-DOM tupleRef child"]["parent_occurrence_id"] == outer["occurrence_id"]
    ids = {row["occurrence_id"] for row in result.facts}
    assert all(
        row["parent_occurrence_id"] is None or row["parent_occurrence_id"] in ids
        for row in result.facts
    )


def test_invalid_occurrence_is_retained_and_model_errors_are_partial() -> None:
    root = FIXTURES / "xbrl"
    result = _parse_xbrl(root / "invalid-instance.xml", root)

    assert result.status == "partial"
    assert len(result.facts) == 1
    row = result.facts[0]
    assert row["raw_value"] == "not-a-number"
    assert row["is_valid"] is False
    assert row["error_count"] == 1
    assert any(error["code"] == "invalid_xbrl_value" for error in result.errors)
    assert any(error["code"] == "xmlSchema:valueError" for error in result.errors)


def test_classic_zero_denominator_and_missing_context_fail_without_partial_rows() -> None:
    root = FIXTURES / "xbrl"
    source = root / "invalid-fraction-instance.xml"
    source_bytes = source.read_bytes()
    assert b'<ex:TextFact contextRef="MissingContext">' in source_bytes
    result = _parse_xbrl(source, root)

    assert result.status == "failed"
    assert result.validation_scope == "not_performed"
    assert result.facts == []
    assert result.source_hash == hashlib.sha256(source_bytes).hexdigest()
    assert source.read_bytes() == source_bytes
    error = next(
        error for error in result.errors if error["code"] == "arelle_zero_denominator_load_failed"
    )
    assert "ZeroDivisionError" in error["message"]
    assert "Fraction(1, 0)" in error["message"]
    assert "validation and fact extraction were not performed" in error["message"]


def test_inline_zero_denominator_fails_without_fabricated_fraction_qname() -> None:
    root = FIXTURES / "xbrl"
    source = root / "invalid-inline-fraction.html"
    source_bytes = source.read_bytes()
    assert b'name="ex:Rate"' in source_bytes
    result = _parse_xbrl(source, root)

    assert result.status == "failed"
    assert result.validation_scope == "not_performed"
    assert result.facts == []
    assert result.source_hash == hashlib.sha256(source_bytes).hexdigest()
    assert source.read_bytes() == source_bytes
    assert not any(row["fact_qname"].endswith("}fraction") for row in result.facts)
    error = next(
        error for error in result.errors if error["code"] == "arelle_zero_denominator_load_failed"
    )
    assert "ZeroDivisionError" in error["message"]
    assert "Fraction(" in error["message"]
    assert ", 0)" in error["message"]


def test_validation_errors_are_final_partial_and_invalid_occurrences_are_retained() -> None:
    root = FIXTURES / "xbrl"
    result = _parse_xbrl(root / "invalid-contexts-instance.xml", root)

    assert result.status == "partial"
    assert result.validation_scope == "arelle_structural_xbrl"
    assert len(result.facts) == 2
    malformed = next(row for row in result.facts if row["raw_value"] == "not-a-number")
    assert malformed["context_id"] == "MissingContext"
    assert malformed["unit_id"] == "MissingUnit"
    assert malformed["is_valid"] is False
    error_codes = {error["code"] for error in result.errors}
    assert "xbrl.4.6.1:itemContextRef" in error_codes
    assert "xbrl.4.6.2:numericUnit" in error_codes
    assert "xbrl.4.7.2:periodStartBeforeEnd" in error_codes
    assert "xbrldie:ExplicitMemberNotExplicitDimensionError" in error_codes
    assert any(error["code"] == "xmlSchema:valueError" for error in result.errors)


def test_parser_refuses_remote_doctype_traversal_and_symlink_dependencies(tmp_path: Path) -> None:
    remote_root = FIXTURES / "security"
    remote = _parse_xbrl(remote_root / "remote-dependency.xml", remote_root)
    assert remote.status == "failed"
    assert remote.errors[0]["code"] == "missing_dependency"
    assert (
        remote.source_hash
        == hashlib.sha256((remote_root / "remote-dependency.xml").read_bytes()).hexdigest()
    )

    entity = _parse_xbrl(remote_root / "entity.xml", remote_root)
    assert entity.status == "failed"
    assert entity.errors[0]["code"] == "xbrl_preflight_failed"

    legacy = _parse_xbrl(remote_root / "legacy.html", remote_root)
    assert legacy.status == "unsupported"
    assert legacy.facts == []

    wrong_namespace_root = FIXTURES / "xbrl"
    wrong_namespace = _parse_xbrl(
        wrong_namespace_root / "wrong-namespace.xml", wrong_namespace_root
    )
    assert wrong_namespace.status == "unsupported"
    assert wrong_namespace.facts == []

    allowed = tmp_path / "allowed"
    nested = allowed / "nested"
    nested.mkdir(parents=True)
    outside = tmp_path / "outside.xsd"
    outside.write_text("<schema/>", encoding="utf-8")
    traversal = nested / "instance.xml"
    traversal.write_text(
        '<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" '
        'xmlns:xlink="http://www.w3.org/1999/xlink" '
        'xmlns:link="http://www.xbrl.org/2003/linkbase">'
        '<link:schemaRef xlink:href="../../outside.xsd"/></xbrli:xbrl>',
        encoding="utf-8",
    )
    escaped = _parse_xbrl(traversal, allowed)
    assert escaped.status == "failed"
    assert escaped.errors[0]["code"] == "xbrl_preflight_failed"

    symlink_root = tmp_path / "symlink-root"
    symlink_root.mkdir()
    source = symlink_root / "instance.xml"
    source.write_text(
        '<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" '
        'xmlns:xlink="http://www.w3.org/1999/xlink" '
        'xmlns:link="http://www.xbrl.org/2003/linkbase">'
        '<link:schemaRef xlink:href="taxonomy.xsd"/></xbrli:xbrl>',
        encoding="utf-8",
    )
    (symlink_root / "taxonomy.xsd").symlink_to(FIXTURES / "xbrl" / "taxonomy.xsd")
    symlink = _parse_xbrl(source, symlink_root)
    assert symlink.status == "failed"
    assert symlink.errors[0]["code"] == "xbrl_preflight_failed"


def test_utf16_utf32_and_external_dtd_entity_declarations_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir()
    attempts: list[str] = []

    def deny(*args, **kwargs):
        attempts.append("network")
        raise AssertionError("unsafe XML must never load an external DTD")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(urllib.request, "urlopen", deny)
    entry = (
        '<?xml version="1.0" encoding="{encoding}"?>'
        '<!DOCTYPE xbrli:xbrl [<!ENTITY secret "blocked">]>'
        '<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance">&secret;</xbrli:xbrl>'
    )
    for encoding in ("utf-16", "utf-32"):
        path = allowed_root / f"entity-{encoding}.xml"
        path.write_bytes(entry.format(encoding=encoding).encode(encoding))
        result = _parse_xbrl(path, allowed_root)
        assert result.status == "failed"
        assert result.errors[0]["code"] == "xbrl_preflight_failed"
        assert result.source_hash == hashlib.sha256(path.read_bytes()).hexdigest()

    external = allowed_root / "external-dtd.xml"
    external.write_text(
        '<?xml version="1.0"?><!DOCTYPE xbrli:xbrl SYSTEM "https://evil.example.invalid/x.dtd">'
        '<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance"/>',
        encoding="utf-8",
    )
    external_result = _parse_xbrl(external, allowed_root)
    assert external_result.status == "failed"
    assert external_result.errors[0]["code"] == "xbrl_preflight_failed"

    inline_internal_subset = allowed_root / "inline-internal-dtd.html"
    inline_internal_subset.write_text(
        '<!DOCTYPE html [<!ENTITY unsafe "no">]>'
        '<html xmlns="http://www.w3.org/1999/xhtml" '
        'xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"><body>'
        '<ix:nonNumeric name="ex:TextFact" contextRef="c">&unsafe;</ix:nonNumeric>'
        "</body></html>",
        encoding="utf-8",
    )
    internal_result = _parse_xbrl(inline_internal_subset, allowed_root)
    assert internal_result.status == "failed"
    assert internal_result.errors[0]["code"] == "xbrl_preflight_failed"
    assert attempts == []

    source = allowed_root / "instance.xml"
    source.write_text(
        '<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" '
        'xmlns:link="http://www.xbrl.org/2003/linkbase" '
        'xmlns:xlink="http://www.w3.org/1999/xlink">'
        '<link:schemaRef xlink:href="utf16-taxonomy.xsd"/></xbrli:xbrl>',
        encoding="utf-8",
    )
    dependency = allowed_root / "utf16-taxonomy.xsd"
    for encoding in ("utf-16", "utf-32"):
        dependency.write_bytes(
            f'<?xml version="1.0" encoding="{encoding.upper()}"?>'
            '<!DOCTYPE schema [<!ENTITY secret "blocked">]>'
            '<schema xmlns="http://www.w3.org/2001/XMLSchema">&secret;</schema>'.encode(encoding)
        )
        result = _parse_xbrl(source, allowed_root)
        assert result.status == "failed"
        assert result.errors[0]["code"] == "xbrl_preflight_failed"


def test_cache_must_be_outside_readonly_input_root(tmp_path: Path) -> None:
    root = FIXTURES / "xbrl"
    result = _parse_xbrl(root / "instance.xml", root, cache_dir=root / "cache")
    assert result.status == "failed"
    assert result.errors[0]["code"] == "unsafe_cache_path"
    assert not (root / "cache").exists()


def test_xbrl_parse_never_attempts_network(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts: list[str] = []

    def deny(*args, **kwargs):
        attempts.append("network")
        raise AssertionError("network access must remain disabled")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(urllib.request, "urlopen", deny)
    root = FIXTURES / "xbrl"
    result = _parse_xbrl(root / "instance.xml", root)
    assert result.status == "full"
    assert attempts == []


def test_text_parser_preserves_unicode_tables_and_heading_anchors() -> None:
    source = FIXTURES / "text" / "annual-report.html"
    original = source.read_bytes()
    result = parse_text(
        source,
        filing_id=FILING_ID,
        document_id="annual-report",
        parse_id="text-parse-1",
    )

    assert result.status == "full"
    fulltext = next(row for row in result.sections if row["section_kind"] == "fulltext")
    content = fulltext["content_text"]
    assert "€12 million in München" in content
    assert "Résumé contains Unicode" in content
    assert "Cash $1,234" in content
    assert "Navigation Link" not in content and "Other navigation" not in content
    assert "not filing text" not in content and ".hidden" not in content
    headings = [row for row in result.sections if row["section_kind"] == "heading"]
    assert [row["heading"] for row in headings] == ["Financial Highlights", "Risk Factors"]
    assert headings[0]["source_xpath"] and headings[0]["source_ordinal"] == 1
    assert "Cash $1,234" in headings[0]["content_text"]
    assert "Résumé contains Unicode" in headings[1]["content_text"]
    assert source.read_bytes() == original

    facts_table, sections_table = result.to_arrow_tables()
    assert facts_table.schema == FACT_SCHEMA and facts_table.num_rows == 0
    assert sections_table.schema == SECTION_SCHEMA
    assert sections_table.num_rows == len(result.sections)


def test_text_parser_excludes_inline_xbrl_metadata_but_keeps_visible_values_and_anchors() -> None:
    source = FIXTURES / "text" / "inline-xbrl-visible.html"
    original = source.read_bytes()
    expected_hash = hashlib.sha256(original).hexdigest()
    result = parse_text(
        source,
        filing_id=FILING_ID,
        document_id="inline-xbrl-visible",
        parse_id="text-inline-xbrl",
        expected_hash=expected_hash,
    )

    assert result.status == "full"
    assert result.parser_version == "1.0.3"
    assert result.source_hash == expected_hash
    fulltext = next(row for row in result.sections if row["section_kind"] == "fulltext")
    content = fulltext["content_text"]
    for visible in (
        "Human Visible Report Header",
        "Visible revenue 1,234 USD",
        "Management discussion retained",
        "Visible ratio 3 over 4",
        "Visible tuple value retained",
        "Visible table metric",
        "Visible table value",
        "Human Header Tail Retained",
        "Alternate Header Tail Retained",
        "Hidden Fact Tail Retained",
        "Hidden Attribute Tail Retained",
        "Hidden CSS Tail Retained",
        "Visible Important Style Retained",
    ):
        assert visible in content
    for machine_only in (
        "MachineEntitySentinel",
        "MachineUnitSentinel",
        "MachineReferencesSentinel",
        "AlternateMachineEntitySentinel",
        "AlternateMachineUnitSentinel",
        "AlternateMachineReferencesSentinel",
        "MachineHiddenFactSentinel",
        "HiddenAttributeSentinel",
        "HiddenCssSentinel",
        "2099-12-31",
        "2098-01-01",
    ):
        assert machine_only not in content
    assert "inline XBRL metadata" in fulltext["provenance"]
    assert "not browser-rendered or economic-coverage validation" in fulltext["provenance"]

    heading = next(row for row in result.sections if row["section_kind"] == "heading")
    assert heading["heading"] == "Financial Highlights"
    assert heading["source_ordinal"] == 1
    assert "Visible revenue 1,234 USD" in heading["content_text"]
    assert "MachineEntitySentinel" not in heading["content_text"]
    from lxml import etree

    original_root = etree.fromstring(original, etree.HTMLParser(encoding="utf-8", no_network=True))
    original_tree = original_root.getroottree()
    heading_nodes = original_tree.xpath(heading["source_xpath"])
    assert len(heading_nodes) == 1
    assert str(heading_nodes[0].tag).casefold() == "h1"
    assert original_tree.getpath(heading_nodes[0]) == heading["source_xpath"]
    assert source.read_bytes() == original


def test_text_parser_root_hash_guard_and_utf16_html(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "utf16.html"
    original = (
        '<!doctype html><html><head><meta charset="utf-16"></head>'
        "<body><p>Résumé München</p></body></html>"
    ).encode("utf-16")
    source.write_bytes(original)
    digest = hashlib.sha256(original).hexdigest()
    result = parse_text(
        source,
        filing_id=FILING_ID,
        document_id="utf16",
        parse_id="text-utf16",
        allowed_root=workspace,
        expected_hash=digest,
    )
    assert result.status == "full"
    assert result.validation_scope == "not_applicable_text_extraction"
    assert result.sections[0]["validation_scope"] == "not_applicable_text_extraction"
    assert "Résumé München" in result.sections[0]["content_text"]
    assert result.source_hash == digest
    assert result.sections[0]["document_hash"] == digest
    assert source.read_bytes() == original

    bad_hash = parse_text(
        source,
        filing_id=FILING_ID,
        document_id="utf16",
        parse_id="text-bad-hash",
        allowed_root=workspace,
        expected_hash="0" * 64,
    )
    assert bad_hash.status == "failed"
    assert bad_hash.errors[0]["code"] == "source_hash_mismatch"

    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    escaped = parse_text(
        outside,
        filing_id=FILING_ID,
        document_id="outside",
        parse_id="text-outside",
        allowed_root=workspace,
    )
    assert escaped.status == "failed"
    assert escaped.errors[0]["code"] == "source_path_rejected"

    symlink = workspace / "link.html"
    symlink.symlink_to(source)
    rejected_link = parse_text(
        symlink,
        filing_id=FILING_ID,
        document_id="link",
        parse_id="text-link",
        allowed_root=workspace,
    )
    assert rejected_link.status == "failed"
    assert rejected_link.errors[0]["code"] == "source_path_rejected"

    original_read_bytes = Path.read_bytes
    reads = 0

    def mutate_on_post_read(path: Path) -> bytes:
        nonlocal reads
        if path == source:
            reads += 1
            if reads == 2:
                return original_read_bytes(path) + b"<!-- changed -->"
        return original_read_bytes(path)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(Path, "read_bytes", mutate_on_post_read)
    try:
        changed = parse_text(
            source,
            filing_id=FILING_ID,
            document_id="utf16",
            parse_id="text-mutated",
            allowed_root=workspace,
        )
    finally:
        monkeypatch.undo()
    assert changed.status == "failed"
    assert changed.errors[0]["code"] == "source_integrity_changed"
    assert source.read_bytes() == original


def test_plain_text_encoding_fallback_and_pdf_unsupported(tmp_path: Path) -> None:
    text_file = tmp_path / "legacy.txt"
    text_file.write_bytes("caf\xe9\nline two".encode("latin-1"))
    text_result = parse_text(
        text_file, filing_id=FILING_ID, document_id="legacy", parse_id="text-1"
    )
    assert text_result.status == "partial"
    assert text_result.sections[0]["content_text"] == "café line two"
    assert text_result.errors[0]["code"] == "encoding_fallback"

    pdf_file = tmp_path / "report.pdf"
    pdf_file.write_bytes(b"%PDF-1.7\nnot extracted")
    pdf_result = parse_text(pdf_file, filing_id=FILING_ID, document_id="pdf", parse_id="text-2")
    assert pdf_result.status == "unsupported"
    assert pdf_result.sections == []
    assert pdf_result.errors[0]["code"] == "unsupported_pdf"
