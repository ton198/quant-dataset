"""Pinned current Samples registry and fixture contract tests."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from samples.builder import build_samples
from samples.contracts import (
    COLUMN_ORDER,
    CS_FEATURES,
    FEATURE_COUNTS,
    FEATURE_LIST,
    LABELS,
    MACRO_RAW,
    MACRO_SERIES,
    MISS_FEATURES,
    PURGE_SESSIONS,
    RAW_FEATURES,
    SAMPLE_SCHEMA,
    SCHEMA_VERSION,
    STOCK_RAW,
    semantic_fingerprint,
)
from samples.query import QuerySamplesError, _bundle_inputs
from samples.validation import (
    SamplesValidationError,
    validate_fresh_output,
    validate_manifest,
    validate_sample_schema,
)

FIXTURES = Path(__file__).parent / "fixtures" / "samples" / "current"


def test_current_registry_is_one_exact_132_column_contract() -> None:
    assert SCHEMA_VERSION == "samples"
    assert (
        len(RAW_FEATURES),
        len(CS_FEATURES),
        len(MISS_FEATURES),
        len(FEATURE_LIST),
        len(LABELS),
    ) == (
        43,
        10,
        43,
        96,
        32,
    )
    assert FEATURE_COUNTS == {
        "raw": 43,
        "cs": 10,
        "miss": 43,
        "feature_list": 96,
        "sample_columns": 132,
    }
    assert len(COLUMN_ORDER) == 132
    assert len(SAMPLE_SCHEMA) == 132
    assert SAMPLE_SCHEMA.names == list(COLUMN_ORDER)
    assert SAMPLE_SCHEMA.field("date").type == pa.date32()
    assert SAMPLE_SCHEMA.field("asset_id").type == pa.large_string()
    assert SAMPLE_SCHEMA.field("is_common").type == pa.bool_()
    assert SAMPLE_SCHEMA.field("flag_extreme_label").type == pa.uint8()
    assert all(SAMPLE_SCHEMA.field(name).type == pa.float32() for name in RAW_FEATURES)
    assert all(SAMPLE_SCHEMA.field(name).type == pa.float32() for name in CS_FEATURES)
    assert all(SAMPLE_SCHEMA.field(name).type == pa.uint8() for name in MISS_FEATURES)
    assert all(SAMPLE_SCHEMA.field(name).type == pa.float32() for name in LABELS)
    assert len(STOCK_RAW) == 10
    assert len(MACRO_RAW) == 33
    assert len(MACRO_SERIES) == 11
    assert PURGE_SESSIONS == 30
    assert semantic_fingerprint() == semantic_fingerprint()
    assert not any("financial" in name.casefold() for name in COLUMN_ORDER)


def test_checked_in_current_fixture_builds_with_independent_expected_labels(tmp_path: Path) -> None:
    organized = FIXTURES / "organized"
    workspace = FIXTURES
    exclusions = FIXTURES / "config" / "universes" / "exclusions_v1.json"
    output = tmp_path / "new-output"
    result = build_samples(
        organized,
        output,
        exclusions,
        workspace_root=workspace,
        canonical_min_tickers=2,
        staging_tickers=1,
        rank_batch_sessions=6,
    )
    assert result["rows"] == 36 * 2
    frame = pd.concat(
        [pd.read_parquet(path) for path in sorted((output / "samples").glob("year=*/*.parquet"))],
        ignore_index=True,
    )
    signal = pd.Timestamp("2022-01-03")
    row = frame.loc[frame["asset_id"].eq("AAA") & (pd.to_datetime(frame["date"]) == signal)].iloc[0]
    # Entry is position 1 (101); the one-session exit is position 2 (102).
    assert row["target_return_1d"] == np.float32(102.0 / 101.0 - 1.0)
    assert row["f_raw_m_CPIAUCSL"] == np.float32(1.0)
    assert pd.isna(row["f_raw_m_CPIAUCSL_d1"])
    assert row["miss_return_1d"] == 0

    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == "samples"
    assert len(manifest["input_provenance"]["files"]) == 4
    assert all(
        record["bytes"] > 0 and len(record["sha256"]) == 64
        for record in manifest["input_provenance"]["files"]
    )
    for name, record in manifest["outputs"].items():
        path = output / name
        assert hashlib.sha256(path.read_bytes()).hexdigest() == record["sha256"]
        assert path.stat().st_size == record["bytes"]


def test_query_bundle_rejects_legacy_product_id_and_current_validator_is_central(
    tmp_path: Path,
) -> None:
    payload = {"schema_version": "samples_v3", "feature_list": list(FEATURE_LIST), "outputs": {}}
    with pytest.raises(SamplesValidationError, match="schema_version must be 'samples'"):
        validate_manifest(payload)

    legacy = tmp_path / "legacy"
    (legacy / "samples" / "year=2024").mkdir(parents=True)
    (legacy / "manifest.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(QuerySamplesError, match="Invalid sample manifest"):
        _bundle_inputs(legacy)

    validate_sample_schema(SAMPLE_SCHEMA)
    with pytest.raises(SamplesValidationError, match="Arrow schema"):
        validate_sample_schema(SAMPLE_SCHEMA.remove(0))
    wrong_asset_id_type = pa.schema(
        [
            pa.field(field.name, pa.string()) if field.name == "asset_id" else field
            for field in SAMPLE_SCHEMA
        ]
    )
    with pytest.raises(SamplesValidationError, match="Arrow schema"):
        validate_sample_schema(wrong_asset_id_type)


def test_output_validation_protects_files_and_rejects_symlink_ancestors(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    data = workspace / "data"
    organized = data / "organized"
    (organized / "stocks").mkdir(parents=True)
    (organized / "shared").mkdir()
    protected = data / "raw"
    protected.mkdir(parents=True)
    sentinel = protected / "keep.bin"
    sentinel.write_bytes(b"keep")
    with pytest.raises(SamplesValidationError, match="protected workspace data path"):
        validate_fresh_output(protected / "nested", organized, workspace)
    assert sentinel.read_bytes() == b"keep"

    external = tmp_path / "external"
    external.mkdir()
    link = external / "linked"
    link.symlink_to(tmp_path / "missing-target", target_is_directory=True)
    with pytest.raises(SamplesValidationError, match="must not be a symlink"):
        validate_fresh_output(link, organized, workspace)
