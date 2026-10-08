**English** | [简体中文](architecture.zh-CN.md)

# Architecture

The repository keeps existing market/SEC/FRED ingestion separate from one finance-free `samples` builder and offers optional `query-samples` reads of current-contract bundles. The migration to the five-module `samples` package and shared read-only SQL guard is implemented in the current source tree; it did not change download or organize behavior. The 2026-10-03 publication at `data/output/` retains the historical manifest value `schema_version="samples_v3"`; the former 147-column artifact and frozen baseline remain separately preserved. These bundles stay unchanged and read-only; this document does not announce a new publication. Querying adds no new storage format.

Before changing code, read [AGENT.md](../../AGENT.md). Single sample contract and package migration: [samples-architecture.md](samples-architecture.md), [samples.md](samples.md), and [data-contracts.md](data-contracts.md). Existing source behavior: [download.md](download.md). The separate bounded filing archive has independent real pilot roots; neither is a `samples` input or broad-coverage claim. See [financial-filing-archive.md](financial-filing-archive.md) and the [user guide](../user/filings.md) for current results and limits.

## 1. Data-flow overview

```text
existing sources                  data/raw/                         data/organized/                 samples candidate
───────────────                   ─────────                         ────────────────                 ───────────────────
Yahoo market ────────────────→ yahoo/<T>/<range>.csv ───────────→ stocks/<T>/market.csv ─────┐
FRED observations ───────────→ fred/<series>/<sha>.json ────────→ shared/macro.csv ──────────┼→ build-samples
                                                                                              │   fresh --out only
SEC CompanyFacts/submissions → sec/financials/<sha>.json ──┐                                   │
                         + manifest.json                     └→ financials.csv / selected       ┘
                                                              financial_events_v1 artifacts
                                                              (separate; not read by builder)
```

```text
existing sample bundle (Parquet files + manifest)
        ├── direct Python reads with PyArrow
        └── optional query-samples → in-memory DuckDB → CSV output
```

The SEC raw cache still contains Company Facts, submissions and historical submissions-page JSON; it is distinct from the explicit-root filing archive. `filings catalog` copies verified cached submissions into that archive; bounded `filings download` stores selected inventory/documents; `filings parse` publishes parser attempts and extracted facts/sections. The first-five private pilot is at v7 (6,017 facts/165 sections); a separate ten-filing, eight-CIK private batch is at v15 (25,656 facts/95 sections; 15 full text, 6 full XBRL, 4 unsupported XBRL). The RBC group is one primary-anchored 7,543-occurrence parse (40 primary/7,503 EX2), but raw scope remains partial. These archives are separate and neither feeds `samples`. `financial_events_v1` remains the selected nine-concept artifact. Query commands use in-memory DuckDB, not a persistent database or sample-data migration.

Existing lifecycle stages:

1. **universe:** fetch SEC `company_tickers_exchange`, apply the configured exchange filter and ticker→CIK overrides.
2. **download:** existing Yahoo bars, SEC Company Facts/submissions/history pages, and configured FRED series; this behavior remains unchanged.
3. **organize:** write cleaned market and macro panels and the separate existing financial outputs. Financial organize artifacts remain available independently.
4. **build-samples:** derive the canonical calendar from market panels, construct non-financial features/labels, cross-sectional ranks, and QC, then write a fresh candidate output directory. It does not read financial files or financial `_meta.json` records and performs no financial preflight, coverage, or inventory; the current manifest contains no financial-status claim.
5. **consumption:** validate a candidate's `manifest.json` and output hashes; use `splits.json`. Purge is already applied and consumers do not purge again.
6. **optional sample query:** `query-samples` accepts only the strict current `samples` contract, validates its schema/feature registry/fingerprints and all manifest-registered output hashes, byte counts and row counts, then reads sample Parquet files. This integrity validation is not independent semantic or release verification.
7. **separate filing archive:** `filings catalog` reads the local cache; bounded `filings download` fills selected inventory/documents; `filings parse` processes already-present selected bytes and publishes parse/fact/text/dependency tables; `verify` and `query` inspect the explicit archive. This workflow does not feed `samples`. Two finite real pilots are persisted in separate roots (first-five v7 and second-ten v15); see [financial-filing-archive.md](financial-filing-archive.md) for per-case statuses and limits.

The archived `data/output/` bundle has 96 features and 132 physical columns under its original `samples_v3` manifest label. The former 147-column output remains at `data/output-v1-backup-20261003T172214933236Z`; the frozen baseline remains separate. These are read-only historical artifact facts, not the new product contract.

## 2. Module responsibilities

| Module | Responsibility |
|---|---|
| `src/cli/main.py` | `download`, `build-samples`, and optional `query-samples` CLI parsing and dispatch |
| `src/samples/query.py` | Implemented optional read-only SQL queries over strict current-contract sample bundles; no persistent database |
| `src/query_support/readonly_sql.py` | Implemented neutral shared SQL guard for `samples` and filings query consumers |
| `src/filings/{cli,workflow,archive,acquisition,processing,query}.py` | Separate catalog/archive, bounded SEC acquisition, offline-first parse integration, archive verification, and descriptor-backed local query commands |
| `src/filings/config.py` | SEC-only contact setting for acquisition and explicit taxonomy preparation; no FRED key required |
| `src/filings/{parse_xbrl,parse_text,dependencies,parsing_models}.py` | Source-preserving local parsers, bounded taxonomy dependency preparation, and parser-owned Arrow occurrence schemas |
| `src/download/manager.py` | Existing download-stage orchestration, progress, lock, and parallel organize |
| `src/download/universe.py` | SEC ticker snapshot and ticker→CIK overrides |
| `src/download/market.py` | Yahoo daily-bar download |
| `src/download/financials.py` | Existing Company Facts/submissions/history-page JSON cache; not full filing HTML |
| `src/download/macros.py` | Existing FRED observations download |
| `src/download/organize.py` | Market cleaning/derived values, macro alignment, and organize metadata |
| `src/download/organize_financials.py` | Existing selected-concept financial extraction and legacy daily snapshot |
| `src/download/financial_events.py` | Existing selected `financial_events_v1` event/fact Parquet artifact; separate from sample building |
| `src/samples/{__init__,builder,query,contracts,validation}.py` | Current five-file package implementing the single finance-free `samples` product. The retired top-level modules and historical tools are absent; see [source-layout-audit.md](source-layout-audit.md) for the dated pre-migration snapshot and current tree. |
| `config/` | Sources, secrets template, universe overrides and exclusions |
| `tests/` | Offline tests and fixtures |

## 3. Data locations and lifecycle

| Location | Producer | Contents / update behavior | `samples` input? |
|---|---|---|---|
| `data/raw/yahoo/<T>/<start>_<end>.csv` | Existing market downloader | Yahoo CSV; same interval filename may be replaced | Indirectly, after market organize |
| `data/raw/sec/universe/<sha256>.json` | Existing universe downloader | Content-addressed universe snapshot | No |
| `data/raw/sec/financials/<sha256>.json` + `manifest.json` | Existing SEC financial downloader | Company Facts, submissions, historical page payloads; content-addressed payloads, append-version manifest | No |
| Explicit filing archive run root (for example `data/filings/pilot`) | `filings catalog/download/parse` | Separate immutable source CAS and manifest-listed snapshots; `facts`/`sections`, auxiliary `parses`/`dependencies` tables exist only after processing publishes them | No |
| `data/raw/fred/<SERIES>/<sha256>.json` + manifest | Existing macro downloader | FRED observations and versions | Indirectly, after macro organize |
| `data/organized/stocks/<T>/market.csv` | `organize_market` | Cleaned market panel | **Yes** |
| `data/organized/shared/macro.csv` | `organize_macros` | Session-aligned macro panel | **Yes** |
| `data/organized/stocks/<T>/financials.csv` | `organize_financials` | Existing daily whole-filing snapshot, unchanged | **No** |
| `data/organized/stocks/<T>/financial_events.parquet` and `financial_facts.parquet` | Existing financial organizer | Selected nine-concept `financial_events_v1`; not a complete archive | **No** |
| `data/organized/stocks/<T>/_meta.json` | Existing organize functions | Raw/organized input-output provenance, including legacy financial artifact records | Builder does not read financial records |
| Fresh candidate output path (CLI default: `data/samples-output`) | `build-samples` | Single `samples` contract, meta, split/manifest/QC files; safe destination checks apply | Output |
| Existing `data/output/` | Archived daily bundle | Its 2026-10-03 manifest retains `schema_version="samples_v3"` | Read-only historical artifact; do not target as a candidate |
| V1 backup and frozen baseline | Preserved historical references | `data/output-v1-backup-20261003T172214933236Z` and `data/baselines/samples_v1_financial_upgrade/` | Keep separate; do not overwrite |

## 4. Existing ingestion versus modular filing archive

The current `download --stage financials` remains the Company Facts/submissions/history-page workflow and does not fetch full filing documents. The separate `filings catalog/download/parse/verify/query` commands operate on explicit archive roots. Catalog is local-cache-only; download is bounded; parse works only from already-present selected files, is offline by default, and prepares taxonomy only when explicitly requested; query reads only existing manifest-listed tables. The pilot archives are not `samples` inputs. Filing pilot outcomes, archived-source grouping, raw-coverage limits, and their separately recorded verification evidence are in [financial-filing-archive.md](financial-filing-archive.md); they are not a samples gate.

## 5. Determinism and safe output

- Existing raw SEC/FRED payloads are content-addressed; their manifests map logical resources to payload versions.
- Sample row ordering, canonical session alignment, registry order, and manifest hashing remain part of the `samples` contract.
- `workspace_root` defaults to CWD and can be set with `--workspace-root`; the default exclusions path is `<workspace_root>/config/universes/exclusions_v1.json`. Explicit exclusions paths resolve relative to CWD, and a missing exclusions file is an error. Do not infer workspace/config/data paths from the installed source package.
- Protect `<workspace_root>/data/` and related raw/output/baselines locations; for a normal `<data>/organized` input, recognized raw/output/baseline/archive siblings beside its data directory are protected independently of `workspace_root`. The explicit workspace root must exist. Reject symlink paths/ancestors, non-empty targets, and destinations overlapping protected paths or actual inputs. The CLI default output is `data/samples-output`; use a fresh unused `--out` path and do not target preserved bundles or baselines.
- The archived publication report is historical evidence only; it is not current test or performance evidence. New sample builds require independent current-contract verification and must not depend on a cross-generation projection oracle.
