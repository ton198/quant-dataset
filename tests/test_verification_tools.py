"""Focused tests for shared current sample bundle validation."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from samples.contracts import (
    FEATURE_LIST,
    SAMPLE_SCHEMA,
    schema_fingerprint,
    semantic_contract,
    semantic_fingerprint,
)
from samples.query import _bundle_inputs
from samples.validation import (
    SamplesValidationError,
    sha256_file,
    validate_fresh_output,
    validate_manifest,
    validate_output_hashes,
    validate_sample_schema,
)


def _write_manifest(
    bundle: Path, outputs: dict[str, dict[str, object]], version: str = "samples"
) -> None:
    (bundle / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": version,
                "feature_list": list(FEATURE_LIST),
                "outputs": outputs,
                "semantic_contract": semantic_contract(),
                "semantic_fingerprint": semantic_fingerprint(),
                "schema_fingerprint": schema_fingerprint(),
            }
        ),
        encoding="utf-8",
    )


def test_streamed_hash_returns_exact_digest_and_size(tmp_path: Path) -> None:
    path = tmp_path / "bytes.bin"
    path.write_bytes(b"samples\x00contract" * 1024)
    digest, size = sha256_file(path)
    assert digest == hashlib.sha256(path.read_bytes()).hexdigest()
    assert size == path.stat().st_size


def test_manifest_output_registry_hashes_and_schema_are_checked(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    shard = bundle / "samples" / "year=2024" / "part-00000.parquet"
    shard.parent.mkdir(parents=True)
    table = pa.Table.from_arrays(
        [pa.nulls(1, type=field.type) for field in SAMPLE_SCHEMA],
        schema=SAMPLE_SCHEMA,
    )
    pq.write_table(table, shard)
    digest, size = sha256_file(shard)
    outputs = {"samples/year=2024/part-00000.parquet": {"sha256": digest, "bytes": size, "rows": 1}}
    _write_manifest(bundle, outputs)
    manifest = validate_manifest(json.loads((bundle / "manifest.json").read_text(encoding="utf-8")))
    assert validate_output_hashes(bundle, manifest)
    validate_sample_schema(pq.read_schema(shard))
    bundle_path, schema_version, sample_files, _ = _bundle_inputs(bundle)
    assert bundle_path == bundle.resolve()
    assert schema_version == "samples"
    assert sample_files == [shard.resolve()]

    shard.write_bytes(b"changed")
    with pytest.raises(SamplesValidationError, match="hash or byte count mismatch"):
        validate_output_hashes(bundle, manifest)


def test_legacy_or_bad_manifest_identifiers_are_rejected(tmp_path: Path) -> None:
    for version in ("samples_v1", "samples_v2", "samples_v3", "samples_v3_financial_free"):
        with pytest.raises(SamplesValidationError, match="schema_version must be 'samples'"):
            validate_manifest(
                {"schema_version": version, "feature_list": list(FEATURE_LIST), "outputs": {}}
            )


def test_manifest_output_paths_cannot_escape_bundle(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside")
    digest, size = sha256_file(outside)
    manifest = {
        "schema_version": "samples",
        "feature_list": list(FEATURE_LIST),
        "outputs": {"../outside.bin": {"sha256": digest, "bytes": size}},
    }
    with pytest.raises(SamplesValidationError, match="safe relative path"):
        validate_output_hashes(bundle, manifest)


def test_fresh_output_validation_protects_workspace_siblings_without_deletion(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    organized = workspace / "data" / "organized"
    (organized / "stocks").mkdir(parents=True)
    protected = workspace / "data" / "baselines"
    protected.mkdir(parents=True)
    sentinel = protected / "saved.txt"
    sentinel.write_text("preserve", encoding="utf-8")
    with pytest.raises(SamplesValidationError, match="protected workspace data path"):
        validate_fresh_output(protected / "nested", organized, workspace)
    assert sentinel.read_text(encoding="utf-8") == "preserve"

    nonempty = tmp_path / "nonempty"
    nonempty.mkdir()
    (nonempty / "file.txt").write_text("preserve", encoding="utf-8")
    with pytest.raises(SamplesValidationError, match="fresh and empty"):
        validate_fresh_output(nonempty, organized, workspace)


def test_sample_schema_rejects_reordered_columns() -> None:
    reordered = pa.schema(list(reversed(list(SAMPLE_SCHEMA))))
    with pytest.raises(SamplesValidationError, match="Arrow schema"):
        validate_sample_schema(reordered)
