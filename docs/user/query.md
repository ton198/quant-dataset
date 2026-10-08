**English** | [简体中文](query.zh-CN.md)

# Querying a sample bundle

`query-samples` is an optional command for reading rows from an existing sample bundle with SQL. The bundle stays in its existing Parquet files. The command reads those files in place; it does not copy the data into a persistent database or change the bundle. Python users can continue reading Parquet directly with PyArrow.

## Install the optional query command

The regular install remains unchanged. Add the `query` extra only when you want this command:

```bash
uv sync --frozen --extra query
# or
pip install -e '.[query]'
```

## Run a query

Both the bundle path and SQL statement are required. `query-samples` accepts only bundles built with the current `samples` contract; it does not offer aliases or compatibility modes for historical schema identifiers. It validates the exact 132-column Arrow schema, all 96 ordered features, the semantic contract and schema/semantic fingerprints, and every manifest-registered output's SHA-256, byte count, and row count before querying. The preserved `data/output/` bundle (manifest label `samples_v3`) and former 147-column backup are historical artifacts, not inputs supported by this command. Inspect those artifacts with Python/PyArrow or build a fresh current-contract bundle first. The following examples assume a fresh candidate at `/tmp/opencode/candidate-samples-finance-free`.

```bash
quant-dataset query-samples --bundle /tmp/opencode/candidate-samples-finance-free \
  --sql 'SELECT COUNT(*) AS row_count FROM samples'
```

The `samples` view reads the validated sample Parquet files. Output is CSV, including a header, on stdout; the validated current schema label (`samples`) is printed to stderr. Redirect stdout to save a clean CSV file:

```bash
quant-dataset query-samples --bundle PATH \
  --sql "SELECT date, asset_id, is_common FROM samples WHERE date >= DATE '2020-01-01' AND date < DATE '2021-01-01' ORDER BY date, asset_id" \
  --limit 5 > sample.csv
```

`--limit` caps the returned rows. It defaults to 20 and accepts values from 1 to 1,000. Use `date` for date filters; this command does not add a virtual `year` column from partition-folder names.

If the bundle contains `meta.parquet`, a `meta` view is also available. This example joins on the shared `asset_id` field and returns only sample columns:

```bash
quant-dataset query-samples --bundle PATH \
  --sql 'SELECT s.date, s.asset_id FROM samples AS s JOIN meta AS m USING (asset_id)' \
  --limit 5
```

## Scope and limits

- One `SELECT` statement is accepted; `WITH` common-table expressions are allowed. Write statements and multiple statements are rejected before execution.
- Each run uses an in-memory DuckDB connection to read the existing bundle. Temporary spill files, if needed, are placed outside the input directories. No `.duckdb` file or persistent catalog is created, and the input files are not modified.
- The command checks every file registered in the manifest, including streamed content hashes/byte counts and Parquet row counts, and validates current contract identity/schema before querying. This integrity check does not regenerate the bundle or replace independent semantic/release verification. An aggregate such as `COUNT(*)` reads the sample input; `--limit` bounds returned rows, not aggregate work.
- It is a local tool, not a security sandbox or public SQL service. Run SQL you trust against a bundle you intend to read.

DuckDB is only an optional reading layer; Parquet remains the stored format. This command does not fetch full filings or create financial tables. See [the data-format guide](data-format.md) for the bundle contract and [the CLI manual](cli.md) for the existing download and build commands.
