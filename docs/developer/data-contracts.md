# Data Contracts

The maintained data flow has two outputs: raw provider payloads and cleaned SEC report samples. Existing organized/output artifacts are user data and are preserved, but no retained command regenerates them.

## Raw provider data

`quant-dataset download` writes under `data/raw/` and keeps resumable progress separately. Yahoo interval files are ticker/range scoped. SEC and FRED resources use their existing content-addressed manifests. SEC `financials` stage stores Company Facts and submissions metadata, including referenced historical submission pages; it is not full filing content. Do not hand-edit raw payloads or rewrite existing caches during source maintenance.

## Report-sample output

`filings build-report-samples` emits year-partitioned Parquet shards and diagnostics. The exact Arrow schema is:

| Field | Type |
|---|---|
| `sample_id` | string |
| `entity_id` | nullable string |
| `filed_date` | nullable date32 |
| `blocks` | list of `{order_index: int32, block_type: string, text: string}` |

Block types are `financial_statement`, `note`, `other_context`, and `unknown`. Each filing identity appears once; shard paths are unique and year partitions follow filed date. Diagnostics retain unavailable, skipped and failed outcomes. The validator checks exact schema, partition and shard structure, identities, counts and diagnostic consistency.

Source filing bytes are fetched into private per-filing temporary directories and removed after parsing/checkpoint commit. Resume uses cleaned checkpoint rows and diagnostics, scoped to requested IDs; it does not reuse source bytes. Output and work directories must be new, empty, non-symlink paths separate from the cache and preserved user data.

## Preservation boundary

Existing `data/`, `baselines/`, archive snapshots and `config/secrets.toml` are user-owned. The retained code does not migrate or republish those artifacts. See [download internals](download.md) and [report-sample guide](../user/filings.md).
