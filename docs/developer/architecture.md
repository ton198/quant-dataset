# Architecture

The repository retains three raw-provider downloads and a separate SEC report-sample workflow. It does not organize provider data into derived panels.

```text
Yahoo ─┐
SEC ───┼→ download → data/raw/ + progress
FRED ──┘

local SEC submissions cache → filing identity scan → bounded SEC report fetch
                                                  → cleaned blocks → Parquet + diagnostics
```

Raw downloads live in `src/download/`; CLI dispatch lives in `src/cli/main.py`. SEC identity/cache validation is in `src/filings/catalog.py`; document selection, SEC transport, parsing and the report-sample workflow live in adjacent `src/filings/` modules. `data/raw/sec/financials` supplies filing identity metadata; report documents are fetched separately and are not retained in the raw cache.

The report-sample builder uses bounded workers, per-filing private temporary directories, checkpointed cleaned outputs, year partitions and sharded Parquet. Diagnostics preserve partial/failure status. Validation checks schema, paths, shard coverage and row identities. SEC bytes are removed after parse/checkpoint commit; resume uses cleaned checkpoints only.

User data—existing `data/`, baselines, archive snapshots and private credentials—is outside source cleanup. Tests use local fixtures and mocked HTTP only. See [download internals](download.md), [SEC user guide](../user/filings.md), and [testing](testing.md).
