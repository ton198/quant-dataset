from __future__ import annotations

import builtins
import csv
import hashlib
import importlib.util
import json
import sys
import tempfile
from datetime import date
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cli import main as cli_main
from samples.contracts import (
    FEATURE_LIST,
    SAMPLE_SCHEMA,
    schema_fingerprint,
    semantic_contract,
    semantic_fingerprint,
)
from samples.query import QuerySamplesError, query_samples

REQUIRES_DUCKDB = pytest.mark.skipif(
    importlib.util.find_spec("duckdb") is None,
    reason="DuckDB is an optional dependency; install the query extra to run these tests",
)


def _sample_table(days: list[date], assets: list[str], values: list[float]) -> pa.Table:
    arrays = []
    for field in SAMPLE_SCHEMA:
        if field.name == "date":
            arrays.append(pa.array(days, type=field.type))
        elif field.name == "asset_id":
            arrays.append(pa.array(assets, type=field.type))
        elif field.name == "is_common":
            arrays.append(pa.array([True] * len(days), type=field.type))
        elif field.name == "flag_extreme_label":
            arrays.append(pa.array([0] * len(days), type=field.type))
        elif field.name == "f_raw_return_1d":
            arrays.append(pa.array(values, type=field.type))
        elif field.name == "miss_return_1d":
            arrays.append(pa.array([0] * len(days), type=field.type))
        elif field.name.startswith("miss_"):
            arrays.append(pa.array([1] * len(days), type=field.type))
        elif field.name == "target_return_1d":
            arrays.append(pa.array(values, type=field.type))
        else:
            arrays.append(pa.nulls(len(days), type=field.type))
    return pa.Table.from_arrays(arrays, schema=SAMPLE_SCHEMA)


def _refresh_manifest(directory: Path, schema_version: str = "samples") -> None:
    outputs = {}
    for path in sorted(directory.rglob("*")):
        if not path.is_file() or path.name == "manifest.json":
            continue
        relative = path.relative_to(directory).as_posix()
        row_count = pq.ParquetFile(path).metadata.num_rows if path.suffix == ".parquet" else None
        outputs[relative] = {
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "bytes": path.stat().st_size,
            "rows": row_count,
        }
    (directory / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": schema_version,
                "feature_list": list(FEATURE_LIST),
                "outputs": outputs,
                "semantic_contract": semantic_contract(),
                "semantic_fingerprint": semantic_fingerprint(),
                "schema_fingerprint": schema_fingerprint(),
            }
        ),
        encoding="utf-8",
    )


def _write_bundle(
    directory: Path,
    *,
    schema_version: str = "samples",
    with_meta: bool = True,
    two_shards: bool = True,
) -> Path:
    sample_dir = directory / "samples" / "year=2024"
    sample_dir.mkdir(parents=True)
    pq.write_table(
        _sample_table([date(2024, 1, 2), date(2024, 1, 3)], ["AAA", "BBB"], [10.0, 20.0]),
        sample_dir / "part-00000.parquet",
    )
    if two_shards:
        pq.write_table(
            _sample_table([date(2024, 1, 4)], ["AAA"], [30.0]),
            sample_dir / "part-00001.parquet",
        )
    if with_meta:
        pq.write_table(
            pa.table({"asset_id": ["AAA", "BBB"], "is_common": [True, True]}),
            directory / "meta.parquet",
        )
    _refresh_manifest(directory, schema_version)
    return directory


def _bundle_hashes(bundle: Path) -> dict[str, str]:
    return {
        str(path.relative_to(bundle)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(bundle.rglob("*"))
        if path.is_file()
    }


@REQUIRES_DUCKDB
def test_query_current_bundle_and_basic_selects(tmp_path: Path) -> None:
    bundle = _write_bundle(tmp_path / "bundle")

    count = query_samples(bundle, "SELECT COUNT(*) AS rows FROM samples")
    assert count.schema_version == "samples"
    assert count.columns == ("rows",)
    assert count.rows == ((3,),)

    filtered = query_samples(
        bundle,
        "SELECT asset_id, target_return_1d FROM samples WHERE date >= DATE '2024-01-03' "
        "ORDER BY date, asset_id",
    )
    assert filtered.rows == (("BBB", 20.0), ("AAA", 30.0))

    cte = query_samples(
        bundle,
        "WITH recent AS (SELECT asset_id FROM samples WHERE date >= DATE '2024-01-04') "
        "SELECT asset_id FROM recent",
    )
    assert cte.rows == (("AAA",),)

    joined = query_samples(
        bundle,
        "SELECT s.asset_id, m.is_common FROM samples AS s JOIN meta AS m USING (asset_id) "
        "WHERE s.date = DATE '2024-01-02' ORDER BY s.asset_id",
    )
    assert joined.rows == (("AAA", True),)


@REQUIRES_DUCKDB
def test_result_limit_and_single_semicolon_inside_string(tmp_path: Path) -> None:
    bundle = _write_bundle(tmp_path / "bundle")
    before = _bundle_hashes(bundle)

    result = query_samples(
        bundle,
        "SELECT asset_id FROM samples ORDER BY asset_id; -- only one statement",
        limit=1,
    )
    assert result.rows == (("AAA",),)
    literal = query_samples(bundle, "SELECT ';' AS punctuation")
    assert literal.rows == ((";",),)
    assert _bundle_hashes(bundle) == before


@REQUIRES_DUCKDB
def test_outer_limit_wraps_cte_with_own_limit_comments_and_trailing_semicolon(
    tmp_path: Path,
) -> None:
    bundle = _write_bundle(tmp_path / "bundle")
    sql = """-- leading comment
        WITH selected AS (
            SELECT date, asset_id, target_return_1d FROM samples
            ORDER BY date, asset_id
            LIMIT 10
        )
        SELECT asset_id, target_return_1d FROM selected
        ORDER BY date, asset_id
        LIMIT 10; -- trailing comment
    """

    result = query_samples(bundle, sql, limit=2)

    assert result.columns == ("asset_id", "target_return_1d")
    assert result.rows == (("AAA", 10.0), ("BBB", 20.0))


@REQUIRES_DUCKDB
def test_duckdb_limit_is_applied_before_bounded_fetch_and_keeps_duplicate_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import duckdb

    bundle = _write_bundle(tmp_path / "bundle")
    user_sql = (
        "SELECT i AS duplicate, (i + 1) AS duplicate FROM range(100_000) AS input(i) ORDER BY i"
    )
    events: list[str] = []
    executed_sql: list[str] = []
    real_connect = duckdb.connect

    class LimitedRelationSpy:
        def __init__(self, relation: object) -> None:
            self._relation = relation

        def fetchall(self) -> list[tuple[object, ...]]:
            assert events[-1] == "limit:7"
            events.append("fetchall")
            return self._relation.fetchall()

    class RelationSpy:
        def __init__(self, relation: object) -> None:
            self._relation = relation

        @property
        def columns(self) -> list[str]:
            events.append("columns")
            return self._relation.columns

        def limit(self, count: int) -> LimitedRelationSpy:
            events.append(f"limit:{count}")
            return LimitedRelationSpy(self._relation.limit(count))

    class ConnectionSpy:
        def __init__(self, connection: object) -> None:
            self._connection = connection

        def execute(self, statement: str, *args: object, **kwargs: object) -> object:
            executed_sql.append(statement)
            return self._connection.execute(statement, *args, **kwargs)

        def sql(self, statement: str) -> RelationSpy:
            events.append("connection.sql")
            assert statement == user_sql
            return RelationSpy(self._connection.sql(statement))

        def __getattr__(self, name: str) -> object:
            return getattr(self._connection, name)

    monkeypatch.setattr(
        duckdb,
        "connect",
        lambda *args, **kwargs: ConnectionSpy(real_connect(*args, **kwargs)),
    )
    result = query_samples(bundle, user_sql, limit=7)

    assert result.columns == ("duplicate", "duplicate")
    assert result.rows == tuple((number, number + 1) for number in range(7))
    assert events.index("limit:7") < events.index("fetchall")
    assert executed_sql == ["SET allowed_paths = ?", "SET enable_external_access = false"]


@REQUIRES_DUCKDB
def test_bundle_path_with_spaces_apostrophe_and_unicode(tmp_path: Path) -> None:
    bundle = _write_bundle(tmp_path / "sample bundle '量子'")

    result = query_samples(bundle, "SELECT COUNT(*) FROM samples")
    assert result.rows == ((3,),)


@REQUIRES_DUCKDB
def test_mixed_physical_shard_schemas_are_rejected(tmp_path: Path) -> None:
    bundle = _write_bundle(tmp_path / "bundle", two_shards=False)
    pq.write_table(
        pa.table({"date": [date(2024, 1, 4)], "asset_id": ["AAA"], "value": ["30"]}),
        bundle / "samples" / "year=2024" / "part-00001.parquet",
    )
    _refresh_manifest(bundle)

    with pytest.raises(QuerySamplesError, match="incompatible physical schemas"):
        query_samples(bundle, "SELECT COUNT(*) FROM samples")


@REQUIRES_DUCKDB
@pytest.mark.parametrize(
    "manifest",
    [
        "[]",
        "{}",
        '{"schema_version": 3}',
        '{"schema_version": "  "}',
        '{"schema_version": "legacy-product", "feature_list": []}',
        '{"schema_version": "samples", "feature_list": "not-a-list", "outputs": {}}',
        '{"schema_version": "samples", "feature_list": [7], "outputs": {}}',
        '{"schema_version": "samples", "feature_list": ["duplicate", "duplicate"], "outputs": {}}',
    ],
)
def test_invalid_manifest_metadata_is_rejected(tmp_path: Path, manifest: str) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "manifest.json").write_text(manifest, encoding="utf-8")

    with pytest.raises(QuerySamplesError, match="manifest"):
        query_samples(bundle, "SELECT 1")


@REQUIRES_DUCKDB
@pytest.mark.parametrize("query_kind", ["missing_manifest", "missing_samples"])
def test_missing_manifest_and_samples_have_clear_errors(tmp_path: Path, query_kind: str) -> None:
    bundle = tmp_path / query_kind
    bundle.mkdir()
    if query_kind == "missing_manifest":
        (bundle / "samples" / "year=2024").mkdir(parents=True)
        pq.write_table(
            pa.table({"ticker": ["AAA"]}),
            bundle / "samples" / "year=2024" / "part-00000.parquet",
        )
        expected = "manifest.json"
    else:
        _refresh_manifest(bundle)
        expected = "Sample directory is missing"

    with pytest.raises(QuerySamplesError, match=expected):
        query_samples(bundle, "SELECT 1")


@REQUIRES_DUCKDB
def test_missing_optional_meta_has_no_view_and_reports_reason(tmp_path: Path) -> None:
    bundle = _write_bundle(tmp_path / "bundle", with_meta=False)

    with pytest.raises(QuerySamplesError, match="no optional meta.parquet or meta view"):
        query_samples(bundle, "SELECT * FROM meta")


@REQUIRES_DUCKDB
def test_symlinked_sample_shard_cannot_escape_bundle(tmp_path: Path) -> None:
    bundle = _write_bundle(tmp_path / "bundle", two_shards=False)
    original = bundle / "samples" / "year=2024" / "part-00000.parquet"
    outside = tmp_path / "outside.parquet"
    pq.write_table(pa.table({"ticker": ["OUT"]}), outside)
    original.unlink()
    try:
        original.symlink_to(outside)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlinks are not available: {exc}")

    with pytest.raises(QuerySamplesError, match="resolves outside the bundle"):
        query_samples(bundle, "SELECT COUNT(*) FROM samples")


@REQUIRES_DUCKDB
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1; SELECT 2",
        "SELECT 1; SELECT ';'",
        "CREATE TABLE changed AS SELECT 1",
        "DELETE FROM samples",
        "COPY samples TO '/tmp/query-samples-copy.parquet'",
        "INSTALL httpfs",
        "LOAD httpfs",
        "ATTACH ':memory:' AS other",
        "PRAGMA version",
    ],
)
def test_only_one_select_is_accepted_and_failed_queries_do_not_write(
    tmp_path: Path, sql: str
) -> None:
    bundle = _write_bundle(tmp_path / "bundle")
    before = _bundle_hashes(bundle)

    with pytest.raises(QuerySamplesError):
        query_samples(bundle, sql)

    assert _bundle_hashes(bundle) == before
    assert not (tmp_path / "query-samples-copy.parquet").exists()


@REQUIRES_DUCKDB
def test_external_file_access_is_blocked(tmp_path: Path) -> None:
    bundle = _write_bundle(tmp_path / "bundle")
    outside = tmp_path / "outside.parquet"
    pq.write_table(pa.table({"secret": [42]}), outside)
    outside_literal = str(outside).replace("'", "''")

    with pytest.raises(QuerySamplesError, match="Query failed"):
        query_samples(bundle, f"SELECT * FROM read_parquet('{outside_literal}')")


@REQUIRES_DUCKDB
def test_network_access_and_implicit_http_extension_loading_are_blocked(tmp_path: Path) -> None:
    bundle = _write_bundle(tmp_path / "bundle")

    with pytest.raises(QuerySamplesError, match="Query failed"):
        query_samples(
            bundle,
            "SELECT * FROM read_csv_auto('https://example.invalid/private.csv')",
        )


@REQUIRES_DUCKDB
def test_duckdb_uses_bounded_ephemeral_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import duckdb

    bundle = _write_bundle(tmp_path / "bundle")
    observed: dict[str, object] = {}
    real_connect = duckdb.connect

    def inspect_connect(*args: object, **kwargs: object) -> object:
        observed.update(kwargs["config"])
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(duckdb, "connect", inspect_connect)
    result = query_samples(bundle, "SELECT COUNT(*) FROM samples")
    assert result.rows == ((3,),)
    assert observed["memory_limit"] == "1GB"
    assert observed["threads"] == "2"
    spill_path = Path(str(observed["temp_directory"]))
    expected_parent = (
        Path("/tmp/opencode") if Path("/tmp/opencode").is_dir() else Path(tempfile.gettempdir())
    )
    assert spill_path.parent == expected_parent
    assert observed["autoinstall_known_extensions"] == "false"
    assert observed["autoload_known_extensions"] == "false"
    assert not list(tmp_path.rglob("*.duckdb"))


@REQUIRES_DUCKDB
@pytest.mark.parametrize("limit", [0, -1, 1001, True, 1.5])
def test_invalid_result_limits_are_rejected(tmp_path: Path, limit: object) -> None:
    bundle = _write_bundle(tmp_path / "bundle")
    with pytest.raises(QuerySamplesError, match="limit must be an integer"):
        query_samples(bundle, "SELECT 1", limit=limit)  # type: ignore[arg-type]


def test_cli_help_and_query_branch_are_lazy_about_duckdb(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    real_import = builtins.__import__

    def without_duckdb(name: str, *args: object, **kwargs: object) -> object:
        if name == "duckdb":
            raise ImportError("duckdb intentionally hidden for test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_duckdb)
    with pytest.raises(SystemExit) as help_exit:
        cli_main.main(["--help"])
    assert help_exit.value.code == 0
    assert "query-samples" in capsys.readouterr().out

    bundle = _write_bundle(tmp_path / "bundle")
    with pytest.raises(SystemExit) as query_exit:
        cli_main.main(["query-samples", "--bundle", str(bundle), "--sql", "SELECT 1"])
    assert query_exit.value.code == 2
    captured = capsys.readouterr()
    assert "uv sync --extra query" in captured.err


@REQUIRES_DUCKDB
def test_cli_emits_csv_and_logs_actual_schema_version(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    bundle = _write_bundle(tmp_path / "bundle")
    caplog.set_level("INFO")

    code = cli_main.main(
        [
            "query-samples",
            "--bundle",
            str(bundle),
            "--sql",
            "SELECT asset_id, target_return_1d FROM samples ORDER BY asset_id, date",
            "--limit",
            "2",
        ]
    )

    captured = capsys.readouterr()
    assert code == 0
    assert list(csv.reader(captured.out.splitlines())) == [
        ["asset_id", "target_return_1d"],
        ["AAA", "10.0"],
        ["AAA", "30.0"],
    ]
    assert "schema_version=samples" in caplog.text


@pytest.mark.parametrize("limit", ["0", "-3", "1001"])
def test_cli_rejects_out_of_range_limit(tmp_path: Path, limit: str) -> None:
    bundle = tmp_path / "does-not-need-to-exist"
    with pytest.raises(SystemExit) as exit_info:
        cli_main.main(
            ["query-samples", "--bundle", str(bundle), "--sql", "SELECT 1", "--limit", limit]
        )
    assert exit_info.value.code == 2
