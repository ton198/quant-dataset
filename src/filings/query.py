"""Bounded, read-only SQL queries over verified filing-archive descriptors.

Descriptor verification checks archive lineage, file hashes, Parquet schemas, and
footer row counts. It does not perform full core-row acceptance or establish
financial-integrity semantics; use the archive's full reader for those guarantees.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .archive import ArchiveError, read_snapshot_descriptors


class FilingQueryError(ValueError):
    """Raised when an archive query cannot be run safely or completely."""


@dataclass(frozen=True)
class ArchiveQueryResult:
    """A bounded result from one archive snapshot.

    The manifest fields identify the snapshot queried. They do not imply full
    core-row or financial-integrity acceptance; only descriptor-level verification
    was performed before the SQL query.
    """

    manifest_version: int
    format: str
    columns: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]


def query_archive(archive: Path, sql: str, limit: int = 20) -> ArchiveQueryResult:
    """Run one SELECT over existing archive tables and return at most ``limit`` rows.

    Only manifest-listed batches for existing ``filings``, ``documents``,
    ``facts``, ``sections``, ``parses``, and ``dependencies`` tables are registered
    as views. Query execution
    uses an in-memory DuckDB database, disables external access and extension
    loading, and places any spill files in a private temporary directory outside
    the archive. Aggregations and ordering can still process the full inputs.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise FilingQueryError("limit must be an integer from 1 through 1000")
    if not isinstance(sql, str):
        raise FilingQueryError("SQL must be a string")

    try:
        import duckdb
    except ImportError as exc:
        raise FilingQueryError(
            "DuckDB is required for filing archive queries; install the query extra "
            "with `uv sync --extra query` or `pip install 'quant-dataset[query]'`"
        ) from exc

    try:
        from query_support.readonly_sql import ReadonlySQLError, validate_select
    except ImportError as exc:
        raise FilingQueryError(
            "The DuckDB SELECT guard is unavailable; install the query extra and use a "
            "DuckDB build with parsed statement classifications"
        ) from exc
    try:
        validate_select(duckdb, sql)
    except ReadonlySQLError as exc:
        raise FilingQueryError(str(exc)) from exc

    try:
        descriptors = read_snapshot_descriptors(archive)
    except ArchiveError as exc:
        raise FilingQueryError(f"Archive descriptor verification failed: {exc}") from exc

    manifest = descriptors["manifest"]
    tables = descriptors["tables"]
    # Parser outputs are optional archive tables: register them only when the
    # current immutable manifest actually lists them, never as empty placeholders.
    view_names = ("filings", "documents", "facts", "sections", "parses", "dependencies")
    files_by_view: dict[str, list[str]] = {}
    for name in view_names:
        descriptor = tables.get(name)
        if descriptor is not None:
            files_by_view[name] = [str(batch["path"]) for batch in descriptor["batches"]]

    allowed_paths = [path for paths in files_by_view.values() for path in paths]
    temp_parent = Path("/tmp/opencode")
    if not temp_parent.is_dir():
        raise FilingQueryError("Private DuckDB spill directory /tmp/opencode is unavailable")

    try:
        with tempfile.TemporaryDirectory(prefix="filings-duckdb-", dir=temp_parent) as spill_dir:
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
                # Paths are bound via DuckDB's settings API and passed through its
                # relation API; they are never interpolated into SQL text.
                connection.execute("SET allowed_paths = ?", [allowed_paths])
                connection.execute("SET enable_external_access = false")
                for name, paths in files_by_view.items():
                    connection.read_parquet(paths, hive_partitioning=False).create_view(name)
                relation = connection.sql(sql)
                columns = tuple(str(name) for name in relation.columns)
                rows = tuple(tuple(row) for row in relation.limit(limit).fetchall())
                return ArchiveQueryResult(
                    manifest_version=int(manifest["manifest_version"]),
                    format=str(manifest["format"]),
                    columns=columns,
                    rows=rows,
                )
            except FilingQueryError:
                raise
            except Exception as exc:
                raise FilingQueryError(f"Archive query failed: {exc}") from exc
            finally:
                connection.close()
    except FilingQueryError:
        raise
    except Exception as exc:
        raise FilingQueryError(f"Could not initialize the read-only archive query: {exc}") from exc
