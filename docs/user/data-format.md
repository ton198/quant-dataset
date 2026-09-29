**English** | [简体中文](data-format.zh-CN.md)

# Sample Data Format (data/output/)

The frozen sample bundle lives in `data/output/`, schema version `samples_v1` (see `manifest.json`). Every figure below matches the current artifacts in this repo; after a rebuild, the freshly generated `manifest.json` / `qc_report.*` are authoritative.

## 1. File inventory

| File | Size (current artifacts) | Description |
|---|---|---|
| `samples/year=YYYY/part-00000.parquet` | 36 partitions, 23,938,669 rows | Training sample long table, Hive-partitioned |
| `meta.parquet` | 6,532 rows | Per-stock summary: `asset_id`, `is_common`, `first_date`, `last_date`, `n_rows`, `missing_frac` |
| `splits.json` | — | Four-way split boundaries, purge semantics and `purged_windows`, rows per split |
| `manifest.json` | — | Schema, column list, contracts, bias declarations, output sha256 |
| `qc_report.json` / `qc_report.md` | — | QC: row counts, label stats, missing rates, cross-section sizes, extreme labels, failure details |

## 2. dtype and partitioning conventions

| Item | Convention |
|---|---|
| Partition path | `samples/year=YYYY/part-00000.parquet`, one file per year, directly glob-able |
| Compression | snappy |
| `date` | `date32[day]` (signal date, canonical session) |
| `asset_id` | string (ticker, upper-case; currently the `large_string` physical type) |
| `is_common` | bool |
| `flag_extreme_label` | uint8 |
| `f_raw_*` / `f_cs_*` / `target_return_*` / `excess_*` | float32; non-finite values are normalized to null (NaN) |
| `miss_*` | uint8 (1 = the corresponding `f_raw_*` is null) |

## 3. The 147-column overview

| Group | Columns | Count | Description |
|---|---|--:|---|
| Keys / flags | `date`, `asset_id`, `is_common`, `flag_extreme_label` | 4 | 120,510 rows (≈0.5%) have `flag_extreme_label=1` |
| Raw features | `f_raw_*` | 48 | 10 price/volume + 5 financial + 33 macro |
| Cross-sectional features | `f_cs_*` | 15 | Stock-level features ranked per day → inverse-normal CDF (≈N(0,1)); macro columns are not included |
| Missing indicators | `miss_*` | 48 | One-to-one with `f_raw_*` |
| Labels | `target_return_1d..30d` | 30 | See formulas in §5 |
| Labels | `excess_5d`, `excess_21d` | 2 | Excess over the same-date equal-weight mean benchmark of `is_common` stocks |

`f_cs_*` mapping: `f_cs_<name>` is the same-date cross-sectional transform of `f_raw_<name>`; the cross-section covers all stocks with a non-null value for that column on that date (including rows with `is_common=false`). Macro columns are excluded because they are constant within a day. Method: `(rank - 0.5) / same-date non-null count`, passed through the inverse normal CDF.

## 4. f_raw_* details

Price/volume (10):

| Column | Meaning |
|---|---|
| `f_raw_return_1d` / `_5d` / `_20d` | 1/5/20-session close-to-close returns (computed in the organize stage, past-only) |
| `f_raw_momentum_60` / `_120` | `adj_close(t)/adj_close(t-60/120) - 1` (canonical axis) |
| `f_raw_volatility_20` | 20-session rolling standard deviation of close-to-close returns (organize stage) |
| `f_raw_volatility_60` | 60-session rolling standard deviation of adjusted-close returns on the canonical axis |
| `f_raw_volume_ratio_20` | volume / 20-session mean volume (organize stage) |
| `f_raw_volume_zscore_60` | z-score of volume against its 60-session mean/standard deviation (canonical axis) |
| `f_raw_intraday_range` | `(high - low) / close` |

Financials (5, point-in-time snapshots: latest filing with `available_as_of <= signal date`):

| Column | Meaning |
|---|---|
| `f_raw_revenue_yoy` / `f_raw_net_income_yoy` / `f_raw_operating_income_yoy` / `f_raw_assets_yoy` | Year-over-year growth (prefers the same `fiscal_period` in the prior `fiscal_year`; when identifiers are missing, uses the nearest report period within ±15 days of one year earlier) |
| `f_raw_days_since_filing` | Days from the signal date to the latest available filing |

Macro (33 = 11 series × level / `_d1` / `_d5`), series:

| Series | Alias |
|---|---|
| `BAMLH0A0HYM2` | US high-yield bond OAS |
| `CPIAUCSL` / `CPILFESL` | CPI / core CPI |
| `PAYEMS` / `UNRATE` | Nonfarm payrolls / unemployment rate |
| `FEDFUNDS` / `DGS2` / `DGS10` | Fed funds rate / 2Y / 10Y Treasury yield |
| `DCOILWTICO` / `DEXUSEU` / `VIXCLS` | WTI crude / USD per EUR / VIX |

Level columns are named `f_raw_m_<SERIES>`; `_d1` and `_d5` are positional differences on the canonical date axis (no forward-fill). Visibility rule: the first session at least 1 month after the reference period (a conservative approximation, since FRED responses carry no release timestamp).

## 5. Label semantics

Formulas (`manifest.json.label_semantics`; verified by recomputation, matching bit for bit):

```text
adjusted_open = open × adj_close / close
target_return_hd = adjusted_open(t+1+h) / adjusted_open(t+1) − 1
excess_5d/21d    = target_return_5d/21d − same-date equal-weight mean of is_common rows with flag_extreme_label=0
```

```text
signal t            entry t+1          exit t+1+h
   │                    │                   │
   │  features end at t │  label window: h  │
   └────────────────────┴───────────────────┘
   h is a canonical-session positional offset; the entry and exit bars must exist
   and be positive/finite — no substitution or ffill
```

- The primary training targets are `excess_5d` / `excess_21d`; the other `target_return_*` columns are auxiliary tasks (declared in the manifest).
- At the tail of the dataset (the last ~31 signal dates) there is not enough future price data, so long-horizon labels are naturally null — expected behavior.
- `flag_extreme_label=1`: a 1..30-session label window crosses a price glitch (consecutive `adjusted_open` ratios outside [0.5, 2.0]). The row stays in the table but is excluded from the excess benchmark mean; the training layer should drop or down-weight it.

## 6. splits.json and purge

Four-way split (boundaries are already purged at build time — **do not purge again downstream**):

| split | Range | retained | before_purge | purged |
|---|---|--:|--:|--:|
| fit | 1990-01-02 – 2018-12-31 | 15,253,930 | 15,374,917 | 120,987 |
| select | 2019-01-01 – 2020-12-31 | 1,966,167 | 2,105,325 | 139,158 |
| screen | 2021-01-01 – 2024-12-31 | 5,168,902 | 5,351,300 | 182,398 |
| reserve | 2025-01-01 – 2025-12-31 | 1,549,670 | 1,549,670 | 0 |

Purge semantics: `purge_sessions=30`. The 31 signal sessions before a boundary are dropped entirely (labels enter at t+1 and exit at t+31, so the longest horizon would reach into the next split); the dataset tail has no following split and is not purged. The top-level `purge_semantics` in `splits.json` is the prose rule; `purged_windows` records the following fields for the three boundaries:

| Field | Meaning |
|---|---|
| `split_start` / `boundary_date` | Start date of the new split / its first canonical session |
| `purged_split` | Split whose rows were removed |
| `first_purged_session` / `last_purged_session` | Range of purged signal dates |
| `sessions` / `rows_removed` | 31 sessions purged; number of rows removed |

## 7. Key manifest.json fields

| Field | Description |
|---|---|
| `schema_version` | Currently `samples_v1` |
| `outputs` | 40 files → `{sha256, rows, bytes}`; `manifest.json` itself is not hashed |
| `row_counts` | `samples` total, `meta` row count, `by_year` |
| `feature_list` | 111 feature columns (48+15+48), in the same order as the feature columns in the files |
| `feature_contract` | `raw_features`, `cross_sectional_features`, `missing_indicators`, cross-sectional/macro definitions |
| `label_semantics` | Label formulas and primary-target declaration |
| `build_params` | Canonical rule (≥500 `is_common`), 9,067 sessions, purge, splits and row counts |
| `data_quality_flags` | `flag_extreme_label` rule, count, handling advice |
| `known_biases` | Survivorship, non-vintage FRED, adjusted open not executable, `is_common` suffix heuristic |
| `exclusions_applied` | Exclusion file and the `asset_ids` actually applied |
| `input_inventory` | Input inventory: 7,662 stock directories, 6,534 with `market.csv`, 1,128 without |

## 8. meta.parquet and qc_report

- `meta.parquet`: contains only stocks that produced retained samples (6,532). `missing_frac` is the share of null cells across all `f_raw_*` values for that stock; `first_date`/`last_date` bound its sample coverage.
- `qc_report.json`: `rows_total`, `rows_per_year`, `label_stats` (all/clean statistics per label), `missingness_per_feature`, `cross_section_size_per_year`, `purge`, `extreme_labels` (with up to 100 examples), `ticker_failures`.
- `qc_report.md`: a human-readable version of the above; the highest missing rates are in the financial columns (≈96%) and `BAMLH0A0HYM2` (87%) — see [recommended-usage.md](recommended-usage.md) §3 for the explanation.

## 9. Validation advice

After a rebuild, verify sha256 file by file against `manifest.json.outputs`; if row counts disagree, check `ticker_failures` and `input_inventory` in `qc_report` first. For column-level contracts and developer conventions, see [AGENT.md](../../AGENT.md).
