# quant-dataset

An offline pipeline for US-equity market/macro data and finance-free sample bundles. The maintained sample contract is a single unversioned `samples` product; existing output artifacts are preserved separately and are not rewritten by documentation or code migration. This repository does not train models or run backtests.

[![CI](https://github.com/ton198/quant-dataset/actions/workflows/ci.yml/badge.svg)](https://github.com/ton198/quant-dataset/actions/workflows/ci.yml)

**English** | [简体中文](README.zh-CN.md)

## Current contract and historical artifacts

The single `samples` contract is finance-free and contains **132 physical columns**: 4 keys/flags + 43 raw features + 10 cross-sectional features + 43 missing indicators + 32 labels. Its ordered `manifest.feature_list` contains 96 features. The builder reads organized market/macro inputs and exclusions only; it does not read financial files or financial metadata. Financial status, where retained in an artifact, is `not_applicable` and does not claim coverage.

**Archived publication record (2026-10-03):** the existing `data/output/` bundle was published with the historical manifest value `schema_version="samples_v3"`: **23,938,669 rows**, **6,532 tickers**, **132 physical columns**, and **96 features**. Treat this bundle as an unchanged, read-only artifact; this documentation does not republish it or rewrite its manifest. The former 147-column output remains at `data/output-v1-backup-20261003T172214933236Z`, and the frozen `data/baselines/samples_v1_financial_upgrade/` remains separately preserved. Its publication record reports an invariant projection with 0 differences; the 735-passed/2-skipped suite evidence was reused because code was unchanged, not rerun at publication. This is historical evidence only, not a result for current code or a new candidate, and makes no performance or financial-correctness claim. Records: `/tmp/opencode/v3_release_publication/report.json` and `data/.output-publication-20261003T172214933236Z.json` (`COMMITTED_V3_V1_BACKUP_RETAINED`).

| Preserved historical `samples_v1` output/baseline | Recorded value |
|---|---|
| Rows × physical columns | 23,938,669 × 147 |
| Stocks in `meta.parquet` | 6,532 |
| Canonical sessions | 9,067 |
| Coverage | 1990-01-02 – 2025-12-31 |

A *canonical session* is a shared trading-calendar date with at least 500 common stocks (`is_common`) present.

## What one sample row is

One row represents one ticker (`asset_id`) on one signal date (`date`). The `samples` contract has 43 raw market/macro features, 10 same-date cross-sectional transforms, 43 matching missing indicators, and 32 forward labels. Financial data is not a sample input.

```text
signal day t             entry at t+1 open          exit at t+1+h open
    │                          │                            │
    │  features end at t       │  label window: h sessions
    └──────────────────────────┴────────────────────────────┘
      h = 1..30 canonical sessions; excess targets use h = 5 and 21
```

## Build a separate candidate

By default, the samples workspace root is the current working directory; set `--workspace-root PATH` to choose it explicitly. The default exclusions file is `<workspace_root>/config/universes/exclusions_v1.json`; a missing file is an error, and an explicit `--exclusions-file` path is resolved relative to the current working directory. The filename remains `exclusions_v1.json` and is not a sample product version.

Build candidates only to a fresh output path. The guard protects `<workspace_root>/data/` and related raw/output/baselines locations identified from actual inputs; it rejects symlink targets, non-empty targets, and targets overlapping protected paths or actual inputs. Never target the preserved `data/output/`, backup, or baseline; if the example path exists, choose another unused path rather than deleting/reusing it.

```bash
WORKSPACE_ROOT=$PWD
CANDIDATE_DIR=/tmp/opencode/candidate-samples-finance-free
test ! -e "$CANDIDATE_DIR" && test ! -L "$CANDIDATE_DIR" || { echo "Choose a new, unused, non-symlink candidate directory" >&2; exit 1; }
PYTHONPATH=src .venv/bin/python -m cli.main build-samples \
  --workspace-root "$WORKSPACE_ROOT" \
  --data-dir data/organized --out "$CANDIDATE_DIR"
```

A candidate build needs existing organized market and macro panels. It does not read SEC financial files or financial metadata, run financial preflight/coverage/inventory, or download data. Verify new behavior with small independent current-contract fixtures and recorded invariants; a historical projection comparison is not the correctness oracle. Each publication needs its own recorded evidence. The historical release record above is not a test result for the current source tree.

## Query an existing bundle

Bundles stay as Parquet files. If you want the optional SQL command, install its extra and point it at the bundle you intend to read:

```bash
uv sync --frozen --extra query
BUNDLE_DIR=/tmp/opencode/candidate-samples-finance-free
quant-dataset query-samples --bundle "$BUNDLE_DIR" \
  --sql 'SELECT COUNT(*) AS row_count FROM samples'
```

`query-samples` accepts only a current `samples` bundle and validates its strict manifest/schema and registered output hashes before querying; preserved historical bundles with `samples_v3` or the former 147-column layout are not supported by this command. Inspect those historical artifacts with Python/PyArrow, or query a fresh candidate such as the path above. Queries read in place and do not create a persistent database or migrate data. The regular pipeline install is unchanged. See the [query guide](docs/user/query.md) for date filters, result limits, and read-only rules.

## Existing download/organize pipeline

The existing `download` and `organize` commands are unchanged. The SEC `financials` stage still fetches Company Facts, submissions, and historical submissions-page JSON; it does not fetch full filing HTML/iXBRL. Separately, bounded `filings catalog/download/parse/verify/query` commands use explicit archive paths and the local cache. Two private parser pilots remain independent: first-five archive v7 and second-ten archive v15. They are not `samples` inputs or broad issuer-coverage claims; the v15 active snapshot contains 25,656 facts and one primary-anchored RBC IXDS group with 7,543 original occurrences, while RBC raw coverage remains partial. Organized `financials.csv` and selected `financial_events_v1` extracts remain separate products; the latter covers selected nine-concept facts, not all XBRL or complete filings. See the [filing guide](docs/user/filings.md), [CLI manual](docs/user/cli.md), [download internals](docs/developer/download.md), and [archive status](docs/developer/financial-filing-archive.md).

## Documentation

| User guides | Developer docs |
|---|---|
| [CLI manual](docs/user/cli.md) — existing downloads, candidate build, and CLI commands | [AGENT.md](AGENT.md) — task routing and invariants |
| [Filing guide](docs/user/filings.md) — bounded catalog, acquisition, parsing, verification, query, pilot evidence, and limits | |
| [Query guide](docs/user/query.md) — optional SQL over existing sample Parquet bundles | |
| [Data format](docs/user/data-format.md) — the single samples contract and preserved artifacts | [architecture.md](docs/developer/architecture.md) — data flow and input separation |
| [Recommended usage](docs/user/recommended-usage.md) — candidate consumption caveats | [data-contracts.md](docs/developer/data-contracts.md) — layers, active contract and archived schema facts |
| | [samples-architecture.md](docs/developer/samples-architecture.md) — package and migration contract |
| | [samples.md](docs/developer/samples.md) — sample features, labels, and current verification cues |
| | [download.md](docs/developer/download.md) — existing source behavior |
| | [financial-filing-archive.md](docs/developer/financial-filing-archive.md) — implemented archive boundary and deferred processing status |
| | [known-quirks.md](docs/developer/known-quirks.md) — data-source limitations |
| | [testing.md](docs/developer/testing.md) — offline tests and validation |

## Known limitations

- **Survivorship:** the universe is based on a current SEC ticker list and does not provide delisted-stock history.
- **FRED is not vintage:** macro inputs use latest-revised values and an approximate visibility date.
- **Adjusted open is not an executable fill:** label prices are adjusted values, not simulated trades or costs.
- **Financial data is separate:** financial features and financial missingness are absent from the `samples` contract. Existing structured SEC extracts remain available separately but do not constitute a complete filing archive.
- **Legacy bundles are not product modes:** the archived `data/output/` bundle retains its original `samples_v3` manifest value, and the former 147-column artifact is preserved at `data/output-v1-backup-20261003T172214933236Z`. These paths are read-only historical artifacts, not version-selectable `samples` products; the frozen baseline remains separately retained.

More detail: [known-quirks.md](docs/developer/known-quirks.md).

## License

MIT — see [LICENSE](LICENSE).
