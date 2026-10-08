"""Integrity-pinned loader for the bundled SEC Inline XBRL transformations.

The vendored module is never imported by this helper. It is given to Arelle only
as a local plugin path after every runtime payload is verified against the
immutable upstream manifest.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

_UPSTREAM_COMMIT = "47a372d099168f8669d20a8a6bbe5cb16bbf71ac"
_UPSTREAM_RELEASE = "25.2.1.1"
_UPSTREAM_MANIFEST_SHA256 = "0da2c7c26e69285fd86895c208a30145f1d3b1f1ce26e6bd729452509d67ed60"
_TRANSFORM_NAMESPACE = "http://www.sec.gov/inlineXBRL/transformation/2015-08-31"
_TRANSFORM_NAMES = frozenset(
    {
        "duryear",
        "durmonth",
        "durweek",
        "durday",
        "durhour",
        "datequarterend",
        "numinf",
        "numneginf",
        "numnan",
        "numwordsen",
        "durwordsen",
        "boolballotbox",
        "yesnoballotbox",
        "countrynameen",
        "stateprovnameen",
        "edgarprovcountryen",
        "exchnameen",
        "entityfilercategoryen",
    }
)
_EXPECTED_SHA256 = {
    "__init__.py": "2296586f945ffd95ab37d3d4147c4e45f42ce4e5f397f61143e053208adefbf1",
    "text2num.py": "6c9b26354320a2fb34fe380cdc71ff7f8d1cc712811e6b10c661e512f0383409",
    "LICENSE-APACHE-2.0.txt": "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30",
    "NOTICE.md": "3dc1e96a0e20054b9928119ced42e4e615de1bb1cf3162c5491c46a67200e2a4",
}


class SecTransformIntegrityError(RuntimeError):
    """The local SEC transform package is missing, altered, or outside its root."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def sec_transform_plugin_path() -> Path:
    """Return the sole approved plugin directory after checking all pinned files."""
    module_file = Path(__file__)
    if module_file.is_symlink():
        raise SecTransformIntegrityError("SEC transform loader cannot be a symlink")
    try:
        module_file = module_file.resolve(strict=True)
        package_root = module_file.parent
        vendor_root = package_root / "_vendor"
        plugin_root = vendor_root / "sec_transforms"
        for directory in (vendor_root, plugin_root):
            if directory.is_symlink() or not directory.is_dir():
                raise SecTransformIntegrityError(
                    "SEC transform package directory is missing or unsafe"
                )
            if not directory.resolve(strict=True).is_relative_to(package_root):
                raise SecTransformIntegrityError(
                    "SEC transform package escaped its installed module root"
                )

        manifest_path = plugin_root / "UPSTREAM.json"
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise SecTransformIntegrityError("SEC transform upstream manifest is missing or unsafe")
        if _sha256(manifest_path) != _UPSTREAM_MANIFEST_SHA256:
            raise SecTransformIntegrityError("SEC transform upstream manifest checksum mismatch")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (
            manifest.get("project") != "Arelle/EDGAR"
            or manifest.get("release") != _UPSTREAM_RELEASE
            or manifest.get("commit") != _UPSTREAM_COMMIT
        ):
            raise SecTransformIntegrityError(
                "SEC transform upstream identity does not match its pin"
            )

        for filename, expected_hash in _EXPECTED_SHA256.items():
            path = plugin_root / filename
            if path.is_symlink() or not path.is_file():
                raise SecTransformIntegrityError("SEC transform package file is missing or unsafe")
            resolved = path.resolve(strict=True)
            if not resolved.is_relative_to(plugin_root.resolve(strict=True)):
                raise SecTransformIntegrityError(
                    "SEC transform package file escaped its trusted root"
                )
            if _sha256(path) != expected_hash:
                raise SecTransformIntegrityError("SEC transform package file checksum mismatch")

        return plugin_root
    except SecTransformIntegrityError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise SecTransformIntegrityError("SEC transform package could not be verified") from exc


def has_complete_sec_transform_registry(custom_transforms: Any) -> bool:
    """Check Arelle's loaded custom-transform registry for every pinned SEC QName."""
    if not isinstance(custom_transforms, dict):
        return False
    found: set[str] = set()
    for qname, function in custom_transforms.items():
        if getattr(qname, "namespaceURI", None) != _TRANSFORM_NAMESPACE:
            continue
        name = getattr(qname, "localName", None)
        if isinstance(name, str) and name in _TRANSFORM_NAMES and callable(function):
            found.add(name)
    return found == _TRANSFORM_NAMES


def sec_transform_identity() -> dict[str, str]:
    """Return the stable upstream identity used in parser provenance/versioning."""
    return {"project": "Arelle/EDGAR", "release": _UPSTREAM_RELEASE, "commit": _UPSTREAM_COMMIT}
