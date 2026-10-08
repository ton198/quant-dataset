**English** | [简体中文](data-format.zh-CN.md)

# Sample Data Format (`samples`)

This document describes the single maintained, finance-free `samples` contract for new builds. Existing bundles remain unchanged historical artifacts: the 2026-10-03 publication at `data/output/` retains `manifest.json.schema_version="samples_v3"`; the former 147-column output remains at `data/output-v1-backup-20261003T172214933236Z`, and the frozen baseline is separate. Read each bundle's actual manifest and schema. Historical labels identify artifact contents; they are not product selectors or compatibility aliases. The old publication report does not verify current source or a new build.

## 1. Candidate bundle

| File | Contents |
|---|---|
| `samples/year=YYYY/part-00000.parquet` | Signal-date/ticker rows, partitioned by year |
| `meta.parquet` | Per-asset summary including `missing_frac` over the 43 non-financial raw columns after purge |
| `splits.json` | Split boundaries and build-time purge record |
| `manifest.json` | For a new build, the single contract label `schema_version="samples"`, ordered 96-column feature list, provenance/output hashes, build parameters, and labels |
| `qc_report.json` / `qc_report.md` | Row/label/missingness/cross-section/purge/extreme-label/ticker-failure summaries; no financial coverage section |

A new `samples` build reads organized market and macro inputs plus exclusions only. It does not read financial files or financial `_meta.json` records, and it does not perform financial preflight, coverage, or input inventory. Existing SEC structured files remain separate organized data and are not sample columns or model inputs.

## 2. Types and physical column order

Parquet uses Arrow `date32` for `date`, `large_string` for ticker `asset_id`, bool for `is_common`, uint8 for `flag_extreme_label` and `miss_*`, and float32 for feature/label numerics. The canonical column order is:

```text
date, asset_id, is_common, flag_extreme_label,
43 non-financial f_raw columns,
10 f_cs columns,
43 miss columns,
32 label columns
```

| Group | Count | Description |
|---|---:|---|
| Keys / flags | 4 | `date`, `asset_id`, `is_common`, `flag_extreme_label` |
| Raw features | 43 | 10 market/derived stock features + 33 macro features |
| Cross-sectional features | 10 | Same-date rank transform for the 10 stock-level raw features; no macro ranks |
| Missing indicators | 43 | One per raw feature |
| Feature columns | **96** | 43 + 10 + 43; `manifest.feature_list` order follows physical feature order |
| Labels | 32 | 30 forward-return columns + 2 excess-return columns |
| Physical sample columns | **132** | 4 + 96 + 32 |

There are no financial raw, cross-sectional, or missing-indicator columns. The 51 prior finance-related columns (17 raw + 17 CS + 17 MISS) are removed from the active schema.

## 3. The 43 raw features

### Market/derived stock features (10)

| Columns | Definition |
|---|---|
| `f_raw_return_1d`, `f_raw_return_5d`, `f_raw_return_20d` | Existing close-to-close returns from the organized market panel |
| `f_raw_volatility_20` | Existing 20-session return volatility |
| `f_raw_volume_ratio_20` | Existing volume / 20-session mean volume |
| `f_raw_intraday_range` | `(high - low) / close` |
| `f_raw_momentum_60`, `f_raw_momentum_120` | `adj_close(t) / adj_close(t-n) - 1` on the canonical axis |
| `f_raw_volatility_60` | 60-position rolling standard deviation of adjusted-close returns on the canonical axis |
| `f_raw_volume_zscore_60` | Volume z-score against a 60-position rolling mean/std |

### Macro features (33)

The 11 configured macro series each contribute a level and positional `_d1` and `_d5` differences on the canonical axis: `BAMLH0A0HYM2`, `CPIAUCSL`, `CPILFESL`, `DCOILWTICO`, `DEXUSEU`, `DGS10`, `DGS2`, `FEDFUNDS`, `PAYEMS`, `UNRATE`, `VIXCLS`. Existing macro visibility/ffill behavior is unchanged; FRED observations are latest-revised rather than vintage.

### Cross-section and missing indicators

For each of the 10 stock-level raw features, `f_cs_<name>` is computed within each date from non-null rows using average ranks, `(rank - 0.5) / n`, and the inverse-normal transform. Non-common rows remain in the cross-section; macro columns are excluded because they are date-constant.

Each of the 43 raw columns has a `miss_<name>` indicator: 1 exactly when the corresponding raw value is null. Non-finite raw values are normalized to null before the indicator is generated.

## 4. Labels and flags

The existing 32 labels remain unchanged:

```text
adjusted_open = open × adj_close / close
target_return_hd = adjusted_open(t+1+h) / adjusted_open(t+1) - 1, h = 1..30
excess_5d/21d = target_return_5d/21d - same-date equal-weight common-stock mean
```

The excess benchmark uses `is_common=true` and `flag_extreme_label=0` rows. Signal rows enter at the next canonical session's open; intermediate bars are not substituted or filled. Labels are naturally null when the required entry/exit bars are unavailable.

`flag_extreme_label` annotates a label window that crosses an adjacent-session adjusted-open ratio outside `[0.5, 2.0]`. Flagged rows remain in the sample and are excluded from the excess benchmark mean. Signal-date splits are fit through 2018, select 2019–2020, screen 2021–2024, and reserve from 2025 onward. Applicable split boundaries purge the preceding 31 canonical-calendar signal sessions during the build; consumers should not purge again.

## 5. `meta.parquet`, manifest, and verification

`meta.parquet.missing_frac` is the number of null cells across the retained sample rows' 43 raw features divided by `retained_rows × 43`. It excludes CS/MISS columns, labels, keys/flags, and finance.

Each new manifest records the single `schema_version="samples"` identity; exact ordered 96-feature list; semantic contract and schema/semantic fingerprints; label semantics and build parameters; streamed SHA-256/byte counts for every consumed market, macro, and exclusions input; and hashed code-file/dependency identity. Input hashes are rechecked before manifest publication. `input_inventory` is separately counts-only and is not content provenance. Every registered output records SHA-256, byte count, and rows; validation and `query-samples` check all declared files. Financial files and metadata are neither read nor listed as sample inputs.

Verify a candidate with independent current-contract expected-value fixtures and invariants: row keys/flags/labels, split/purge, ticker-failure behavior, raw/CS/MISS values, and schema/order/dtypes. Do not use cross-generation projections as a correctness oracle. A candidate is not itself a release. The existing `data/output/` remains an unchanged historical artifact with manifest label `samples_v3`; the former 147-column output and frozen baseline remain separately preserved. No performance or financial-correctness claim is made.

For safe candidate construction, the CLI defaults to `--out data/samples-output`; `workspace_root` defaults to CWD or may be set with `--workspace-root` to an existing directory. The default exclusions file is under that workspace at `config/universes/exclusions_v1.json`; missing config is an error, and an explicitly supplied exclusions path is relative to CWD. The guard protects `<workspace_root>/data/` and related paths, and also recognized raw/output/baseline/archive siblings beside a normal `<data>/organized` input independently of `workspace_root`. It rejects symlink paths/ancestors, non-empty destinations, and overlap with protected paths or actual inputs. Use a fresh `--out` outside those paths; never target the preserved `data/output/`.
