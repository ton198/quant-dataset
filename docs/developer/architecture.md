**English** | [简体中文](architecture.zh-CN.md)

# Architecture

Scope: an **offline pipeline from external data sources to frozen training samples** — `data/raw/` (append-only raw snapshots) → `data/organized/` (per-ticker daily panels) → `data/output/` (sample bundle + audit files). Behavior is defined by the code in `src/` and the configuration in `config/`; `data/`, `logs/`, and `archive/` are not version-controlled.

Before changing code, read the task-routing table in [AGENT.md](../../AGENT.md). Field-level contracts for each layer are in [data-contracts.md](data-contracts.md); download details in [download.md](download.md); sample details in [samples.md](samples.md).

## 1. Data-flow overview

```text
external sources                        data/raw/ (payload filename = sha256, append-only)   data/organized/                data/output/
──────────────                          ────────────────────────────────────────────────     ────────────────               ────────────
SEC company_tickers_exchange ─┐
config/universes/*.json ──────┴──→  sec/universe/<sha256>.json ──────┐
Yahoo Finance ───────────────────→  yahoo/<TICKER>/<start>_<end>.csv  ├─→ stocks/<T>/market.csv     ─┐
SEC companyfacts / submissions ──→  sec/financials/<sha256>.json      │   stocks/<T>/financials.csv ─┼─→ build-samples ─→ samples/year=YYYY/
(incl. historical filing pages)      + manifest.json (key → versions) │   shared/macro.csv          ─┘      + meta / manifest / splits
FRED observations ───────────────→  fred/<SERIES>/<sha256>.json       │                                          / qc_report
                                    + manifest.json                   ┘
```

The lifecycle has five stages:

1. **universe**: fetch a SEC `company_tickers_exchange` snapshot → filter by `config/sources.toml [universe].exchanges` (currently Nasdaq + NYSE) → merge manual corrections from `config/universes/ticker_cik_overrides.json` (currently only `XOM → 0000034088`).
2. **download** (CLI stages: `market` / `financials` / `macros`): Yahoo daily bars; SEC Company Facts + Submissions + historical filing pages; 11 FRED macro series. Raw payloads are append-only and never rewritten.
3. **organize** (CLI stage: `organize`): market-data cleaning + derived columns; financials aligned as-of to sessions; macro wide table; multi-process per ticker (`--workers`, default 16).
4. **build-samples** (standalone command): bootstrap the canonical calendar from the full market panel → stream features/labels per ticker → per-day cross-sectional ranks → write yearly parquet partitions + audit files.
5. **consumption**: the training side verifies hashes against `data/output/manifest.json` and picks windows from `splits.json`; it **does not purge again**.

## 2. Module responsibilities

| Module | Responsibility | Key entry points |
|---|---|---|
| `src/cli/main.py` | The only command-line entry point; argument parsing, stage expansion, exit codes | `_parser()`, `main()` |
| `src/download/manager.py` | Orchestrates a download run: ticker selection, progress lock, per-stage execution, organize process pool | `run_download()`, `_organize_tickers()` |
| `src/download/universe.py` | SEC ticker-list snapshot (content-addressed cache) + ticker→CIK override merge | `fetch_universe()`, `load_ticker_cik_overrides()` |
| `src/download/market.py` | Yahoo daily-bar download (yfinance, auto_adjust=false) | `fetch_market()` |
| `src/download/financials.py` | SEC companyfacts / submissions / historical page download; content-addressed + manifest appends | `fetch_financials()` |
| `src/download/macros.py` | FRED observations download; content-addressed + manifest appends | `fetch_macros()` |
| `src/download/organize.py` | Market cleaning/derived columns/`quality_flag`; macro wide table and visibility rules; writes `_meta.json` | `organize_market()`, `organize_macros()` |
| `src/download/organize_financials.py` | Filing fact extraction (concept whitelist), fiscal identifier fallback, per-session as-of expansion | `organize_financials()` |
| `src/download/progress.py` | Resumable worklist (atomic writes) + PID file lock (stale locks reclaimed automatically) | `initialize()`, `save_atomic()`, `acquire_lock()` |
| `src/download/config.py` | Reads `config/sources.toml` / `config/secrets.toml` into frozen dataclasses | `load_sources()`, `load_secrets()` |
| `src/download/errors.py` | Three domain exception types | `ConfigError` / `DownloadError` / `OrganizeError` |
| `src/build_samples.py` | Sample long table: canonical calendar, features/labels, purge, cross-sectional ranks, partitioned writes and audit | `build_samples()` |
| `config/` | Endpoint and stage configuration, secrets (gitignored), universe overrides/exclusions | `sources.toml`, `secrets.toml`, `universes/*.json` |
| `tests/` | All offline tests (6 files) | see [testing.md](testing.md) |

## 3. Data locations and lifecycle

| Location | Producer | Content | Update semantics | Consumer |
|---|---|---|---|---|
| `data/raw/yahoo/<T>/<start>_<end>.csv` | `market.fetch_market` | Raw yfinance CSV (Adj Close/Close/Dividends/High/Low/Open/Stock Splits/Volume) | Re-downloading the same name overwrites it (**not** content-addressed) | `organize.organize_market` |
| `data/raw/sec/universe/<sha256>.json` | `universe.fetch_universe` | Full SEC ticker-list snapshot (`fields`/`data`) | New content → new file; filename = content sha256, verified on read | `manager._cached_universe`, `organize_financials._ticker_mappings` |
| `data/raw/sec/financials/<sha256>.json` | `financials.fetch_financials` | companyfacts / submissions / historical page payloads | Append-only, never rewritten | `organize_financials` |
| `data/raw/sec/financials/manifest.json` | same as above | `logical_key` → version list (path/sha256/url/...) | tmp + rename atomic replacement; versions only appended | same as above |
| `data/raw/fred/<SERIES>/<sha256>.json` + `fred/manifest.json` | `macros.fetch_macros` | FRED observations | Same as above (append-only manifest) | `organize.organize_macros` |
| `data/organized/stocks/<T>/market.csv` | `organize.organize_market` | Cleaned market panel | Overwritten on rerun; `_meta.json` records input/output hashes | `build_samples` |
| `data/organized/stocks/<T>/financials.csv` | `organize_financials` | Session-level as-of financial snapshots | Overwritten on rerun | `build_samples` |
| `data/organized/stocks/<T>/_meta.json` | both organize functions | Completion marker + input/output sha256 + row_counts | Merged/updated on rerun | `manager._ticker_is_organized` |
| `data/organized/shared/macro.csv` | `organize.organize_macros` | Session-aligned macro wide table | Overwritten on rerun | `build_samples` |
| `data/output/{samples,meta.parquet,manifest.json,splits.json,qc_report.*}` | `build_samples` | Sample bundle | Fully rebuilt every run (old outputs cleared first) | Training side |
| `data/.download_progress{,.tmp,.lock}` | `progress` | Resumable worklist, temp file, PID lock | Atomic replacement; `--force` discards and rebuilds | `manager` only |
| `logs/`, `archive/` | Driver scripts / humans | Run logs, cold archives | Gitignored, managed manually | none |

Configuration and run-state details:

| Path | Content |
|---|---|
| `config/sources.toml` | `[universe]` endpoint and exchange whitelist; `[market]`/`[financials]`/`[macros]` throttling, timeouts, retries; `[macros].series` series list; `[download]` paths |
| `config/secrets.toml` | `[secrets] sec_user_agent`, `fred_api_key`; gitignored (template `secrets.example.toml`) |
| `config/universes/ticker_cik_overrides.json` | Manual ticker→CIK corrections (currently XOM → 0000034088) |
| `config/universes/exclusions_v1.json` | Sample-build exclusion list (currently AYA, FUND) |
| `data/.download_progress` | JSON worklist: `run_id`, `started_at_utc`, `universe_source`, `stages{stage:{item: done / pending / failed:...}}`; paired `.tmp` (write buffer) and `.lock` (PID lock) |
| `archive/` | Local cold storage (e.g. `2026-09-24_pre_download_v2/`, a full-repo backup from before the migration); not in git, managed manually |

## 4. Scheduling and concurrency

- A single `download` run holds `data/.download_progress.lock` (PID file lock) for its entire duration; **two runs must not share one data-dir**. The organize process pool only reads `raw/` and writes its own ticker directories.
- `--force`: ignore the old progress state and rebuild the worklist for the selected stages (this re-downloads). `--force-rebuild`: force only organize outputs to be refreshed, without affecting download progress.
- organize is idempotent per ticker: it skips when `_meta.json` records the outputs and their file sha256 verify; `--force-rebuild` or corrupt metadata forces a redo.
- With `--stage organize` and no `--start/--end`, the organize calendar runs from `1990-01-01` through today (`manager.run_download`). An organize-only rerun extends `financials.csv` to today; build-samples only takes canonical sessions inside the sample window, so it is unaffected.
- build-samples is streaming end to end, so memory does not grow with full-market row count: 50 tickers per staging batch → cross-sectional ranks per 40-session chunk → appended to per-year `ParquetWriter` files.

## 5. Determinism mechanisms

| Mechanism | Location |
|---|---|
| Stable sorting (`kind="mergesort"`; market.csv sorted by date, duplicate dates keep last) | `build_samples`, `organize_market` |
| JSON always `sort_keys=True` + atomic replacement via temp files | all manifests / splits / progress / `_meta.json` |
| Raw payload content addressing (filename = sha256) | `universe` / `financials` / `macros` |
| sha256 registration of all outputs (`manifest.json` itself excluded) | `build_samples._hash_output_files` |
| Canonical axis driven by data rather than a hard-coded calendar | `build_samples` (XNYS sessions with ≥500 common tickers) |

## 6. Test layout

| File | Coverage |
|---|---|
| `tests/test_download.py` | secrets/sources loading, atomic progress writes, market cleaning, financial as-of, CLI help |
| `tests/test_organize_parallel.py` | process pool matches serial results, skip/redo, shared CIK, failure isolation |
| `tests/test_organize_financials.py` | concept priority, fiscal identifier fallback, parsing of three submissions payload shapes |
| `tests/test_build_samples.py` | labels/features/as-of/purge/output dtypes and exclusions |
| `tests/test_progress_lock.py` | stale lock reclamation, live lock rejection |
| `tests/test_ticker_overrides.py` | XOM CIK override forward/reverse lookup, `--force-rebuild` |
