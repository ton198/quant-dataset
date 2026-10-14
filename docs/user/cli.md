**English** | [简体中文](cli.zh-CN.md)

# CLI Manual

The CLI exposes raw `download` stages and `filings build-report-samples` / `filings validate-report-samples`.

```bash
quant-dataset --help
quant-dataset download --help
quant-dataset filings --help
```

## Setup

```bash
uv sync --extra dev
cp config/secrets.example.toml config/secrets.toml
```

Set `[secrets].fred_api_key` for FRED and `[secrets].sec_user_agent` to a real project contact for SEC. Keep `config/secrets.toml` private and never commit it. No live provider access is permitted in tests; mock network transport.

## Raw downloads

```text
quant-dataset download [--stage {market,financials,macros,all}]... \
  [--start YYYY-MM-DD] [--end YYYY-MM-DD] [--tickers A,B,C] \
  [--force] [--dry-run] [--data-dir PATH]
```

| Option | Behavior |
|---|---|
| `--stage` | Repeatable; defaults to `all`, which expands to the three raw stages. |
| `--start` / `--end` | Inclusive market dates; both are required when `market` is selected. |
| `--tickers` | Optional comma-separated ticker subset. |
| `--force` | Ignore prior progress and restart selected work. |
| `--dry-run` | Print selected stages without network access or data writes. |
| `--data-dir` | Base for raw and progress paths; defaults to `data`. |

`market` downloads Yahoo daily bars, `financials` downloads SEC Company Facts/submissions and referenced historical submission pages, and `macros` downloads configured FRED series. Outputs remain raw provider data. There is no organize stage. SEC financial metadata is not a filing-document archive.

## SEC report samples

```text
quant-dataset filings build-report-samples \
  [--out PATH] [--work-dir PATH] [--cache-root PATH] [--limit N] \
  [--filing-id ID]... [--workers N] [--resume]
quant-dataset filings validate-report-samples [--out PATH] [--cache-root PATH]
```

Build scans the local financials cache for configured forms, makes bounded SEC requests, parses report blocks, and writes Parquet shards, diagnostics, and checkpoints. Default cache root: `data/raw/sec/financials`; default output: `data/financial_reports`; default work directory: `data/.financial_reports.work`. Output and work paths may be under `data/` but must not overlap `data/raw/`, `data/baselines/`, `baselines/`, `archive/`, the SEC cache, or preserved user data. A non-empty output is rejected unless `--resume` is used with owned diagnostics and identity metadata; unrelated files are never replaced. `--resume` reuses cleaned checkpoints, not source bytes. SEC source bytes are removed from the private work area after processing. `--limit` must be positive; `--filing-id` scopes the run but neither disables network. Real builds require a valid SEC contact. `--workers` must be positive.

Validation checks the report-sample schema, shard/partition consistency, row identities, and registered diagnostics. Exit status is 0 on success, 1 on runtime/configuration failure, and 2 for invalid arguments. Tests use mocked HTTP; do not run them against real SEC, Yahoo, or FRED endpoints.
