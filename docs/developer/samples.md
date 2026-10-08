**English** | [简体中文](samples.zh-CN.md)

# Sample Building Internals (`src/samples/`)

> The maintained contract is one finance-free `samples` product. The 2026-10-03 publication at `data/output/` retains its original `schema_version="samples_v3"` manifest and is an unchanged, read-only historical artifact, not a new or current publication. The former 147-column output and frozen baseline are also preserved separately. Its release report and reused 735-passed/2-skipped test evidence are historical only; they do not verify the current source tree. Any retained financial status `not_applicable` is not financial coverage.
> The samples package migration is implemented in the current source tree; current-contract test/release gates are tracked separately and no full-gate pass is implied here. See [samples-architecture.md](samples-architecture.md), [data-contracts.md](data-contracts.md), [architecture.md](architecture.md), [AGENT.md](../../AGENT.md), and [known-quirks.md](known-quirks.md).

## 1. Inputs, outputs, and safe build

The sample builder consumes the organized market and macro panels plus the configured exclusions. It does **not** read `financials.csv`, `financial_events.parquet`, `financial_facts.parquet`, or financial `_meta.json` records; it performs no financial preflight, coverage calculation, or financial input inventory. The current manifest has no financial-status field or financial-coverage claim.

The existing SEC download/organize workflow is separate and unchanged. Its structured `financials.csv` and `financial_events_v1` artifacts remain available in their existing locations, but neither is a `samples` input. `financial_events_v1` is a selected nine-concept structured extract, not a complete XBRL dataset or full filing archive.

The `samples.builder.build_samples` keyword `workspace_root=None` defaults to the current working directory; the CLI exposes the same setting as `--workspace-root PATH`. When `--exclusions-file` is omitted, the builder reads `<workspace_root>/config/universes/exclusions_v1.json`. An explicitly supplied exclusions path is resolved relative to the current working directory. A missing exclusions file is an error; there is no implicit empty-list fallback. Keep the existing filename `exclusions_v1.json`—it is a config filename, not a sample product version.

Build into a new, separate output directory. The CLI defaults to `--out data/samples-output`. The guard protects `<workspace_root>/data/` and related raw/output/baselines paths; for a normal `<data>/organized` input it also protects recognized raw/output/baseline/archive siblings beside `<data>` independently of `workspace_root`. The explicit workspace root must exist. It rejects symlink paths/ancestors, non-empty targets, and targets overlapping protected paths or actual inputs. Do not write over a historical bundle or baseline.

```bash
WORKSPACE_ROOT=$PWD
CANDIDATE_DIR=/tmp/opencode/candidate-samples-finance-free
# Pick another unused path if this one already exists; do not remove old data to reuse it.
test ! -e "$CANDIDATE_DIR" && test ! -L "$CANDIDATE_DIR" || { echo "Choose a new, unused, non-symlink candidate directory" >&2; exit 1; }
PYTHONPATH=src .venv/bin/python -m cli.main build-samples \
  --workspace-root "$WORKSPACE_ROOT" \
  --data-dir data/organized --out "$CANDIDATE_DIR"
```

Workspace, default config, and protected paths are not inferred from the installed source-package location.

No new financial download or filing-archive operation is required for a `samples` build.

## 2. Single `samples` feature inventory

| Group | Definition | Count |
|---|---|---:|
| Non-financial raw features | 10 market/derived stock features + 33 macro features | 43 |
| Cross-sectional features | One `f_cs_*` mirror for each of the 10 stock-level raw features; macro is excluded | 10 |
| Missing indicators | One `miss_*` for each of the 43 raw features | 43 |
| `manifest.feature_list` | Raw + CS + MISS | **96** |
| Labels | `target_return_1d..30d`, `excess_5d`, `excess_21d` | 32 |
| Physical sample columns | 4 keys/flags + 96 features + 32 labels | **132** |

The canonical physical order is:

```text
date, asset_id, is_common, flag_extreme_label,
43 non-financial f_raw columns,
10 f_cs columns,
43 miss columns,
32 labels
```

Physical dtypes are Arrow `date32` for `date`, `large_string` for `asset_id`, bool for `is_common`, uint8 for `flag_extreme_label`/`miss_*`, and float32 for raw/CS/label numeric columns.

The 10 stock raw features retain the existing six market passthroughs (`return_1d/5d/20d`, `volatility_20`, `volume_ratio_20`, `intraday_range`) and four derived features (`momentum_60/120`, `volatility_60`, `volume_zscore_60`). The 33 macro raw columns remain 11 series × level / positional `_d1` / `_d5`.

All 51 prior financial columns are removed from samples: 17 financial raw values, 17 cross-sectional mirrors, and 17 missingness indicators. This includes filing ages, financial ages, levels, growth rates, ratios, and quarter-over-quarter fields. No finance value is substituted or carried into another sample column.

## 3. Preserved sample semantics

The current behavioral contract preserves the defined market/macro raw and CS/MISS calculations, label semantics, `(date, asset_id)` keys, flags, split assignment, purge behavior, extreme-label logic, and ticker-failure handling. Test formulas and edge cases against independent current-contract fixtures; a historical v1 projection is not the correctness oracle.

- Canonical calendar: dates with at least `canonical_min_tickers` common tickers (default 500) present in the market panel.
- Cross-sectional transform: per date and stock raw feature, average rank among non-null rows → `(rank - 0.5) / n` → inverse-normal transform. `is_common=false` rows remain included; macro columns are not ranked.
- Missing indicators: `miss_* = 1` exactly when the corresponding raw feature is null; raw non-finite values are normalized to null first.
- Labels: `adjusted_open = open × adj_close / close`; for signal position `t`, entry is `t+1` and exit is `t+1+h`, for `h=1..30`. `excess_5d/21d` subtract the same-date equal-weight target mean for `is_common=true` and `flag_extreme_label=0` rows.
- Splits are assigned by signal date: fit ≤ 2018, select 2019–2020, screen 2021–2024, and reserve from 2025 onward.
- Purge: remove 31 canonical-calendar signal sessions before each applicable split boundary at build time when the later window has sessions. Consumers do not purge again.

### `meta.parquet`

`missing_frac` is recomputed after purge using only the 43 non-financial raw columns:

```text
null raw cells / (retained rows × 43)
```

It does not include CS columns, missing indicators, keys/flags, labels, or any financial field.

## 4. Manifest, inventory, and QC

A newly built manifest identifies the single contract as `schema_version="samples"`, with its ordered 96-column feature list, semantic contract, schema/semantic fingerprints, label semantics, and build parameters. `input_provenance.files` records the path, streamed SHA-256, and byte count for every consumed market/macro/exclusion file; input hashes are rechecked before manifest publication. `code_identity` records hash/byte identity for all five samples package files and NumPy/pandas/PyArrow versions. `input_inventory` is counts-only, not a content inventory. Every registered output records SHA-256, bytes, and rows. Financial files and finance metadata are not read, preflighted, covered, or listed as sample inputs.

QC continues to describe row counts, labels, raw/CS/MISS missingness, cross-section sizes, purge, extreme-label rows, and ticker failures. There is no financial coverage report or financial feature inventory in the active contract.

## 5. Verification and release status

Verify a fresh candidate using current-contract fixtures and invariants. Checks should cover:

1. The 96 ordered features and 132 physical columns, including dtypes and null/missing-indicator semantics.
2. Independent expected-value cases for labels, missing entry/exit, horizon edges, split boundaries, and extreme-value behavior.
3. No financial columns or financial-file/metadata reads, preflight, coverage, or inventory.
4. The `meta.missing_frac` denominator of retained rows × 43 raw features.
5. Determinism, safe output paths, actual input provenance, and output hashes, according to verified implementation behavior.

Use current small fixtures under `tests/fixtures/samples/current/` and the current sample tests (`tests/test_samples_current_contract.py`, `tests/test_samples_semantic_regression.py`, `tests/test_samples_integrity_regression.py`, `tests/test_samples_safety_regression.py`, `tests/test_query_samples.py`, `tests/test_verification_tools.py`, and `tests/test_build_samples.py` as relevant) with expected values independent of the builder. Do not invoke removed cross-generation projection/baseline tools as a correctness oracle; no compatibility shims remain. The archived 2026-10-03 publication report and its reused test evidence are historical only; do not infer current test status, performance, or financial correctness from them. Preserve existing output, backup, and baseline directories unchanged.

## 6. Verification command

```bash
# Run only task-assigned current-contract tests from the current tests/ tree.

CANDIDATE_DIR=/tmp/opencode/candidate-samples-finance-free
# Ensure the candidate path is unused before building; do not overwrite existing artifacts.
test ! -e "$CANDIDATE_DIR" && test ! -L "$CANDIDATE_DIR" || { echo "Choose a new, unused, non-symlink candidate directory" >&2; exit 1; }
PYTHONPATH=src .venv/bin/python -m cli.main build-samples \
  --workspace-root "$PWD" --data-dir data/organized --out "$CANDIDATE_DIR"
```

Run only tests and verification assigned by the task owner. Do not treat archived outputs or historical verification reports as a substitute for independent current-contract fixtures.
