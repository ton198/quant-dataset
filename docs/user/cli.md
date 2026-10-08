**English** | [简体中文](cli.zh-CN.md)

# CLI Manual

The CLI in `src/cli/main.py` exposes the existing `download` and `build-samples` workflows, optional `query-samples`, and the local `filings catalog`, `download`, `parse`, `verify`, `query`, and `extract-financials` commands. The sample CLI command names remain `build-samples` and `query-samples`; moving their implementation into packages does not add product-version selection.

```bash
PYTHONPATH=src .venv/bin/python -m cli.main --help
```

## 1. Prerequisites

```bash
uv sync --frozen                 # or pip install -e .
cp config/secrets.example.toml config/secrets.toml
cp config/extraction.example.toml config/extraction.toml
```
`download` and `build-samples` use the regular install. Filing catalog/download/verify need no parser or query extra. `filings parse` needs optional Arelle support (`uv sync --frozen --extra filings`); `filings query` and `query-samples` need DuckDB (`uv sync --frozen --extra query`). To use both filing parsing and SQL queries, install `--extra filings --extra query`. See the [sample query guide](query.md) and [filing archive guide](filings.md).

| `[secrets]` key | Purpose |
|---|---|
| `sec_user_agent` | User-Agent for existing SEC requests, formatted as `Name email` |
| `fred_api_key` | FRED observations API key |

`config/secrets.toml` is gitignored; never commit it. Existing SEC/FRED download stages require their configured keys. A market-only run need not load secrets.

## Financial extraction

Copy `config/extraction.example.toml` to `config/extraction.toml` for the provider URL, model, structured output mode, numeric policies and request limits. Set `[secrets].api_key` in the gitignored `config/secrets.toml`; extraction reads only that field, never an environment variable. Use `--secrets PATH` to select another private secrets file (default: `config/secrets.toml`). Keep real credentials out of committed examples. Window extraction uses bounded worker concurrency (default 32 from `[extraction].workers`; `--workers 1` runs serially) with stable source order and no automatic retries; validation failures are never auto-corrected. Bound concurrency against service rate limits.

The model receives window-local checked short refs. The journal stores the exact mapping to original evidence refs; decoded output retains original source locations. Invalid or shortened IDs remain unresolved, without fuzzy repair or automatic retry. Short-ref requests have different identities from historical long-ref requests. Citation validity is not financial accuracy or numerical coverage: amounts split across HTML cells may remain text facts.

```bash
quant-dataset filings extract-financials \
  --archive ARCHIVE --work-dir PRIVATE_DIR --config config/extraction.toml \
  --filing-id FROZEN_ELIGIBLE_ID
```

Repeat `--filing-id` for a frozen list of at most 50 unique IDs. The command sends selected source contents to the configured provider, writes its journal and non-publishable `result.json` outside the archive, and never publishes. Do not run until both the provider and eligible filing list have been explicitly configured.

Live progress is written to stderr; the final JSON summary stays on stdout. Window totals remain unknown until evidence planning is exhausted. Pass `--no-progress` to disable progress output.

## 2. Existing `download` workflow

```text
quant-dataset download [--stage {market,financials,macros,organize,all}]... \
  [--start YYYY-MM-DD] [--end YYYY-MM-DD] [--tickers A,B,C] [--force] \
  [--force-rebuild] [--dry-run] [--data-dir PATH] [--workers N]
```

| Flag | Default | Description |
|---|---|---|
| `--stage` | `all` | Repeatable; `all` expands to market + financials + macros + organize |
| `--start` / `--end` | none | Inclusive market range; required when `market` is selected |
| `--tickers` | full universe | Comma-separated ticker subset |
| `--force` | off | Reset download progress and re-download selected items |
| `--force-rebuild` | off | Rebuild organize outputs without resetting download progress |
| `--dry-run` | off | Print the plan without network access or writes |
| `--data-dir` | `data` | Base directory for raw, organized, and progress data |
| `--workers` | 16 | Organize workers; values below 1 are invalid |

Existing stage outputs and scope:

| Stage | Existing behavior |
|---|---|
| `market` | Fetch Yahoo daily bars to `data/raw/yahoo/<TICKER>/<start>_<end>.csv` |
| `financials` | Fetch SEC Company Facts, submissions, and historical submissions-page JSON into `data/raw/sec/financials/` using the existing content-addressed cache/manifest. This is **not** a full filing HTML/iXBRL archive. |
| `macros` | Fetch the configured 11 FRED series to `data/raw/fred/` |
| `organize` | Write the existing market/macro panels and separate financial outputs, including legacy `financials.csv` and the selected `financial_events_v1` event/fact Parquet artifacts when applicable |

The legacy `financials.csv` and `financial_events_v1` organized artifacts remain separate from `samples` and are not sample-builder inputs. The event/fact artifact is a selected nine-concept extract, not a full XBRL/filing archive. Download and organize behavior is unchanged by the sample package migration. A separate bounded filing archive has catalog/download/parse/verify/query commands; neither parser statuses nor acquisition integrity are a financial-coverage claim. See the [user filing guide](filings.md) and [developer status/contract](../developer/financial-filing-archive.md).

Download progress resumes per item. Rerunning a command skips completed items unless `--force` resets the worklist; `--force-rebuild` affects organize only.

> **For samples:** running a download is not required to build a candidate from existing organized market/macro inputs. Do not run `download --stage all` as part of a sample-contract change; it includes the existing SEC structured-data download.

## 3. `build-samples` and safe candidate outputs

```text
quant-dataset build-samples [--workspace-root PATH] [--data-dir data/organized] \
  [--out data/samples-output] [--exclusions-file PATH]
```

| Flag | CLI default | Description |
|---|---|---|
| `--workspace-root` | Current working directory | Workspace base used for the default exclusions path and workspace data protection. It is not inferred from the installed source-package location. |
| `--data-dir` | `data/organized` | Organized market and macro inputs. Financial files and financial `_meta.json` records are not sample inputs. |
| `--out` | `data/samples-output` | Output bundle directory, resolved relative to CWD. The destination must be fresh/empty and cannot be a symlink path or overlap protected paths or actual inputs. The preserved `data/output/` is historical and protected; choose a new unused candidate path if the default already exists. |
| `--exclusions-file` | `<workspace_root>/config/universes/exclusions_v1.json` | If omitted, load this workspace-relative file. If explicitly supplied, resolve the path relative to the current working directory (CWD). A missing exclusions file is an error; no empty-list fallback is used. The filename remains `exclusions_v1.json` and is not a sample product version. |

By default, `workspace_root` is the process current working directory. You may set it explicitly with `--workspace-root PATH`; this controls the default exclusions lookup at `<workspace_root>/config/universes/exclusions_v1.json`, but does not change how an explicitly supplied `--exclusions-file` is resolved (relative to CWD).

The build protects `<workspace_root>/data/` and related raw/output/baselines paths, and also protects recognized raw/output/baseline/archive sibling paths next to a normal `<data>/organized` input independently of `workspace_root`. The explicit workspace root must already exist. The output guard rejects symlink paths/ancestors, non-empty destinations, and destinations overlapping protected paths or actual inputs. Use a fresh unused output path outside those locations. Do not target the preserved `data/output/`, a baseline, or an input directory; never delete historical data to reuse a path.

```bash
WORKSPACE_ROOT=$PWD
CANDIDATE_DIR=/tmp/opencode/candidate-samples-finance-free
test ! -e "$CANDIDATE_DIR" && test ! -L "$CANDIDATE_DIR" || { echo "Choose a new, unused, non-symlink candidate directory" >&2; exit 1; }
PYTHONPATH=src .venv/bin/python -m cli.main build-samples \
  --workspace-root "$WORKSPACE_ROOT" \
  --data-dir data/organized --out "$CANDIDATE_DIR"
```

The implemented `samples` contract has 96 features (43 raw + 10 CS + 43 MISS) and 132 physical sample columns (4 keys/flags + 96 features + 32 labels). New builds use `schema_version="samples"`; manifests record the exact ordered feature list, semantic contract and schema/semantic fingerprints, build parameters, streamed content hashes/byte counts for every consumed input, code/dependency identity, and hashes/bytes/rows for every registered output. Input hashing is rechecked before manifest publication; the separate `input_inventory` remains counts-only and is not content provenance. The existing `data/output/` bundle remains an unchanged historical publication with manifest value `schema_version="samples_v3"` (23,938,669 rows and 6,532 tickers); it is not relabeled or republished by this guide. Financial files and metadata are not builder inputs. Candidate creation remains separate and never replaces archived output, backup, or baseline.

## 4. Verification and release status

Run the task-assigned current-contract checks against the fresh candidate. Use independently authored fixtures for labels and boundary behavior; also check schema/order/dtypes, finance isolation, split/purge, missingness, safe output paths, actual input provenance, and output hashes. Historical cross-generation projections are not a correctness oracle.

The 2026-10-03 publication record (`COMMITTED_V3_V1_BACKUP_RETAINED`) describes only the preserved artifact and its then-recorded checks. It is not validation of current code or a new candidate; do not inherit its result. No performance or financial-correctness claim is made. The former 147-column output is retained at `data/output-v1-backup-20261003T172214933236Z`; the frozen baseline remains separate.

No current v3 runtime or performance measurements are asserted here. Record any measurement only from an identified run and environment; do not copy old v2 placeholders into the active contract.

## 5. General existing workflows

The following is an example of the existing source-ingestion workflow for other tasks, not a sample-candidate procedure. `--stage all` includes the existing SEC structured-data download described above.

```bash
PYTHONPATH=src .venv/bin/python -m cli.main download \
  --stage all --start 1990-01-01 --end 2025-12-31 --workers 16
```

Use `--dry-run` to inspect an existing download plan without writing. The separate `filings download` command is for bounded acquisition into an existing catalog archive; it is not the old Company Facts stage. `filings parse` handles selected archived bytes in a separate workspace. See the [filing guide](filings.md) for the partial five-filing pilot and its limits.

## 6. Exit codes and common errors

| Exit code | Meaning |
|---|---|
| 0 | Command completed successfully (including `--dry-run`) |
| 1 | Existing download/configuration failure, sample ticker failure, or filings archive/parse failure (parse may still publish a partial summary) |
| 2 | Argument parse/validation error, including invalid limits or incompatible `filings parse` flags |

| Symptom | Handling |
|---|---|
| Missing SEC/FRED secret | Create `config/secrets.toml` from the example and populate the required key for the selected existing download stage |
| Market stage missing `--start`/`--end` | Provide both inclusive dates, or omit `market` |
| `build-samples` cannot find organized market inputs | Check `--data-dir`; organize the existing market data separately if that is an independently intended task |
| Candidate `--out` path rejected | Choose a fresh non-symlink path outside protected/input directories; do not clear or reuse historical outputs |
| Default exclusions file is missing | Restore `<workspace_root>/config/universes/exclusions_v1.json` or pass an existing `--exclusions-file`; missing exclusions do not mean an empty list |
| `filings parse` missing `--workspace-root` or wrong `--max-filings` | Supply an existing separate workspace and a bound from 1 through 50 |
| `filings parse` reports missing taxonomy/dependency | Offline mode never fetches implicitly. Retry explicitly with `--prepare-dependencies`, valid SEC contact, and only the required approved taxonomy hosts; unresolved aliases remain failed/partial, not zeros |
| Arelle parser support missing | Install `uv sync --frozen --extra filings`; query tools separately need `--extra query` |

## 7. Optional `query-samples` command

```text
quant-dataset query-samples --bundle PATH --sql 'SELECT ...' [--limit N]
```

Both `--bundle` and `--sql` are required. Results are CSV on stdout, the validated bundle's schema label (`samples`) is printed on stderr, and `--limit` defaults to 20 and accepts values from 1 to 1,000. The command strictly accepts only a current `samples` manifest and verifies the 132-column sample schema, full feature list, semantic contract and fingerprints, and all manifest-registered output hashes/byte counts/row counts. It rejects historical `samples_v1`/`samples_v2`/`samples_v3` bundles and does not provide aliases or migration modes. The preserved `data/output/` bundle and former 147-column backup are historical artifacts; inspect them with Python/PyArrow, not `query-samples`. See the [query guide](query.md) for examples, read-only behavior, and limits.

## 8. Separate `filings` archive commands

```text
quant-dataset filings catalog --archive PATH --cache-root PATH --cik DIGITS... \
  --start YYYY-MM-DD --end YYYY-MM-DD [--form FORM] [--resume] [--allow-partial]
quant-dataset filings download --archive PATH [--max-filings N] [--filing-id ID]... [--secrets PATH]
quant-dataset filings parse --archive PATH --workspace-root PATH \
  [--filing-id ID]... [--max-filings N] [--prepare-dependencies] \
  [--taxonomy-host HOST]... [--secrets PATH]
quant-dataset filings verify --archive PATH
quant-dataset filings query --archive PATH --sql 'SELECT ...' [--limit N]
```

This is distinct from `download --stage financials`: catalog reads the already-populated local cache without fetching missing inputs, while download is bounded (default 5, maximum 50) and requires an existing catalog archive. Catalog, acquisition, and parse resume only within the existing run; changing catalog CIK/date/form scope requires a new root rather than a force-overwrite. Select a new archive path outside protected input/cache/output and candidate roots. Download contact is `[secrets].sec_user_agent` only; it does not require a FRED key. A blocked/unavailable/error acquisition returns nonzero with a partial summary; `needs_review` alone is informational.

`filings parse` also requires that existing archive and a separate existing workspace, defaults to at most five ready filings (maximum 50), and accepts repeated exact `cik10:accession_number` IDs. It uses only present selected documents; it does not fetch filing documents or change filing identity/dates/scope. Parsing defaults to local/offline dependency resolution and reads no secrets. `--prepare-dependencies` is an explicit opt-in to bounded taxonomy fetching; only then is `[secrets].sec_user_agent` read. Arelle still parses offline. `--taxonomy-host` can narrow the built-in known-host set, not expand it to arbitrary servers. Missing/invalid taxonomy can yield failed or partial attempts rather than empty/zero facts. The command reports bounded status/counts and diagnostic codes; actual partial/failed outcomes return nonzero. Unsupported-only results are labeled `completed_with_unsupported`.

`query` needs the optional `query` extra. It returns CSV on stdout and query metadata on stderr. SQL is one trusted local SELECT/CTE; the row limit bounds returned rows, not aggregate/sort work. Only existing manifest-listed `filings`, `documents`, `facts`, `sections`, `parses`, and `dependencies` tables are available; no placeholders are created. Descriptor checks are not full core row acceptance; use `verify` for full core archive checking, not for financial completeness. Amendments remain independent and no automatic latest-only or point-in-time logic is applied. The independent five-filing archive pilot is now persisted but remains partial (10 parser attempts: 6 full, 2 partial, 1 unsupported, 1 failed); it is not an input to the daily v3 bundle. See [filings.md](filings.md) for per-filing results and limits. The daily v3 release status and proof are separate from filing-archive status.
