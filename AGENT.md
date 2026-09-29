# AGENT navigation

**This repo = an offline data pipeline + a frozen training-sample package** (`data/raw/` → `data/organized/` → `data/output/`). Before changing code, read the §1 routing table: read only the minimum docs it points to for your task type, then edit `src/`. If the data "looks wrong", start with §6.

**English** | [简体中文](AGENT.zh-CN.md)

## 1. Task routing table (read before changing code)

| Task type | Required reading (≤2) | Code entry points | How to verify |
|---|---|---|---|
| Label / feature / split logic (`target_*`, `excess_*`, `f_cs`, `miss_*`, purge) | [samples.md](docs/developer/samples.md), [data-contracts.md](docs/developer/data-contracts.md) | `src/build_samples.py:build_samples`, `_ticker_samples`, `_add_cross_sectional_features`, `_purge_windows` | `pytest tests/test_build_samples.py -q`; after a rebuild, compare manifest row counts / split baselines with [data-contracts.md](docs/developer/data-contracts.md) §3/§4 |
| Financial extraction / as-of rules (concept whitelist, `fiscal_year/period`, `days_since_filing`) | [data-contracts.md](docs/developer/data-contracts.md), [known-quirks.md](docs/developer/known-quirks.md) | `src/download/organize_financials.py:_CONCEPTS`, `_fact_for_period`, `_fiscal_identifiers`, `organize_financials`; `src/build_samples.py:_financial_features` | `pytest tests/test_organize_financials.py tests/test_build_samples.py -q`; single-ticker smoke (AAPL/XOM), then inspect `financials.csv` |
| Download / data sources (Yahoo, SEC, FRED, universe, overrides) | [download.md](docs/developer/download.md), [architecture.md](docs/developer/architecture.md) | `src/download/{universe,market,financials,macros}.py`; `config/sources.toml`; `config/universes/ticker_cik_overrides.json` | `pytest tests/test_download.py tests/test_ticker_overrides.py -q`; `... download --dry-run` to check the plan |
| Add / change CLI options | [cli.md](docs/user/cli.md), [download.md](docs/developer/download.md) | `src/cli/main.py:_parser`, `main`; `src/download/manager.py:run_download` | `pytest tests/test_download.py -q -k cli`, `pytest tests/test_ticker_overrides.py -q -k force_rebuild`; `--help`; **must sync [cli.md](docs/user/cli.md)** (invariant 8) |
| Fix data-quality issues (missing values, outliers, exclusions, ticker→CIK) | [known-quirks.md](docs/developer/known-quirks.md), [data-contracts.md](docs/developer/data-contracts.md) | `config/universes/exclusions_v1.json`, `ticker_cik_overrides.json`; `src/build_samples.py:_load_exclusions`, `flag_extreme_label`; organize's `quality_flag`/`quality_status` | `pytest tests/test_build_samples.py tests/test_ticker_overrides.py -q`; compare with `data/output/qc_report.json` |
| Performance (organize parallelism, sample rebuild) | [architecture.md](docs/developer/architecture.md), [testing.md](docs/developer/testing.md) | `src/download/manager.py:_organize_tickers` (`--workers`, default 16); `src/build_samples.py:build_samples` (`staging_tickers`/`rank_batch_sessions`) | `pytest tests/test_organize_parallel.py -q`; time a single-ticker smoke; manifest hashes / row counts unchanged after the rebuild |
| Add tests | [testing.md](docs/developer/testing.md) | `tests/` (6 offline test files) | `.venv/bin/python -m pytest -q` (baseline 61 passed / 2 skipped) |
| Verify rebuilt artifacts (hashes / rows / QC) | [data-contracts.md](docs/developer/data-contracts.md), [samples.md](docs/developer/samples.md) | `data/output/manifest.json` `outputs`; `data/output/splits.json`; `data/output/qc_report.json`; `src/build_samples.py:_hash_output_files` | verify hashes with the §3.6 snippet in [data-contracts.md](docs/developer/data-contracts.md); compare rows/purge/flags with the §4 baselines |

## 2. Invariants (confirm none are broken before changing any code)

1. **raw is append-only**: SEC/FRED/universe payloads are content-addressed (filename = content sha256) and never rewritten; rebuilds only add files. Yahoo market files are named `<start>_<end>.csv` and a re-download overwrites them, but **no file under `data/raw/` may ever be edited by hand**.
2. **Determinism**: same inputs must give same outputs — stable sorts (`mergesort`), JSON `sort_keys=True`, and every output hash registered in `manifest.json`; organize is idempotent when re-run.
3. **No lookahead**: labels enter at the `t+1` open and exit at the `t+1+h` open (`adj_open = open × adj_close / close`); financial as-of = `latest available_as_of ≤ signal day`; macro uses diffs along canonical axis order.
4. **Manifest hashes**: `data/output/manifest.json.outputs` records `sha256`/`bytes`/`rows` for every output (except `manifest.json`'s self-reference); consumers verify by hash.
5. **Structural invariants**: a rebuild that only changes financial features **must not** move row counts, labels, or flags. Current baseline: `23,938,669` rows; purge `120,987 / 139,158 / 182,398` (fit/select/screen); `flag_extreme_label` `120,510`. Row-count changes are only allowed from purge or label-logic changes, and must sync [data-contracts.md](docs/developer/data-contracts.md).
6. **Purge happens at build time**: the 31 signal sessions before each split boundary are dropped inside build-samples (three boundaries: fit/select/screen, see [data-contracts.md](docs/developer/data-contracts.md) §3.5); consumers do not purge again.
7. **Non-finite values become missing**: ±inf/NaN in raw features must be converted to missing, with the matching `miss_*=1`; never write inf into samples.
8. **CLI contract sync**: any change to `download` / `build-samples` options, defaults, or exit codes must update [cli.md](docs/user/cli.md) in the same PR.

## 3. Command cheat sheet

```bash
# Full test suite (offline; baseline 61 passed / 2 skipped, ~40s)
.venv/bin/python -m pytest -q

# Single-ticker smoke: no download, rebuild only the organized AAPL/XOM outputs (reads the raw cache)
PYTHONPATH=src .venv/bin/python -m cli.main download --stage organize --force-rebuild --tickers AAPL,XOM

# Full rebuild step 1: download + organize (market stage requires --start/--end; --workers defaults to 16)
PYTHONPATH=src .venv/bin/python -m cli.main download --stage all --start 1990-01-01 --end 2025-12-31 --workers 16

# Full rebuild step 2: sample package (defaults data/organized → data/output)
PYTHONPATH=src .venv/bin/python -m cli.main build-samples

# Plan only, write nothing; check which stages/tickers will run
PYTHONPATH=src .venv/bin/python -m cli.main download --stage all --start 1990-01-01 --end 2025-12-31 --dry-run

# Pin a single test file
.venv/bin/python -m pytest tests/test_build_samples.py -q
```

Timing reference (measured locally): the full `pytest` run is ~40s; a single-ticker smoke (AAPL,XOM) is ~20s; a full download is hours (bounded by the network and throttling: Yahoo 0.5s/request, SEC 0.2s/request); `build-samples` recomputes 23.9M rows and writes yearly parquet — minutes to hours on one machine, depending on CPU/disk/concurrency; the tail of the most recent run (label QC + manifest hashing) took ~3.6 minutes. Current artifact sizes: `data/organized` ≈ 9.3G, `data/output` ≈ 6.3G.

## 4. Documentation map

| Doc | One-liner |
|---|---|
| [AGENT.md](AGENT.md) / [AGENT.zh-CN.md](AGENT.zh-CN.md) | This file: task routing table + invariants + command cheat sheet |
| [architecture.md](docs/developer/architecture.md) / [architecture.zh-CN.md](docs/developer/architecture.zh-CN.md) | Five-stage data flow, module responsibilities, data locations, concurrency and determinism |
| [data-contracts.md](docs/developer/data-contracts.md) / [data-contracts.zh-CN.md](docs/developer/data-contracts.zh-CN.md) | Field contracts for raw/organized/output, manifest/splits/qc structure, baseline numbers |
| [download.md](docs/developer/download.md) / [download.zh-CN.md](docs/developer/download.zh-CN.md) | Download & organize deep dive: SEC/Yahoo/FRED endpoints, progress locks, resume, CIK overrides |
| [samples.md](docs/developer/samples.md) / [samples.zh-CN.md](docs/developer/samples.zh-CN.md) | Sample building deep dive: canonical calendar, feature/label implementation, purge, partition writes |
| [testing.md](docs/developer/testing.md) / [testing.zh-CN.md](docs/developer/testing.zh-CN.md) | Test layout, how to run, offline conventions, tmp-data fixtures |
| [known-quirks.md](docs/developer/known-quirks.md) / [known-quirks.zh-CN.md](docs/developer/known-quirks.zh-CN.md) | The "looks like a bug" list caused by data-source realities |
| [cli.md](docs/user/cli.md) / [cli.zh-CN.md](docs/user/cli.zh-CN.md) | CLI user manual: commands, options, exit codes (source of the CLI contract) |
| [data-format.md](docs/user/data-format.md) / [data-format.zh-CN.md](docs/user/data-format.zh-CN.md) | User-side output format (how to read the sample package) |
| [recommended-usage.md](docs/user/recommended-usage.md) / [recommended-usage.zh-CN.md](docs/user/recommended-usage.zh-CN.md) | Recommended usage and caveats |

## 5. Maintenance conventions

- When docs and reality disagree, reality (`src/`, `config/`, `data/output/manifest.json`) wins; fix the docs in the same PR.
- **Bilingual sync**: any documentation change must update both language versions in the same PR; if they disagree, the English version is authoritative.
- A rebuild that only changes financial features is the most common regression check: row counts, labels, and flags must be bit-for-bit unchanged (invariant 5).
- After a rebuild, verify every output by hash against `data/output/manifest.json` (see [data-contracts.md](docs/developer/data-contracts.md) §3.6).

## 6. Data looks wrong?

Read [known-quirks.md](docs/developer/known-quirks.md) first: most "anomalies" are data-source realities (missing bars, non-calendar fiscal years, FRED revised values, unadjusted split jumps, etc.), not code bugs. Once you have confirmed a bug, route to the matching docs and code entry points via §1.
