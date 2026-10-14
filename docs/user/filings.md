**English** | [简体中文](filings.zh-CN.md)

# SEC Report Samples

The `filings` command builds and validates cleaned report samples from filing identities in the local SEC financials cache. It does not create an archive, run model extraction, or modify the source cache.

## Build

```bash
quant-dataset filings build-report-samples \
  --cache-root data/raw/sec/financials \
  --out data/financial_reports \
  --work-dir data/.financial_reports.work \
  --limit 10 --workers 4
```

The configured `filings.forms` list in `config/sources.toml` determines eligible forms. The cache is read only to identify filings; selected report documents are fetched from SEC using the configured contact. Primary-document selection, safe URL validation, bounded workers, checkpointing, and per-filing diagnostics are part of the workflow. A plain-text accession fallback may be used when the primary document is unavailable; non-HTML/PDF payloads are skipped rather than treated as parsed reports.

Use fresh, empty output and work paths. Paths may be under `data/` but must not overlap `data/raw/`, `data/baselines/`, `baselines/`, `archive/`, the cache, or preserved user data. Output protection rejects symlink paths and non-empty destinations.

`--limit N` bounds the selected filing count; repeated `--filing-id` selects exact identities from the local cache. `--workers` is a positive bounded concurrency setting. `--resume` reuses validated cleaned checkpoints and diagnostics, and filters them to the current selection. Source bytes live only in per-filing private temporary directories and are removed after parsing/checkpoint commit; resume does not reuse downloaded source bytes.

## Output and validation

The output contains year-partitioned Parquet shards with exact schema:

- `sample_id: string`
- `entity_id: string?`
- `filed_date: date32?`
- `blocks: list<struct<order_index: int32, block_type: string, text: string>>`

Block types are `financial_statement`, `note`, `other_context`, or `unknown`. Diagnostics are JSONL. Work checkpoints are private to `--work-dir` and are not part of the published sample output.

```bash
quant-dataset filings validate-report-samples \
  --out /path/to/report-samples --cache-root data/raw/sec/financials
```

Validation checks exact Parquet schema, partition/shard paths, unique identities, valid block contents, row counts, diagnostics, and missing shards. A successful result establishes dataset structural integrity, not complete SEC coverage or financial correctness. SEC HTTP errors and unsupported document types are visible in diagnostics; partial coverage must not be described as complete.

`--resume` is intended for rerunning the same selected work against the same output/work locations. Do not use existing output or cache directories as scratch space.
