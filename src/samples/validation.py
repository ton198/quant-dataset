"""Shared sample manifest, Arrow schema, hash, and filesystem validation."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any

import pyarrow as pa

from .contracts import (
    FEATURE_LIST,
    SAMPLE_SCHEMA,
    SCHEMA_VERSION,
    schema_fingerprint,
    semantic_contract,
    semantic_fingerprint,
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PROTECTED_DATA_NAMES = {"output", "organized", "raw", "baselines", "_archive", "archive"}


class SamplesValidationError(ValueError):
    """Raised when a sample bundle or output path violates the current contract."""


def sha256_file(path: str | Path) -> tuple[str, int]:
    """Hash a file with bounded memory and return its digest and byte count."""
    digest = hashlib.sha256()
    size = 0
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def _lexical_absolute(path: str | Path) -> Path:
    expanded = Path(path).expanduser()
    if not expanded.is_absolute():
        expanded = Path.cwd() / expanded
    return expanded


def _reject_symlink_ancestors(path: Path, label: str) -> None:
    for candidate in (path, *path.parents):
        if candidate.is_symlink():
            raise SamplesValidationError(
                f"{label} path or ancestor must not be a symlink: {candidate}"
            )


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _workspace_protected_paths(workspace: Path) -> set[Path]:
    protected = {workspace / "data" / name for name in _PROTECTED_DATA_NAMES}
    protected.update(
        {workspace / name for name in ("archive", "archives", "backup", "backups", "baselines")}
    )
    data_root = workspace / "data"
    if data_root.is_dir():
        for child in data_root.iterdir():
            folded = child.name.casefold()
            if any(token in folded for token in ("backup", "baseline", "archive")):
                protected.add(child)
    return {path.resolve(strict=False) for path in protected}


def validate_fresh_output(
    output_dir: str | Path,
    organized_dir: str | Path,
    workspace_root: str | Path | None = None,
) -> Path:
    """Resolve a fresh output only after rejecting lexical symlinks and protected paths."""
    output_lexical = _lexical_absolute(output_dir)
    _reject_symlink_ancestors(output_lexical, "output")
    output = output_lexical.resolve(strict=False)
    organized = Path(organized_dir).expanduser().resolve(strict=False)
    workspace_input = Path.cwd() if workspace_root is None else workspace_root
    workspace = _lexical_absolute(workspace_input).resolve(strict=False)
    if not workspace.exists() or not workspace.is_dir():
        raise SamplesValidationError(
            f"workspace root must exist and be a directory: {workspace}"
        )

    if _paths_overlap(output, organized):
        raise SamplesValidationError(
            f"output directory must not overlap organized input data: {output} and {organized}"
        )

    protected_paths = _workspace_protected_paths(workspace)
    # Protect input-owned data stores even when the CLI was launched from another
    # directory. The normal data root protects all existing sibling directories while
    # leaving a fresh, nonexistent output child (such as data/samples-output) available.
    if organized.name == "organized":
        organized_parent = organized.parent
        if organized_parent.name == "data":
            if organized_parent.is_dir():
                protected_paths.update(
                    child.resolve(strict=False)
                    for child in organized_parent.iterdir()
                    if child.is_dir()
                )
            for name in _PROTECTED_DATA_NAMES:
                protected_paths.add((organized_parent / name).resolve(strict=False))
        elif organized_parent.is_dir():
            for child in organized_parent.iterdir():
                folded = child.name.casefold()
                if child.is_dir() and any(
                    token in folded for token in ("backup", "baseline", "archive")
                ):
                    protected_paths.add(child.resolve(strict=False))
    for protected in sorted(protected_paths, key=str):
        if _paths_overlap(output, protected):
            raise SamplesValidationError(
                f"output directory overlaps protected workspace data path {protected}: {output}"
            )

    if output.exists():
        if not output.is_dir():
            raise SamplesValidationError(f"output directory must be fresh and empty: {output}")
        try:
            next(output.iterdir())
        except StopIteration:
            pass
        else:
            raise SamplesValidationError(
                f"output directory must be fresh and empty; choose a new --out path: {output}"
            )
    return output


def validate_manifest(payload: Any) -> dict[str, Any]:
    """Validate the common current manifest identity and registered feature order."""
    if not isinstance(payload, dict):
        raise SamplesValidationError("manifest.json must contain a JSON object")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise SamplesValidationError(f"manifest.json schema_version must be {SCHEMA_VERSION!r}")
    feature_list = payload.get("feature_list")
    if feature_list != list(FEATURE_LIST):
        raise SamplesValidationError(
            "manifest.json feature_list does not match the current registry"
        )
    outputs = payload.get("outputs")
    if not isinstance(outputs, dict):
        raise SamplesValidationError("manifest.json outputs must be an object")
    if payload.get("semantic_fingerprint") != semantic_fingerprint():
        raise SamplesValidationError(
            "manifest.json semantic_fingerprint does not match the current contract"
        )
    if payload.get("schema_fingerprint") != schema_fingerprint():
        raise SamplesValidationError(
            "manifest.json schema_fingerprint does not match the current Arrow schema"
        )
    if payload.get("semantic_contract") != semantic_contract():
        raise SamplesValidationError(
            "manifest.json semantic_contract does not match the current contract"
        )
    return payload


def validate_sample_schema(schema: pa.Schema) -> None:
    """Require the exact ordered current Arrow names and physical data types."""
    actual = [(field.name, field.type, field.nullable) for field in schema]
    expected = [(field.name, field.type, field.nullable) for field in SAMPLE_SCHEMA]
    if actual != expected:
        raise SamplesValidationError(
            "sample Arrow schema does not match the current contract "
            "(field names, types, or order differ)"
        )


def _bundle_file(bundle: Path, relative_name: str) -> Path:
    rel = PurePosixPath(relative_name)
    if rel.is_absolute() or not rel.parts or any(part in {"", ".", ".."} for part in rel.parts):
        raise SamplesValidationError(
            f"manifest output path is not a safe relative path: {relative_name!r}"
        )
    lexical = bundle.joinpath(*rel.parts)
    try:
        resolved = lexical.resolve(strict=True)
    except OSError as exc:
        raise SamplesValidationError(
            f"manifest output is missing or unreadable: {relative_name}"
        ) from exc
    if not resolved.is_relative_to(bundle) or not resolved.is_file():
        raise SamplesValidationError(
            f"manifest output resolves outside the bundle or is not a file: {relative_name}"
        )
    return resolved


def validate_output_hashes(bundle_dir: str | Path, manifest: dict[str, Any]) -> dict[str, Path]:
    """Validate all declared outputs using streamed hashes and bundle-contained paths."""
    bundle = Path(bundle_dir).expanduser().resolve(strict=True)
    outputs = manifest.get("outputs")
    if not isinstance(outputs, dict):
        raise SamplesValidationError("manifest.json outputs must be an object")
    resolved: dict[str, Path] = {}
    for relative_name, record in sorted(outputs.items()):
        if not isinstance(relative_name, str) or not isinstance(record, dict):
            raise SamplesValidationError("manifest.json outputs entries must be named objects")
        path = _bundle_file(bundle, relative_name)
        expected_hash = record.get("sha256")
        expected_bytes = record.get("bytes")
        if not isinstance(expected_hash, str) or not _SHA256.fullmatch(expected_hash):
            raise SamplesValidationError(f"manifest output has an invalid sha256: {relative_name}")
        if (
            isinstance(expected_bytes, bool)
            or not isinstance(expected_bytes, int)
            or expected_bytes < 0
        ):
            raise SamplesValidationError(
                f"manifest output has an invalid byte count: {relative_name}"
            )
        actual_hash, actual_bytes = sha256_file(path)
        if actual_hash != expected_hash or actual_bytes != expected_bytes:
            raise SamplesValidationError(
                f"manifest output hash or byte count mismatch: {relative_name}"
            )
        expected_rows = record.get("rows")
        if expected_rows is not None:
            if (
                isinstance(expected_rows, bool)
                or not isinstance(expected_rows, int)
                or expected_rows < 0
            ):
                raise SamplesValidationError(
                    f"manifest output has an invalid row count: {relative_name}"
                )
            if path.suffix == ".parquet":
                try:
                    import pyarrow.parquet as pq

                    actual_rows = pq.ParquetFile(path).metadata.num_rows
                except Exception as exc:
                    raise SamplesValidationError(
                        f"cannot read manifest Parquet output: {relative_name}"
                    ) from exc
                if actual_rows != expected_rows:
                    raise SamplesValidationError(
                        f"manifest output row count mismatch: {relative_name}"
                    )
        resolved[relative_name] = path
    return resolved


def validate_manifest_file(path: str | Path) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SamplesValidationError(f"Invalid manifest.json: {exc}") from exc
    return validate_manifest(payload)


__all__ = [
    "SamplesValidationError",
    "sha256_file",
    "validate_fresh_output",
    "validate_manifest",
    "validate_manifest_file",
    "validate_output_hashes",
    "validate_sample_schema",
]
