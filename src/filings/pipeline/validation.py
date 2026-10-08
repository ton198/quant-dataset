"""Pure parser fact-source checks before facts enter the archive ledger."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..dependencies import PreparedDependencies
    from ..packages import PreparedPackage
    from ..parsing_models import ParseResult

_HEX_256 = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)


def fact_sources_verified(
    result: ParseResult,
    package: PreparedPackage,
    prepared: PreparedDependencies | None,
    source_group: Mapping[str, Any] | None = None,
) -> bool:
    """Verify fact ownership by bytes in the exact prepared parser source tree."""
    if not result.facts:
        return True
    if source_group is not None:
        members = source_group.get("members")
        if not isinstance(members, list):
            return False
        root = (
            prepared.cache_dir.resolve(strict=True)
            if prepared is not None
            else package.workspace_root.resolve(strict=True)
        )
        expected_sources: dict[str, tuple[str, str]] = {}
        for member in members:
            if not isinstance(member, Mapping):
                return False
            source_url = member.get("source_url")
            digest = member.get("raw_sha256")
            try:
                path = (
                    prepared.entrypoint_path(source_url)
                    if prepared is not None
                    else package.entrypoints[source_url].local_path
                )
                relative = Path(path).resolve(strict=True).relative_to(root).as_posix()
            except (KeyError, OSError, ValueError, TypeError):
                return False
            if not isinstance(source_url, str) or not isinstance(digest, str):
                return False
            expected_sources[source_url] = (digest, relative)
        for row in result.facts:
            source_uri = row.get("source_uri")
            document_hash = row.get("document_hash")
            source_relpath = row.get("source_relpath")
            expected = expected_sources.get(source_uri) if isinstance(source_uri, str) else None
            if (
                expected is None
                or not isinstance(source_relpath, str)
                or not source_relpath
                or not isinstance(document_hash, str)
                or _HEX_256.fullmatch(document_hash) is None
                or document_hash != expected[0]
                or source_relpath != expected[1]
            ):
                return False
        return True
    if prepared is not None:
        root = prepared.cache_dir.resolve(strict=True)
        authorities: dict[str, tuple[str, frozenset[str]]] = {}
        for path, origin in prepared.origin_map.items():
            try:
                resolved = Path(path).resolve(strict=True)
                relative = resolved.relative_to(root).as_posix()
            except (OSError, ValueError):
                continue
            expected_hash = prepared.expected_hashes.get(path)
            if isinstance(expected_hash, str) and _HEX_256.fullmatch(expected_hash):
                authorities[relative] = (expected_hash, frozenset({origin}))
    else:
        root = package.workspace_root.resolve(strict=True)
        authorities = {}
        for source_url, entry in package.entrypoints.items():
            try:
                path = entry.local_path.resolve(strict=True)
                relative = path.relative_to(root).as_posix()
            except (OSError, ValueError):
                continue
            aliases = frozenset({source_url, str(path), path.as_uri(), relative})
            authorities[relative] = (entry.raw_sha256, aliases)

    for row in result.facts:
        relative = row.get("source_relpath")
        source_uri = row.get("source_uri")
        document_hash = row.get("document_hash")
        if not isinstance(relative, str) or not isinstance(source_uri, str):
            return False
        authority = authorities.get(relative)
        if authority is None:
            return False
        expected_hash, allowed_uris = authority
        if (
            source_uri not in allowed_uris
            or document_hash != expected_hash
            or not isinstance(document_hash, str)
            or _HEX_256.fullmatch(document_hash) is None
        ):
            return False
    return True


def annotate_group_fact_owners(
    result: ParseResult,
    source_group: Mapping[str, Any],
    *,
    canonical_json: Callable[[Any], bytes],
) -> bool:
    """Add canonical source document ownership to inline-group fact provenance."""
    members = source_group.get("members")
    if not isinstance(members, list):
        return False
    for row in result.facts:
        owners = [
            member
            for member in members
            if isinstance(member, Mapping)
            and member.get("source_url") == row.get("source_uri")
            and member.get("raw_sha256") == row.get("document_hash")
        ]
        if len(owners) != 1:
            return False
        try:
            provenance = json.loads(row.get("provenance"))
        except (TypeError, ValueError):
            return False
        if not isinstance(provenance, Mapping):
            return False
        owner_id = owners[0].get("document_id")
        prior_owner = provenance.get("source_document_id")
        if prior_owner is not None and prior_owner != owner_id:
            return False
        merged = dict(provenance)
        merged["source_document_id"] = owner_id
        row["provenance"] = canonical_json(merged).decode("utf-8")
    return True
