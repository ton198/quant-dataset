"""Offline XBRL/iXBRL occurrence parser with a local-file dependency boundary."""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
import threading
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit
from urllib.request import url2pathname

from filings.parsing_models import ParseResult

PARSER_NAME = "filings.xbrl"
PARSER_VERSION = "1.2.1+ixds.1.arelle.2.46.0.edgar.25.2.1.1.47a372d09916"
_SEC_TRANSFORM_PROVENANCE = "Arelle/EDGAR 25.2.1.1 @ 47a372d099168f8669d20a8a6bbe5cb16bbf71ac"
_ARELLE_PARSE_LOCK = threading.Lock()
_DOCTYPE_OR_ENTITY = re.compile(rb"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)
_DOCTYPE_OR_ENTITY_TEXT = re.compile(r"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)
_XBRL_SOURCE_MARKER = re.compile(
    r"(?:http://www\.xbrl\.org/(?:2003/instance|2008/inlineXBRL|2013/inlineXBRL)"
    r"|<\s*ix:[A-Za-z_][A-Za-z0-9_.-]*\b"
    r"|<\s*(?:[A-Za-z_][A-Za-z0-9_.-]*:)?xbrl\b)",
    re.IGNORECASE,
)
_XLINK_HREF = "{http://www.w3.org/1999/xlink}href"
_XSI_SCHEMA_LOCATION = "{http://www.w3.org/2001/XMLSchema-instance}schemaLocation"
_XSI_NO_NS_SCHEMA_LOCATION = "{http://www.w3.org/2001/XMLSchema-instance}noNamespaceSchemaLocation"


def _json(value: Any) -> str | None:
    if value is None:
        return None

    def encode(item: Any) -> Any:
        if isinstance(item, Fraction):
            return {"numerator": str(item.numerator), "denominator": str(item.denominator)}
        if isinstance(item, Decimal):
            return str(item)
        if isinstance(item, bytes):
            return item.decode("utf-8", errors="replace")
        if isinstance(item, (str, int, float, bool)):
            return item
        if isinstance(item, dict):
            return {str(key): encode(val) for key, val in item.items()}
        if isinstance(item, (list, tuple, set)):
            return [encode(val) for val in item]
        return str(item)

    return json.dumps(encode(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, Decimal):
        return str(value)
    return str(value)


def _local_name(element: Any) -> str:
    tag = getattr(element, "tag", "")
    return str(tag).rsplit("}", 1)[-1].lower()


def _qname(value: Any) -> tuple[str | None, str | None, str | None]:
    if value is None:
        return None, None, None
    namespace = getattr(value, "namespaceURI", None)
    local = getattr(value, "localName", None)
    if local is None:
        text = str(value)
        if text.startswith("{") and "}" in text:
            namespace, local = text[1:].split("}", 1)
        else:
            local = text
    lexical = f"{{{namespace}}}{local}" if namespace else str(local)
    return lexical, _text(namespace), _text(local)


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _checked_path(path: Path, allowed_root: Path, *, must_exist: bool = True) -> Path:
    """Resolve a path under root while rejecting symlink components and escapes."""
    root = allowed_root.resolve(strict=True)
    candidate = path if path.is_absolute() else root / path
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("path is not lexically beneath allowed_root") from exc

    current = root
    for part in relative.parts:
        if part in ("", "."):
            continue
        if part == "..":
            if current == root:
                raise ValueError("path traversal escapes allowed_root")
            current = current.parent
            continue
        current = current / part
        if current.is_symlink():
            raise ValueError("symlink paths are not allowed in the parser input tree")
    resolved = candidate.resolve(strict=must_exist)
    if not _inside(resolved, root):
        raise ValueError("path resolves outside allowed_root")
    if must_exist and not resolved.is_file():
        raise ValueError("XML dependency is not a regular file")
    return resolved


def _doctype_has_internal_subset(raw: bytes, encoding: str | None) -> bool:
    try:
        text = raw.decode(encoding or "utf-8", errors="ignore")
    except (LookupError, UnicodeError):
        text = raw.decode("utf-8", errors="ignore")
    match = re.search(r"<!DOCTYPE\b", text, re.IGNORECASE)
    if match is None:
        return False
    quote: str | None = None
    for char in text[match.end() :]:
        if quote is not None:
            if char == quote:
                quote = None
        elif char in {"'", '"'}:
            quote = char
        elif char == "[":
            return True
        elif char == ">":
            return False
    return False


def _parse_xml(path: Path, raw: bytes, *, allow_bare_html_doctype: bool = False) -> Any:
    from lxml import etree

    parser = etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
        huge_tree=False,
        remove_comments=False,
    )
    root = etree.fromstring(raw, parser, base_url=path.as_uri())
    docinfo = root.getroottree().docinfo
    if docinfo.doctype:
        bare_html = re.fullmatch(r"<!DOCTYPE\s+html\s*>", docinfo.doctype, re.IGNORECASE)
        primary_html_doctype = (
            allow_bare_html_doctype
            and bool(bare_html)
            and _local_name(root) == "html"
            and not docinfo.system_url
            and not docinfo.public_id
            and not _doctype_has_internal_subset(raw, docinfo.encoding)
            and not (docinfo.internalDTD and docinfo.internalDTD.entities())
        )
        if not primary_html_doctype:
            raise ValueError(f"DOCTYPE/entity declarations are not allowed: {path.name}")
    return root


def _schema_location_references(element: Any) -> list[str]:
    references: list[str] = []
    for node in element.iter():
        for attribute, value in node.attrib.items():
            if attribute == "{http://www.w3.org/XML/1998/namespace}base":
                base = urlsplit(value.strip())
                if base.scheme or base.netloc:
                    raise LookupError(f"unresolved remote xml:base dependency: {value}")
                raise ValueError("xml:base dependency rebasing is not supported")
            if attribute == _XLINK_HREF:
                references.append(value)
            elif attribute == _XSI_NO_NS_SCHEMA_LOCATION:
                references.append(value)
            elif attribute == _XSI_SCHEMA_LOCATION:
                pieces = value.split()
                # xsi:schemaLocation consists of namespace/location pairs.
                references.extend(pieces[1::2])
            elif attribute.rsplit("}", 1)[-1] == "schemaLocation":
                tag_name = _local_name(node)
                if tag_name in {"import", "include", "redefine", "override"}:
                    references.append(value)
    return references


def _resolve_reference(base_file: Path, href: str, root: Path) -> Path | None:
    """Resolve one XML resource reference, rejecting network/file URIs and escapes."""
    href = href.strip()
    if not href or href.startswith("#"):
        return None
    parsed = urlsplit(href)
    if parsed.scheme or parsed.netloc:
        if parsed.scheme.lower() == "file":
            raise ValueError(f"file URI dependency is not allowed: {href}")
        raise LookupError(f"unresolved remote dependency: {href}")
    if parsed.query:
        raise ValueError(f"query-bearing dependency URI is not allowed: {href}")
    if not parsed.path:
        return None
    decoded = unquote(parsed.path)
    if "\\" in decoded or "\x00" in decoded:
        raise ValueError(f"invalid dependency URI: {href}")
    target = Path(decoded)
    if target.is_absolute() or re.match(r"^[A-Za-z]:", decoded):
        raise ValueError(f"absolute dependency path is not allowed: {href}")

    root = root.resolve(strict=True)
    current = base_file.parent
    for part in target.parts:
        if part in ("", "."):
            continue
        if part == "..":
            if current == root:
                raise ValueError(f"dependency traversal escapes allowed_root: {href}")
            current = current.parent
            continue
        current = current / part
        if current.is_symlink():
            raise ValueError(f"symlink dependency is not allowed: {href}")
    return _checked_path(current, root, must_exist=True)


def _resource_uri_key(uri: str) -> str:
    parsed = urlsplit(uri)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"resource URI must be explicit HTTP(S): {uri}")
    if parsed.query:
        raise ValueError(f"query-bearing resource URI is not supported: {uri}")
    netloc = parsed.netloc.lower()
    return urlunsplit((parsed.scheme.lower(), netloc, parsed.path or "/", "", ""))


def _verified_maps(
    *,
    allowed_root: Path,
    uri_map: Mapping[str, Path] | None,
    source_origins: Mapping[Path, str] | None,
    expected_hashes: Mapping[Path, str] | None,
) -> tuple[dict[str, Path], dict[Path, str], dict[Path, str]]:
    root = allowed_root.resolve(strict=True)
    if uri_map is not None and expected_hashes is None:
        raise ValueError("expected_hashes is required when uri_map is supplied")

    hashes: dict[Path, str] = {}
    for path, expected in (expected_hashes or {}).items():
        safe_path = _checked_path(Path(path), root, must_exist=True)
        if not re.fullmatch(r"[0-9a-fA-F]{64}", str(expected)):
            raise ValueError(f"invalid expected SHA-256 for {safe_path}")
        hashes[safe_path] = str(expected).lower()

    mapped_uris: dict[str, Path] = {}
    for uri, path in (uri_map or {}).items():
        safe_path = _checked_path(Path(path), root, must_exist=True)
        key = _resource_uri_key(str(uri))
        if safe_path not in hashes:
            raise ValueError(f"missing expected hash for mapped dependency: {safe_path}")
        previous = mapped_uris.get(key)
        if previous is not None and previous != safe_path:
            raise ValueError(f"conflicting paths mapped to resource URI: {key}")
        mapped_uris[key] = safe_path

    origins: dict[Path, str] = {}
    for path, origin_uri in (source_origins or {}).items():
        safe_path = _checked_path(Path(path), root, must_exist=True)
        if safe_path not in hashes:
            raise ValueError(f"missing expected hash for source-origin path: {safe_path}")
        key = _resource_uri_key(str(origin_uri))
        mapped_path = mapped_uris.get(key)
        if mapped_path != safe_path:
            raise ValueError(
                f"source origin is not mapped to its verified local path: {origin_uri}"
            )
        origins[safe_path] = key

    if uri_map is not None:
        for key, path in mapped_uris.items():
            origins.setdefault(path, key)
    for path, expected in hashes.items():
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            raise ValueError(f"source hash mismatch for verified input: {path}")
    return mapped_uris, origins, hashes


def _mapped_reference(
    base_uri: str,
    href: str,
    uri_map: dict[str, Path],
) -> Path:
    target_uri = urljoin(base_uri, href.strip())
    try:
        key = _resource_uri_key(target_uri)
    except ValueError as exc:
        raise ValueError(f"unsafe or unsupported dependency reference {href!r}: {exc}") from exc
    try:
        return uri_map[key]
    except KeyError as exc:
        raise LookupError(f"unresolved remote dependency: {target_uri}") from exc


def _preflight(
    entrypoint: Path,
    allowed_root: Path,
    *,
    uri_map: Mapping[str, Path] | None = None,
    source_origins: Mapping[Path, str] | None = None,
    expected_hashes: Mapping[Path, str] | None = None,
    inline_entrypoints: frozenset[Path] = frozenset(),
) -> tuple[
    Path,
    dict[Path, bytes],
    dict[Path, Any],
    dict[Path, str],
    dict[str, Path],
    dict[Path, str],
]:
    root = allowed_root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("allowed_root must be a directory")
    mapped_uris, origins, expected = _verified_maps(
        allowed_root=root,
        uri_map=uri_map,
        source_origins=source_origins,
        expected_hashes=expected_hashes,
    )
    entry = _checked_path(entrypoint, root, must_exist=True)
    if expected and entry not in expected:
        raise ValueError(f"missing expected hash for primary source: {entry}")
    if uri_map is not None and entry not in origins:
        raise ValueError(f"primary source is not mapped to an approved original URI: {entry}")
    pending = [entry]
    source_bytes: dict[Path, bytes] = {}
    roots: dict[Path, Any] = {}
    while pending:
        path = pending.pop()
        if path in source_bytes:
            continue
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise ValueError(f"cannot read local XML dependency {path.name}: {exc}") from exc
        actual_hash = hashlib.sha256(raw).hexdigest()
        if path in expected and actual_hash != expected[path]:
            raise ValueError(f"source hash mismatch for verified input: {path}")
        try:
            document_root = _parse_xml(
                path,
                raw,
                allow_bare_html_doctype=(path == entry or path in inline_entrypoints),
            )
        except Exception as exc:
            if isinstance(exc, ValueError):
                raise
            raise ValueError(f"invalid or unsafe XML document {path.name}: {exc}") from exc
        source_bytes[path] = raw
        roots[path] = document_root
        base_uri = origins.get(path)
        for href in _schema_location_references(document_root):
            if not href or href.strip().startswith("#"):
                continue
            try:
                if base_uri is not None and urlsplit(base_uri).scheme in {"http", "https"}:
                    target = _mapped_reference(base_uri, href, mapped_uris)
                else:
                    target = _resolve_reference(path, href, root)
            except LookupError as exc:
                raise LookupError(str(exc)) from exc
            if target is not None and target not in source_bytes:
                pending.append(target)
    integrity_hashes = dict(expected)
    integrity_hashes.update(
        {path: hashlib.sha256(raw).hexdigest() for path, raw in source_bytes.items()}
    )
    return entry, source_bytes, roots, origins, mapped_uris, integrity_hashes


def _source_path(
    uri: Any,
    allowed_root: Path,
    uri_map: Mapping[str, Path] | None = None,
) -> Path | None:
    if uri is None:
        return None
    value = str(uri)
    parsed = urlsplit(value)
    if parsed.scheme.lower() in {"http", "https"}:
        if uri_map is None:
            return None
        try:
            return Path(uri_map[_resource_uri_key(value)])
        except (KeyError, ValueError):
            return None
    if parsed.scheme.lower() == "file":
        value = url2pathname(unquote(parsed.path))
    elif parsed.scheme:
        return None
    try:
        return _checked_path(Path(value), allowed_root, must_exist=True)
    except (OSError, ValueError):
        return None


def _source_uri_and_path(
    fact: Any,
    allowed_root: Path,
    source_origins: Mapping[Path, str],
    uri_map: Mapping[str, Path] | None,
) -> tuple[str | None, Path | None, Any]:
    model_document = getattr(fact, "modelDocument", None)
    uri = getattr(model_document, "uri", None)
    source = getattr(fact, "source", None)
    if uri is None and source is not None:
        uri = getattr(getattr(source, "modelDocument", None), "uri", None)
    path = _source_path(uri, allowed_root, uri_map)
    if path is not None:
        uri = source_origins.get(path, _text(uri))
    return (_text(uri), path, source)


def _xml_serialization(value: Any) -> str | None:
    if value is None:
        return None
    try:
        from lxml import etree

        if hasattr(value, "tag"):
            return etree.tostring(value, encoding="unicode", with_tail=False)
        element = getattr(value, "element", None)
        if element is not None:
            return etree.tostring(element, encoding="unicode", with_tail=False)
    except Exception:
        return None
    return None


def _raw_element(fact: Any) -> Any:
    for candidate in (fact, getattr(fact, "source", None), getattr(fact, "element", None)):
        if (
            candidate is not None
            and hasattr(candidate, "tag")
            and hasattr(candidate, "getroottree")
        ):
            return candidate
    return None


def _context_json(context: Any) -> str | None:
    if context is None:
        return None
    payload: dict[str, Any] = {}
    entity = getattr(context, "entityIdentifier", None)
    if entity is not None:
        try:
            payload["entity_identifier"] = list(entity)
        except TypeError:
            payload["entity_identifier"] = _text(entity)
    for name in ("startDatetime", "endDatetime", "instantDatetime", "periodType"):
        value = getattr(context, name, None)
        if value is not None:
            payload[f"arelle_{name}"] = _text(value)

    element = _raw_element(context)
    if element is not None:
        payload["context_xml"] = _xml_serialization(element)
        period: dict[str, str] = {}
        segment: list[str] = []
        scenario: list[str] = []
        for child in element:
            name = _local_name(child)
            if name == "period":
                for period_child in child:
                    period[_local_name(period_child)] = "".join(period_child.itertext())
            elif name == "entity":
                for entity_child in child:
                    if _local_name(entity_child) == "segment":
                        segment.extend(
                            _xml_serialization(member) or ""
                            for member in entity_child
                            if _local_name(member) not in {"explicitmember", "typedmember"}
                        )
            elif name == "scenario":
                scenario.extend(
                    _xml_serialization(member) or ""
                    for member in child
                    if _local_name(member) not in {"explicitmember", "typedmember"}
                )
        if period:
            payload["original_period_lexical"] = period
        payload["nondimensional_segment_xml"] = segment
        payload["nondimensional_scenario_xml"] = scenario

    dimensions: list[dict[str, Any]] = []
    for dimension, member in (getattr(context, "qnameDims", {}) or {}).items():
        dimension_qname, dimension_ns, dimension_local = _qname(dimension)
        member_qname, member_ns, member_local = _qname(getattr(member, "memberQname", None))
        typed_element = getattr(member, "typedMember", None)
        dimensions.append(
            {
                "dimension": dimension_qname,
                "dimension_namespace": dimension_ns,
                "dimension_local_name": dimension_local,
                "kind": "explicit" if getattr(member, "isExplicit", False) else "typed",
                "member_qname": member_qname,
                "member_namespace": member_ns,
                "member_local_name": member_local,
                "typed_member_xml": _xml_serialization(typed_element),
            }
        )
    payload["dimensions"] = sorted(dimensions, key=lambda row: row["dimension"] or "")
    return _json(payload)


def _unit_json(unit: Any) -> str | None:
    if unit is None:
        return None
    numerators = getattr(unit, "numeratorMeasures", None)
    denominators = getattr(unit, "denominatorMeasures", None)
    if numerators is None or denominators is None:
        measures = getattr(unit, "measures", None)
        if measures and len(measures) == 2:
            numerators, denominators = measures
    payload = {
        "numerator_measures": [_qname(value)[0] for value in (numerators or [])],
        "denominator_measures": [_qname(value)[0] for value in (denominators or [])],
        "unit_xml": _xml_serialization(_raw_element(unit)),
    }
    return _json(payload)


def _decimal_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, float):
        # Arelle normally supplies Decimal for XBRL numeric values. Avoid storing a
        # binary float's expanded representation if a plugin/concept supplies one.
        try:
            return str(Decimal(str(value)))
        except InvalidOperation:
            return str(value)
    return str(value)


def _object_index(fact: Any, fallback: int) -> int:
    try:
        return int(fact.objectIndex)
    except (AttributeError, TypeError, ValueError):
        return fallback


def _integer_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _model_errors(model: Any) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    for item in getattr(model, "errors", []) or []:
        code = getattr(item, "code", None)
        message = getattr(item, "message", None)
        if isinstance(item, dict):
            code = item.get("code", code)
            message = item.get("message", message)
        elif isinstance(item, str):
            code = item.partition(" ")[0]
            message = item
        errors.append(
            {
                "code": _text(code) or "arelle_model_error",
                "message": _text(message) or str(item),
                "severity": "error",
            }
        )
    return errors


def _occurrence_id(
    filing_id: str,
    document_id: str,
    parse_id: str,
    source_identity: str,
    object_index: int,
) -> str:
    payload = "\\0".join((filing_id, document_id, parse_id, source_identity, str(object_index)))
    return hashlib.sha256(payload.encode()).hexdigest()


def _fact_source_details(
    fact: Any,
    fallback_index: int,
    allowed_root: Path,
    source_bytes: dict[Path, bytes],
    source_origins: Mapping[Path, str],
    uri_map: Mapping[str, Path] | None,
    filing_id: str,
    document_id: str,
    parse_id: str,
) -> dict[str, Any]:
    uri, path, source = _source_uri_and_path(fact, allowed_root, source_origins, uri_map)
    actual_element = _raw_element(fact)
    sourceline = getattr(source, "sourceline", None) if source is not None else None
    if sourceline is None:
        sourceline = getattr(actual_element, "sourceline", None)
    if sourceline is None:
        sourceline = getattr(fact, "sourceline", None)
    object_index = _object_index(fact, fallback_index)
    relpath = path.relative_to(allowed_root.resolve(strict=True)) if path is not None else None
    document_hash = (
        hashlib.sha256(source_bytes[path]).hexdigest()
        if path is not None and path in source_bytes
        else None
    )
    source_identity = uri or (relpath.as_posix() if relpath is not None else "unknown-source")
    return {
        "source_uri": uri,
        "source_relpath": relpath.as_posix() if relpath is not None else None,
        "document_hash": document_hash,
        "source_verified": path is not None and path in source_bytes,
        "source_path": path,
        "source_xml": _xml_serialization(actual_element),
        "source_line": _integer_or_none(sourceline),
        "object_index": object_index,
        "occurrence_id": _occurrence_id(
            filing_id, document_id, parse_id, source_identity, object_index
        ),
        "source_identity": source_identity,
        "element": actual_element,
    }


def _tuple_parent(
    fact: Any,
    source_info: Mapping[str, Any],
    id_to_occurrence: Mapping[int, str],
    key_to_occurrence: Mapping[tuple[str, int], str],
    tuple_ids: Mapping[tuple[str, str], str],
    unambiguous_tuple_ids: Mapping[str, str],
) -> str | None:
    inline = type(fact).__name__ == "ModelInlineFact"
    try:
        tuple_ref = getattr(fact, "tupleRef", None)
    except Exception:
        tuple_ref = None
    if tuple_ref:
        parent_id = tuple_ids.get((str(source_info["source_identity"]), str(tuple_ref)))
        if parent_id is None:
            parent_id = unambiguous_tuple_ids.get(str(tuple_ref))
        if parent_id is not None:
            return parent_id

    try:
        parent = getattr(fact, "parentElement", None)
    except Exception:
        parent = None
    while parent is not None:
        parent_id = id_to_occurrence.get(id(parent))
        if parent_id is not None:
            return parent_id
        try:
            parent_index = int(parent.objectIndex)
            parent_document = getattr(parent, "modelDocument", None)
            parent_uri = _text(getattr(parent_document, "uri", None))
            parent_identity = parent_uri or source_info["source_identity"]
            parent_id = key_to_occurrence.get((parent_identity, parent_index))
            if parent_id is not None:
                return parent_id
        except (AttributeError, TypeError, ValueError):
            pass
        if inline:
            # For inline facts, parentElement is Arelle's semantic instance parent.
            # Do not walk the physical HTML DOM when it is not a tuple fact.
            return None
        try:
            next_parent = getattr(parent, "parentElement", None)
            if next_parent is None:
                parent_getter = getattr(parent, "getparent", None)
                next_parent = parent_getter() if callable(parent_getter) else None
            parent = next_parent
        except Exception:
            return None
    return None


def _lexical_content(element: Any) -> str:
    if element is None:
        return ""
    return "".join(element.itertext())


def _fact_row(
    fact: Any,
    source_info: dict[str, Any],
    *,
    filing_id: str,
    document_id: str,
    parse_id: str,
    source_hash: str,
    status: str,
    valid_constant: Any,
    validation_scope: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:

    qname, namespace, local_name = _qname(getattr(fact, "qname", None))
    concept = getattr(fact, "concept", None)
    is_numeric = getattr(concept, "isNumeric", None)
    is_tuple = getattr(fact, "isTuple", None)
    if is_tuple is None:
        is_tuple = bool(getattr(concept, "isTuple", False))

    errors: list[dict[str, Any]] = []
    error_codes: list[str] = []
    x_valid = getattr(fact, "xValid", None)
    try:
        valid = x_valid is not None and x_valid >= valid_constant
    except TypeError:
        valid = False

    element = source_info["element"]

    def attribute(name: str) -> str | None:
        if element is None or not hasattr(element, "get"):
            return None
        return element.get(name)

    def part_attribute(part: Any, name: str) -> str | None:
        return part.get(name) if part is not None and hasattr(part, "get") else None

    is_inline = type(fact).__name__ == "ModelInlineFact"
    if is_inline:
        try:
            raw_value = fact.rawValue
        except Exception as exc:
            raw_value = None
            errors.append(
                {
                    "code": "inline_raw_value_error",
                    "message": f"Inline raw value could not be read: {type(exc).__name__}: {exc}",
                    "severity": "error",
                }
            )
            error_codes.append("inline_raw_value_error")
    else:
        # ModelFact.value may substitute an element default/fixed value. Preserve
        # source lexical text (including empty and whitespace-only text) separately.
        raw_value = _lexical_content(element)
    try:
        transformed_value = fact.value
    except Exception as exc:
        transformed_value = None
        errors.append(
            {
                "code": "fact_value_error",
                "message": f"Interpreted value could not be read: {type(exc).__name__}: {exc}",
                "severity": "error",
            }
        )
        error_codes.append("fact_value_error")
    x_value = None
    if valid:
        try:
            x_value = fact.xValue
        except Exception as exc:
            errors.append(
                {
                    "code": "fact_xvalue_error",
                    "message": f"Validated xValue could not be read: {type(exc).__name__}: {exc}",
                    "severity": "error",
                }
            )
            error_codes.append("fact_xvalue_error")
            valid = False

    numerator = None
    denominator = None
    numerator_element = None
    denominator_element = None
    if element is not None:
        for child in element:
            if _local_name(child) == "numerator":
                numerator_element = child
                numerator = _lexical_content(child)
            elif _local_name(child) == "denominator":
                denominator_element = child
                denominator = _lexical_content(child)
    concept_is_fraction = bool(getattr(concept, "isFraction", False))
    if concept_is_fraction and (numerator is None or denominator is None):
        try:
            fraction_values = fact.fractionValue
            numerator = numerator if numerator is not None else fraction_values[0]
            denominator = denominator if denominator is not None else fraction_values[1]
        except Exception:
            pass
    is_fraction = concept_is_fraction or numerator is not None or denominator is not None
    numeric = _decimal_text(x_value) if valid and is_numeric and not is_fraction else None

    if x_valid is not None and not valid:
        error_codes.append("invalid_xbrl_value")
        errors.append(
            {
                "code": "invalid_xbrl_value",
                "message": (
                    "Arelle did not validate this occurrence as datatype-valid; "
                    "occurrence retained."
                ),
                "severity": "error",
                "source_uri": source_info["source_uri"],
                "source_line": source_info["source_line"],
                "object_index": source_info["object_index"],
            }
        )
    if not source_info["source_verified"]:
        error_codes.append("unverified_fact_source")
        errors.append(
            {
                "code": "unverified_fact_source",
                "message": "Arelle fact source does not map to a preflighted, hash-verified input.",
                "severity": "error",
                "source_uri": source_info["source_uri"],
                "source_line": source_info["source_line"],
                "object_index": source_info["object_index"],
            }
        )
    validated_fraction_numerator = None
    validated_fraction_denominator = None
    if is_fraction and valid and x_value is not None:
        try:
            validated_fraction_numerator = str(x_value.numerator)
            validated_fraction_denominator = str(x_value.denominator)
        except AttributeError:
            errors.append(
                {
                    "code": "fraction_value_not_rational",
                    "message": "Arelle validated a fraction without an exact rational xValue.",
                    "severity": "error",
                }
            )
            error_codes.append("fraction_value_not_rational")

    inline_format = getattr(fact, "format", None) or attribute("format")
    inline_sign = getattr(fact, "sign", None) or attribute("sign")
    inline_scale = getattr(fact, "scale", None) or attribute("scale")
    inline_hidden = getattr(fact, "isHidden", None)
    if inline_hidden is None and element is not None:
        ancestor = element
        while ancestor is not None:
            if _local_name(ancestor) == "hidden":
                inline_hidden = True
                break
            try:
                ancestor = ancestor.getparent()
            except Exception:
                ancestor = None
    continued_at = getattr(fact, "continuedAt", None) or attribute("continuedAt")
    fraction_num_text = _text(numerator)
    fraction_den_text = _text(denominator)

    context = getattr(fact, "context", None)
    unit = getattr(fact, "unit", None)
    context_id = getattr(fact, "contextID", None)
    unit_id = getattr(fact, "unitID", None)
    context_payload = _context_json(context)
    unit_payload = _unit_json(unit)
    provenance = {
        "source_serialization": "lxml XML serialization of original source element; not byte-exact",
        "raw_value_source": "inline rawValue or original classic XML element lexical text",
        "transformed_value_source": "Arelle fact.value; kept separate from raw source text",
        "sec_inline_transform_plugin": _SEC_TRANSFORM_PROVENANCE,
        "validity_scope": (
            "Arelle datatype value validity only; see validation_scope for model validation"
        ),
        "arelle_validity_constant": _text(valid_constant),
    }
    return (
        {
            "filing_id": filing_id,
            "document_id": document_id,
            "parse_id": parse_id,
            "parser_name": PARSER_NAME,
            "parser_version": PARSER_VERSION,
            "status": status,
            "source_hash": source_hash,
            "occurrence_id": source_info["occurrence_id"],
            "source_uri": source_info["source_uri"],
            "source_relpath": source_info["source_relpath"],
            "document_hash": source_info["document_hash"],
            "source_xml": source_info["source_xml"],
            "source_line": source_info["source_line"],
            "object_index": source_info["object_index"],
            "provenance": _json(provenance),
            "fact_qname": qname,
            "namespace_uri": namespace,
            "concept_local_name": local_name,
            "concept_type": _text(getattr(getattr(concept, "type", None), "qname", None)),
            "is_numeric": bool(is_numeric) if is_numeric is not None else None,
            "is_tuple": bool(is_tuple) if is_tuple is not None else None,
            "parent_occurrence_id": None,
            "context_id": _text(context_id),
            "context_json": context_payload,
            "unit_id": _text(unit_id),
            "unit_json": unit_payload,
            "raw_value": _text(raw_value),
            "transformed_value": _text(transformed_value),
            "normalized_numeric": numeric,
            "x_value_json": _json(x_value),
            "decimals_raw": attribute("decimals"),
            "decimals": _text(getattr(fact, "decimals", None)),
            "precision_raw": attribute("precision"),
            "precision": _text(getattr(fact, "precision", None)),
            "is_nil": bool(getattr(fact, "isNil", False)),
            "x_valid": _integer_or_none(x_valid),
            "is_valid": bool(valid) if x_valid is not None else None,
            "validity_scope": "xbrl_datatype_value",
            "validation_scope": validation_scope,
            "validated_fraction_numerator": validated_fraction_numerator,
            "validated_fraction_denominator": validated_fraction_denominator,
            "fraction_numerator_format": part_attribute(numerator_element, "format"),
            "fraction_numerator_sign": part_attribute(numerator_element, "sign"),
            "fraction_numerator_scale": part_attribute(numerator_element, "scale"),
            "fraction_denominator_format": part_attribute(denominator_element, "format"),
            "fraction_denominator_sign": part_attribute(denominator_element, "sign"),
            "fraction_denominator_scale": part_attribute(denominator_element, "scale"),
            "inline_format": _text(inline_format),
            "inline_sign": _text(inline_sign),
            "inline_scale": _text(inline_scale),
            "inline_hidden": bool(inline_hidden) if inline_hidden is not None else None,
            "continued_at": _text(continued_at),
            "fraction_numerator": fraction_num_text,
            "fraction_denominator": fraction_den_text,
            "error_count": len(errors),
            "error_codes_json": _json(error_codes),
        },
        errors,
    )


def _failed(
    *,
    filing_id: str,
    document_id: str,
    parse_id: str,
    code: str,
    message: str,
    source_hash: str | None = None,
) -> ParseResult:
    return ParseResult(
        filing_id=filing_id,
        document_id=document_id,
        parse_id=parse_id,
        parser_name=PARSER_NAME,
        parser_version=PARSER_VERSION,
        status="failed",
        source_hash=source_hash,
        errors=[{"code": code, "message": message, "severity": "error"}],
    )


def _unsupported(
    *,
    filing_id: str,
    document_id: str,
    parse_id: str,
    source_hash: str,
    message: str,
) -> ParseResult:
    return ParseResult(
        filing_id=filing_id,
        document_id=document_id,
        parse_id=parse_id,
        parser_name=PARSER_NAME,
        parser_version=PARSER_VERSION,
        status="unsupported",
        source_hash=source_hash,
        errors=[{"code": "unsupported_non_xbrl", "message": message, "severity": "warning"}],
    )


def _is_xbrl_document(root: Any) -> bool:
    tag = getattr(root, "tag", None)
    if tag == "{http://www.xbrl.org/2003/instance}xbrl":
        return True
    if tag == "{http://www.w3.org/1999/xhtml}html":
        return any(
            str(getattr(node, "tag", "")).startswith(
                (
                    "{http://www.xbrl.org/2013/inlineXBRL}",
                    "{http://www.xbrl.org/2008/inlineXBRL}",
                )
            )
            for node in root.iter()
        )
    return False


def _preflight_inline_document_set(
    entrypoint: Path,
    inline_document_set: Sequence[Path],
    allowed_root: Path,
    *,
    uri_map: Mapping[str, Path] | None,
    source_origins: Mapping[Path, str] | None,
    expected_hashes: Mapping[Path, str] | None,
) -> tuple[
    Path,
    tuple[Path, ...],
    dict[Path, bytes],
    dict[Path, Any],
    dict[Path, str],
    dict[str, Path],
    dict[Path, str],
]:
    """Verify every explicitly declared IXDS member and its shared local DTS graph."""
    if isinstance(inline_document_set, (str, bytes)):
        raise ValueError("inline_document_set must be an explicit sequence of member paths")
    if not isinstance(inline_document_set, Sequence) or len(inline_document_set) < 2:
        raise ValueError("inline_document_set must contain at least two explicit member paths")
    if uri_map is None or expected_hashes is None:
        raise ValueError(
            "inline_document_set requires original-URI mappings and expected hashes "
            "for every member"
        )

    root = Path(allowed_root).resolve(strict=True)
    safe_entry = _checked_path(Path(entrypoint), root, must_exist=True)
    members = tuple(
        _checked_path(Path(item), root, must_exist=True) for item in inline_document_set
    )
    if members[0] != safe_entry:
        raise ValueError("inline_document_set must list the primary entrypoint first")
    if len(set(members)) != len(members):
        raise ValueError("inline_document_set contains duplicate member paths")

    mapped_uris, origins, expected = _verified_maps(
        allowed_root=root,
        uri_map=uri_map,
        source_origins=source_origins,
        expected_hashes=expected_hashes,
    )
    for member in members:
        if member not in expected:
            raise ValueError(f"missing expected hash for inline document set member: {member}")
        if member not in origins:
            raise ValueError(f"inline document set member lacks an approved original URI: {member}")
    source_uris = [origins[member] for member in members]
    if source_uris[1:] != sorted(source_uris[1:]):
        raise ValueError(
            "inline_document_set members after the primary must be sorted by original URI"
        )

    source_bytes: dict[Path, bytes] = {}
    xml_roots: dict[Path, Any] = {}
    integrity_hashes: dict[Path, str] = {}
    for member in members:
        result = _preflight(
            member,
            root,
            uri_map=uri_map,
            source_origins=source_origins,
            expected_hashes=expected_hashes,
            inline_entrypoints=frozenset(members),
        )
        checked_entry, member_bytes, member_roots, member_origins, member_map, member_hashes = (
            result
        )
        if checked_entry != member:
            raise ValueError("Arelle IXDS member path changed during preflight")
        for path, raw in member_bytes.items():
            previous = source_bytes.get(path)
            if previous is not None and previous != raw:
                raise ValueError(f"conflicting preflight bytes for dependency: {path}")
            source_bytes[path] = raw
        xml_roots.update(member_roots)
        for path, original_uri in member_origins.items():
            previous_uri = origins.get(path)
            if previous_uri is not None and previous_uri != original_uri:
                raise ValueError(f"conflicting original URI provenance for dependency: {path}")
        if member_map != mapped_uris:
            raise ValueError("inline document set members do not share one verified URI map")
        for path, digest in member_hashes.items():
            previous_hash = integrity_hashes.get(path)
            if previous_hash is not None and previous_hash != digest:
                raise ValueError(f"conflicting expected hashes for dependency: {path}")
            integrity_hashes[path] = digest

    for member in members:
        if member not in source_bytes or member not in xml_roots:
            raise ValueError(f"declared inline document set member was not preflighted: {member}")
    return (
        safe_entry,
        members,
        source_bytes,
        xml_roots,
        origins,
        mapped_uris,
        integrity_hashes,
    )


def _inline_targets(root: Any) -> set[str]:
    inline_namespaces = {
        "{http://www.xbrl.org/2013/inlineXBRL}",
        "{http://www.xbrl.org/2008/inlineXBRL}",
    }
    targets: set[str] = set()
    for node in root.iter():
        tag = str(getattr(node, "tag", ""))
        if (
            tag.startswith(tuple(inline_namespaces))
            and tag.rsplit("}", 1)[-1].lower() == "references"
        ):
            targets.add(node.get("target") or "(default)")
    return targets or {"(default)"}


def _populate_inline_private_cache(
    cache_root: Path,
    *,
    uri_map: Mapping[str, Path],
    source_bytes: Mapping[Path, bytes],
    integrity_hashes: Mapping[Path, str],
) -> None:
    """Mirror only the preflighted IXDS dependency graph into a fresh offline cache."""
    from .dependencies import arelle_cache_path

    cache_root = cache_root.resolve(strict=True)
    for uri, source_path in uri_map.items():
        local_path = Path(source_path)
        if local_path not in source_bytes:
            continue
        raw = source_bytes[local_path]
        expected = integrity_hashes.get(local_path)
        if expected is None or hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError(
                f"preflighted IXDS dependency hash is unavailable or inconsistent: {uri}"
            )
        target = arelle_cache_path(uri, cache_root)
        if not _inside(target.resolve(strict=False), cache_root):
            raise ValueError(f"Arelle IXDS cache mapping escaped its private root: {uri}")
        current = cache_root
        for part in target.relative_to(cache_root).parts[:-1]:
            current = current / part
            if current.is_symlink():
                raise ValueError(f"symlink in private IXDS cache path: {current}")
            current.mkdir(exist_ok=True)
        if target.exists() or target.is_symlink():
            if target.is_symlink() or target.read_bytes() != raw:
                raise ValueError(f"conflicting private IXDS cache entry: {uri}")
        else:
            target.write_bytes(raw)


def _trusted_arelle_ixds_plugin() -> Path:
    """Verify the fixed SDK and its bundled IXDS plugin without accepting caller paths."""
    from importlib.metadata import version

    import arelle

    sdk_root = Path(arelle.__file__).resolve(strict=True).parent
    plugin_path = sdk_root / "plugin" / "inlineXbrlDocumentSet.py"
    if version("arelle-release") != "2.46.0":
        raise RuntimeError("the bundled IXDS plugin requires pinned arelle-release==2.46.0")
    if plugin_path.is_symlink() or not plugin_path.is_file():
        raise RuntimeError("the fixed bundled inlineXbrlDocumentSet plugin is unavailable")
    if not plugin_path.resolve(strict=True).is_relative_to(sdk_root):
        raise RuntimeError("the fixed bundled inlineXbrlDocumentSet plugin escaped the SDK root")
    return plugin_path


def _unsupported_inline_document_set(
    *, filing_id: str, document_id: str, parse_id: str, source_hash: str, message: str
) -> ParseResult:
    return ParseResult(
        filing_id=filing_id,
        document_id=document_id,
        parse_id=parse_id,
        parser_name=PARSER_NAME,
        parser_version=PARSER_VERSION,
        status="unsupported",
        source_hash=source_hash,
        errors=[
            {
                "code": "unsupported_inline_document_set",
                "message": message,
                "severity": "warning",
            }
        ],
    )


def _looks_like_legacy_html(raw: bytes) -> bool:
    return bool(
        re.search(rb"<\s*(?:html|body|head|div|p|table|br|meta|title)\b", raw[:8192], re.IGNORECASE)
    )


def _verified_fallback_source(
    entrypoint: Path,
    allowed_root: Path,
    *,
    uri_map: Mapping[str, Path] | None,
    source_origins: Mapping[Path, str] | None,
    expected_hashes: Mapping[Path, str] | None,
) -> tuple[Path, bytes, str] | None:
    """Recheck input path, expected hashes, and URI binding before legacy fallback."""
    try:
        root = Path(allowed_root).resolve(strict=True)
        safe_entry = _checked_path(Path(entrypoint), root, must_exist=True)
        raw = safe_entry.read_bytes()
        _mapped, origins, expected = _verified_maps(
            allowed_root=root,
            uri_map=uri_map,
            source_origins=source_origins,
            expected_hashes=expected_hashes,
        )
        if expected_hashes and safe_entry not in expected:
            return None
        if uri_map is not None and safe_entry not in origins:
            return None
        return safe_entry, raw, hashlib.sha256(raw).hexdigest()
    except Exception:
        # Fallback classification is never allowed to override path/source failures.
        return None


def _plain_text_probe(raw: bytes) -> str:
    """Expose ASCII SGML/XML markers across UTF encodings for conservative checks."""
    return raw.decode("utf-8", errors="ignore").replace("\x00", "")


def _non_xbrl_source_format(raw: bytes) -> str | None:
    """Return only a safely recognized non-XBRL format, protecting XBRL/unsafe envelopes."""
    try:
        from .source.sec_envelope import (
            extract_sec_envelope,
            is_complete_sec_pdf_envelope,
            sec_envelope_encoding,
        )

        encoding_info = sec_envelope_encoding(raw)
        if encoding_info is None:
            probe = _plain_text_probe(raw)
            if re.search(r"<!\s*ENTITY\b", probe, re.IGNORECASE):
                return None
            doctype_markers = list(re.finditer(r"<!\s*DOCTYPE\b", probe, re.IGNORECASE))
            if doctype_markers:
                # Preserve the historical non-XML HTML fallback for a single bare
                # HTML doctype, but never treat external/internal DTDs as unsupported.
                if (
                    len(doctype_markers) != 1
                    or re.match(
                        r"<!\s*DOCTYPE\s+html\s*>",
                        probe[doctype_markers[0].start() :],
                        re.IGNORECASE,
                    )
                    is None
                ):
                    return None
            if _XBRL_SOURCE_MARKER.search(probe):
                return None
            return "legacy_html" if _looks_like_legacy_html(raw) else None

        try:
            envelope = extract_sec_envelope(raw)
        except Exception:
            # A detected but malformed/truncated/multi-document wrapper is not an
            # unsupported non-XBRL document; preserve the original preflight error.
            return "protected_sec_envelope"
        if envelope is None:
            return "protected_sec_envelope"

        encoding = str(envelope["encoding"])
        source_text = raw.decode(encoding)
        payload = envelope["payload"]
        payload_text = payload.decode(encoding)
        if (
            _DOCTYPE_OR_ENTITY.search(raw)
            or _DOCTYPE_OR_ENTITY_TEXT.search(source_text)
            or _DOCTYPE_OR_ENTITY_TEXT.search(payload_text)
            or is_complete_sec_pdf_envelope(raw)
            or payload.lstrip(b" \t\r\n\v\f").startswith(b"%PDF-")
            or _XBRL_SOURCE_MARKER.search(source_text)
            or _XBRL_SOURCE_MARKER.search(payload_text)
        ):
            return "protected_sec_envelope"
        if envelope["payload_kind"] == "legacy_text":
            return "legacy_text"
        if envelope["payload_kind"] == "html" and _looks_like_legacy_html(payload):
            return "legacy_html"
        return "protected_sec_envelope"
    except Exception:
        # Encoding/format uncertainty must remain failed or unsupported by the
        # original parser path, never be translated into unsupported_non_xbrl.
        return None


def _zero_fraction_loader_crash(logs: str) -> bool:
    return "ZeroDivisionError" in logs and bool(re.search(r"Fraction\(\s*-?\d+\s*,\s*0\s*\)", logs))


def parse_xbrl(
    entrypoint: Path,
    *,
    filing_id: str,
    document_id: str,
    parse_id: str,
    allowed_root: Path,
    cache_dir: Path | None = None,
    uri_map: Mapping[str, Path] | None = None,
    source_origins: Mapping[Path, str] | None = None,
    expected_hashes: Mapping[Path, str] | None = None,
    inline_document_set: Sequence[Path] | None = None,
) -> ParseResult:
    """Parse a verified local XBRL/iXBRL package with structural Arelle validation.

    Without explicit maps, only local XML references beneath ``allowed_root`` are
    accepted. When ``uri_map``, ``source_origins``, and ``expected_hashes`` are
    supplied, original remote URIs resolve only to hash-verified files in the
    prepared cache. Supplying ``inline_document_set`` enables the fixed Arelle IXDS
    plugin for an explicit primary-first, source-URI-sorted group; every member must
    have a verified original URI and expected hash. Arelle runs offline with
    persistent user configuration disabled.
    ``full`` means structural XBRL validation and complete occurrence extraction
    returned without Arelle/source-integrity errors; it does not claim economic
    coverage or SEC/EFM compliance.
    """
    source_hash: str | None = None
    inline_members: tuple[Path, ...] = ()
    try:
        root = Path(allowed_root).resolve(strict=True)
        safe_entry = _checked_path(Path(entrypoint), root, must_exist=True)
        source_hash = hashlib.sha256(safe_entry.read_bytes()).hexdigest()
        if inline_document_set is None:
            (
                entry,
                source_bytes,
                xml_roots,
                verified_origins,
                verified_uri_map,
                integrity_hashes,
            ) = _preflight(
                safe_entry,
                root,
                uri_map=uri_map,
                source_origins=source_origins,
                expected_hashes=expected_hashes,
            )
        else:
            (
                entry,
                inline_members,
                source_bytes,
                xml_roots,
                verified_origins,
                verified_uri_map,
                integrity_hashes,
            ) = _preflight_inline_document_set(
                safe_entry,
                inline_document_set,
                root,
                uri_map=uri_map,
                source_origins=source_origins,
                expected_hashes=expected_hashes,
            )
    except LookupError as exc:
        return _failed(
            filing_id=filing_id,
            document_id=document_id,
            parse_id=parse_id,
            code="missing_dependency",
            message=str(exc),
            source_hash=source_hash,
        )
    except Exception as exc:
        if inline_document_set is None:
            verified_source = _verified_fallback_source(
                entrypoint,
                allowed_root,
                uri_map=uri_map,
                source_origins=source_origins,
                expected_hashes=expected_hashes,
            )
            if verified_source is not None:
                _, raw, verified_hash = verified_source
                if verified_hash == source_hash:
                    source_format = _non_xbrl_source_format(raw)
                    if source_format == "legacy_text":
                        return _unsupported(
                            filing_id=filing_id,
                            document_id=document_id,
                            parse_id=parse_id,
                            source_hash=verified_hash,
                            message=(
                                "Complete single-document SEC envelope contains a verified "
                                "legacy-text payload without supported XBRL/iXBRL markers."
                            ),
                        )
                    if source_format == "legacy_html":
                        return _unsupported(
                            filing_id=filing_id,
                            document_id=document_id,
                            parse_id=parse_id,
                            source_hash=verified_hash,
                            message="Legacy non-XML HTML is not an XBRL/iXBRL document.",
                        )
        error_code = (
            "source_integrity_failed"
            if any(marker in str(exc).lower() for marker in ("hash", "verified", "origin map"))
            else "xbrl_preflight_failed"
        )
        return _failed(
            filing_id=filing_id,
            document_id=document_id,
            parse_id=parse_id,
            code=error_code,
            message=str(exc),
            source_hash=source_hash,
        )

    source_hash = hashlib.sha256(source_bytes[entry]).hexdigest()
    if inline_members:
        if any(not _is_xbrl_document(xml_roots[member]) for member in inline_members):
            return _unsupported_inline_document_set(
                filing_id=filing_id,
                document_id=document_id,
                parse_id=parse_id,
                source_hash=source_hash,
                message=(
                    "Every explicit inline document set member must be an Inline XBRL document."
                ),
            )
        targets = set().union(*(_inline_targets(xml_roots[member]) for member in inline_members))
        if targets != {"(default)"}:
            target_message = (
                "Only one default IXDS target is supported; found targets "
                + repr(sorted(targets))
                + "."
            )
            return _unsupported_inline_document_set(
                filing_id=filing_id,
                document_id=document_id,
                parse_id=parse_id,
                source_hash=source_hash,
                message=target_message,
            )
    elif not _is_xbrl_document(xml_roots[entry]):
        source_format = _non_xbrl_source_format(source_bytes[entry])
        if source_format == "protected_sec_envelope":
            return _failed(
                filing_id=filing_id,
                document_id=document_id,
                parse_id=parse_id,
                code="xbrl_preflight_failed",
                message=(
                    "SEC envelope is malformed, contains PDF/DOCTYPE/entity content, or has "
                    "XBRL/iXBRL markers that this XBRL document loader cannot safely classify."
                ),
                source_hash=source_hash,
            )
        return _unsupported(
            filing_id=filing_id,
            document_id=document_id,
            parse_id=parse_id,
            source_hash=source_hash,
            message=(
                "Entrypoint must be an {http://www.xbrl.org/2003/instance}xbrl "
                "instance or supported XHTML Inline XBRL document."
            ),
        )

    try:
        from .sec_transforms import (
            SecTransformIntegrityError,
            has_complete_sec_transform_registry,
            sec_transform_plugin_path,
        )

        sec_transform_path = sec_transform_plugin_path()
    except ImportError:
        return _failed(
            filing_id=filing_id,
            document_id=document_id,
            parse_id=parse_id,
            code="sec_transform_package_missing",
            message="The pinned SEC transformation package is not installed with this parser.",
            source_hash=source_hash,
        )
    except SecTransformIntegrityError:
        return _failed(
            filing_id=filing_id,
            document_id=document_id,
            parse_id=parse_id,
            code="sec_transform_integrity_failed",
            message="The pinned SEC transformation package failed integrity verification.",
            source_hash=source_hash,
        )

    try:
        from arelle.api.Session import Session
        from arelle.ModelDocument import Type as ArelleDocumentType
        from arelle.ModelInstanceObject import VALID
        from arelle.RuntimeOptions import RuntimeOptions
    except ImportError as exc:
        return _failed(
            filing_id=filing_id,
            document_id=document_id,
            parse_id=parse_id,
            code="arelle_dependency_missing",
            message=f"Install the filings extra (arelle-release==2.46.0): {exc}",
            source_hash=source_hash,
        )

    temporary_cache: tempfile.TemporaryDirectory[str] | None = None
    owned_cache_path: Path | None = None
    validation_scope = "arelle_structural_xbrl"
    if inline_members:
        try:
            _trusted_arelle_ixds_plugin()
        except Exception as exc:
            return _failed(
                filing_id=filing_id,
                document_id=document_id,
                parse_id=parse_id,
                code="arelle_ixds_plugin_unavailable",
                message=f"Pinned bundled IXDS plugin verification failed: {exc}",
                source_hash=source_hash,
            )
    try:
        if inline_members:
            if cache_dir is not None and Path(cache_dir).resolve(strict=True) != root:
                return _failed(
                    filing_id=filing_id,
                    document_id=document_id,
                    parse_id=parse_id,
                    code="cache_root_mismatch",
                    message=(
                        "When uri_map is supplied for an IXDS, cache_dir must equal allowed_root."
                    ),
                    source_hash=source_hash,
                )
            temporary_cache = tempfile.TemporaryDirectory(prefix="filings-xbrl-ixds-cache-")
            cache_path = Path(temporary_cache.name)
            _populate_inline_private_cache(
                cache_path,
                uri_map=verified_uri_map,
                source_bytes=source_bytes,
                integrity_hashes=integrity_hashes,
            )
            ixds_entrypoint = json.dumps(
                [
                    {
                        "ixds": [{"file": str(member)} for member in inline_members],
                        "ixdsTarget": "(default)",
                    }
                ],
                separators=(",", ":"),
            )
            options = RuntimeOptions(
                entrypointFile=ixds_entrypoint,
                internetConnectivity="offline",
                cacheDirectory=str(cache_path),
                keepOpen=True,
                disablePersistentConfig=True,
                logFile="logToBuffer",
                validate=True,
            )
        elif uri_map is not None:
            if cache_dir is not None and Path(cache_dir).resolve(strict=True) != root:
                return _failed(
                    filing_id=filing_id,
                    document_id=document_id,
                    parse_id=parse_id,
                    code="cache_root_mismatch",
                    message="When uri_map is supplied, cache_dir must equal allowed_root.",
                    source_hash=source_hash,
                )
            from filings.dependencies import offline_runtime_options

            options = offline_runtime_options(entry, root)
            # Keep the helper's private-cache/offline/no-persistent-config binding,
            # while explicitly requesting Arelle's structural XBRL validator.
            options.validate = True
        else:
            if cache_dir is None:
                temporary_cache = tempfile.TemporaryDirectory(prefix="filings-xbrl-cache-")
                cache_path = Path(temporary_cache.name)
            else:
                cache_parent = Path(cache_dir).resolve(strict=False)
                if _inside(cache_parent, root):
                    return _failed(
                        filing_id=filing_id,
                        document_id=document_id,
                        parse_id=parse_id,
                        code="unsafe_cache_path",
                        message="cache_dir must be outside allowed_root.",
                        source_hash=source_hash,
                    )
                cache_parent.mkdir(parents=True, exist_ok=True)
                cache_path = Path(tempfile.mkdtemp(prefix="filings-xbrl-cache-", dir=cache_parent))
                owned_cache_path = cache_path
            options = RuntimeOptions(
                entrypointFile=str(entry),
                internetConnectivity="offline",
                cacheDirectory=str(cache_path),
                keepOpen=True,
                disablePersistentConfig=True,
                logFile="logToBuffer",
                validate=True,
            )

        # The IXDS feature is activated only by its fixed SDK-bundled plugin name;
        # the only external plugin path admitted is the verified SEC module.
        options.plugins = (
            f"inlineXbrlDocumentSet|{sec_transform_path}"
            if inline_members
            else str(sec_transform_path)
        )
        with _ARELLE_PARSE_LOCK, Session() as session:
            run_succeeded = session.run(options=options)
            models = list(session.get_models())
            if inline_members and len(models) > 1:
                return _failed(
                    filing_id=filing_id,
                    document_id=document_id,
                    parse_id=parse_id,
                    code="ixds_multiple_models",
                    message=(
                        "Arelle loaded multiple IXDS target models; only one default-target "
                        "model is supported."
                    ),
                    source_hash=source_hash,
                )
            model = models[0] if models else None
            if model is None:
                logs = session.get_logs("text")
                if _zero_fraction_loader_crash(logs):
                    errors = [
                        {
                            "code": "arelle_zero_denominator_load_failed",
                            "message": (
                                "Arelle raised ZeroDivisionError while loading an XBRL fraction "
                                "with a zero denominator; validation and fact extraction were "
                                "not performed. Arelle logs: "
                                f"{logs}"
                            ),
                            "severity": "error",
                        }
                    ]
                    for source_path, original_hash in integrity_hashes.items():
                        try:
                            post_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
                        except OSError as exc:
                            post_hash = None
                            message = (
                                f"Verified input became unreadable after Arelle load: "
                                f"{source_path}: {exc}"
                            )
                        else:
                            message = f"Verified input changed during Arelle load: {source_path}"
                        if post_hash != original_hash:
                            errors.append(
                                {
                                    "code": "source_integrity_changed",
                                    "message": message,
                                    "severity": "error",
                                    "source_uri": verified_origins.get(source_path),
                                }
                            )
                    return ParseResult(
                        filing_id=filing_id,
                        document_id=document_id,
                        parse_id=parse_id,
                        parser_name=PARSER_NAME,
                        parser_version=PARSER_VERSION,
                        status="failed",
                        source_hash=source_hash,
                        validation_scope="not_performed",
                        facts=[],
                        errors=errors,
                    )
                return _failed(
                    filing_id=filing_id,
                    document_id=document_id,
                    parse_id=parse_id,
                    code="arelle_no_model",
                    message="Arelle did not return a model for the XBRL entrypoint.",
                    source_hash=source_hash,
                )
            try:
                if sec_transform_plugin_path() != sec_transform_path:
                    raise SecTransformIntegrityError("SEC transformation package root changed")
            except SecTransformIntegrityError:
                return _failed(
                    filing_id=filing_id,
                    document_id=document_id,
                    parse_id=parse_id,
                    code="sec_transform_integrity_failed",
                    message="The pinned SEC transformation package changed during Arelle parsing.",
                    source_hash=source_hash,
                )
            model_manager = getattr(model, "modelManager", None)
            load_transforms = getattr(model_manager, "loadCustomTransforms", None)
            if callable(load_transforms):
                load_transforms()
            custom_transforms = getattr(model_manager, "customTransforms", None)
            if not has_complete_sec_transform_registry(custom_transforms):
                return _failed(
                    filing_id=filing_id,
                    document_id=document_id,
                    parse_id=parse_id,
                    code="sec_transform_registry_incomplete",
                    message=(
                        "Arelle did not register all pinned SEC Inline XBRL transformations; "
                        "no fact occurrences were accepted."
                    ),
                    source_hash=source_hash,
                )
            supported_types = {
                ArelleDocumentType.INSTANCE,
                ArelleDocumentType.INLINEXBRL,
                ArelleDocumentType.INLINEXBRLDOCUMENTSET,
            }
            model_document = getattr(model, "modelDocument", None)
            if getattr(model_document, "type", None) not in supported_types:
                return _unsupported(
                    filing_id=filing_id,
                    document_id=document_id,
                    parse_id=parse_id,
                    source_hash=source_hash,
                    message="Arelle did not load a supported XBRL instance document type.",
                )
            if inline_members:
                if (
                    getattr(model_document, "type", None)
                    != ArelleDocumentType.INLINEXBRLDOCUMENTSET
                    or getattr(model, "ixdsTarget", "__missing__") is not None
                ):
                    return _unsupported_inline_document_set(
                        filing_id=filing_id,
                        document_id=document_id,
                        parse_id=parse_id,
                        source_hash=source_hash,
                        message="Arelle did not load exactly the supported default IXDS target.",
                    )
                loaded_member_paths: list[Path] = []
                for html_element in getattr(model, "ixdsHtmlElements", ()) or ():
                    html_document = getattr(html_element, "modelDocument", None)
                    html_uri = getattr(html_document, "uri", None)
                    html_path = _source_path(html_uri, root, verified_uri_map)
                    if html_path is None or html_path not in inline_members:
                        return _failed(
                            filing_id=filing_id,
                            document_id=document_id,
                            parse_id=parse_id,
                            code="ixds_untrusted_member",
                            message=(
                                "Arelle loaded an inline document whose original URI is not one "
                                "of the explicitly declared, verified members."
                            ),
                            source_hash=source_hash,
                        )
                    loaded_member_paths.append(html_path)
                if len(loaded_member_paths) != len(inline_members) or set(
                    loaded_member_paths
                ) != set(inline_members):
                    return _failed(
                        filing_id=filing_id,
                        document_id=document_id,
                        parse_id=parse_id,
                        code="ixds_member_set_mismatch",
                        message=(
                            "Arelle did not load every explicit IXDS member exactly once into "
                            "the one default-target model."
                        ),
                        source_hash=source_hash,
                    )

            facts_container = getattr(model, "factsInInstance", None)
            facts = list(facts_container or [])
            fact_infos: list[tuple[Any, dict[str, Any]]] = []
            for fallback_index, fact in enumerate(facts):
                info = _fact_source_details(
                    fact,
                    fallback_index,
                    root,
                    source_bytes,
                    verified_origins,
                    verified_uri_map,
                    filing_id,
                    document_id,
                    parse_id,
                )
                if inline_members:
                    actual_owner = info["source_path"]
                    expected_owner_hash = integrity_hashes.get(actual_owner)
                    if (
                        actual_owner not in inline_members
                        or actual_owner not in source_bytes
                        or not info["source_verified"]
                        or info["source_uri"] != verified_origins.get(actual_owner)
                        or expected_owner_hash is None
                        or info["document_hash"] != expected_owner_hash
                    ):
                        return _failed(
                            filing_id=filing_id,
                            document_id=document_id,
                            parse_id=parse_id,
                            code="ixds_untrusted_fact_source",
                            message=(
                                "Arelle fact ownership did not resolve to a declared member "
                                "with the verified original URI and source hash."
                            ),
                            source_hash=source_hash,
                        )
                fact_infos.append((fact, info))
            fact_infos.sort(
                key=lambda pair: (
                    pair[1]["source_uri"] or pair[1]["source_relpath"] or "",
                    pair[1]["object_index"],
                    pair[1]["source_line"] or 0,
                    pair[1]["occurrence_id"],
                )
            )

            id_to_occurrence = {id(fact): info["occurrence_id"] for fact, info in fact_infos}
            key_to_occurrence = {
                (info["source_identity"], info["object_index"]): info["occurrence_id"]
                for _, info in fact_infos
            }
            tuple_candidates: dict[tuple[str, str], list[str]] = {}
            global_tuple_candidates: dict[str, list[str]] = {}
            for fact, info in fact_infos:
                if not bool(getattr(fact, "isTuple", False)):
                    continue
                tuple_id = getattr(fact, "tupleID", None)
                if not tuple_id and info["element"] is not None:
                    tuple_id = info["element"].get("tupleID") or info["element"].get("id")
                if tuple_id:
                    tuple_candidates.setdefault(
                        (info["source_identity"], str(tuple_id)), []
                    ).append(info["occurrence_id"])
                    global_tuple_candidates.setdefault(str(tuple_id), []).append(
                        info["occurrence_id"]
                    )
            tuple_ids = {
                key: values[0] for key, values in tuple_candidates.items() if len(values) == 1
            }
            unambiguous_tuple_ids = {
                key: values[0]
                for key, values in global_tuple_candidates.items()
                if len(values) == 1
            }

            facts_rows: list[dict[str, Any]] = []
            occurrence_errors: list[dict[str, Any]] = []
            for fact, info in fact_infos:
                row, errors = _fact_row(
                    fact,
                    info,
                    filing_id=filing_id,
                    document_id=document_id,
                    parse_id=parse_id,
                    source_hash=source_hash,
                    status="full",
                    valid_constant=VALID,
                    validation_scope=validation_scope,
                )
                row["parent_occurrence_id"] = _tuple_parent(
                    fact,
                    info,
                    id_to_occurrence,
                    key_to_occurrence,
                    tuple_ids,
                    unambiguous_tuple_ids,
                )
                facts_rows.append(row)
                occurrence_errors.extend(errors)

            errors = _model_errors(model)
            errors.extend(occurrence_errors)
            if not run_succeeded:
                errors.append(
                    {
                        "code": "arelle_run_failed",
                        "message": (
                            "Arelle reported an unsuccessful run; extracted occurrences retained."
                        ),
                        "severity": "error",
                    }
                )

            # Arelle opens inputs after preflight and may lazily materialize values.
            # Rehash every source after extraction so no changed source can be full.
            for source_path, original_hash in integrity_hashes.items():
                try:
                    post_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
                except OSError as exc:
                    post_hash = None
                    message = (
                        f"Verified input became unreadable after Arelle load: {source_path}: {exc}"
                    )
                else:
                    message = f"Verified input changed during Arelle load: {source_path}"
                if post_hash != original_hash:
                    errors.append(
                        {
                            "code": "source_integrity_changed",
                            "message": message,
                            "severity": "error",
                            "source_uri": verified_origins.get(source_path),
                        }
                    )

            if inline_members and any(
                error.get("code") == "source_integrity_changed" for error in errors
            ):
                return ParseResult(
                    filing_id=filing_id,
                    document_id=document_id,
                    parse_id=parse_id,
                    parser_name=PARSER_NAME,
                    parser_version=PARSER_VERSION,
                    status="failed",
                    source_hash=source_hash,
                    validation_scope="not_performed",
                    facts=[],
                    errors=errors,
                )

            status = "partial" if errors else "full"
            for row in facts_rows:
                row["status"] = status
            return ParseResult(
                filing_id=filing_id,
                document_id=document_id,
                parse_id=parse_id,
                parser_name=PARSER_NAME,
                parser_version=PARSER_VERSION,
                status=status,
                source_hash=source_hash,
                validation_scope=validation_scope,
                facts=facts_rows,
                errors=errors,
            )
    except Exception as exc:
        return _failed(
            filing_id=filing_id,
            document_id=document_id,
            parse_id=parse_id,
            code="xbrl_parse_failed",
            message=f"Arelle offline parse failed: {type(exc).__name__}: {exc}",
            source_hash=source_hash,
        )
    finally:
        if temporary_cache is not None:
            temporary_cache.cleanup()
        elif owned_cache_path is not None:
            import shutil

            shutil.rmtree(owned_cache_path, ignore_errors=True)
