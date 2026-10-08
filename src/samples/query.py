"""Bounded, read-only SQL access to an existing sample bundle."""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from query_support.readonly_sql import ReadonlySQLError, validate_select

from .validation import (
    SamplesValidationError,
    validate_manifest,
    validate_output_hashes,
    validate_sample_schema,
)


class QuerySamplesError(ValueError):
    """Raised when a bundle or query cannot be used safely."""


@dataclass(frozen=True)
class QueryResult:
    """A bounded query result together with the bundle's declared schema version."""

    schema_version: str
    columns: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]


def _inside_bundle(path: Path, bundle: Path, description: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise QuerySamplesError(f"Cannot resolve {description}: {path}") from exc
    if not resolved.is_relative_to(bundle):
        raise QuerySamplesError(f"{description} resolves outside the bundle: {path}")
    if not resolved.is_file():
        raise QuerySamplesError(f"{description} is not a regular file: {path}")
    return resolved


def _bundle_inputs(bundle_dir: str | Path) -> tuple[Path, str, list[Path], Path | None]:
    requested = Path(bundle_dir).expanduser()
    try:
        bundle = requested.resolve(strict=True)
    except OSError as exc:
        raise QuerySamplesError(f"Bundle directory does not exist: {requested}") from exc
    if not bundle.is_dir():
        raise QuerySamplesError(f"Bundle path is not a directory: {requested}")

    manifest_path = _inside_bundle(bundle / "manifest.json", bundle, "manifest.json")
    try:
        manifest = validate_manifest(json.loads(manifest_path.read_text(encoding="utf-8")))
        declared_outputs = validate_output_hashes(bundle, manifest)
    except (OSError, UnicodeError, json.JSONDecodeError, SamplesValidationError) as exc:
        raise QuerySamplesError(f"Invalid sample manifest or outputs: {exc}") from exc
    schema_version = str(manifest["schema_version"])

    samples_dir = bundle / "samples"
    try:
        resolved_samples_dir = samples_dir.resolve(strict=True)
    except OSError as exc:
        raise QuerySamplesError(f"Sample directory is missing: {samples_dir}") from exc
    if not resolved_samples_dir.is_relative_to(bundle) or not resolved_samples_dir.is_dir():
        raise QuerySamplesError(
            f"Sample directory is invalid or resolves outside the bundle: {samples_dir}"
        )
    sample_paths = sorted(samples_dir.glob("year=*/part-*.parquet"), key=lambda path: str(path))
    if not sample_paths:
        raise QuerySamplesError(f"No sample shards found at {samples_dir}/year=*/part-*.parquet")
    sample_files = [_inside_bundle(path, bundle, "sample shard") for path in sample_paths]
    declared_paths = set(declared_outputs.values())
    if any(path not in declared_paths for path in sample_files):
        raise QuerySamplesError("Sample shard is not registered in manifest.json outputs")

    try:
        import pyarrow.parquet as pq

        reference_schema = pq.read_schema(sample_files[0])
        validate_sample_schema(reference_schema)
        expected_names = reference_schema.names
        expected_types = [field.type for field in reference_schema]
        for path in sample_files[1:]:
            current_schema = pq.read_schema(path)
            if (
                current_schema.names != expected_names
                or [field.type for field in current_schema] != expected_types
            ):
                raise QuerySamplesError(
                    "Sample shards have incompatible physical schemas "
                    "(field names, types, or order differ)"
                )
            validate_sample_schema(current_schema)
    except SamplesValidationError as exc:
        raise QuerySamplesError(str(exc)) from exc
    except QuerySamplesError:
        raise
    except Exception as exc:
        raise QuerySamplesError(f"Could not read sample shard schemas: {exc}") from exc

    meta_path = bundle / "meta.parquet"
    meta_file: Path | None = None
    if meta_path.exists() or meta_path.is_symlink():
        meta_file = _inside_bundle(meta_path, bundle, "meta.parquet")
        if meta_file not in declared_paths:
            raise QuerySamplesError("meta.parquet is not registered in manifest.json outputs")
    return bundle, schema_version, sample_files, meta_file


def query_samples(
    bundle_dir: str | Path,
    sql: str,
    limit: int = 20,
) -> QueryResult:
    """Run one SELECT against bundle views and return at most ``limit`` rows.

    DuckDB runs in an in-memory database, cannot access external files beyond the
    explicitly registered Parquet inputs, and has extension loading disabled. The
    result relation is limited before fetching, so only bounded output rows are
    materialized in Python. Aggregations and ordering may still process full inputs.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise QuerySamplesError("limit must be an integer from 1 through 1000")
    if not isinstance(sql, str):
        raise QuerySamplesError("SQL must be a string")

    try:
        import duckdb
    except ImportError as exc:
        raise QuerySamplesError(
            "DuckDB is required for query-samples; install it with `uv sync --extra query` "
            "or `pip install 'quant-dataset[query]'`"
        ) from exc

    try:
        validate_select(duckdb, sql)
    except ReadonlySQLError as exc:
        raise QuerySamplesError(str(exc)) from exc
    bundle, schema_version, sample_files, meta_file = _bundle_inputs(bundle_dir)
    allowed_files = [str(path) for path in sample_files]
    if meta_file is not None:
        allowed_files.append(str(meta_file))

    temp_parent = Path("/tmp/opencode")
    temp_options: dict[str, str] = {}
    if temp_parent.is_dir():
        temp_options["dir"] = str(temp_parent)

    try:
        with tempfile.TemporaryDirectory(
            prefix="quant-dataset-duckdb-", **temp_options
        ) as spill_dir:
            connection = duckdb.connect(
                database=":memory:",
                config={
                    "memory_limit": "1GB",
                    "threads": "2",
                    "temp_directory": spill_dir,
                    "autoinstall_known_extensions": "false",
                    "autoload_known_extensions": "false",
                },
            )
            try:
                # Bind the file list through DuckDB's setting API; this is not SQL
                # generated from user-provided bundle paths.
                connection.execute("SET allowed_paths = ?", [allowed_files])
                connection.execute("SET enable_external_access = false")
                connection.read_parquet(
                    allowed_files[: len(sample_files)], hive_partitioning=False
                ).create_view("samples")
                if meta_file is not None:
                    connection.read_parquet(str(meta_file), hive_partitioning=False).create_view(
                        "meta"
                    )
                relation = connection.sql(sql)
                columns = tuple(str(name) for name in relation.columns)
                bounded_relation = relation.limit(limit)
                rows = tuple(tuple(row) for row in bounded_relation.fetchall())
                return QueryResult(schema_version=schema_version, columns=columns, rows=rows)
            except QuerySamplesError:
                raise
            except Exception as exc:
                detail = str(exc)
                if meta_file is None and "meta" in sql.casefold():
                    detail = f"{detail} (this bundle has no optional meta.parquet or meta view)"
                raise QuerySamplesError(f"Query failed: {detail}") from exc
            finally:
                connection.close()
    except QuerySamplesError:
        raise
    except Exception as exc:
        raise QuerySamplesError(f"Could not initialize the read-only DuckDB query: {exc}") from exc
