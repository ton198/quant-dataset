"""Offline tests for bounded read-only SQL queries over filing archives."""

from __future__ import annotations

import builtins
import hashlib
import importlib.util
import sys
from datetime import date
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from filings import archive as archive_module  # noqa: E402
from filings.archive import open_archive  # noqa: E402
from filings.models import DOCUMENTS_SCHEMA, FILINGS_SCHEMA, RunSpec, filing_id  # noqa: E402
from filings.parsing_models import FACT_SCHEMA, SECTION_SCHEMA  # noqa: E402
from filings.processing import DEPENDENCIES_SCHEMA, PARSES_SCHEMA  # noqa: E402
from filings.query import FilingQueryError, query_archive  # noqa: E402

REQUIRES_DUCKDB = pytest.mark.skipif(
    importlib.util.find_spec("duckdb") is None,
    reason="DuckDB is optional; install the query extra to run archive query tests",
)


CIK = "0000000123"
HISTORIC_ACCESSION = "0000999999-23-000001"
BASE_ACCESSION = "0000999999-24-000001"
AMENDMENT_ACCESSION = "0000999999-24-000002"
HISTORIC_ID = filing_id(CIK, HISTORIC_ACCESSION)
BASE_ID = filing_id(CIK, BASE_ACCESSION)
AMENDMENT_ID = filing_id(CIK, AMENDMENT_ACCESSION)


def _filing_row(
    accession: str,
    source: Any,
    *,
    form: str,
    filed: date,
    visible: date,
    parent: str | None = None,
) -> dict[str, Any]:
    identity = filing_id(CIK, accession)
    return {
        "filing_id": identity,
        "cik10": CIK,
        "accession_number": accession,
        "form": form,
        "filed_date": filed,
        "report_period_end": date(2023, 12, 31),
        "acceptance_datetime_raw": None,
        "acceptance_datetime_utc": None,
        "effective_visible_session": visible,
        "is_amendment": form.endswith("/A"),
        "parent_filing_id": parent,
        "parent_link_source": "fixture" if parent else None,
        "primary_document_name": "annual.htm",
        "scope_status": "included",
        "inventory_status": "known",
        "raw_coverage_status": "partial",
        "source_submission_logical_key": "submissions:0000000123",
        "source_submission_sha256": source.sha256,
        "source_submission_path": source.path,
        "source_submission_locator": "filings.recent[0]",
    }


def _create_archive(
    root: Path,
    *,
    include_facts: bool = True,
    include_sections: bool = True,
    publish: bool = True,
) -> Path:
    protected = root.parent / "protected-input"
    protected.mkdir(parents=True, exist_ok=True)
    spec = RunSpec(input_fingerprint="a" * 64)
    with open_archive(root, spec, protected_paths=(protected,)) as writer:
        submission = writer.put_raw_bytes(b"small local submission fixture")
        filings = pa.Table.from_pylist(
            [
                _filing_row(
                    HISTORIC_ACCESSION,
                    submission,
                    form="10-Q",
                    filed=date(2023, 12, 29),
                    visible=date(2024, 1, 2),
                ),
                _filing_row(
                    BASE_ACCESSION,
                    submission,
                    form="10-K",
                    filed=date(2024, 4, 12),
                    visible=date(2024, 4, 15),
                ),
                _filing_row(
                    AMENDMENT_ACCESSION,
                    submission,
                    form="10-K/A",
                    filed=date(2024, 4, 15),
                    visible=date(2024, 4, 16),
                    parent=BASE_ID,
                ),
            ],
            schema=FILINGS_SCHEMA,
        )
        documents = pa.Table.from_pylist([], schema=DOCUMENTS_SCHEMA)
        tables: dict[str, pa.Table] = {"filings": filings, "documents": documents}
        if include_facts:
            tables["facts"] = pa.Table.from_pylist(
                [
                    {
                        "filing_id": HISTORIC_ID,
                        "fact_qname": "us-gaap:Revenue",
                        "normalized_numeric": "70",
                    },
                    {
                        "filing_id": BASE_ID,
                        "fact_qname": "us-gaap:Revenue",
                        "normalized_numeric": "100",
                    },
                    {
                        "filing_id": AMENDMENT_ID,
                        "fact_qname": "us-gaap:Revenue",
                        "normalized_numeric": "110",
                    },
                ],
                schema=FACT_SCHEMA,
            )
        if include_sections:
            tables["sections"] = pa.Table.from_pylist(
                [
                    {
                        "filing_id": BASE_ID,
                        "section_kind": "item_7",
                        "heading": "Management discussion",
                        "content_text": "Fixture text only.",
                    }
                ],
                schema=SECTION_SCHEMA,
            )
        if publish:
            writer.commit(
                tables=tables,
                raw_objects=(submission,),
                expected_manifest_version=0,
            )
    return root


def _file_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


@REQUIRES_DUCKDB
def test_archive_queries_cover_current_tables_and_user_authored_asof_filters(
    tmp_path: Path,
) -> None:
    root = _create_archive(tmp_path / "archive")
    before = _file_hashes(root)

    count = query_archive(root, "SELECT COUNT(*) AS filing_count FROM filings")
    assert count.manifest_version == 1
    assert count.format == "filings-core-archive"
    assert count.columns == ("filing_count",)
    assert count.rows == ((3,),)  # No implicit year or latest-filing filter.

    aggregate = query_archive(
        root,
        "SELECT EXTRACT(YEAR FROM filed_date)::INTEGER AS filing_year, COUNT(*) AS n "
        "FROM filings GROUP BY filing_year ORDER BY filing_year",
    )
    assert aggregate.rows == ((2023, 1), (2024, 2))

    joined = query_archive(
        root,
        "SELECT f.accession_number, x.normalized_numeric "
        "FROM filings AS f JOIN facts AS x USING (filing_id) "
        "WHERE x.fact_qname = 'us-gaap:Revenue' ORDER BY f.filed_date, f.accession_number",
    )
    assert joined.rows == (
        (HISTORIC_ACCESSION, "70"),
        (BASE_ACCESSION, "100"),
        (AMENDMENT_ACCESSION, "110"),
    )

    before_amendment_visible = query_archive(
        root,
        "SELECT f.accession_number, x.normalized_numeric "
        "FROM filings AS f JOIN facts AS x USING (filing_id) "
        "WHERE f.effective_visible_session <= DATE '2024-04-15' "
        "AND x.fact_qname = 'us-gaap:Revenue' "
        "ORDER BY f.effective_visible_session, f.accession_number",
    )
    after_amendment_visible = query_archive(
        root,
        "SELECT f.accession_number, x.normalized_numeric "
        "FROM filings AS f JOIN facts AS x USING (filing_id) "
        "WHERE f.effective_visible_session <= DATE '2024-04-16' "
        "AND x.fact_qname = 'us-gaap:Revenue' "
        "ORDER BY f.effective_visible_session, f.accession_number",
    )
    assert before_amendment_visible.rows == (
        (HISTORIC_ACCESSION, "70"),
        (BASE_ACCESSION, "100"),
    )
    assert after_amendment_visible.rows == (
        (HISTORIC_ACCESSION, "70"),
        (BASE_ACCESSION, "100"),
        (AMENDMENT_ACCESSION, "110"),
    )

    section = query_archive(root, "SELECT section_kind, heading FROM sections")
    assert section.rows == (("item_7", "Management discussion"),)
    assert _file_hashes(root) == before
    assert not list(root.rglob("*.duckdb"))


@REQUIRES_DUCKDB
def test_result_limit_cte_comments_duplicate_labels_and_unicode_paths(tmp_path: Path) -> None:
    root = _create_archive(tmp_path / "issuer's café" / "archive")
    sql = """-- leading comment
        WITH selected AS (
            SELECT filing_id FROM filings ORDER BY accession_number LIMIT 3
        )
        SELECT f.form AS duplicate, s.filing_id AS duplicate
        FROM filings AS f JOIN selected AS s USING (filing_id)
        ORDER BY f.accession_number
        LIMIT 3; -- trailing comment
    """

    result = query_archive(root, sql, limit=1)

    assert result.columns == ("duplicate", "duplicate")
    assert result.rows == (("10-Q", HISTORIC_ID),)


@REQUIRES_DUCKDB
def test_duckdb_is_memory_bounded_and_reads_only_explicit_manifest_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import duckdb

    root = _create_archive(tmp_path / "archive")
    descriptors = archive_module.read_snapshot_descriptors(root)
    expected_paths = {
        name: [str(batch["path"]) for batch in descriptors["tables"][name]["batches"]]
        for name in ("filings", "documents", "facts", "sections")
        if name in descriptors["tables"]
    }
    observed: dict[str, Any] = {"settings": [], "reads": []}
    real_connect = duckdb.connect

    class ConnectionSpy:
        def __init__(self, connection: Any) -> None:
            self.connection = connection

        def execute(self, statement: str, parameters: Any = None) -> Any:
            observed["settings"].append((statement, parameters))
            return self.connection.execute(statement, parameters)

        def read_parquet(self, paths: list[str], **kwargs: Any) -> Any:
            observed["reads"].append((list(paths), kwargs))
            return self.connection.read_parquet(paths, **kwargs)

        def sql(self, statement: str) -> Any:
            return self.connection.sql(statement)

        def close(self) -> None:
            self.connection.close()

    def connect(*args: Any, **kwargs: Any) -> ConnectionSpy:
        observed["database"] = kwargs["database"]
        observed["config"] = dict(kwargs["config"])
        assert Path(observed["config"]["temp_directory"]).is_dir()
        return ConnectionSpy(real_connect(*args, **kwargs))

    monkeypatch.setattr(duckdb, "connect", connect)
    result = query_archive(root, "SELECT COUNT(*) AS n FROM facts")

    assert result.rows == ((3,),)
    assert observed["database"] == ":memory:"
    config = observed["config"]
    assert config["memory_limit"] == "1GB"
    assert config["threads"] == "2"
    assert config["autoinstall_known_extensions"] == "false"
    assert config["autoload_known_extensions"] == "false"
    assert Path(config["temp_directory"]).parent == Path("/tmp/opencode")
    assert not Path(config["temp_directory"]).exists()
    assert observed["settings"] == [
        ("SET allowed_paths = ?", [[path for paths in expected_paths.values() for path in paths]]),
        ("SET enable_external_access = false", None),
    ]
    assert observed["reads"] == [
        (paths, {"hive_partitioning": False}) for paths in expected_paths.values()
    ]


@pytest.mark.parametrize("limit", [0, -1, 1001, True, 1.5])
def test_invalid_limits_are_rejected(tmp_path: Path, limit: object) -> None:
    with pytest.raises(FilingQueryError, match="limit must be an integer"):
        query_archive(tmp_path / "not-read", "SELECT 1", limit=limit)  # type: ignore[arg-type]


@REQUIRES_DUCKDB
@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM filings",
        "PRAGMA version",
        "SELECT 1; SELECT 2",
        "SELECT * FROM read_csv_auto('/etc/passwd')",
        "SELECT * FROM read_parquet('/etc/passwd')",
    ],
)
def test_mutations_pragma_multiple_statements_and_external_files_are_rejected(
    tmp_path: Path, sql: str
) -> None:
    root = _create_archive(tmp_path / "archive")
    before = _file_hashes(root)
    with pytest.raises(FilingQueryError):
        query_archive(root, sql)
    assert _file_hashes(root) == before


@REQUIRES_DUCKDB
def test_query_exposes_existing_parser_and_dependency_tables_only(tmp_path: Path) -> None:
    root = _create_archive(tmp_path / "archive")
    # Missing tables do not get placeholder views.
    with pytest.raises(FilingQueryError):
        query_archive(root, "SELECT * FROM parses")
    with pytest.raises(FilingQueryError):
        query_archive(root, "SELECT * FROM dependencies")

    parse_table = pa.Table.from_pylist(
        [
            {
                "parse_id": "p" * 64,
                "filing_id": BASE_ID,
                "document_id": "d" * 64,
                "parser_name": "filings.xbrl",
                "parser_version": "fixture-v1",
                "status": "partial",
                "validation_scope": "not_performed",
                "source_sha256": "a" * 64,
                "dependencies_fingerprint": "b" * 64,
                "fact_count": 0,
                "section_count": 0,
                "errors_json": '[{"code":"missing_dependency"}]',
                "notes_json": "{}",
            }
        ],
        schema=PARSES_SCHEMA,
    )
    dependency_table = pa.Table.from_pylist(
        [
            {
                "filing_id": BASE_ID,
                "dependency_fingerprint": "b" * 64,
                "requested_url": "https://taxonomy.example.test/base.xsd",
                "final_url": None,
                "transport_url": None,
                "sha256": None,
                "byte_size": None,
                "raw_path": None,
                "status": "error",
                "diagnostic_code": "missing_dependency",
                "provenance_json": '{"bytes_persisted":false}',
            }
        ],
        schema=DEPENDENCIES_SCHEMA,
    )
    with open_archive(
        root,
        RunSpec(input_fingerprint="a" * 64),
        protected_paths=(tmp_path / "protected-input",),
        resume=True,
    ) as writer:
        snapshot = writer.commit(
            tables={"parses": parse_table, "dependencies": dependency_table},
            raw_objects=(),
            expected_manifest_version=1,
        )

    parsed = query_archive(
        root,
        "SELECT parser_name, status, fact_count FROM parses ORDER BY parser_name",
    )
    dependencies = query_archive(
        root,
        "SELECT status, diagnostic_code FROM dependencies",
    )
    fingerprints = query_archive(
        root,
        "SELECT p.dependencies_fingerprint, d.dependency_fingerprint "
        "FROM parses AS p JOIN dependencies AS d USING (filing_id)",
    )
    diagnostics = query_archive(root, "SELECT errors_json FROM parses WHERE status = 'partial'")
    assert parsed.rows == (("filings.xbrl", "partial", 0),)
    assert dependencies.rows == (("error", "missing_dependency"),)
    assert fingerprints.rows == (("b" * 64, "b" * 64),)
    assert diagnostics.rows == (('[{"code":"missing_dependency"}]',),)
    assert snapshot.manifest_version == 2


@REQUIRES_DUCKDB
def test_missing_archive_table_does_not_create_an_empty_placeholder(tmp_path: Path) -> None:
    root = _create_archive(tmp_path / "archive", include_facts=False, include_sections=False)
    assert query_archive(root, "SELECT COUNT(*) FROM filings").rows == ((3,),)
    for missing in ("facts", "parses", "dependencies"):
        with pytest.raises(FilingQueryError, match=f"{missing}|Query failed|Binder Error"):
            query_archive(root, f"SELECT * FROM {missing}")


def test_incomplete_and_corrupt_descriptor_archives_fail_closed(tmp_path: Path) -> None:
    incomplete = _create_archive(tmp_path / "incomplete", publish=False)
    with pytest.raises(FilingQueryError, match="descriptor verification"):
        query_archive(incomplete, "SELECT 1")

    corrupt = _create_archive(tmp_path / "corrupt")
    descriptors = archive_module.read_snapshot_descriptors(corrupt)
    batch_path = descriptors["tables"]["facts"]["batches"][0]["path"]
    batch_path.write_bytes(b"tampered Parquet bytes")
    with pytest.raises(FilingQueryError, match="descriptor verification.*hash or byte_size"):
        query_archive(corrupt, "SELECT COUNT(*) FROM filings")


@REQUIRES_DUCKDB
def test_query_uses_descriptor_paths_without_arrow_table_materialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _create_archive(tmp_path / "archive")
    real_parquet_file = archive_module.pq.ParquetFile

    class FooterOnlyParquetFile:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self._reader = real_parquet_file(*args, **kwargs)

        @property
        def schema_arrow(self) -> pa.Schema:
            return self._reader.schema_arrow

        @property
        def metadata(self) -> Any:
            return self._reader.metadata

        def read(self, *args: Any, **kwargs: Any) -> Any:
            raise AssertionError("query path must not read complete Arrow tables")

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("query path must not materialize full Arrow tables")

    monkeypatch.setattr(archive_module, "read_snapshot", forbidden)
    monkeypatch.setattr(archive_module.pq, "read_table", forbidden)
    monkeypatch.setattr(archive_module.pq, "ParquetFile", FooterOnlyParquetFile)

    assert query_archive(root, "SELECT COUNT(*) FROM facts").rows == ((3,),)


def test_missing_duckdb_reports_query_extra_without_loading_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_import = builtins.__import__

    def import_without_duckdb(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "duckdb":
            raise ImportError("simulated missing DuckDB")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_duckdb)
    with pytest.raises(FilingQueryError, match="uv sync --extra query"):
        query_archive(tmp_path / "not-read", "SELECT 1")
