"""Integrity and end-to-end regression checks for current Samples bundles."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from datetime import date
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from samples.builder import build_samples
from samples.contracts import (
    FEATURE_LIST,
    SAMPLE_SCHEMA,
    SCHEMA_VERSION,
    schema_fingerprint,
    semantic_contract,
    semantic_fingerprint,
)
from samples.query import QuerySamplesError, _bundle_inputs, query_samples

FIXTURES = Path(__file__).parent / "fixtures" / "samples" / "current"
REQUIRES_DUCKDB = pytest.mark.skipif(
    importlib.util.find_spec("duckdb") is None,
    reason="DuckDB is an optional dependency; install the query extra to run this test",
)


def _sample_table() -> pa.Table:
    arrays: list[pa.Array] = []
    for field in SAMPLE_SCHEMA:
        if field.name == "date":
            arrays.append(pa.array([date(2024, 1, 2)], type=field.type))
        elif field.name == "asset_id":
            arrays.append(pa.array(["AAA"], type=field.type))
        elif field.name == "is_common":
            arrays.append(pa.array([True], type=field.type))
        elif field.name == "flag_extreme_label":
            arrays.append(pa.array([0], type=field.type))
        else:
            arrays.append(pa.nulls(1, type=field.type))
    return pa.Table.from_arrays(arrays, schema=SAMPLE_SCHEMA)


def _refresh_manifest(bundle: Path) -> None:
    outputs = {}
    for path in sorted(bundle.rglob("*")):
        if not path.is_file() or path.name == "manifest.json":
            continue
        relative = path.relative_to(bundle).as_posix()
        rows = pq.ParquetFile(path).metadata.num_rows if path.suffix == ".parquet" else None
        outputs[relative] = {
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "bytes": path.stat().st_size,
            "rows": rows,
        }
    payload = {
        "schema_version": SCHEMA_VERSION,
        "feature_list": list(FEATURE_LIST),
        "outputs": outputs,
        "semantic_contract": semantic_contract(),
        "semantic_fingerprint": semantic_fingerprint(),
        "schema_fingerprint": schema_fingerprint(),
    }
    (bundle / "manifest.json").write_text(json.dumps(payload), encoding="utf-8")


def _write_manual_bundle(bundle: Path, *, with_meta: bool = True) -> Path:
    sample_dir = bundle / "samples" / "year=2024"
    sample_dir.mkdir(parents=True)
    pq.write_table(_sample_table(), sample_dir / "part-00000.parquet")
    if with_meta:
        pq.write_table(
            pa.table({"asset_id": ["AAA"], "is_common": [True]}),
            bundle / "meta.parquet",
        )
    _refresh_manifest(bundle)
    return bundle


def _manifest(bundle: Path) -> dict[str, object]:
    return json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))


def _write_manifest(bundle: Path, payload: dict[str, object]) -> None:
    (bundle / "manifest.json").write_text(json.dumps(payload), encoding="utf-8")


def test_query_refuses_unregistered_sample_shard(tmp_path: Path) -> None:
    bundle = _write_manual_bundle(tmp_path / "bundle")
    pq.write_table(
        _sample_table(),
        bundle / "samples" / "year=2024" / "part-00001.parquet",
    )

    with pytest.raises(QuerySamplesError, match="Sample shard is not registered"):
        _bundle_inputs(bundle)


def test_query_refuses_manifest_declared_missing_output(tmp_path: Path) -> None:
    bundle = _write_manual_bundle(tmp_path / "bundle")
    payload = _manifest(bundle)
    outputs = payload["outputs"]
    assert isinstance(outputs, dict)
    outputs["audit/missing.json"] = {"sha256": "0" * 64, "bytes": 1, "rows": None}
    _write_manifest(bundle, payload)

    with pytest.raises(QuerySamplesError, match="manifest output is missing or unreadable"):
        _bundle_inputs(bundle)


def test_query_refuses_present_meta_not_registered_in_manifest(tmp_path: Path) -> None:
    bundle = _write_manual_bundle(tmp_path / "bundle")
    payload = _manifest(bundle)
    outputs = payload["outputs"]
    assert isinstance(outputs, dict)
    outputs.pop("meta.parquet")
    _write_manifest(bundle, payload)

    with pytest.raises(QuerySamplesError, match="meta.parquet is not registered"):
        _bundle_inputs(bundle)


def test_query_refuses_parquet_row_count_mismatch(tmp_path: Path) -> None:
    bundle = _write_manual_bundle(tmp_path / "bundle")
    payload = _manifest(bundle)
    outputs = payload["outputs"]
    assert isinstance(outputs, dict)
    shard_record = outputs["samples/year=2024/part-00000.parquet"]
    assert isinstance(shard_record, dict)
    shard_record["rows"] = 2
    _write_manifest(bundle, payload)

    with pytest.raises(QuerySamplesError, match="manifest output row count mismatch"):
        _bundle_inputs(bundle)


@REQUIRES_DUCKDB
def test_builder_bundle_is_queryable_with_registered_audit_outputs(tmp_path: Path) -> None:
    output = tmp_path / "built-samples"
    result = build_samples(
        FIXTURES / "organized",
        output,
        FIXTURES / "config" / "universes" / "exclusions_v1.json",
        workspace_root=FIXTURES,
        canonical_min_tickers=2,
        staging_tickers=1,
        rank_batch_sessions=6,
    )

    manifest = _manifest(output)
    outputs = manifest["outputs"]
    assert isinstance(outputs, dict)
    for relative in ("splits.json", "qc_report.json"):
        path = output / relative
        record = outputs[relative]
        assert isinstance(record, dict)
        assert record["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
        assert record["bytes"] == path.stat().st_size
        assert record["rows"] is None

    sample_paths = sorted((output / "samples").glob("year=*/*.parquet"))
    rows_by_year = {
        path.parent.name.removeprefix("year="): pq.ParquetFile(path).metadata.num_rows
        for path in sample_paths
    }
    sample_rows = sum(rows_by_year.values())
    meta_rows = pq.ParquetFile(output / "meta.parquet").metadata.num_rows
    assert manifest["row_counts"] == {
        "samples": sample_rows,
        "meta": meta_rows,
        "by_year": rows_by_year,
    }
    assert result["rows"] == sample_rows

    frame = pa.concat_tables([pq.read_table(path) for path in sample_paths])
    sample_dates = frame.column("date").to_pylist()
    assert manifest["date_range"] == {
        "start": min(sample_dates).isoformat(),
        "end": max(sample_dates).isoformat(),
    }

    splits = json.loads((output / "splits.json").read_text(encoding="utf-8"))
    qc = json.loads((output / "qc_report.json").read_text(encoding="utf-8"))
    assert splits["purge_sessions"] == 30
    assert qc["rows_total"] == sample_rows

    result = query_samples(
        output,
        "SELECT COUNT(*) AS sample_rows, (SELECT COUNT(*) FROM meta) AS meta_rows "
        "FROM samples",
    )
    assert result.schema_version == SCHEMA_VERSION
    assert result.columns == ("sample_rows", "meta_rows")
    assert result.rows == ((sample_rows, meta_rows),)
