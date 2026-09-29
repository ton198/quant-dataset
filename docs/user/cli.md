**English** | [简体中文](cli.zh-CN.md)

# CLI Manual

The CLI lives in `src/cli/main.py` and exposes two subcommands: `download` and `build-samples`. After installation (`pip install -e .` / `uv sync`) the entry point is `quant-dataset`; the equivalent development invocation is:

```bash
PYTHONPATH=src .venv/bin/python -m cli.main --help
```

## 1. Prerequisites

### 1.1 Environment

```bash
uv sync --frozen                 # install dependencies from uv.lock; or pip install -e .
PYTHONPATH=src .venv/bin/python -m cli.main --help
```

### 1.2 Secrets (config/secrets.toml)

```bash
cp config/secrets.example.toml config/secrets.toml
```

| `[secrets]` key | Purpose | Example |
|---|---|---|
| `sec_user_agent` | User-Agent for all SEC requests, formatted as `Name email` | `Jane Doe jane@example.com` |
| `fred_api_key` | FRED observations API key | `abcdef0123456789` |

- `config/secrets.toml` is excluded by `.gitignore` — never commit it; if it leaks, rotate the keys on the FRED/SEC side.
- Both keys must be present whenever a stage loads secrets (any SEC or FRED request), otherwise you get `Missing required secret '<key>'`.
- A market-only run (`--stage market --tickers A,B`, no financials or macros) does not load secrets.

## 2. download

```text
quant-dataset download [--stage {market,financials,macros,organize,all}]... \
  [--start YYYY-MM-DD] [--end YYYY-MM-DD] [--tickers A,B,C] [--force] \
  [--force-rebuild] [--dry-run] [--data-dir PATH] [--workers N]
```

| Flag | Default | Description |
|---|---|---|
| `--stage` | `all` | Repeatable to select multiple stages; `all` expands to market + financials + macros + organize (deduplicated, order preserved) |
| `--start` / `--end` | none | Inclusive range for the market stage; both required when market is selected |
| `--tickers` | none (full universe) | Comma-separated subset, upper-cased internally; tickers outside the SEC universe are ignored with a warning |
| `--force` | off | Ignore `data/.download_progress` and re-download completed items |
| `--force-rebuild` | off | Rebuild organize outputs even if they are already complete (for recomputation after code changes) |
| `--dry-run` | off | Print the stages and scope that would run, then return — no network, no writes |
| `--data-dir` | `data` | Base directory for raw / organized / progress (resolved relative to the repo root) |
| `--workers` | 16 | Process workers for per-ticker organize; values <1 error out |

Where each stage writes:

| Stage | Source | Output |
|---|---|---|
| market | Yahoo daily bars (yfinance) | `data/raw/yahoo/<TICKER>/<start>_<end>.csv` |
| financials | SEC Company Facts + Submissions + historical filing pagination | `data/raw/sec/financials/<sha256>.json` + `manifest.json` (content-addressed) |
| macros | 11 FRED series | `data/raw/fred/<SERIES>/*.json` |
| organize | the raw layers above | `data/organized/stocks/<T>/{market.csv,financials.csv}`, `data/organized/shared/macro.csv` |

Progress and resume (`data/.download_progress`):

- State is tracked per `(stage, item)`; rerunning the same command after an interruption skips `done` items and retries `pending/failed` ones.
- Completion is independent of the date range: once a ticker is marked `done`, changing `--start/--end` still skips it — only `--force` makes it download again.
- `--force` rebuilds the whole progress list from the current command (it is not a per-item retry); the file remains after a successful run and doubles as the re-download list.

## 3. build-samples

```text
quant-dataset build-samples [--data-dir data/organized] [--out data/output] \
  [--exclusions-file config/universes/exclusions_v1.json]
```

| Flag | Default | Description |
|---|---|---|
| `--data-dir` | `data/organized` | Reads `stocks/` and `shared/macro.csv` |
| `--out` | `data/output` | Sample bundle output directory; clears and rebuilds `samples/`, `meta.parquet`, `splits.json`, `manifest.json`, `qc_report.*` first |
| `--exclusions-file` | `config/universes/exclusions_v1.json` | Exclusion list; the current default excludes `AYA` and `FUND` |

Behavior notes: there is no resume — every run rebuilds everything. If any ticker-level failures remain, the log lists them and the command exits 1.

## 4. Typical workflows

First full run (download and organize stage by stage, then build the samples):

```bash
PYTHONPATH=src .venv/bin/python -m cli.main download \
  --stage all --start 1990-01-01 --end 2025-12-31 --workers 16
PYTHONPATH=src .venv/bin/python -m cli.main build-samples
```

Incremental / interrupted resume (just rerun the original command; only unfinished items are processed and failures are retried automatically):

```bash
PYTHONPATH=src .venv/bin/python -m cli.main download \
  --stage all --start 1990-01-01 --end 2025-12-31 --workers 16
```

Single-ticker backfill (for example, refresh one stock's market data and financials and reorganize just that ticker):

```bash
PYTHONPATH=src .venv/bin/python -m cli.main download \
  --stage market,financials,organize --tickers AAPL \
  --start 1990-01-01 --end 2025-12-31 --workers 1
```

Rebuild after code changes (when organize/financials logic changed, run `--force-rebuild` first, then rebuild the whole sample bundle):

```bash
PYTHONPATH=src .venv/bin/python -m cli.main download \
  --stage organize --force-rebuild --workers 16
PYTHONPATH=src .venv/bin/python -m cli.main build-samples
```

Plan only, no writes:

```bash
PYTHONPATH=src .venv/bin/python -m cli.main download \
  --stage all --start 1990-01-01 --end 2025-12-31 --dry-run
```

## 5. Timing reference

Reference values (16 workers, healthy network). Only organize/build-samples are local wall-clock measurements from project run records; network stages fluctuate with source-side rate limits and are not fixed.

| Step | Reference |
|---|---|
| market, full universe (6,500+ tickers, 1990–2025) | depends on Yahoo throttling and retries (order of hours) |
| financials, full universe (SEC pagination) | depends on SEC throttling (order of hours) |
| organize, full | ≈12 min (local measurement) |
| build-samples, full | ≈2 h 50 min (local measurement) |

## 6. Exit codes and common errors

| Exit code | Trigger |
|---|---|
| 0 | Success (including `--dry-run`, and `build-samples` with no ticker failures) |
| 1 | `download` had per-item failures or a configuration error; `build-samples` had ticker-level failures |
| 2 | argparse errors: missing `--start/--end` when market is selected, `--end < --start`, `--workers < 1` |

| Symptom | Cause / handling |
|---|---|
| `Missing required secret 'sec_user_agent'` (or `fred_api_key`) | `config/secrets.toml` does not exist, or the `[secrets]` table is missing a key; fix it as in §1.2 |
| `--start and --end are required when market stage is selected` | The market stage requires both dates; or use `--stage financials,macros,organize` without market |
| Ticker count after download is lower than the universe | SEC universe and Yahoo coverage differ; some directories have no `market.csv`, which is a known condition (see `input_inventory` in `manifest.json`) |
| `build-samples` fails with `organized stocks directory does not exist` | `--data-dir` points at a directory that has not been organized; run `--stage organize` first |
| Want to confirm outputs are unchanged after a rebuild | Compare `outputs[*].sha256` in `data/output/manifest.json` |
