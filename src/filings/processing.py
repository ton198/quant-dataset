"""Bounded provenance-preserving processing of selected filing archive documents.

This is a small integration layer, not a download client or a general processing
server. It never creates an archive, chooses a company universe, rewrites XML, or
adds sample features. An optional caller-supplied dependency fetch callback is the
only possible network boundary; Arelle itself is always configured offline.
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from collections import defaultdict
from collections.abc import Callable, Collection, Mapping, Sequence
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any
from urllib.parse import unquote, urlsplit

import pyarrow as pa

from . import parse_text as text_parser
from . import parse_xbrl as xbrl_parser
from .archive import (
    ArchiveConflictError,
    ArchiveWriter,
    load_archive_runspec,
    open_archive,
    read_snapshot,
)
from .dependencies import DependencyPreparationError, PreparedDependencies, prepare_dependencies
from .models import DOCUMENTS_SCHEMA, FILINGS_SCHEMA, RawObjectRef, Snapshot, filing_id
from .packages import PreparedPackage, materialize_package
from .parsing_models import FACT_SCHEMA, SECTION_SCHEMA, ParseResult
from .pipeline.planning import select_filings as _plan_select_filings
from .pipeline.validation import (
    annotate_group_fact_owners as _validate_group_fact_owners,
)
from .pipeline.validation import (
    fact_sources_verified as _validate_fact_sources,
)
from .processing_contract import (
    DEPENDENCIES_SCHEMA,
    PARSES_SCHEMA,
    PROCESSING_CONTRACT,
)
from .processing_contract import (
    parse_id as _contract_parse_id,
)
from .sec_client import validate_sec_url

_MAX_FILINGS = 50
_MAX_DEPENDENCIES = 500
_MAX_DEPENDENCY_BYTES = 128 * 1024 * 1024
_MAX_INLINE_GROUP_MEMBERS = 16
_INLINE_SOURCE_GROUP_VERSION = "ixds-source-group-v1"
_PROCESSING_CONTRACT = PROCESSING_CONTRACT
_PARSE_STATES = frozenset({"full", "partial", "unsupported", "failed"})
_TERMINAL_PARSE_STATES = frozenset({"full", "unsupported"})
_PACKAGE_ROLES = frozenset({"primary", "xbrl_instance", "schema", "linkbase", "exhibit"})
_TEXT_ROLES = frozenset({"primary", "exhibit"})
_HEX_256 = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_SAFE_CODE = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]{0,127}\Z", re.ASCII)
_INLINE_XBRL_NAMESPACES = frozenset(
    {"http://www.xbrl.org/2013/inlineXBRL", "http://www.xbrl.org/2008/inlineXBRL"}
)
_SOURCE_INTEGRITY_ERRORS = frozenset(
    {
        "source_integrity_failed",
        "source_integrity_changed",
        "source_integrity_mismatch",
        "source_hash_mismatch",
        "unverified_fact_source",
    }
)


class ProcessingError(ValueError):
    """Raised when an archive cannot safely enter or complete processing."""


class ProcessingCorruptionError(ProcessingError):
    """Raised when active parser rows or parser-owned tables are inconsistent."""


@dataclass(frozen=True, slots=True)
class ProcessingResult:
    """Published snapshot plus bounded counts/statuses from this processing call.

    These counts describe parser outputs, not total financial coverage. A parser
    ``full`` status means that parser's declared checks succeeded; it is not an
    economic or fundamental correctness claim.
    """

    snapshot: Snapshot
    selected_filing_ids: tuple[str, ...]
    processed_filing_ids: tuple[str, ...]
    skipped_filing_ids: tuple[str, ...]
    filing_statuses: Mapping[str, str]
    parse_statuses: Mapping[str, str]
    fact_rows_written: int
    section_rows_written: int
    dependency_rows_written: int
    full_parse_count: int
    partial_parse_count: int
    unsupported_parse_count: int
    failed_parse_count: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "filing_statuses", MappingProxyType(dict(self.filing_statuses)))
        object.__setattr__(self, "parse_statuses", MappingProxyType(dict(self.parse_statuses)))


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ProcessingError("processing provenance must contain canonical JSON values") from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _resolve_dir(path: Path, label: str) -> Path:
    try:
        resolved = Path(path).expanduser().resolve(strict=True)
    except (OSError, TypeError) as exc:
        raise ProcessingError(f"{label} must be an existing directory") from exc
    if not resolved.is_dir():
        raise ProcessingError(f"{label} must be an existing directory")
    return resolved


def _overlaps(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _protected_paths(values: Sequence[Path]) -> tuple[Path, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence) or not values:
        raise ProcessingError("protected_paths must be a non-empty explicit path sequence")
    protected: list[Path] = []
    for value in values:
        if not isinstance(value, (str, Path)) or not str(value):
            raise ProcessingError("protected_paths must contain non-empty filesystem paths")
        try:
            protected.append(Path(value).expanduser().resolve(strict=False))
        except (OSError, TypeError) as exc:
            raise ProcessingError("protected_paths contains an invalid path") from exc
    return tuple(protected)


def _check_root_separation(archive: Path, workspace: Path, protected: Sequence[Path]) -> None:
    if _overlaps(archive, workspace):
        raise ProcessingError("workspace_root must be separate from the archive root")
    for path in protected:
        if _overlaps(archive, path):
            raise ProcessingError("archive root overlaps a protected path")
        if _overlaps(workspace, path):
            raise ProcessingError("workspace_root overlaps a protected path")


def _normalize_request(
    filing_ids: Sequence[str] | None,
    max_filings: int,
    fetch_dependencies: Callable[[str], Any] | None,
    allowed_taxonomy_hosts: Collection[str],
) -> tuple[tuple[str, ...] | None, tuple[str, ...]]:
    if (
        isinstance(max_filings, bool)
        or not isinstance(max_filings, int)
        or not 1 <= max_filings <= _MAX_FILINGS
    ):
        raise ProcessingError(f"max_filings must be an integer from 1 to {_MAX_FILINGS}")
    if fetch_dependencies is not None and not callable(fetch_dependencies):
        raise ProcessingError("fetch_dependencies must be callable or None")
    if isinstance(allowed_taxonomy_hosts, (str, bytes)) or not isinstance(
        allowed_taxonomy_hosts, Collection
    ):
        raise ProcessingError("allowed_taxonomy_hosts must be an explicit collection of hostnames")
    hosts: set[str] = set()
    for host in allowed_taxonomy_hosts:
        if not isinstance(host, str) or not host.strip():
            raise ProcessingError("allowed_taxonomy_hosts must contain non-empty hostnames")
        value = host.strip().lower().rstrip(".")
        if not value or "://" in value or "/" in value or "@" in value or "*" in value:
            raise ProcessingError("allowed_taxonomy_hosts must contain exact bare hostnames")
        try:
            hosts.add(value.encode("idna").decode("ascii"))
        except UnicodeError as exc:
            raise ProcessingError("allowed_taxonomy_hosts contains an invalid hostname") from exc
    if filing_ids is None:
        return None, tuple(sorted(hosts))
    if isinstance(filing_ids, (str, bytes)) or not isinstance(filing_ids, Sequence):
        raise ProcessingError("filing_ids must be a sequence of exact filing identities")
    if len(filing_ids) > max_filings:
        raise ProcessingError("explicit filing_ids exceed max_filings; no IDs were truncated")
    identities: list[str] = []
    for value in filing_ids:
        if not isinstance(value, str):
            raise ProcessingError("filing_ids must contain strings")
        try:
            cik, accession = value.split(":", 1)
            normalized = filing_id(cik, accession)
        except (TypeError, ValueError) as exc:
            raise ProcessingError("filing_ids must use canonical CIK:accession identities") from exc
        if normalized != value:
            raise ProcessingError("filing_ids must use canonical CIK:accession identities")
        identities.append(normalized)
    if len(set(identities)) != len(identities):
        raise ProcessingError("filing_ids must not contain duplicates")
    return tuple(identities), tuple(sorted(hosts))


def _read_table(
    snapshot: Snapshot, name: str, schema: pa.Schema, *, required: bool = False
) -> tuple[pa.Table, list[dict[str, Any]]]:
    table = snapshot.tables.get(name)
    if table is None:
        if required:
            raise ProcessingError(f"archive has no active {name} table")
        table = pa.Table.from_pylist([], schema=schema)
    if not isinstance(table, pa.Table) or not table.schema.equals(schema, check_metadata=True):
        raise ProcessingCorruptionError(f"active {name} table differs from its owned Arrow schema")
    return table, table.to_pylist()


def _status(value: Any, label: str) -> str:
    if not isinstance(value, str) or value not in _PARSE_STATES:
        raise ProcessingCorruptionError(f"active parser table has an invalid {label}")
    return value


def _validate_active_state(
    snapshot: Snapshot,
    filings: Sequence[Mapping[str, Any]],
    documents: Sequence[Mapping[str, Any]],
    facts: Sequence[Mapping[str, Any]],
    sections: Sequence[Mapping[str, Any]],
    parses: Sequence[Mapping[str, Any]],
    dependencies: Sequence[Mapping[str, Any]],
    raw_refs: Mapping[str, RawObjectRef],
) -> tuple[dict[str, dict[str, Any]], dict[tuple[str, str], Mapping[str, Any]]]:
    del snapshot
    filing_ids = {row["filing_id"] for row in filings}
    docs_by_id: dict[str, dict[str, Any]] = {}
    for row in documents:
        if row["document_id"] in docs_by_id:
            raise ProcessingCorruptionError("active documents table repeats a document_id")
        if row["filing_id"] not in filing_ids:
            raise ProcessingCorruptionError("active documents table references an unknown filing")
        docs_by_id[row["document_id"]] = row  # type: ignore[assignment]

    parses_by_key: dict[tuple[str, str], Mapping[str, Any]] = {}
    parses_by_id: dict[str, Mapping[str, Any]] = {}
    groups_by_parse_id: dict[str, tuple[dict[str, Any], dict[str, Mapping[str, Any]]]] = {}
    group_by_filing: dict[str, str] = {}
    for row in parses:
        _status(row["status"], "status")
        doc = docs_by_id.get(row["document_id"])
        if (
            row["filing_id"] not in filing_ids
            or doc is None
            or doc["filing_id"] != row["filing_id"]
        ):
            raise ProcessingCorruptionError(
                "active parse references a missing or foreign filing/document"
            )
        key = (row["document_id"], row["parser_name"])
        if key in parses_by_key or row["parse_id"] in parses_by_id:
            raise ProcessingCorruptionError(
                "parses table repeats an active document/parser identity"
            )
        if (
            doc["fetch_status"] != "present"
            or not isinstance(row["source_sha256"], str)
            or _HEX_256.fullmatch(row["source_sha256"]) is None
            or not isinstance(row["dependencies_fingerprint"], str)
            or _HEX_256.fullmatch(row["dependencies_fingerprint"]) is None
        ):
            raise ProcessingCorruptionError(
                "active parse source or dependency fingerprint differs from verified identity"
            )
        try:
            notes = json.loads(row["notes_json"])
        except (TypeError, ValueError) as exc:
            raise ProcessingCorruptionError("active parse notes are not valid JSON") from exc
        if not isinstance(notes, Mapping):
            raise ProcessingCorruptionError("active parse notes must be a JSON object")
        source_group = notes.get("source_group")
        if source_group is not None:
            if row["parser_name"] != xbrl_parser.PARSER_NAME or not isinstance(
                source_group, Mapping
            ):
                raise ProcessingCorruptionError(
                    "source group is attached to an invalid parser record"
                )
            group = dict(source_group)
            fingerprint = notes.get("source_group_fingerprint")
            members = group.get("members")
            runtime_note = notes.get("ixds_runtime")
            retired_ids = notes.get("retired_parse_ids")
            allowed_hosts = notes.get("allowed_taxonomy_hosts")
            if (
                group.get("version") != _INLINE_SOURCE_GROUP_VERSION
                or group.get("target") != ""
                or group.get("anchor_document_id") != row["document_id"]
                or not isinstance(fingerprint, str)
                or _HEX_256.fullmatch(fingerprint) is None
                or not isinstance(members, list)
                or not 2 <= len(members) <= _MAX_INLINE_GROUP_MEMBERS
                or not isinstance(members[0], Mapping)
                or members[0].get("document_id") != row["document_id"]
                or _digest(group) != fingerprint
                or notes.get("grouping_basis") != group.get("grouping_basis")
                or not isinstance(runtime_note, Mapping)
                or runtime_note.get("parser_version") != row["parser_version"]
                or runtime_note.get("arelle_version") != "2.46.0"
                or not isinstance(runtime_note.get("sec_plugin"), str)
                or not runtime_note.get("sec_plugin")
                or not isinstance(retired_ids, list)
                or any(not isinstance(item, str) for item in retired_ids)
                or not isinstance(allowed_hosts, list)
                or any(not isinstance(host, str) for host in allowed_hosts)
            ):
                raise ProcessingCorruptionError(
                    "active source group note or fingerprint is invalid"
                )
            member_docs: dict[str, Mapping[str, Any]] = {}
            member_urls: set[str] = set()
            for member in members:
                if not isinstance(member, Mapping):
                    raise ProcessingCorruptionError("active source group member is malformed")
                identity = member.get("document_id")
                source_url = member.get("source_url")
                digest = member.get("raw_sha256")
                size = member.get("byte_size")
                filename = member.get("original_filename")
                member_doc = docs_by_id.get(identity)
                if (
                    not isinstance(identity, str)
                    or identity in member_docs
                    or not isinstance(source_url, str)
                    or source_url in member_urls
                    or not isinstance(digest, str)
                    or _HEX_256.fullmatch(digest) is None
                    or isinstance(size, bool)
                    or not isinstance(size, int)
                    or size < 0
                    or not isinstance(filename, str)
                    or member_doc is None
                    or member_doc["filing_id"] != row["filing_id"]
                    or member_doc["source_url"] != source_url
                    or member_doc["original_filename"] != filename
                    or member_doc["fetch_status"] != "present"
                    or member_doc["selection_status"] != "required"
                    or member_doc["role"] not in _TEXT_ROLES
                ):
                    raise ProcessingCorruptionError("active source group member binding is invalid")
                if member_doc["raw_sha256"] != digest or member_doc["byte_size"] != size:
                    if not any(
                        ref.sha256 == digest and ref.byte_size == size for ref in raw_refs.values()
                    ):
                        raise ProcessingCorruptionError(
                            "stale source group member has no retained historical content address"
                        )
                member_docs[identity] = member_doc
                member_urls.add(source_url)
            if (
                len(member_docs) != len(members)
                or members[0].get("raw_sha256") != row["source_sha256"]
                or docs_by_id[row["document_id"]]["role"] != "primary"
                or [member["source_url"] for member in members[1:]]
                != sorted(member["source_url"] for member in members[1:])
            ):
                raise ProcessingCorruptionError("active source group ordering or anchor is invalid")
            basis = group.get("grouping_basis")
            evidence = basis.get("references") if isinstance(basis, Mapping) else None
            if (
                not isinstance(basis, Mapping)
                or basis.get("evidence") != "typed_cross_file_references_v1"
                or not isinstance(evidence, list)
                or not evidence
            ):
                raise ProcessingCorruptionError(
                    "active source group has no typed cross-file evidence"
                )
            for item in evidence:
                if (
                    not isinstance(item, Mapping)
                    or item.get("source_document_id") not in member_docs
                    or item.get("target_document_id") not in member_docs
                    or item.get("source_sha256")
                    != next(
                        m["raw_sha256"]
                        for m in members
                        if m["document_id"] == item["source_document_id"]
                    )
                    or item.get("source_url")
                    != next(
                        m["source_url"]
                        for m in members
                        if m["document_id"] == item["source_document_id"]
                    )
                    or item.get("target_sha256")
                    != next(
                        m["raw_sha256"]
                        for m in members
                        if m["document_id"] == item["target_document_id"]
                    )
                    or item.get("target_source_url")
                    != next(
                        m["source_url"]
                        for m in members
                        if m["document_id"] == item["target_document_id"]
                    )
                    or not isinstance(item.get("kind"), str)
                    or not isinstance(item.get("reference_id"), str)
                ):
                    raise ProcessingCorruptionError(
                        "active source group evidence is not source-bound"
                    )
            expected_id = _parse_id(
                row["filing_id"],
                row["document_id"],
                row["source_sha256"],
                row["parser_name"],
                row["parser_version"],
                row["dependencies_fingerprint"],
                source_group_fingerprint=fingerprint,
            )
            if row["parse_id"] != expected_id:
                raise ProcessingCorruptionError(
                    "active grouped parse_id does not bind its source group"
                )
            if row["filing_id"] in group_by_filing:
                raise ProcessingCorruptionError(
                    "filing has more than one active source-group parse"
                )
            group_by_filing[row["filing_id"]] = row["parse_id"]
            groups_by_parse_id[row["parse_id"]] = (group, member_docs)
        elif doc["raw_sha256"] != row["source_sha256"]:
            stale_text_parse = row["parser_name"] == text_parser.PARSER_NAME and any(
                ref.sha256 == row["source_sha256"] for ref in raw_refs.values()
            )
            if not stale_text_parse:
                raise ProcessingCorruptionError(
                    "active standalone parse source differs from verified document identity"
                )
        parses_by_key[key] = row
        parses_by_id[row["parse_id"]] = row

    fact_counts: dict[str, int] = defaultdict(int)
    for row in facts:
        parse = parses_by_id.get(row["parse_id"])
        if parse is None or parse["parser_name"] != xbrl_parser.PARSER_NAME:
            raise ProcessingCorruptionError("fact row has no matching active XBRL parse")
        if any(
            row[field] != parse[value]
            for field, value in (
                ("filing_id", "filing_id"),
                ("document_id", "document_id"),
                ("parse_id", "parse_id"),
                ("parser_name", "parser_name"),
                ("parser_version", "parser_version"),
                ("status", "status"),
                ("source_hash", "source_sha256"),
            )
        ):
            raise ProcessingCorruptionError(
                "fact row identity/status differs from active XBRL parse"
            )
        fact_counts[row["parse_id"]] += 1

    for parse_id_value, group_data in groups_by_parse_id.items():
        parse = parses_by_id[parse_id_value]
        member_docs = group_data[1]
        for member_id, member_doc in member_docs.items():
            if member_doc["fact_extraction_status"] != parse["status"]:
                raise ProcessingCorruptionError(
                    "persisted source-group member state differs from its group parse"
                )
            if (
                member_id != parse["document_id"]
                and (
                    member_id,
                    xbrl_parser.PARSER_NAME,
                )
                in parses_by_key
            ):
                raise ProcessingCorruptionError(
                    "source-group member has a duplicate numeric parse record"
                )

    section_counts: dict[str, int] = defaultdict(int)
    for row in sections:
        parse = parses_by_id.get(row["parse_id"])
        if parse is None or parse["parser_name"] != text_parser.PARSER_NAME:
            raise ProcessingCorruptionError("section row has no matching active text parse")
        if any(
            row[field] != parse[value]
            for field, value in (
                ("filing_id", "filing_id"),
                ("document_id", "document_id"),
                ("parse_id", "parse_id"),
                ("parser_name", "parser_name"),
                ("parser_version", "parser_version"),
                ("status", "status"),
                ("source_hash", "source_sha256"),
            )
        ):
            raise ProcessingCorruptionError(
                "section row identity/status differs from active text parse"
            )
        doc = docs_by_id[row["document_id"]]
        if (
            row["document_hash"] != parse["source_sha256"]
            or row["source_relpath"] != doc["original_filename"]
        ):
            raise ProcessingCorruptionError(
                "section source hash/path differs from its verified document"
            )
        section_counts[row["parse_id"]] += 1

    for row in parses:
        if row["fact_count"] != fact_counts.get(row["parse_id"], 0):
            raise ProcessingCorruptionError(
                "active parse fact_count differs from active facts rows"
            )
        if row["section_count"] != section_counts.get(row["parse_id"], 0):
            raise ProcessingCorruptionError(
                "active parse section_count differs from active sections rows"
            )
        doc = docs_by_id[row["document_id"]]
        state_field = (
            "fact_extraction_status"
            if row["parser_name"] == xbrl_parser.PARSER_NAME
            else "text_extraction_status"
            if row["parser_name"] == text_parser.PARSER_NAME
            else None
        )
        if state_field is not None and doc[state_field] != row["status"]:
            raise ProcessingCorruptionError(
                "document extraction state differs from its active parse"
            )

    active_graphs = {
        (row["filing_id"], row["dependencies_fingerprint"])
        for row in parses
        if row["parser_name"] == xbrl_parser.PARSER_NAME
    }
    dependency_keys: set[tuple[Any, ...]] = set()
    dependency_sources: dict[tuple[str, str], set[tuple[str | None, str | None, str]]] = (
        defaultdict(set)
    )
    for row in dependencies:
        if row["filing_id"] not in filing_ids:
            raise ProcessingCorruptionError("dependency provenance references an unknown filing")
        fingerprint = row["dependency_fingerprint"]
        graph_key = (row["filing_id"], fingerprint)
        if (
            not isinstance(fingerprint, str)
            or _HEX_256.fullmatch(fingerprint) is None
            or graph_key not in active_graphs
        ):
            raise ProcessingCorruptionError(
                "dependency record is not bound to an active XBRL parse graph"
            )
        key = (
            row["filing_id"],
            fingerprint,
            row["requested_url"],
            row["final_url"],
            row["sha256"],
            row["status"],
            row["diagnostic_code"],
        )
        if key in dependency_keys:
            raise ProcessingCorruptionError("dependencies table repeats an active resource record")
        dependency_keys.add(key)
        if row["status"] == "present":
            ref = raw_refs.get(row["raw_path"])
            if (
                not isinstance(row["sha256"], str)
                or _HEX_256.fullmatch(row["sha256"]) is None
                or isinstance(row["byte_size"], bool)
                or not isinstance(row["byte_size"], int)
                or ref is None
                or ref.sha256 != row["sha256"]
                or ref.byte_size != row["byte_size"]
            ):
                raise ProcessingCorruptionError(
                    "dependency bytes are not a verified active raw object"
                )
            dependency_sources[(row["filing_id"], fingerprint)].add(
                (row["requested_url"], row["final_url"], row["sha256"])
            )
        elif row["status"] != "error" or any(
            row[field] is not None for field in ("sha256", "byte_size", "raw_path")
        ):
            raise ProcessingCorruptionError("dependency status has invalid payload fields")

    documents_by_filing: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for document in documents:
        if (
            document["fetch_status"] == "present"
            and document["selection_status"] == "required"
            and document["role"] in _PACKAGE_ROLES
        ):
            documents_by_filing[document["filing_id"]].append(document)
    for parse_id_value, group_data in groups_by_parse_id.items():
        parse = parses_by_id[parse_id_value]
        try:
            group_notes = json.loads(parse["notes_json"])
        except (TypeError, ValueError) as exc:
            raise ProcessingCorruptionError("active source-group notes are not valid JSON") from exc
        if isinstance(group_notes, Mapping) and group_notes.get("dependency_mode") == (
            "preparation_failed"
        ):
            if not _group_preparation_failure_matches(
                parse,
                group_data[0],
                documents_by_filing[parse["filing_id"]],
                dependencies,
                documents_by_id=docs_by_id,
                raw_refs=raw_refs,
                require_current_sources=False,
            ):
                raise ProcessingCorruptionError(
                    "active group dependency-preparation failure provenance is inconsistent"
                )
    for row in facts:
        parse = parses_by_id[row["parse_id"]]
        source_uri = row["source_uri"]
        source_relpath = row["source_relpath"]
        document_hash = row["document_hash"]
        if (
            not isinstance(document_hash, str)
            or _HEX_256.fullmatch(document_hash) is None
            or not isinstance(source_uri, str)
            or not isinstance(source_relpath, str)
            or not source_relpath
        ):
            raise ProcessingCorruptionError("fact row has incomplete verified-source provenance")
        group_data = groups_by_parse_id.get(row["parse_id"])
        if group_data is not None:
            group = group_data[0]
            owners = [
                member
                for member in group["members"]
                if member["source_url"] == source_uri and member["raw_sha256"] == document_hash
            ]
            if (
                len(owners) != 1
                or Path(unquote(source_relpath)).name != owners[0]["original_filename"]
            ):
                raise ProcessingCorruptionError(
                    "grouped fact source is not an exact declared member URI/hash/path binding"
                )
            try:
                provenance = json.loads(row["provenance"])
            except (TypeError, ValueError) as exc:
                raise ProcessingCorruptionError(
                    "grouped fact provenance is not valid JSON"
                ) from exc
            if (
                not isinstance(provenance, Mapping)
                or provenance.get("source_document_id") != owners[0]["document_id"]
            ):
                raise ProcessingCorruptionError(
                    "grouped fact provenance does not identify its verified physical source"
                )
            continue
        try:
            source_parts = urlsplit(source_uri)
        except ValueError as exc:
            raise ProcessingCorruptionError("fact row has an invalid source URI") from exc
        source_filename = (
            Path(unquote(source_parts.path)).name
            if source_parts.scheme == "file"
            else Path(source_uri).name
        )
        source_is_document = any(
            document_hash == doc["raw_sha256"]
            and (
                source_uri == doc["source_url"]
                or (
                    source_parts.scheme in {"", "file"}
                    and source_filename == doc["original_filename"]
                    and Path(source_relpath).name == doc["original_filename"]
                )
            )
            for doc in documents_by_filing[parse["filing_id"]]
        )
        source_is_dependency = any(
            document_hash == expected_hash and source_uri in {requested, final}
            for requested, final, expected_hash in dependency_sources.get(
                (parse["filing_id"], parse["dependencies_fingerprint"]), set()
            )
        )
        if not source_is_document and not source_is_dependency:
            raise ProcessingCorruptionError(
                "fact source URI/hash is not authorized by this filing and parse graph"
            )
    return docs_by_id, parses_by_key


def _raw_hash(row: Mapping[str, Any]) -> str:
    value = row.get("raw_sha256")
    if not isinstance(value, str) or _HEX_256.fullmatch(value) is None:
        raise ProcessingCorruptionError("present document has an invalid raw_sha256")
    return value


def _doc_ref_path(root: Path, row: Mapping[str, Any], refs: Mapping[str, RawObjectRef]) -> Path:
    ref = refs.get(row.get("raw_path"))
    if (
        row.get("fetch_status") != "present"
        or ref is None
        or row.get("raw_sha256") != ref.sha256
        or row.get("byte_size") != ref.byte_size
    ):
        raise ProcessingCorruptionError("selected document is not a verified present raw object")
    return root / ref.path


def _inline_xbrl(path: Path) -> bool:
    """Recognize actual iXBRL elements through secure, encoding-aware XML parsing."""
    import codecs

    from lxml import etree

    class InlineTarget:
        root_seen = False
        is_xhtml = False
        found = False

        def start(self, tag: str, _attributes: Mapping[str, str]) -> None:
            if not self.root_seen:
                self.root_seen = True
                self.is_xhtml = tag == "{http://www.w3.org/1999/xhtml}html"
            if self.is_xhtml and any(
                tag.startswith(f"{{{namespace}}}") for namespace in _INLINE_XBRL_NAMESPACES
            ):
                self.found = True

        def end(self, _tag: str) -> None:
            return None

        def data(self, _value: str) -> None:
            return None

        def close(self) -> bool:
            return self.found

    target = InlineTarget()
    try:
        with path.open("rb") as stream:
            prefix = stream.read(1024)
            if prefix.startswith((b"\x00\x00\xfe\xff", b"\xff\xfe\x00\x00")):
                encoding = "utf-32"
            elif prefix.startswith((b"\xfe\xff", b"\xff\xfe")):
                encoding = "utf-16"
            elif prefix.startswith(b"\xef\xbb\xbf"):
                encoding = "utf-8-sig"
            elif prefix.startswith(b"\x00\x00\x00<"):
                encoding = "utf-32-be"
            elif prefix.startswith(b"<\x00\x00\x00"):
                encoding = "utf-32-le"
            elif prefix.startswith(b"\x00<\x00?"):
                encoding = "utf-16-be"
            elif prefix.startswith(b"<\x00?\x00"):
                encoding = "utf-16-le"
            else:
                declaration = re.search(
                    rb"<\?xml[^>]{0,512}?encoding\s*=\s*['\"]([^'\"]+)",
                    prefix,
                    re.IGNORECASE,
                )
                encoding = declaration.group(1).decode("ascii") if declaration else "utf-8"
            decoder = codecs.getincrementaldecoder(encoding)(errors="strict")
            parser = etree.XMLParser(
                target=target,
                resolve_entities=False,
                no_network=True,
                load_dtd=False,
                huge_tree=False,
            )
            text = decoder.decode(prefix, final=False)
            if text:
                parser.feed(text)
            if target.found or (target.root_seen and not target.is_xhtml):
                return target.found
            while chunk := stream.read(64 * 1024):
                text = decoder.decode(chunk, final=False)
                if text:
                    parser.feed(text)
                if target.found or (target.root_seen and not target.is_xhtml):
                    return target.found
            tail = decoder.decode(b"", final=True)
            if tail:
                parser.feed(tail)
            parser.close()
            return target.found
    except (OSError, LookupError, UnicodeError, etree.XMLSyntaxError, ValueError):
        return target.found


def _entrypoint_path(entry: Any) -> Path:
    return Path(entry.local_path if hasattr(entry, "local_path") else entry)


def _group_source_rows(
    rows: Sequence[Mapping[str, Any]],
    entrypoints: Mapping[str, Any],
) -> tuple[list[Mapping[str, Any]], Mapping[str, Mapping[str, Any]]]:
    """Return present inline sources and their typed cross-document ID evidence."""
    from lxml import etree

    inline_rows = [
        row
        for row in _text_targets(rows)
        if row["source_url"] in entrypoints
        and _inline_xbrl(_entrypoint_path(entrypoints[row["source_url"]]))
    ]
    paths = {
        row["document_id"]: _entrypoint_path(entrypoints[row["source_url"]]) for row in inline_rows
    }
    id_owners: dict[str, list[tuple[str, str]]] = defaultdict(list)
    references: list[dict[str, str]] = []
    unsupported_targets: list[str] = []
    ix_namespaces = _INLINE_XBRL_NAMESPACES
    xbrli_namespace = "http://www.xbrl.org/2003/instance"

    for row in inline_rows:
        path = paths[row["document_id"]]
        parser = etree.XMLParser(
            resolve_entities=False,
            no_network=True,
            load_dtd=False,
            huge_tree=False,
            remove_comments=False,
        )
        try:
            tree = etree.parse(str(path), parser)
        except (OSError, etree.XMLSyntaxError, ValueError) as exc:
            raise ProcessingError(
                "inline source cannot be safely inspected for cross-file references"
            ) from exc
        info = tree.docinfo
        if info.doctype and not (
            re.fullmatch(r"<!DOCTYPE\s+html\s*>", info.doctype, re.IGNORECASE)
            and not info.system_url
            and not info.public_id
            and not (info.internalDTD and info.internalDTD.entities())
        ):
            raise ProcessingError("inline source has an unsupported DTD during group inspection")
        for node in tree.getroot().iter():
            tag = str(getattr(node, "tag", ""))
            if not tag.startswith("{"):
                continue
            namespace, local = tag[1:].split("}", 1)
            identity = node.get("id")
            if namespace in ix_namespaces and local.lower() == "tuple":
                identity = identity or node.get("tupleID")
            if identity:
                if namespace == xbrli_namespace and local in {"context", "unit"}:
                    kind = local
                elif namespace in ix_namespaces and local.lower() == "continuation":
                    kind = "continuation"
                elif namespace in ix_namespaces and local.lower() in {
                    "nonfraction",
                    "nonnumeric",
                    "fraction",
                    "footnote",
                    "relationship",
                }:
                    kind = "relationship_target"
                else:
                    kind = "other"
                id_owners[identity].append((row["document_id"], kind))

            if namespace in ix_namespaces:
                if local.lower() == "references":
                    target = (node.get("target") or "").strip()
                    if target and target != "(default)":
                        unsupported_targets.append(row["document_id"])
                if local.lower() in {"nonnumeric", "nonfraction", "fraction"}:
                    for attr, kind in (("contextRef", "context"), ("unitRef", "unit")):
                        value = node.get(attr)
                        if value:
                            references.append(
                                {
                                    "source_document_id": row["document_id"],
                                    "kind": kind,
                                    "reference_id": value,
                                }
                            )
                if local.lower() in {"nonnumeric", "nonfraction", "fraction"}:
                    value = node.get("continuedAt")
                    if value:
                        references.append(
                            {
                                "source_document_id": row["document_id"],
                                "kind": "continuation",
                                "reference_id": value,
                            }
                        )
                if local.lower() == "relationship":
                    for attr in ("fromRefs", "toRefs"):
                        for value in (node.get(attr) or "").split():
                            references.append(
                                {
                                    "source_document_id": row["document_id"],
                                    "kind": "relationship_target",
                                    "reference_id": value,
                                }
                            )
                for attr in ("tupleRef", "parentRef", "parentTupleRef", "footnoteRefs"):
                    value = node.get(attr)
                    if value:
                        for item in value.split():
                            references.append(
                                {
                                    "source_document_id": row["document_id"],
                                    "kind": "relationship_target",
                                    "reference_id": item,
                                }
                            )

    inline_ids = {row["document_id"] for row in inline_rows}
    edges: list[dict[str, str]] = []
    ambiguous: list[dict[str, str]] = []
    for reference in references:
        owners = id_owners.get(reference["reference_id"], ())
        owner_ids = {owner for owner, kind in owners if kind == reference["kind"]}
        source_id = reference["source_document_id"]
        # A local declaration wins naturally; coincidentally repeated IDs are not
        # evidence that two otherwise-independent inline reports form one set.
        if source_id in owner_ids:
            continue
        matching = sorted(owner_ids & inline_ids)
        if len(matching) == 1 and matching[0] != source_id:
            target_id = matching[0]
            edges.append(
                {
                    **reference,
                    "target_document_id": target_id,
                }
            )
        elif len(matching) > 1 and source_id not in matching:
            ambiguous.append({**reference, "owner_document_ids": ",".join(matching)})

    return inline_rows, {
        "edges": edges,
        "ambiguous": ambiguous,
        "unsupported_targets": unsupported_targets,
        "paths": paths,
        "rows": {r["document_id"]: r for r in inline_rows},
    }


def _resolve_inline_source_group(
    rows: Sequence[Mapping[str, Any]],
    entrypoints: Mapping[str, Any],
    *,
    explicit_member_ids: Sequence[str] | None = None,
    explicit: bool = False,
    allow_group_extension: bool = False,
) -> dict[str, Any] | None:
    """Plan one default-target IXDS only when typed source refs connect its members."""
    inline_rows, scan = _group_source_rows(rows, entrypoints)
    primary_rows = [row for row in inline_rows if row["role"] == "primary"]
    if len(primary_rows) != 1:
        if explicit:
            raise ProcessingError("an inline document set requires exactly one real primary source")
        return None
    primary = primary_rows[0]
    rows_by_id = scan["rows"]
    inline_ids = set(rows_by_id)
    edges = list(scan["edges"])
    ambiguous = list(scan["ambiguous"])

    adjacency: dict[str, set[str]] = {identity: set() for identity in inline_ids}
    for item in edges:
        source = item["source_document_id"]
        target = item["target_document_id"]
        adjacency[source].add(target)
        adjacency[target].add(source)

    connected = {primary["document_id"]}
    pending = [primary["document_id"]]
    while pending:
        for neighbor in adjacency[pending.pop()]:
            if neighbor not in connected:
                connected.add(neighbor)
                pending.append(neighbor)

    targeted_ids = set(scan["unsupported_targets"])
    if explicit_member_ids is not None:
        if targeted_ids.intersection(set(explicit_member_ids) | connected):
            raise ProcessingError(
                "named or multiple inline targets are unsupported; only the default target "
                "is supported"
            )
    elif targeted_ids.intersection(connected):
        if explicit:
            raise ProcessingError(
                "named or multiple inline targets are unsupported; only the default target "
                "is supported"
            )
        return None

    if explicit_member_ids is not None:
        requested = list(explicit_member_ids)
        if len(requested) < 2 or len(requested) > _MAX_INLINE_GROUP_MEMBERS:
            raise ProcessingError(
                "an explicit inline document set exceeds the bounded member count "
                f"({_MAX_INLINE_GROUP_MEMBERS})"
            )
        if len(requested) != len(set(requested)):
            raise ProcessingError("an explicit inline document set repeats a document_id")
        missing = sorted(set(requested) - inline_ids)
        if missing and not allow_group_extension:
            raise ProcessingError(
                "inline document set names an absent or non-inline filing document"
            )
        if primary["document_id"] not in requested:
            raise ProcessingError(
                "an explicit inline document set must contain the real primary document"
            )
        requested_set = set(requested)
        membership_matches = (
            len(connected) >= 2 if allow_group_extension else requested_set == connected
        )
        if not membership_matches:
            raise ProcessingError(
                "inline members must be exactly the explicit group or a valid persisted revision"
            )
        relevant_members = requested_set | connected
        if any(
            item["source_document_id"] in relevant_members
            or relevant_members.intersection(item.get("owner_document_ids", "").split(","))
            for item in ambiguous
        ):
            raise ProcessingError("inline cross-file reference ownership is ambiguous")
    elif len(connected) < 2:
        return None
    elif any(
        item["source_document_id"] in connected
        or connected.intersection(item.get("owner_document_ids", "").split(","))
        for item in ambiguous
    ):
        raise ProcessingError("inline cross-file reference ownership is ambiguous")

    if len(connected) < 2:
        if explicit:
            raise ProcessingError("independent inline documents cannot be combined into one IXDS")
        return None

    member_rows = [rows_by_id[primary["document_id"]]] + sorted(
        (rows_by_id[identity] for identity in connected if identity != primary["document_id"]),
        key=lambda row: row["source_url"],
    )
    if len(member_rows) > _MAX_INLINE_GROUP_MEMBERS:
        raise ProcessingError("cross-file inline source group exceeds the bounded member limit")
    evidence: list[dict[str, str]] = []
    member_ids = {row["document_id"] for row in member_rows}
    for item in edges:
        if (
            item["source_document_id"] not in member_ids
            or item["target_document_id"] not in member_ids
        ):
            continue
        source_row = rows_by_id[item["source_document_id"]]
        target_row = rows_by_id[item["target_document_id"]]
        evidence.append(
            {
                "source_document_id": source_row["document_id"],
                "source_url": source_row["source_url"],
                "source_sha256": source_row["raw_sha256"],
                "kind": item["kind"],
                "reference_id": item["reference_id"],
                "target_document_id": target_row["document_id"],
                "target_source_url": target_row["source_url"],
                "target_sha256": target_row["raw_sha256"],
            }
        )
    if not evidence:
        raise ProcessingError("inline group has no typed cross-file source-reference evidence")
    evidence.sort(
        key=lambda item: (
            item["source_url"],
            item["kind"],
            item["reference_id"],
            item["target_source_url"],
        )
    )
    members = [
        {
            "document_id": row["document_id"],
            "source_url": row["source_url"],
            "raw_sha256": _raw_hash(row),
            "byte_size": row["byte_size"],
            "original_filename": row["original_filename"],
        }
        for row in member_rows
    ]
    source_group = {
        "version": _INLINE_SOURCE_GROUP_VERSION,
        "anchor_document_id": primary["document_id"],
        "target": "",
        "members": members,
        "grouping_basis": {"evidence": "typed_cross_file_references_v1", "references": evidence},
    }
    return {
        "anchor": primary,
        "members": member_rows,
        "source_group": source_group,
        "fingerprint": _digest(source_group),
        "grouping_basis": source_group["grouping_basis"],
    }


def _source_group_notes(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    try:
        notes = json.loads(row["notes_json"])
    except (TypeError, ValueError, KeyError):
        return None
    if not isinstance(notes, Mapping):
        return None
    group = notes.get("source_group")
    fingerprint = notes.get("source_group_fingerprint")
    if not isinstance(group, Mapping) or not isinstance(fingerprint, str):
        return None
    return {"source_group": dict(group), "fingerprint": fingerprint, "notes": dict(notes)}


def _group_ids_from_parse_row(row: Mapping[str, Any] | None) -> tuple[str, ...] | None:
    saved = _source_group_notes(row)
    if saved is None:
        return None
    members = saved["source_group"].get("members")
    if not isinstance(members, list):
        return None
    ids = tuple(
        member.get("document_id")
        for member in members
        if isinstance(member, Mapping) and isinstance(member.get("document_id"), str)
    )
    return ids if len(ids) == len(members) else None


def _saved_group_for_filing(
    filing_id_value: str,
    active_parses: Mapping[tuple[str, str], Mapping[str, Any]],
) -> tuple[str, ...] | None:
    groups = [
        ids
        for (document_identity, parser_name), row in active_parses.items()
        if parser_name == xbrl_parser.PARSER_NAME
        and row["filing_id"] == filing_id_value
        and (ids := _group_ids_from_parse_row(row)) is not None
    ]
    if len(groups) > 1:
        raise ProcessingCorruptionError("filing has more than one active inline source group")
    return groups[0] if groups else None


def _package_rows(
    filing_id_value: str, documents: Sequence[Mapping[str, Any]]
) -> list[Mapping[str, Any]]:
    return [
        row
        for row in documents
        if row["filing_id"] == filing_id_value
        and row["selection_status"] == "required"
        and row["role"] in _PACKAGE_ROLES
    ]


def _package_ready(rows: Sequence[Mapping[str, Any]]) -> bool:
    return (
        bool(rows)
        and any(row["role"] == "primary" for row in rows)
        and all(row["fetch_status"] == "present" for row in rows)
    )


def _text_targets(rows: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return sorted(
        [
            row
            for row in rows
            if row["role"] in _TEXT_ROLES
            and row["selection_status"] == "required"
            and row["fetch_status"] == "present"
        ],
        key=lambda row: (row["original_filename"], row["document_id"]),
    )


def _xbrl_targets(
    rows: Sequence[Mapping[str, Any]],
    entrypoints: Mapping[str, Any],
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]], str]:
    text_docs = _text_targets(rows)
    inline_docs = [
        row
        for row in text_docs
        if row["source_url"] in entrypoints
        and _inline_xbrl(_entrypoint_path(entrypoints[row["source_url"]]))
    ]
    if inline_docs:
        return inline_docs, inline_docs, "inline_preferred"
    classic = sorted(
        [
            row
            for row in rows
            if row["role"] == "xbrl_instance"
            and row["selection_status"] == "required"
            and row["fetch_status"] == "present"
            and row["source_url"] in entrypoints
        ],
        key=lambda row: (row["original_filename"], row["document_id"]),
    )
    if not classic:
        classic = [
            row
            for row in text_docs
            if row["role"] == "primary"
            and row["original_filename"].lower().endswith((".xml", ".xhtml"))
        ]
    if classic:
        return classic, classic, "classic_instance_fallback"
    primary = [row for row in text_docs if row["role"] == "primary"]
    return primary, [], "unsupported_or_legacy_primary"


def _plan_xbrl_work_units(
    rows: Sequence[Mapping[str, Any]],
    entrypoints: Mapping[str, Any],
    *,
    explicit_member_ids: Sequence[str] | None = None,
    explicit: bool = False,
    allow_group_extension: bool = False,
) -> tuple[list[dict[str, Any]], list[Mapping[str, Any]], str]:
    """Share the standalone/group work-unit plan between selection and execution."""
    xbrl_docs, dependency_entrypoints, strategy = _xbrl_targets(rows, entrypoints)
    group: dict[str, Any] | None = None
    if strategy == "inline_preferred":
        group = _resolve_inline_source_group(
            rows,
            entrypoints,
            explicit_member_ids=explicit_member_ids,
            explicit=explicit,
            allow_group_extension=allow_group_extension,
        )
    elif explicit_member_ids is not None:
        raise ProcessingError(
            "explicit inline document set has no supported default-target inline source"
        )

    if group is None:
        if explicit:
            raise ProcessingError(
                "explicit inline document set is not a supported cross-file source group"
            )
        return (
            [{"anchor": row, "members": [row], "group": None} for row in xbrl_docs],
            dependency_entrypoints,
            strategy,
        )

    member_ids = {row["document_id"] for row in group["members"]}
    work_units: list[dict[str, Any]] = []
    group_added = False
    for row in xbrl_docs:
        if row["document_id"] in member_ids:
            if not group_added:
                work_units.append(
                    {"anchor": group["anchor"], "members": group["members"], "group": group}
                )
                group_added = True
            continue
        work_units.append({"anchor": row, "members": [row], "group": None})
    if not group_added:
        raise ProcessingError("planned inline source group is not a selected XBRL work unit")
    return work_units, dependency_entrypoints, strategy


def _offline_dependency_fingerprint(rows: Sequence[Mapping[str, Any]]) -> str:
    resources = sorted(
        (row["source_url"], row["raw_sha256"], row["byte_size"], row["role"])
        for row in rows
        if row["fetch_status"] == "present" and row["role"] in _PACKAGE_ROLES
    )
    return _digest({"policy": "offline-local-package-v1", "resources": resources})


def _text_dependency_fingerprint() -> str:
    return _digest({"policy": "text-extraction-no-dependencies-v1"})


def _parse_id(
    filing_id_value: str,
    document_id_value: str,
    source_hash: str,
    parser_name: str,
    parser_version: str,
    dependencies_fingerprint: str,
    source_group_fingerprint: str | None = None,
) -> str:
    return _contract_parse_id(
        filing_id_value,
        document_id_value,
        source_hash,
        parser_name,
        parser_version,
        dependencies_fingerprint,
        source_group_fingerprint,
        contract=_PROCESSING_CONTRACT,
    )


def _terminal_match(
    row: Mapping[str, Any] | None,
    *,
    parser_version: str,
    source_hash: str,
    dependencies_fingerprint: str | None = None,
) -> bool:
    if row is None or row["status"] not in _TERMINAL_PARSE_STATES:
        return False
    if row["parser_version"] != parser_version or row["source_sha256"] != source_hash:
        return False
    return (
        dependencies_fingerprint is None
        or row["dependencies_fingerprint"] == dependencies_fingerprint
    )


def _terminal_group_match(
    row: Mapping[str, Any] | None,
    *,
    group: Mapping[str, Any],
    filing_id_value: str,
    parser_version: str,
    package_rows: Sequence[Mapping[str, Any]] | None = None,
    dependency_rows: Sequence[Mapping[str, Any]] = (),
) -> bool:
    if row is None or row["status"] not in _TERMINAL_PARSE_STATES:
        return False
    if row["parser_version"] != parser_version:
        return False
    anchor = group["anchor"]
    source_hash = _raw_hash(anchor)
    saved = _source_group_notes(row)
    if (
        saved is None
        or row["source_sha256"] != source_hash
        or saved["source_group"] != group["source_group"]
        or saved["fingerprint"] != group["fingerprint"]
        or saved["fingerprint"] != _digest(saved["source_group"])
        or row["document_id"] != anchor["document_id"]
        or row["filing_id"] != filing_id_value
        or not isinstance(row["dependencies_fingerprint"], str)
        or _HEX_256.fullmatch(row["dependencies_fingerprint"]) is None
    ):
        return False
    expected_parse_id = _parse_id(
        filing_id_value,
        anchor["document_id"],
        source_hash,
        xbrl_parser.PARSER_NAME,
        parser_version,
        row["dependencies_fingerprint"],
        source_group_fingerprint=group["fingerprint"],
    )
    return (
        row["parse_id"] == expected_parse_id
        and package_rows is not None
        and _current_group_dependency_graph_matches(row, package_rows, dependency_rows)
    )


def _current_group_dependency_graph_matches(
    parse: Mapping[str, Any],
    package_rows: Sequence[Mapping[str, Any]],
    dependency_rows: Sequence[Mapping[str, Any]],
) -> bool:
    try:
        notes = json.loads(parse["notes_json"])
    except (TypeError, ValueError, KeyError):
        return False
    if not isinstance(notes, Mapping):
        return False
    mode = notes.get("dependency_mode")
    if mode == "offline_local_only":
        return parse["dependencies_fingerprint"] == _offline_dependency_fingerprint(package_rows)
    if mode != "prepared":
        return False
    allowed_hosts = notes.get("allowed_taxonomy_hosts")
    if not isinstance(allowed_hosts, list) or any(
        not isinstance(host, str) for host in allowed_hosts
    ):
        return False
    graph_rows = [
        row
        for row in dependency_rows
        if row["filing_id"] == parse["filing_id"]
        and row["dependency_fingerprint"] == parse["dependencies_fingerprint"]
    ]
    resources: list[dict[str, Any]] = []
    for row in graph_rows:
        if row["status"] != "present":
            return False
        try:
            provenance = json.loads(row["provenance_json"])
        except (TypeError, ValueError):
            return False
        redirect_chain = (
            provenance.get("redirect_chain") if isinstance(provenance, Mapping) else None
        )
        if not isinstance(redirect_chain, list) or any(
            not isinstance(url, str) for url in redirect_chain
        ):
            return False
        resources.append(
            {
                "requested_url": row["requested_url"],
                "final_url": row["final_url"],
                "transport_url": row["transport_url"],
                "redirect_chain": redirect_chain,
                "sha256": row["sha256"],
                "byte_size": row["byte_size"],
            }
        )
    resources.sort(key=lambda item: (item["requested_url"], item["final_url"], item["sha256"]))
    return (
        bool(resources)
        and _digest(
            {
                "policy": "prepared-xbrl-dependency-graph-v2",
                "allowed_hosts": allowed_hosts,
                "resources": resources,
            }
        )
        == parse["dependencies_fingerprint"]
    )


def _group_preparation_failure_matches(
    parse: Mapping[str, Any],
    source_group: Mapping[str, Any],
    package_rows: Sequence[Mapping[str, Any]],
    dependency_rows: Sequence[Mapping[str, Any]],
    *,
    documents_by_id: Mapping[str, Mapping[str, Any]] | None = None,
    raw_refs: Mapping[str, RawObjectRef] | None = None,
    require_current_sources: bool,
) -> bool:
    if parse["status"] not in {"failed", "partial"}:
        return False
    try:
        notes = json.loads(parse["notes_json"])
    except (TypeError, ValueError, KeyError):
        return False
    if not isinstance(notes, Mapping) or notes.get("dependency_mode") != "preparation_failed":
        return False
    saved = _source_group_notes(parse)
    attempt = notes.get("dependency_preparation")
    if saved is None or not isinstance(attempt, Mapping):
        return False
    group_fingerprint = saved["fingerprint"]
    code = attempt.get("diagnostic_code")
    allowed_hosts = attempt.get("allowed_taxonomy_hosts")
    entrypoints = attempt.get("entrypoints")
    dependency_fingerprint = attempt.get("dependency_fingerprint")
    if (
        not isinstance(code, str)
        or _SAFE_CODE.fullmatch(code) is None
        or not isinstance(allowed_hosts, list)
        or any(not isinstance(host, str) for host in allowed_hosts)
        or not isinstance(entrypoints, list)
        or not isinstance(dependency_fingerprint, str)
        or _HEX_256.fullmatch(dependency_fingerprint) is None
        or dependency_fingerprint != parse["dependencies_fingerprint"]
        or attempt.get("source_group_fingerprint") != group_fingerprint
        or notes.get("source_group_fingerprint") != group_fingerprint
        or notes.get("allowed_taxonomy_hosts") != allowed_hosts
    ):
        return False
    try:
        _requested, normalized_hosts = _normalize_request(None, 1, None, allowed_hosts)
    except ProcessingError:
        return False
    if list(normalized_hosts) != allowed_hosts:
        return False

    row_by_id = {row["document_id"]: row for row in package_rows}
    seen_ids: set[str] = set()
    seen_urls: set[str] = set()
    entry_pairs: list[tuple[str, str]] = []
    entry_bindings: set[tuple[str, str, str, int]] = set()
    for entry in entrypoints:
        if not isinstance(entry, Mapping) or set(entry) != {
            "document_id",
            "source_url",
            "raw_sha256",
            "byte_size",
        }:
            return False
        document_identity = entry.get("document_id")
        source_url = entry.get("source_url")
        digest = entry.get("raw_sha256")
        byte_size = entry.get("byte_size")
        if (
            not isinstance(document_identity, str)
            or document_identity in seen_ids
            or not isinstance(source_url, str)
            or source_url in seen_urls
            or not isinstance(digest, str)
            or _HEX_256.fullmatch(digest) is None
            or isinstance(byte_size, bool)
            or not isinstance(byte_size, int)
            or byte_size < 0
        ):
            return False
        document = (documents_by_id or {}).get(document_identity) or row_by_id.get(
            document_identity
        )
        if (
            document is None
            or document["filing_id"] != parse["filing_id"]
            or document["source_url"] != source_url
            or document["fetch_status"] != "present"
            or document["selection_status"] != "required"
            or document["role"] not in _TEXT_ROLES
        ):
            return False
        binding_is_current = document["raw_sha256"] == digest and document["byte_size"] == byte_size
        if require_current_sources and not binding_is_current:
            return False
        if not binding_is_current and not any(
            ref.sha256 == digest and ref.byte_size == byte_size for ref in (raw_refs or {}).values()
        ):
            return False
        try:
            host = urlsplit(source_url).hostname
        except ValueError:
            return False
        if host is None or host.lower() not in normalized_hosts:
            return False
        seen_ids.add(document_identity)
        seen_urls.add(source_url)
        entry_pairs.append((source_url, digest))
        entry_bindings.add((document_identity, source_url, digest, byte_size))
    if not entrypoints or entrypoints != sorted(
        entrypoints, key=lambda item: (item["source_url"], item["document_id"])
    ):
        return False
    group_members = source_group.get("members")
    if not isinstance(group_members, list) or any(
        (
            member.get("document_id"),
            member.get("source_url"),
            member.get("raw_sha256"),
            member.get("byte_size"),
        )
        not in entry_bindings
        for member in group_members
        if isinstance(member, Mapping)
    ):
        return False
    if len(group_members) != sum(isinstance(member, Mapping) for member in group_members):
        return False
    if (
        _dependency_preparation_fingerprint(
            [
                {"source_url": source_url, "raw_sha256": digest}
                for source_url, digest in entry_pairs
            ],
            normalized_hosts,
            code,
        )
        != dependency_fingerprint
    ):
        return False

    expected_provenance = {
        "bytes_persisted": False,
        "kind": "dependency_preparation_failed",
        "filing_id": parse["filing_id"],
        "dependency_fingerprint": dependency_fingerprint,
        "source_group_fingerprint": group_fingerprint,
        "diagnostic_code": code,
        "allowed_taxonomy_hosts": allowed_hosts,
        "entrypoints": entrypoints,
    }
    matching_errors: list[Mapping[str, Any]] = []
    for dependency in dependency_rows:
        if (
            dependency["filing_id"] != parse["filing_id"]
            or dependency["dependency_fingerprint"] != dependency_fingerprint
            or dependency["status"] != "error"
        ):
            continue
        try:
            provenance = json.loads(dependency["provenance_json"])
        except (TypeError, ValueError):
            continue
        if isinstance(provenance, Mapping) and provenance.get("kind") == (
            "dependency_preparation_failed"
        ):
            matching_errors.append(dependency)
            if provenance != expected_provenance:
                return False
    if len(matching_errors) != 1:
        return False
    error_row = matching_errors[0]
    if error_row["diagnostic_code"] != code or any(
        error_row[field] is not None for field in ("sha256", "byte_size", "raw_path")
    ):
        return False
    requested_url = error_row["requested_url"]
    if requested_url is not None:
        try:
            parsed_url = urlsplit(requested_url)
        except ValueError:
            return False
        if (
            parsed_url.scheme not in {"http", "https"}
            or parsed_url.hostname is None
            or parsed_url.hostname.lower() not in normalized_hosts
            or parsed_url.username is not None
            or parsed_url.password is not None
            or parsed_url.query
        ):
            return False
    return True


def _allowlist_for_sources(
    entrypoints: Sequence[Mapping[str, Any]], taxonomy_hosts: Sequence[str]
) -> tuple[str, ...]:
    hosts = set(taxonomy_hosts)
    for document in entrypoints:
        try:
            source_url = validate_sec_url(document["source_url"])
        except (TypeError, ValueError) as exc:
            raise ProcessingError("XBRL source URL is not an approved SEC URL") from exc
        hostname = urlsplit(source_url).hostname
        if hostname:
            hosts.add(hostname.lower())
    return tuple(sorted(hosts))


def _response_provenance(response: Any) -> dict[str, Any]:
    return {
        "request_url": getattr(response, "request_url", None),
        "final_url": getattr(response, "final_url", None),
        "transport_url": getattr(response, "transport_url", None),
        "redirect_chain": list(getattr(response, "redirect_chain", ()) or ()),
    }


def _dependency_preparation_fingerprint(
    entrypoints: Sequence[Mapping[str, Any]],
    allowed_hosts: Sequence[str],
    diagnostic_code: str,
) -> str:
    return _digest(
        {
            "policy": "dependency-preparation-failed-v2",
            "entrypoints": sorted((item["source_url"], item["raw_sha256"]) for item in entrypoints),
            "allowed_hosts": list(allowed_hosts),
            "diagnostic_code": _safe_code(diagnostic_code, "dependency_failed"),
        }
    )


def _dependency_preparation_bindings(
    entrypoints: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    return [
        {
            "document_id": row["document_id"],
            "source_url": row["source_url"],
            "raw_sha256": row["raw_sha256"],
            "byte_size": row["byte_size"],
        }
        for row in sorted(
            entrypoints,
            key=lambda item: (item["source_url"], item["document_id"]),
        )
    ]


def _dependency_error_row(
    filing_id_value: str,
    dependency_fingerprint: str,
    code: str,
    requested_url: str | None = None,
    provenance: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    safe_url = None
    if isinstance(requested_url, str):
        try:
            parsed = urlsplit(requested_url)
            if parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.query:
                safe_url = requested_url
        except ValueError:
            pass
    return {
        "filing_id": filing_id_value,
        "dependency_fingerprint": dependency_fingerprint,
        "requested_url": safe_url,
        "final_url": None,
        "transport_url": None,
        "sha256": None,
        "byte_size": None,
        "raw_path": None,
        "status": "error",
        "diagnostic_code": _safe_code(code, "dependency_failed"),
        "provenance_json": _canonical_json(
            dict(provenance) if provenance is not None else {"bytes_persisted": False}
        ).decode("utf-8"),
    }


def _dependency_record_rows(
    filing_id_value: str,
    dependency_fingerprint: str,
    prepared: PreparedDependencies,
    response_details: Mapping[str, Mapping[str, Any]],
    entrypoint_urls: set[str],
    writer: ArchiveWriter,
    raw_refs: dict[str, RawObjectRef],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for record in prepared.records:
        key = (record.requested_url, record.final_url, record.sha256)
        if key in seen:
            continue
        seen.add(key)
        ref = writer.put_raw_file(record.local_path, expected_sha256=record.sha256)
        raw_refs[ref.path] = ref
        transport_url = response_details.get(record.requested_url, {}).get("transport_url")
        if not isinstance(transport_url, str):
            transport_url = None
        rows.append(
            {
                "filing_id": filing_id_value,
                "dependency_fingerprint": dependency_fingerprint,
                "requested_url": record.requested_url,
                "final_url": record.final_url,
                "transport_url": transport_url,
                "sha256": ref.sha256,
                "byte_size": ref.byte_size,
                "raw_path": ref.path,
                "status": "present",
                "diagnostic_code": None,
                "provenance_json": _canonical_json(
                    {
                        "kind": "entrypoint"
                        if record.requested_url in entrypoint_urls
                        else "dependency",
                        "redirect_chain": list(record.redirect_chain),
                        "transport_url": transport_url,
                    }
                ).decode("utf-8"),
            }
        )
    return rows


def _prepared_fingerprint(
    prepared: PreparedDependencies,
    response_details: Mapping[str, Mapping[str, Any]],
    allowed_hosts: Sequence[str],
) -> str:
    resources = [
        {
            "requested_url": item.requested_url,
            "final_url": item.final_url,
            "transport_url": response_details.get(item.requested_url, {}).get("transport_url"),
            "redirect_chain": list(item.redirect_chain),
            "sha256": item.sha256,
            "byte_size": item.size,
        }
        for item in prepared.records
    ]
    resources.sort(key=lambda row: (row["requested_url"], row["final_url"], row["sha256"]))
    return _digest(
        {
            "policy": "prepared-xbrl-dependency-graph-v2",
            "allowed_hosts": list(allowed_hosts),
            "resources": resources,
        }
    )


def _safe_code(value: Any, fallback: str = "parser_error") -> str:
    """Retain parser diagnostic codes while allowing only short public tokens."""
    return value if isinstance(value, str) and _SAFE_CODE.fullmatch(value) else fallback


def _ledger_code(value: Any) -> str:
    """Reduce public parser codes to ArchiveWriter's sanitized lowercase code set."""
    token = re.sub(r"[^a-z0-9_]", "_", _safe_code(value).lower())
    token = token[:64].strip("_")
    if not token or not token[0].isalpha():
        return "parser_error"
    return token


def _sanitize_errors(result: ParseResult, private_paths: Sequence[Path]) -> list[dict[str, Any]]:
    cleaned: list[dict[str, Any]] = []
    for item in result.errors:
        if not isinstance(item, Mapping):
            cleaned.append(
                {"code": "parser_error", "message": str(item)[:4096], "severity": "error"}
            )
            continue
        message = str(item.get("message", ""))
        for path in private_paths:
            message = message.replace(str(path), "<private-workspace>")
        severity = item.get("severity")
        cleaned.append(
            {
                "code": _safe_code(item.get("code")),
                "message": message[:4096],
                "severity": severity if severity in {"error", "warning", "info"} else "error",
            }
        )
    return cleaned


def _safe_result(
    result: ParseResult,
    *,
    filing_id_value: str,
    document: Mapping[str, Any],
    parse_id_value: str,
    parser_name: str,
    parser_version: str,
    source_hash: str,
) -> ParseResult:
    valid = (
        result.filing_id == filing_id_value
        and result.document_id == document["document_id"]
        and result.parse_id == parse_id_value
        and result.parser_name == parser_name
        and result.parser_version == parser_version
        and result.status in _PARSE_STATES
        and result.source_hash == source_hash
    )
    if not valid or (
        result.status in {"failed", "unsupported"} and (result.facts or result.sections)
    ):
        return ParseResult(
            filing_id=filing_id_value,
            document_id=document["document_id"],
            parse_id=parse_id_value,
            parser_name=parser_name,
            parser_version=parser_version,
            status="failed",
            source_hash=source_hash,
            validation_scope="not_performed",
            errors=[
                {
                    "code": "parser_result_mismatch",
                    "message": (
                        "Parser identity/status/source hash did not match the verified document."
                    ),
                    "severity": "error",
                }
            ],
        )
    integrity_codes = {
        error.get("code")
        for error in result.errors
        if isinstance(error, Mapping) and error.get("code") in _SOURCE_INTEGRITY_ERRORS
    }
    for row in result.facts:
        try:
            row_codes = json.loads(row.get("error_codes_json") or "[]")
        except (TypeError, ValueError):
            row_codes = []
        if isinstance(row_codes, list):
            integrity_codes.update(code for code in row_codes if code in _SOURCE_INTEGRITY_ERRORS)
    if integrity_codes:
        preserved_errors = list(result.errors)
        present_codes = {item.get("code") for item in preserved_errors if isinstance(item, Mapping)}
        preserved_errors.extend(
            {
                "code": code,
                "message": (
                    "Parser source-integrity validation failed; no occurrences were accepted."
                ),
                "severity": "error",
            }
            for code in sorted(integrity_codes - present_codes)
        )
        return ParseResult(
            filing_id=filing_id_value,
            document_id=document["document_id"],
            parse_id=parse_id_value,
            parser_name=parser_name,
            parser_version=parser_version,
            status="failed",
            source_hash=source_hash,
            validation_scope="not_performed",
            facts=[],
            sections=[],
            errors=preserved_errors,
        )
    return result


def _failed_result(
    filing_id_value: str,
    document_id_value: str,
    parse_id_value: str,
    parser_name: str,
    parser_version: str,
    source_hash: str,
    code: str,
    message: str,
) -> ParseResult:
    return ParseResult(
        filing_id=filing_id_value,
        document_id=document_id_value,
        parse_id=parse_id_value,
        parser_name=parser_name,
        parser_version=parser_version,
        status="failed",
        source_hash=source_hash,
        validation_scope="not_performed",
        errors=[{"code": _safe_code(code), "message": message, "severity": "error"}],
    )


def _unsupported_xbrl_result(
    filing_id_value: str,
    document_id_value: str,
    parse_id_value: str,
    source_hash: str,
    message: str,
) -> ParseResult:
    return ParseResult(
        filing_id=filing_id_value,
        document_id=document_id_value,
        parse_id=parse_id_value,
        parser_name=xbrl_parser.PARSER_NAME,
        parser_version=xbrl_parser.PARSER_VERSION,
        status="unsupported",
        source_hash=source_hash,
        validation_scope="not_performed",
        errors=[{"code": "unsupported_xbrl_format", "message": message, "severity": "warning"}],
    )


def _partial_after_dependency_error(result: ParseResult, code: str) -> ParseResult:
    if result.status != "full":
        return result
    error = {
        "code": _safe_code(code, "dependency_failed"),
        "message": "XBRL dependency preparation did not complete; parser result is partial.",
        "severity": "error",
    }
    return ParseResult(
        filing_id=result.filing_id,
        document_id=result.document_id,
        parse_id=result.parse_id,
        parser_name=result.parser_name,
        parser_version=result.parser_version,
        status="partial",
        source_hash=result.source_hash,
        validation_scope=result.validation_scope,
        facts=[dict(row, status="partial") for row in result.facts],
        sections=[dict(row, status="partial") for row in result.sections],
        errors=[*result.errors, error],
    )


def _failed_untrusted_source(result: ParseResult, message: str) -> ParseResult:
    return ParseResult(
        filing_id=result.filing_id,
        document_id=result.document_id,
        parse_id=result.parse_id,
        parser_name=result.parser_name,
        parser_version=result.parser_version,
        status="failed",
        source_hash=result.source_hash,
        validation_scope="not_performed",
        facts=[],
        sections=[],
        errors=[
            *result.errors,
            {
                "code": "unverified_fact_source",
                "message": message,
                "severity": "error",
            },
        ],
    )


def _fact_sources_verified(
    result: ParseResult,
    package: PreparedPackage,
    prepared: PreparedDependencies | None,
    source_group: Mapping[str, Any] | None = None,
) -> bool:
    return _validate_fact_sources(result, package, prepared, source_group)


def _annotate_group_fact_owners(
    result: ParseResult,
    source_group: Mapping[str, Any],
) -> bool:
    return _validate_group_fact_owners(
        result,
        source_group,
        canonical_json=_canonical_json,
    )


def _call_text(
    package: PreparedPackage,
    document: Mapping[str, Any],
    parse_id_value: str,
    source_hash: str,
) -> ParseResult:
    entry = package.entrypoints.get(document["source_url"])
    if entry is None:
        raise ProcessingCorruptionError("prepared package is missing a required text entrypoint")
    result = text_parser.parse_text(
        entry.local_path,
        filing_id=package.filing_id,
        document_id=document["document_id"],
        parse_id=parse_id_value,
        allowed_root=package.workspace_root,
        expected_hash=source_hash,
    )
    safe_result = _safe_result(
        result,
        filing_id_value=package.filing_id,
        document=document,
        parse_id_value=parse_id_value,
        parser_name=text_parser.PARSER_NAME,
        parser_version=text_parser.PARSER_VERSION,
        source_hash=source_hash,
    )
    expected_relpath = (
        entry.local_path.resolve(strict=True)
        .relative_to(package.workspace_root.resolve(strict=True))
        .as_posix()
    )
    if any(
        section.get("document_hash") != source_hash
        or section.get("source_relpath") != expected_relpath
        for section in safe_result.sections
    ):
        return _failed_untrusted_source(
            safe_result,
            "Text section source hash/path did not match the verified package document.",
        )
    return safe_result


def _call_xbrl(
    package: PreparedPackage,
    document: Mapping[str, Any],
    parse_id_value: str,
    source_hash: str,
    prepared: PreparedDependencies | None,
    source_group: Mapping[str, Any] | None = None,
) -> ParseResult:
    if document["original_filename"].lower().endswith(".pdf"):
        return _unsupported_xbrl_result(
            package.filing_id,
            document["document_id"],
            parse_id_value,
            source_hash,
            "PDF fact extraction is unsupported; original source bytes remain archived.",
        )
    entry = package.entrypoints.get(document["source_url"])
    if entry is None:
        raise ProcessingCorruptionError("prepared package is missing a required XBRL entrypoint")
    if prepared is None:
        parse_kwargs: dict[str, Any] = {
            "filing_id": package.filing_id,
            "document_id": document["document_id"],
            "parse_id": parse_id_value,
            "allowed_root": package.workspace_root,
        }
        parse_entrypoint = entry.local_path
        if source_group is not None:
            group_members = source_group["members"]
            paths: list[Path] = []
            uri_map: dict[str, Path] = {}
            origins: dict[Path, str] = {}
            expected: dict[Path, str] = {}
            for source_url, source_entry in package.entrypoints.items():
                uri_map[source_url] = source_entry.local_path
                origins[source_entry.local_path] = source_url
                expected[source_entry.local_path] = source_entry.raw_sha256
            for member in group_members:
                source_entry = package.entrypoints.get(member["source_url"])
                if source_entry is None or source_entry.raw_sha256 != member["raw_sha256"]:
                    raise ProcessingCorruptionError(
                        "prepared package does not match the declared inline source group"
                    )
                paths.append(source_entry.local_path)
            parse_kwargs.update(
                {
                    "uri_map": uri_map,
                    "source_origins": origins,
                    "expected_hashes": expected,
                    "inline_document_set": paths,
                }
            )
        result = xbrl_parser.parse_xbrl(parse_entrypoint, **parse_kwargs)
    else:
        try:
            cached_entrypoint = prepared.entrypoint_path(document["source_url"])
            parse_kwargs = {
                "filing_id": package.filing_id,
                "document_id": document["document_id"],
                "parse_id": parse_id_value,
                "allowed_root": prepared.cache_dir,
                "cache_dir": prepared.cache_dir,
                "uri_map": prepared.url_map,
                "source_origins": prepared.origin_map,
                "expected_hashes": prepared.expected_hashes,
            }
            if source_group is not None:
                cached_members = [
                    prepared.entrypoint_path(member["source_url"])
                    for member in source_group["members"]
                ]
                parse_kwargs["inline_document_set"] = cached_members
        except KeyError as exc:
            raise ProcessingCorruptionError(
                "offline taxonomy cache omitted an XBRL group entrypoint"
            ) from exc
        result = xbrl_parser.parse_xbrl(cached_entrypoint, **parse_kwargs)
    safe_result = _safe_result(
        result,
        filing_id_value=package.filing_id,
        document=document,
        parse_id_value=parse_id_value,
        parser_name=xbrl_parser.PARSER_NAME,
        parser_version=xbrl_parser.PARSER_VERSION,
        source_hash=source_hash,
    )
    if not _fact_sources_verified(safe_result, package, prepared, source_group):
        return _failed_untrusted_source(
            safe_result,
            "Arelle fact source URI/path/hash did not match this filing's prepared graph.",
        )
    if source_group is not None and not _annotate_group_fact_owners(safe_result, source_group):
        return _failed_untrusted_source(
            safe_result,
            "Arelle grouped fact provenance did not resolve to one declared source document.",
        )
    return safe_result


def _parse_record(
    result: ParseResult,
    source_hash: str,
    dependency_fingerprint: str,
    errors: Sequence[Mapping[str, Any]],
    notes: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "parse_id": result.parse_id,
        "filing_id": result.filing_id,
        "document_id": result.document_id,
        "parser_name": result.parser_name,
        "parser_version": result.parser_version,
        "status": result.status,
        "validation_scope": result.validation_scope or "not_performed",
        "source_sha256": source_hash,
        "dependencies_fingerprint": dependency_fingerprint,
        "fact_count": len(result.facts),
        "section_count": len(result.sections),
        "errors_json": _canonical_json(list(errors)).decode("utf-8"),
        "notes_json": _canonical_json(dict(notes)).decode("utf-8"),
    }


def _record_parser_results(
    filing_id_value: str,
    results: Sequence[tuple[Mapping[str, Any], ParseResult, str, str, str, Mapping[str, Any]]],
    *,
    workspace: Path,
    cache_dir: Path | None,
    documents_by_id: Mapping[str, dict[str, Any]],
    new_parse_rows: list[dict[str, Any]],
    new_fact_rows: list[dict[str, Any]],
    new_section_rows: list[dict[str, Any]],
    replacement_parse_keys: set[tuple[str, str]],
    parse_statuses: dict[str, str],
    attempt_statuses: list[str],
    writer: ArchiveWriter,
) -> list[str]:
    statuses: list[str] = []
    for document, result, parser_name, parser_version, dependency_fingerprint, notes in results:
        source_hash = _raw_hash(document)
        if result.source_hash != source_hash:
            result = _failed_result(
                filing_id_value,
                document["document_id"],
                result.parse_id,
                parser_name,
                parser_version,
                source_hash,
                "source_integrity_mismatch",
                "Parser source hash differs from the active verified document.",
            )
        if parser_name not in {xbrl_parser.PARSER_NAME, text_parser.PARSER_NAME}:
            raise ProcessingError("processing attempted to publish an unknown parser name")
        if result.status not in _PARSE_STATES:
            result = _failed_result(
                filing_id_value,
                document["document_id"],
                result.parse_id,
                parser_name,
                parser_version,
                source_hash,
                "invalid_parser_status",
                "Parser returned an unsupported processing status.",
            )
        if result.status in {"failed", "unsupported"} and (result.facts or result.sections):
            result = _failed_result(
                filing_id_value,
                document["document_id"],
                result.parse_id,
                parser_name,
                parser_version,
                source_hash,
                "invalid_parser_rows",
                "Failed or unsupported parser result returned rows; rows were cleared.",
            )

        for row in (*result.facts, *result.sections):
            if (
                row.get("filing_id") != filing_id_value
                or row.get("document_id") != document["document_id"]
                or row.get("parse_id") != result.parse_id
                or row.get("parser_name") != parser_name
                or row.get("parser_version") != parser_version
                or row.get("status") != result.status
                or row.get("source_hash") != source_hash
            ):
                result = _failed_result(
                    filing_id_value,
                    document["document_id"],
                    result.parse_id,
                    parser_name,
                    parser_version,
                    source_hash,
                    "parser_row_provenance_mismatch",
                    "Parser row provenance did not match its verified source and attempt.",
                )
                break
        if result.status == "failed" and (result.facts or result.sections):
            result = _failed_result(
                filing_id_value,
                document["document_id"],
                result.parse_id,
                parser_name,
                parser_version,
                source_hash,
                "parser_row_provenance_mismatch",
                "Parser rows were cleared after provenance validation failed.",
            )

        cleaned_errors = _sanitize_errors(
            result, (workspace, cache_dir) if cache_dir else (workspace,)
        )
        new_parse_rows.append(
            _parse_record(result, source_hash, dependency_fingerprint, cleaned_errors, notes)
        )
        key = (document["document_id"], parser_name)
        replacement_parse_keys.add(key)
        if parser_name == xbrl_parser.PARSER_NAME:
            new_fact_rows.extend(dict(row) for row in result.facts)
            state_field = "fact_extraction_status"
        else:
            new_section_rows.extend(dict(row) for row in result.sections)
            state_field = "text_extraction_status"
        document_row = documents_by_id.get(document["document_id"])
        if document_row is None or document_row["filing_id"] != filing_id_value:
            raise ProcessingCorruptionError(
                "attempted parse document disappeared from active core rows"
            )
        document_row[state_field] = result.status
        if parser_name == xbrl_parser.PARSER_NAME and isinstance(
            notes.get("source_group"), Mapping
        ):
            ledger_state = {
                "full": ("complete", None),
                "partial": ("partial", "parse_partial"),
                "unsupported": ("skipped", "unsupported_format"),
                "failed": (
                    "error",
                    _ledger_code(cleaned_errors[0]["code"] if cleaned_errors else "parser_error"),
                ),
            }[result.status]
            for member in notes["source_group"].get("members", []):
                member_id = member.get("document_id") if isinstance(member, Mapping) else None
                if not isinstance(member_id, str) or member_id == document["document_id"]:
                    continue
                member_row = documents_by_id.get(member_id)
                if member_row is None or member_row["filing_id"] != filing_id_value:
                    raise ProcessingCorruptionError(
                        "source-group member disappeared from active core rows"
                    )
                member_row["fact_extraction_status"] = result.status
                writer.record_attempt(member_id, *ledger_state)
        status_key = f"{document['document_id']}:{parser_name}"
        parse_statuses[status_key] = result.status
        statuses.append(result.status)
        attempt_statuses.append(result.status)
        if result.status == "full":
            writer.record_attempt(document["document_id"], "complete", None)
        elif result.status == "partial":
            writer.record_attempt(document["document_id"], "partial", "parse_partial")
        elif result.status == "unsupported":
            writer.record_attempt(document["document_id"], "skipped", "unsupported_format")
        else:
            code = cleaned_errors[0]["code"] if cleaned_errors else "parser_error"
            writer.record_attempt(document["document_id"], "error", _ledger_code(code))
    return statuses


def _filing_status(statuses: Sequence[str]) -> str:
    if "failed" in statuses:
        return "failed"
    if "partial" in statuses:
        return "partial"
    if statuses and all(status == "unsupported" for status in statuses):
        return "unsupported"
    return "processed"


def _table(rows: Sequence[Mapping[str, Any]], schema: pa.Schema, label: str) -> pa.Table:
    try:
        result = pa.Table.from_pylist(list(rows), schema=schema)
    except (pa.ArrowException, TypeError, ValueError) as exc:
        raise ProcessingError(f"{label} rows do not match their typed schema") from exc
    if not result.schema.equals(schema, check_metadata=True):
        raise ProcessingError(f"{label} table schema differs from its owning module schema")
    return result


def _counts(statuses: Sequence[str]) -> tuple[int, int, int, int]:
    return tuple(statuses.count(item) for item in ("full", "partial", "unsupported", "failed"))  # type: ignore[return-value]


def _same_table(snapshot: Snapshot, name: str, replacement: pa.Table) -> bool:
    active = snapshot.tables.get(name)
    return (
        isinstance(active, pa.Table)
        and active.schema.equals(replacement.schema, check_metadata=True)
        and active.equals(replacement, check_metadata=True)
    )


def _commit_or_reconcile(
    writer: ArchiveWriter,
    previous: Snapshot,
    replacements: Mapping[str, pa.Table],
    raw_refs: Sequence[RawObjectRef],
) -> Snapshot:
    try:
        return writer.commit(
            tables=replacements,
            raw_objects=raw_refs,
            expected_manifest_version=previous.manifest_version,
        )
    except Exception:
        # Publication truth is the head manifest. A fault after atomic replacement
        # must reconcile to that exact next snapshot rather than retrying version 0
        # or reporting an ambiguous failure. Pre-publication faults leave the old
        # version intact and the original exception is re-raised below.
        published = read_snapshot(previous.root)
        if (
            published.manifest_version == previous.manifest_version + 1
            and published.manifest["parent_snapshot_id"] == previous.snapshot_id
            and published.manifest["run_spec_sha256"] == previous.manifest["run_spec_sha256"]
            and all(_same_table(published, name, table) for name, table in replacements.items())
        ):
            active = {ref.path: ref for ref in published.raw_objects}
            if all(active.get(ref.path) == ref for ref in raw_refs):
                return published
        raise


def _normalize_inline_document_sets(
    values: Mapping[str, Sequence[str]] | None,
) -> dict[str, tuple[str, ...]] | None:
    if values is None:
        return None
    if not isinstance(values, Mapping):
        raise ProcessingError(
            "inline_document_sets must map exact filing IDs to member document IDs"
        )
    if len(values) > _MAX_FILINGS:
        raise ProcessingError("inline_document_sets exceeds the bounded filing limit")
    normalized: dict[str, tuple[str, ...]] = {}
    for identity, members in values.items():
        if not isinstance(identity, str):
            raise ProcessingError("inline_document_sets filing IDs must be strings")
        try:
            cik, accession = identity.split(":", 1)
            canonical = filing_id(cik, accession)
        except (TypeError, ValueError) as exc:
            raise ProcessingError("inline_document_sets keys must be canonical filing IDs") from exc
        if canonical != identity:
            raise ProcessingError("inline_document_sets keys must be canonical filing IDs")
        if isinstance(members, (str, bytes)) or not isinstance(members, Sequence):
            raise ProcessingError(
                "inline_document_sets values must be explicit document-ID sequences"
            )
        if not 2 <= len(members) <= _MAX_INLINE_GROUP_MEMBERS:
            raise ProcessingError(
                f"inline_document_sets groups must contain 2 to {_MAX_INLINE_GROUP_MEMBERS} IDs"
            )
        if any(
            not isinstance(member, str) or _HEX_256.fullmatch(member) is None for member in members
        ):
            raise ProcessingError("inline_document_sets members must be exact SHA-256 document IDs")
        if len(set(members)) != len(members):
            raise ProcessingError("inline_document_sets members must not contain duplicates")
        normalized[canonical] = tuple(members)
    return normalized


def _assert_current_source_groups(
    archive_root: Path,
    documents: Sequence[Mapping[str, Any]],
    parses: Sequence[Mapping[str, Any]],
    raw_refs: Mapping[str, RawObjectRef],
    dependency_rows: Sequence[Mapping[str, Any]],
) -> None:
    """Prevent a bounded publication from leaving a superseded group active."""
    for parse in parses:
        if parse["parser_name"] != xbrl_parser.PARSER_NAME:
            continue
        saved = _source_group_notes(parse)
        if saved is None:
            continue
        if parse["parser_version"] != xbrl_parser.PARSER_VERSION:
            raise ProcessingCorruptionError(
                "stale source group is outside the current bounded processing worklist"
            )
        group_ids = _group_ids_from_parse_row(parse)
        if group_ids is None:
            raise ProcessingCorruptionError("active source group has no complete member inventory")
        package_rows = _package_rows(parse["filing_id"], documents)
        if not _package_ready(package_rows):
            raise ProcessingCorruptionError(
                "active source group has missing required source members"
            )
        entrypoints = {
            row["source_url"]: _doc_ref_path(archive_root, row, raw_refs)
            for row in package_rows
            if row["fetch_status"] == "present"
        }
        try:
            current = _resolve_inline_source_group(
                package_rows,
                entrypoints,
                explicit_member_ids=group_ids,
                explicit=True,
                allow_group_extension=True,
            )
        except ProcessingError as exc:
            raise ProcessingCorruptionError(
                "active source group no longer has its verified cross-file source basis"
            ) from exc
        if (
            current is None
            or current["source_group"] != saved["source_group"]
            or current["fingerprint"] != saved["fingerprint"]
        ):
            raise ProcessingCorruptionError(
                "stale source group is outside the current bounded processing worklist"
            )
        try:
            notes = json.loads(parse["notes_json"])
        except (TypeError, ValueError) as exc:
            raise ProcessingCorruptionError("active source-group notes are not valid JSON") from exc
        if not isinstance(notes, Mapping):
            raise ProcessingCorruptionError("active source-group notes must be a JSON object")
        if notes.get("dependency_mode") == "preparation_failed":
            if not _group_preparation_failure_matches(
                parse,
                current["source_group"],
                package_rows,
                dependency_rows,
                require_current_sources=True,
            ):
                raise ProcessingCorruptionError(
                    "group dependency-preparation failure is not bound to current source inputs"
                )
        elif not _current_group_dependency_graph_matches(parse, package_rows, dependency_rows):
            raise ProcessingCorruptionError(
                "stale dependency graph is outside the current bounded processing worklist"
            )


def parse_archive(
    archive: Path,
    *,
    protected_paths: Sequence[Path],
    workspace_root: Path,
    filing_ids: Sequence[str] | None = None,
    max_filings: int = 5,
    fetch_dependencies: Callable[[str], Any] | None = None,
    allowed_taxonomy_hosts: Collection[str] = (),
    inline_document_sets: Mapping[str, Sequence[str]] | None = None,
) -> ProcessingResult:
    """Parse a bounded set of present archive documents and publish active parser rows.

    The archive must already contain a published catalog snapshot. Without a fetch
    callback, XBRL parsing is local/offline only. To explicitly refresh remote
    taxonomy resources for selected filing IDs, provide a callback and an exact
    hostname allowlist. The callback must validate hosts/redirects before issuing
    requests; Arelle receives only a prepared private cache and runs offline.
    """
    requested_ids, taxonomy_hosts = _normalize_request(
        filing_ids, max_filings, fetch_dependencies, allowed_taxonomy_hosts
    )
    inline_sets = _normalize_inline_document_sets(inline_document_sets)
    if inline_sets:
        if requested_ids is not None and not set(inline_sets).issubset(requested_ids):
            raise ProcessingError(
                "inline_document_sets keys must be included in explicit filing_ids"
            )
        if requested_ids is None:
            if len(inline_sets) > max_filings:
                raise ProcessingError(
                    "explicit inline groups exceed max_filings; no IDs were truncated"
                )
            requested_ids = tuple(inline_sets)
    archive_root = _resolve_dir(archive, "archive")
    workspace = _resolve_dir(workspace_root, "workspace_root")
    protected = _protected_paths(protected_paths)
    _check_root_separation(archive_root, workspace, protected)

    spec = load_archive_runspec(archive_root)
    with open_archive(archive_root, spec, protected_paths=protected, resume=True) as writer:
        snapshot = read_snapshot(archive_root)
        if snapshot.manifest.get("run_spec_sha256") != spec.sha256:
            raise ArchiveConflictError("archive RunSpec changed during processing setup")
        _, filing_rows = _read_table(snapshot, "filings", FILINGS_SCHEMA, required=True)
        _, document_rows = _read_table(snapshot, "documents", DOCUMENTS_SCHEMA)
        _, active_facts = _read_table(snapshot, "facts", FACT_SCHEMA)
        _, active_sections = _read_table(snapshot, "sections", SECTION_SCHEMA)
        _, active_parses = _read_table(snapshot, "parses", PARSES_SCHEMA)
        _, active_dependencies = _read_table(snapshot, "dependencies", DEPENDENCIES_SCHEMA)
        raw_refs = {ref.path: ref for ref in snapshot.raw_objects}
        documents_by_id, active_parse_by_key = _validate_active_state(
            snapshot,
            filing_rows,
            document_rows,
            active_facts,
            active_sections,
            active_parses,
            active_dependencies,
            raw_refs,
        )
        selected_rows, docs_by_filing = _select_filings(
            filing_rows,
            document_rows,
            requested_ids,
            max_filings,
            active_parse_by_key,
            archive_root,
            raw_refs,
            dependency_rows=active_dependencies,
        )
        selected_ids = tuple(row["filing_id"] for row in selected_rows)
        processed_ids: list[str] = []
        skipped_ids: list[str] = []
        filing_statuses: dict[str, str] = {}
        parse_statuses: dict[str, str] = {}
        new_parse_rows: list[dict[str, Any]] = []
        new_fact_rows: list[dict[str, Any]] = []
        new_section_rows: list[dict[str, Any]] = []
        attempt_statuses: list[str] = []
        replacement_parse_keys: set[tuple[str, str]] = set()
        replacement_dependency_graphs: set[tuple[str, str]] = set()
        dependencies_by_graph: dict[tuple[str, str], list[dict[str, Any]]] = {}
        retirement_by_inline_doc: dict[str, set[str]] = defaultdict(set)
        new_raw_refs: dict[str, RawObjectRef] = {}

        for filing in selected_rows:
            filing_identity = filing["filing_id"]
            package_rows = _package_rows(filing_identity, docs_by_filing.get(filing_identity, ()))
            if not _package_ready(package_rows):
                skipped_ids.append(filing_identity)
                filing_statuses[filing_identity] = "awaiting_present_required_documents"
                continue

            package = materialize_package(
                archive_root,
                filing_identity,
                workspace_root=workspace / filing_identity,
            )
            text_docs = _text_targets(package_rows)
            saved_group_ids = _saved_group_for_filing(filing_identity, active_parse_by_key)
            supplied_group_ids = (
                inline_sets.get(filing_identity) if inline_sets is not None else None
            )
            group_member_ids = (
                supplied_group_ids if supplied_group_ids is not None else saved_group_ids
            )
            xbrl_units, dependency_entrypoints, xbrl_strategy = _plan_xbrl_work_units(
                package_rows,
                package.entrypoints,
                explicit_member_ids=group_member_ids,
                explicit=group_member_ids is not None,
                allow_group_extension=(supplied_group_ids is None and saved_group_ids is not None),
            )
            xbrl_docs = [unit["anchor"] for unit in xbrl_units]
            superseded_parse_ids: set[str] = set()
            if xbrl_strategy == "inline_preferred":
                active_inline_ids = {
                    member["document_id"] for unit in xbrl_units for member in unit["members"]
                }
                for row in package_rows:
                    if row["role"] != "xbrl_instance" or row["document_id"] in active_inline_ids:
                        continue
                    previous = active_parse_by_key.get(
                        (row["document_id"], xbrl_parser.PARSER_NAME)
                    )
                    if previous is None:
                        continue
                    replacement_parse_keys.add((row["document_id"], xbrl_parser.PARSER_NAME))
                    superseded_parse_ids.add(previous["parse_id"])
                    documents_by_id[row["document_id"]]["fact_extraction_status"] = "not_attempted"
                    writer.record_attempt(
                        row["document_id"], "skipped", "superseded_derived_instance"
                    )
                if superseded_parse_ids:
                    for row in xbrl_docs:
                        retirement_by_inline_doc[row["document_id"]].update(superseded_parse_ids)

            pending_xbrl_units = [
                unit
                for unit in xbrl_units
                if not (
                    _terminal_group_match(
                        active_parse_by_key.get(
                            (unit["anchor"]["document_id"], xbrl_parser.PARSER_NAME)
                        ),
                        group=unit["group"],
                        filing_id_value=filing_identity,
                        parser_version=xbrl_parser.PARSER_VERSION,
                        package_rows=package_rows,
                        dependency_rows=active_dependencies,
                    )
                    if unit["group"] is not None
                    else _terminal_match(
                        active_parse_by_key.get(
                            (unit["anchor"]["document_id"], xbrl_parser.PARSER_NAME)
                        ),
                        parser_version=xbrl_parser.PARSER_VERSION,
                        source_hash=_raw_hash(unit["anchor"]),
                    )
                )
            ]
            pending_xbrl_docs = [unit["anchor"] for unit in pending_xbrl_units]
            pending_xbrl_ids = {row["document_id"] for row in pending_xbrl_docs}
            actual_xbrl_entrypoints = []
            seen_entrypoint_ids: set[str] = set()
            for unit in pending_xbrl_units:
                for member in unit["members"]:
                    if member["document_id"] not in seen_entrypoint_ids and not member[
                        "original_filename"
                    ].lower().endswith(".pdf"):
                        seen_entrypoint_ids.add(member["document_id"])
                        actual_xbrl_entrypoints.append(member)

            # Prepare a graph only for documents whose active result can be retried.
            # Terminal peers keep their own prior graph and dependency provenance.
            should_prepare = bool(fetch_dependencies is not None and actual_xbrl_entrypoints)
            pending_source_group = next(
                (unit["group"] for unit in pending_xbrl_units if unit["group"] is not None),
                None,
            )

            prepared: PreparedDependencies | None = None
            dependency_error: DependencyPreparationError | None = None
            preparation_failure: dict[str, Any] | None = None
            response_details: dict[str, dict[str, Any]] = {}
            allowed_hosts: tuple[str, ...] = ()
            dependency_fingerprint = _offline_dependency_fingerprint(package_rows)
            new_dependency_rows: list[dict[str, Any]] = []
            cache_parent: Path | None = None
            temp_context = (
                tempfile.TemporaryDirectory(prefix="filings-processing-", dir=workspace)
                if should_prepare
                else nullcontext(None)
            )
            with temp_context as temporary_root:
                if should_prepare:
                    allowed_hosts = _allowlist_for_sources(actual_xbrl_entrypoints, taxonomy_hosts)
                    assert fetch_dependencies is not None

                    def tracked_fetch(
                        url: str,
                        details: dict[str, dict[str, Any]] = response_details,
                        dependency_fetch: Callable[[str], Any] = fetch_dependencies,
                    ) -> Any:
                        response = dependency_fetch(url)
                        details[url] = _response_provenance(response)
                        return response

                    dependency_cache = Path(temporary_root) / "arelle-cache"
                    try:
                        prepared = prepare_dependencies(
                            {
                                row["source_url"]: package.entrypoints[row["source_url"]].local_path
                                for row in actual_xbrl_entrypoints
                            },
                            workspace_root=package.workspace_root,
                            cache_dir=dependency_cache,
                            fetch=tracked_fetch,
                            allowed_hosts=allowed_hosts,
                            max_dependencies=_MAX_DEPENDENCIES,
                            max_total_bytes=_MAX_DEPENDENCY_BYTES,
                        )
                    except DependencyPreparationError as exc:
                        dependency_error = exc
                        diagnostic_code = _safe_code(exc.code, "dependency_failed")
                        entrypoint_bindings = _dependency_preparation_bindings(
                            actual_xbrl_entrypoints
                        )
                        dependency_fingerprint = _dependency_preparation_fingerprint(
                            entrypoint_bindings, allowed_hosts, diagnostic_code
                        )
                        error_provenance: dict[str, Any] | None = None
                        if pending_source_group is not None:
                            preparation_failure = {
                                "dependency_fingerprint": dependency_fingerprint,
                                "diagnostic_code": diagnostic_code,
                                "source_group_fingerprint": pending_source_group["fingerprint"],
                                "allowed_taxonomy_hosts": list(allowed_hosts),
                                "entrypoints": entrypoint_bindings,
                            }
                            error_provenance = {
                                "bytes_persisted": False,
                                "kind": "dependency_preparation_failed",
                                "filing_id": filing_identity,
                                **preparation_failure,
                            }
                        new_dependency_rows.append(
                            _dependency_error_row(
                                filing_identity,
                                dependency_fingerprint,
                                diagnostic_code,
                                exc.url,
                                provenance=error_provenance,
                            )
                        )
                    else:
                        dependency_fingerprint = _prepared_fingerprint(
                            prepared, response_details, allowed_hosts
                        )
                        new_dependency_rows = _dependency_record_rows(
                            filing_identity,
                            dependency_fingerprint,
                            prepared,
                            response_details,
                            {row["source_url"] for row in actual_xbrl_entrypoints},
                            writer,
                            new_raw_refs,
                        )
                    cache_parent = prepared.cache_dir if prepared is not None else None
                else:
                    cache_parent = None

                filing_results: list[
                    tuple[Mapping[str, Any], ParseResult, str, str, str, Mapping[str, Any]]
                ] = []
                for document in text_docs:
                    source_hash = _raw_hash(document)
                    parser_name = text_parser.PARSER_NAME
                    parser_version = text_parser.PARSER_VERSION
                    dep_fingerprint = _text_dependency_fingerprint()
                    previous = active_parse_by_key.get((document["document_id"], parser_name))
                    if _terminal_match(
                        previous,
                        parser_version=parser_version,
                        source_hash=source_hash,
                        dependencies_fingerprint=dep_fingerprint,
                    ):
                        continue
                    parse_id_value = _parse_id(
                        filing_identity,
                        document["document_id"],
                        source_hash,
                        parser_name,
                        parser_version,
                        dep_fingerprint,
                    )
                    result = _call_text(package, document, parse_id_value, source_hash)
                    filing_results.append(
                        (
                            document,
                            result,
                            parser_name,
                            parser_version,
                            dep_fingerprint,
                            {"strategy": "local_text", "scope_status": package.scope_status},
                        )
                    )

                for unit in xbrl_units:
                    document = unit["anchor"]
                    if document["document_id"] not in pending_xbrl_ids:
                        continue
                    source_hash = _raw_hash(document)
                    parser_name = xbrl_parser.PARSER_NAME
                    parser_version = xbrl_parser.PARSER_VERSION
                    dep_fingerprint = dependency_fingerprint
                    group = unit["group"]
                    group_fingerprint = group["fingerprint"] if group is not None else None
                    retired_ids = set(superseded_parse_ids)
                    previous_group_parse = active_parse_by_key.get(
                        (document["document_id"], parser_name)
                    )
                    if group is not None:
                        previous_group_notes = _source_group_notes(previous_group_parse)
                        if previous_group_notes is not None:
                            retired_ids.update(
                                previous_group_notes["notes"].get("retired_parse_ids", ())
                            )
                        for member in group["members"]:
                            member_id = member["document_id"]
                            replacement_parse_keys.add((member_id, parser_name))
                            previous_member = active_parse_by_key.get((member_id, parser_name))
                            if previous_member is not None:
                                retired_ids.add(previous_member["parse_id"])
                        for old_id in group_member_ids or ():
                            previous_member = active_parse_by_key.get((old_id, parser_name))
                            if previous_member is not None:
                                retired_ids.add(previous_member["parse_id"])
                        # A previously persisted group may be replaced by a different
                        # valid membership revision; retire every old member in this work unit.
                        previous_group_ids = _saved_group_for_filing(
                            filing_identity, active_parse_by_key
                        )
                        if previous_group_ids is not None:
                            for old_id in previous_group_ids:
                                replacement_parse_keys.add((old_id, parser_name))
                                previous_member = active_parse_by_key.get((old_id, parser_name))
                                if previous_member is not None:
                                    retired_ids.add(previous_member["parse_id"])
                    parse_id_value = _parse_id(
                        filing_identity,
                        document["document_id"],
                        source_hash,
                        parser_name,
                        parser_version,
                        dep_fingerprint,
                        source_group_fingerprint=group_fingerprint,
                    )
                    if group is not None:
                        retired_ids.discard(parse_id_value)
                    result = _call_xbrl(
                        package,
                        document,
                        parse_id_value,
                        source_hash,
                        prepared,
                        group["source_group"] if group is not None else None,
                    )
                    if dependency_error is not None:
                        result = _partial_after_dependency_error(result, dependency_error.code)
                    notes: dict[str, Any] = {
                        "strategy": "inline_document_set" if group is not None else xbrl_strategy,
                        "dependency_mode": "preparation_failed"
                        if dependency_error is not None
                        else "prepared"
                        if prepared is not None
                        else "offline_local_only",
                    }
                    if group is not None:
                        notes["source_group"] = group["source_group"]
                        notes["source_group_fingerprint"] = group_fingerprint
                        notes["grouping_basis"] = group["grouping_basis"]
                        notes["ixds_runtime"] = {
                            "parser_version": parser_version,
                            "arelle_version": "2.46.0",
                            "sec_plugin": getattr(
                                xbrl_parser,
                                "_SEC_TRANSFORM_PROVENANCE",
                                "pinned-sec-transform-plugin",
                            ),
                        }
                        notes["allowed_taxonomy_hosts"] = list(allowed_hosts)
                        if dependency_error is not None:
                            if preparation_failure is None:
                                raise ProcessingError(
                                    "grouped dependency failure has no source-bound attempt record"
                                )
                            notes["dependency_preparation"] = preparation_failure
                        retired_ids.update(
                            retirement_by_inline_doc.get(document["document_id"], ())
                        )
                        notes["retired_parse_ids"] = sorted(retired_ids)
                    elif retirement_by_inline_doc.get(document["document_id"]):
                        notes["retirement_reason"] = "superseded_derived_instance"
                        notes["retired_parse_ids"] = sorted(
                            retirement_by_inline_doc[document["document_id"]]
                        )
                    filing_results.append(
                        (
                            document,
                            result,
                            parser_name,
                            parser_version,
                            dep_fingerprint,
                            notes,
                        )
                    )
                    if (
                        prepared is None
                        and result.status == "failed"
                        and any(
                            error.get("code") == "missing_dependency" for error in result.errors
                        )
                    ):
                        new_dependency_rows.append(
                            _dependency_error_row(
                                filing_identity,
                                dep_fingerprint,
                                "missing_dependency",
                            )
                        )

                changed_source = False
                for row in package_rows:
                    entry = package.entrypoints[row["source_url"]]
                    digest, size = _hash_file(entry.local_path)
                    if digest != row["raw_sha256"] or size != row["byte_size"]:
                        changed_source = True
                        break
                if changed_source:
                    invalidated_results = []
                    for item in filing_results:
                        document, result, parser_name, parser_version, dep_fingerprint, notes = item
                        failed = _failed_result(
                            filing_identity,
                            document["document_id"],
                            result.parse_id,
                            parser_name,
                            parser_version,
                            _raw_hash(document),
                            "source_integrity_changed",
                            "A copied source changed during processing; no rows were accepted.",
                        )
                        invalidated_results.append(
                            (
                                document,
                                failed,
                                parser_name,
                                parser_version,
                                dep_fingerprint,
                                notes,
                            )
                        )
                    filing_results = invalidated_results

                if filing_results:
                    statuses = _record_parser_results(
                        filing_identity,
                        filing_results,
                        workspace=package.workspace_root,
                        cache_dir=cache_parent,
                        documents_by_id=documents_by_id,
                        new_parse_rows=new_parse_rows,
                        new_fact_rows=new_fact_rows,
                        new_section_rows=new_section_rows,
                        replacement_parse_keys=replacement_parse_keys,
                        parse_statuses=parse_statuses,
                        attempt_statuses=attempt_statuses,
                        writer=writer,
                    )
                    processed_ids.append(filing_identity)
                    filing_statuses[filing_identity] = _filing_status(statuses)
                elif superseded_parse_ids:
                    processed_ids.append(filing_identity)
                    filing_statuses[filing_identity] = "processed"
                else:
                    skipped_ids.append(filing_identity)
                    filing_statuses[filing_identity] = "already_terminal"

            xbrl_attempted = any(
                result_item[2] == xbrl_parser.PARSER_NAME for result_item in filing_results
            )
            if should_prepare or xbrl_attempted:
                graph_key = (filing_identity, dependency_fingerprint)
                dependencies_by_graph[graph_key] = new_dependency_rows
                replacement_dependency_graphs.add(graph_key)

        if not new_parse_rows and not replacement_dependency_graphs and not replacement_parse_keys:
            return ProcessingResult(
                snapshot=snapshot,
                selected_filing_ids=selected_ids,
                processed_filing_ids=tuple(processed_ids),
                skipped_filing_ids=tuple(dict.fromkeys(skipped_ids)),
                filing_statuses=filing_statuses,
                parse_statuses=parse_statuses,
                fact_rows_written=0,
                section_rows_written=0,
                dependency_rows_written=0,
                full_parse_count=0,
                partial_parse_count=0,
                unsupported_parse_count=0,
                failed_parse_count=0,
            )

        parse_keys = replacement_parse_keys
        retained_parses = [
            row
            for row in active_parses
            if (row["document_id"], row["parser_name"]) not in parse_keys
        ]
        retained_facts = [
            row
            for row in active_facts
            if (row["document_id"], row["parser_name"]) not in parse_keys
        ]
        retained_sections = [
            row
            for row in active_sections
            if (row["document_id"], row["parser_name"]) not in parse_keys
        ]
        parses_out = [*retained_parses, *new_parse_rows]
        for row in parses_out:
            retired = retirement_by_inline_doc.get(row["document_id"])
            if row["parser_name"] != xbrl_parser.PARSER_NAME or not retired:
                continue
            try:
                existing_notes = json.loads(row["notes_json"])
            except (TypeError, ValueError):
                existing_notes = {}
            notes = dict(existing_notes) if isinstance(existing_notes, Mapping) else {}
            notes["retirement_reason"] = "superseded_derived_instance"
            notes["retired_parse_ids"] = sorted(set(notes.get("retired_parse_ids", ())) | retired)
            row["notes_json"] = _canonical_json(notes).decode("utf-8")
        facts_out = [*retained_facts, *new_fact_rows]
        sections_out = [*retained_sections, *new_section_rows]
        needed_dependency_graphs = {
            (row["filing_id"], row["dependencies_fingerprint"])
            for row in parses_out
            if row["parser_name"] == xbrl_parser.PARSER_NAME
        }
        dependencies_out = [
            row
            for row in active_dependencies
            if (row["filing_id"], row["dependency_fingerprint"]) in needed_dependency_graphs
            and (row["filing_id"], row["dependency_fingerprint"])
            not in replacement_dependency_graphs
        ]
        for graph_key in sorted(replacement_dependency_graphs):
            if graph_key in needed_dependency_graphs:
                dependencies_out.extend(dependencies_by_graph.get(graph_key, []))

        candidate_facts = _table(facts_out, FACT_SCHEMA, "facts")
        candidate_sections = _table(sections_out, SECTION_SCHEMA, "sections")
        candidate_parses = _table(parses_out, PARSES_SCHEMA, "parses")
        candidate_dependencies = _table(dependencies_out, DEPENDENCIES_SCHEMA, "dependencies")
        candidate_documents = _table(document_rows, DOCUMENTS_SCHEMA, "documents")
        combined_refs = {**raw_refs, **new_raw_refs}
        _assert_current_source_groups(
            archive_root, document_rows, parses_out, combined_refs, dependencies_out
        )
        _validate_active_state(
            snapshot,
            filing_rows,
            document_rows,
            facts_out,
            sections_out,
            parses_out,
            dependencies_out,
            combined_refs,
        )

        replacements = {
            "documents": candidate_documents,
            "facts": candidate_facts,
            "sections": candidate_sections,
            "parses": candidate_parses,
            "dependencies": candidate_dependencies,
        }
        if all(_same_table(snapshot, name, table) for name, table in replacements.items()):
            final_snapshot = snapshot
        else:
            final_snapshot = _commit_or_reconcile(
                writer,
                snapshot,
                replacements,
                tuple(new_raw_refs.values()),
            )

    full_count, partial_count, unsupported_count, failed_count = _counts(attempt_statuses)
    return ProcessingResult(
        snapshot=final_snapshot,
        selected_filing_ids=selected_ids,
        processed_filing_ids=tuple(processed_ids),
        skipped_filing_ids=tuple(dict.fromkeys(skipped_ids)),
        filing_statuses=filing_statuses,
        parse_statuses=parse_statuses,
        fact_rows_written=len(new_fact_rows),
        section_rows_written=len(new_section_rows),
        dependency_rows_written=sum(len(rows) for rows in dependencies_by_graph.values()),
        full_parse_count=full_count,
        partial_parse_count=partial_count,
        unsupported_parse_count=unsupported_count,
        failed_parse_count=failed_count,
    )


def _select_filings(
    filing_rows: Sequence[Mapping[str, Any]],
    documents: Sequence[Mapping[str, Any]],
    requested_ids: tuple[str, ...] | None,
    max_filings: int,
    active_parses: Mapping[tuple[str, str], Mapping[str, Any]],
    archive_root: Path,
    refs: Mapping[str, RawObjectRef],
    *,
    dependency_rows: Sequence[Mapping[str, Any]] = (),
) -> tuple[list[Mapping[str, Any]], dict[str, list[Mapping[str, Any]]]]:
    return _plan_select_filings(
        filing_rows,
        documents,
        requested_ids,
        max_filings,
        active_parses,
        archive_root,
        refs,
        dependency_rows=dependency_rows,
        has_pending_work=_has_pending_work,
        error_type=ProcessingError,
    )


def _has_pending_work(
    filing_id_value: str,
    documents: Sequence[Mapping[str, Any]],
    active_parses: Mapping[tuple[str, str], Mapping[str, Any]],
    archive_root: Path,
    refs: Mapping[str, RawObjectRef],
    *,
    dependency_rows: Sequence[Mapping[str, Any]] = (),
) -> bool:
    rows = _package_rows(filing_id_value, documents)
    if not _package_ready(rows):
        return False
    for document in _text_targets(rows):
        if not _terminal_match(
            active_parses.get((document["document_id"], text_parser.PARSER_NAME)),
            parser_version=text_parser.PARSER_VERSION,
            source_hash=_raw_hash(document),
            dependencies_fingerprint=_text_dependency_fingerprint(),
        ):
            return True
    # Avoid materializing workspaces merely to select a bounded worklist. Inline
    # detection and source-bound grouping read only manifest-listed raw bytes.
    entrypoints = {
        row["source_url"]: _doc_ref_path(archive_root, row, refs)
        for row in rows
        if row["fetch_status"] == "present"
    }
    saved_group_ids = _saved_group_for_filing(filing_id_value, active_parses)
    work_units, _dependency_entrypoints, _strategy = _plan_xbrl_work_units(
        rows,
        entrypoints,
        explicit_member_ids=saved_group_ids,
        explicit=saved_group_ids is not None,
        allow_group_extension=saved_group_ids is not None,
    )
    for unit in work_units:
        anchor = unit["anchor"]
        previous = active_parses.get((anchor["document_id"], xbrl_parser.PARSER_NAME))
        if unit["group"] is not None:
            terminal = _terminal_group_match(
                previous,
                group=unit["group"],
                filing_id_value=filing_id_value,
                parser_version=xbrl_parser.PARSER_VERSION,
                package_rows=rows,
                dependency_rows=dependency_rows,
            )
        else:
            terminal = _terminal_match(
                previous,
                parser_version=xbrl_parser.PARSER_VERSION,
                source_hash=_raw_hash(anchor),
            )
        if not terminal:
            return True
    return False
