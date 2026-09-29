**English** | [简体中文](data-contracts.zh-CN.md)

# Data Contracts

This document is the schema and semantic contract for every data layer (`data/raw/` → `data/organized/` → `data/output/`) and the basis for verifying a rebuild. All figures match the current repository artifacts (generated 2026-09-29); after a rebuild, the freshly generated `manifest.json` / `qc_report.*` / `splits.json` are authoritative.

For the consumer-side guide see [../user/data-format.md](../user/data-format.md); for sample-build implementation details see [samples.md](samples.md); for download and organize implementation details see [download.md](download.md).

## 0. Contract layers

| Layer | Path | Contract strength | Change impact |
|---|---|---|---|
| raw | `data/raw/` | Payloads are content-addressed and append-only; `manifest.json` records key → versions | Appends do not affect downstream; but organized data does not automatically follow |
| organized | `data/organized/` | The contract in this section; each output records input/output sha256 in `_meta.json` | Changing columns/semantics → must rebuild `data/output/` |
| output | `data/output/` | `samples_v1`; `manifest.json.outputs` records sha256/bytes/rows for every output | Contract changes must bump `schema_version` and update this document |

## 1. data/raw/

### 1.1 Yahoo market data

```
data/raw/yahoo/<TICKER>/<start>_<end>.csv        # start/end are download --start/--end
```

- Columns (written by yfinance as-is, `auto_adjust=false, actions=true`): `date`(index), `Adj Close`, `Close`, `Dividends`, `High`, `Low`, `Open`, `Stock Splits`, `Volume`.
- **Not content-addressed**: re-downloading the same ticker over the same range overwrites the same-name file; failed/empty downloads write nothing — so "no directory" means the ticker has no market data (currently 1,128 such tickers, see §4).
- The range in the filename is the CLI value, not the range the data actually covers; trimming to sessions happens later, in organize.

### 1.2 SEC universe snapshot

```
data/raw/sec/universe/<sha256(content)>.json
```

- Raw `company_tickers_exchange` JSON: `{"fields": ["cik","name","ticker","exchange"], "data": [[...], ...]}`.
- The reader requires the filename stem == content sha256, otherwise the file is skipped; among multiple snapshots it takes the first valid one in sorted order. Exchange filtering (Nasdaq/NYSE) happens at read time.

### 1.3 SEC financials (content-addressed + manifest)

```
data/raw/sec/financials/
  <sha256>.json          # companyfacts / submissions / submissions-page payload
  manifest.json          # {"resources": {"<logical_key>": [entry, ...]}}
```

| `logical_key` | Corresponding URL / content |
|---|---|
| `companyfacts:<cik10>` | `https://data.sec.gov/api/xbrl/companyfacts/CIK<cik10>.json` |
| `submissions:<cik10>` | `https://data.sec.gov/submissions/CIK<cik10>.json` (`filings.recent`) |
| `submissions-page:<cik10>:<name>` | Historical page `CIK<cik10>-submissions-<...>.json` |

Fields of each `manifest.json` entry (version):

| Field | Type | Semantics |
|---|---|---|
| `path` | string | Filename relative to `data/raw/sec/financials/` (= `<sha256>.json`) |
| `sha256` | string | File content sha256, verified on read |
| `url` | string | Request URL (one of the cache match keys) |
| `fetched_at_utc` | string | Fetch timestamp (UTC ISO) |
| `attempts` | int | Number of network attempts |
| `byte_size` | int | Size in bytes |
| `status` | string | Currently always `"done"` |

- The same key can accumulate multiple versions; reads take the **first sha256-valid version scanning in reverse order** for `(logical_key, url)`, and a cache hit makes no network request.
- `manifest.json` itself is not content-addressed (updated in place via tmp + rename); all other payloads are never rewritten.

### 1.4 FRED

```
data/raw/fred/<SERIES_ID>/<sha256>.json          # FRED observations JSON
data/raw/fred/manifest.json                      # entry structure same as 1.3
```

- `logical_key = observations:<SERIES_ID>`; `path` has the form `<SERIES_ID>/<sha256>.json`.
- 11 series currently: `BAMLH0A0HYM2`, `CPIAUCSL`, `CPILFESL`, `DCOILWTICO`, `DEXUSEU`, `DGS10`, `DGS2`, `FEDFUNDS`, `PAYEMS`, `UNRATE`, `VIXCLS` (`config/sources.toml [macros].series`).

## 2. data/organized/

### 2.1 stocks/<TICKER>/market.csv

| Column | Type | Semantics |
|---|---|---|
| `date` | string `YYYY-MM-DD` | XNYS session; rows = Yahoo range ∩ organize calendar, sorted by date |
| `open` / `high` / `low` / `close` | float64 | Raw (unadjusted) OHLC |
| `adj_close` | float64 | Yahoo adjusted close |
| `volume` | int64 | Raw volume |
| `adjustment_factor` | float64 | `adj_close / close` (the label-side adj_open recomputes the same ratio inside build-samples) |
| `return_1d` / `return_5d` / `return_20d` | float64 | `close.pct_change(n)`; the first n rows are empty |
| `volatility_20` | float64 | 20-session rolling standard deviation of `return_1d` (ddof=1) |
| `volume_ratio_20` | float64 | `volume / volume.rolling(20).mean()` |
| `intraday_range` | float64 | `(high - low) / close` |
| `quality_flag` | string | `ok` / `invalid_ohlc` / `negative_price`, decided row by row from OHLC+volume validity |

- Row counts vary with IPO/delisting/data gaps (currently 6,534 files and 3,377 distinct row counts); in the full baseline the largest single file has 9,067 rows (= the number of sample canonical sessions), and a rerun with a later organize end date can be longer.
- Duplicate dates keep the last row; non-session rows are dropped during organize (counted in `_meta.row_counts.market_dropped_non_session`).

### 2.2 stocks/<TICKER>/financials.csv

One row per session, wide table; content = the latest filing snapshot visible at that session.

| Column | Type (read convention) | Semantics |
|---|---|---|
| `date` | string `YYYY-MM-DD` | organize calendar session |
| `available_as_of` | string / null | The snapshot's SEC `filingDate`; visibility rule **filingDate < session** (visible from the next session onward) |
| `accession_number` | string / null | SEC accession |
| `form` | string / null | Form type as-is (including `/A`) |
| `is_amendment` | bool / null | `form.endswith("/A")` |
| `fiscal_year` | nullable Int64 | Fiscal year covered by the filing; null when undeterminable |
| `fiscal_period` | string / null | `Q1`..`Q4` / `FY` |
| `report_period_end` | string / null | Period end covered by the filing |
| `days_since_filing` | int / null | `session - filingDate` |
| `revenue` `gross_profit` `operating_income` `net_income` `operating_cash_flow` `capital_expenditure` `assets` `liabilities` `equity` | float / null | Whitelisted concept extraction results (next table); null = the filing has no matching fact |
| `quality_status` | string | `ok` (at least one concept non-null) / `missing` (no concept) / `amendment_only` (amendment with no original filing for the same period end) |

Concept whitelist (first tag with a value wins, in order; from `organize_financials._CONCEPTS`):

| Output column | Tag priority (high → low) |
|---|---|
| `revenue` | `Revenues`, `RevenueFromContractWithCustomerExcludingAssessedTax`, `RevenueFromContractWithCustomerIncludingAssessedTax`, `SalesRevenueNet`, `SalesRevenueGoodsNet`, `SalesRevenueServicesNet`, `SalesRevenueGoodsGross`, `SalesRevenueServicesGross`, `RevenuesNetOfInterestExpense`, `FinancialServicesRevenue`, `InsuranceServicesRevenue`, `RevenueNotFromContractWithCustomer`, `SalesRevenueOilGas` |
| `net_income` | `NetIncomeLoss`, `ProfitLoss`, `NetIncomeLossAvailableToCommonStockholdersBasic`, `NetIncomeLossAvailableToCommonStockholdersDiluted` |
| `operating_income` | `OperatingIncomeLoss` |
| `gross_profit` | `GrossProfit` |
| `operating_cash_flow` | `NetCashProvidedByUsedInOperatingActivities` |
| `capital_expenditure` | `PaymentsToAcquirePropertyPlantAndEquipment` |
| `assets` | `Assets`, `AssetsNet` |
| `liabilities` | `Liabilities` |
| `equity` | `StockholdersEquity` |

Fact selection and fiscal identifiers:

- Only facts from the same filing (`filed` + `end` + optional `fy`/`fp`/`form`/`accn`) are matched; 10-Q prefers single-quarter spans of 70–125 days, 10-K prefers annual spans of 300–400 days (instant facts match on `end`); ties go to the duration closest to 91/365 days.
- `fiscal_year`/`fiscal_period` are taken in order from: the selected fact's `fy`/`fp` → submissions metadata → the filing's own `fy`/`fp` → for 10-K, report year + `FY` → for 10-Q, quarters counted from the previous 10-K (Q1..Q3) → otherwise null.
- Row count = the length of the session calendar at organize time; current artifacts are mixed (9,067 rows × 1,118 files; 9,252 rows × 6,539; a few at 9,250). build-samples only consumes snapshots with `available_as_of ≤ signal date`, regardless of file row count.

### 2.3 shared/macro.csv

| Column | Type | Semantics |
|---|---|---|
| `date` | string `YYYY-MM-DD` | organize calendar session |
| 11 series (same as §1.4) | float64 | Session-aligned, forward-filled (ffill) observation values |

- Visibility rule (conservative proxy): the observation reference period + 1 calendar month approximates the release date, and the value becomes visible from the first session **strictly after** that date; gaps are ffilled on the session axis.
- FRED responses carry no release dates — this is a known approximation (recorded in `_meta.json.known_issues`), not a bug.

### 2.4 _meta.json (one per ticker and one under shared/)

| Field | Semantics |
|---|---|
| `ticker` | Directory name in uppercase; `"shared"` for shared |
| `generated_at_utc` | Timestamp of the most recent organize |
| `cleaning_rules_version` | Currently `"v1"` |
| `inputs` | `[{path, sha256}]`, the raw files actually consumed |
| `outputs` | `[{path, sha256, rows}]`, the organized outputs |
| `row_counts` | `market_input`/`market_output`/`market_dropped_non_session`, or `financials_input` (filing count)/`financials_output` (session rows)/`macro_output` |
| `known_issues` | Manual/programmatic notes on semantics |

- `manager._ticker_is_organized` uses this file plus file existence/hash checks to decide whether to skip.

## 3. data/output/ (sample bundle, schema_version = samples_v1)

### 3.1 File inventory

| File | Size (current artifacts) | Notes |
|---|---|---|
| `samples/year=YYYY/part-00000.parquet` | 36 files, 23,938,669 rows | Hive-partitioned by signal-session year, snappy |
| `meta.parquet` | 6,532 rows | Per-ticker summary (see §3.4) |
| `manifest.json` | — | Contract, parameters, biases, sha256 of all outputs |
| `splits.json` | — | Four-window boundaries, purge semantics and row counts (see §3.5) |
| `qc_report.json` / `qc_report.md` | — | QC (see §3.7) |

### 3.2 samples column contract (147 columns)

Column order is fixed: 4 keys/flags + 48 `f_raw` + 15 `f_cs` + 48 `miss` + 30 `target_return_*` + 2 `excess_*`. Parquet dtypes: `date`=date32, `asset_id`=large_string, `is_common`=bool, `flag_extreme_label` and all `miss_*`=uint8, all other numerics=float32.

**Keys and flags (4)**

| Column | dtype | Semantics |
|---|---|---|
| `date` | date32 | Signal session (canonical calendar) |
| `asset_id` | large_string | Ticker (uppercase) |
| `is_common` | bool | Heuristic flag for non-unit/warrant/preferred/right tickers (suffix rule); false rows are retained |
| `flag_extreme_label` | uint8 | See §3.3 |

**f_raw: stock-level (15)**, `float32`

| Column | Semantics |
|---|---|
| `f_raw_return_1d` / `_5d` / `_20d` | Taken directly from the same-name market.csv columns (raw close returns) |
| `f_raw_volatility_20` | market.csv `volatility_20` |
| `f_raw_volume_ratio_20` | market.csv `volume_ratio_20` |
| `f_raw_intraday_range` | market.csv `intraday_range` |
| `f_raw_momentum_60` / `_120` | `adj_close[t]/adj_close[t-n] - 1` on the canonical axis, requiring both endpoints positive and finite |
| `f_raw_volatility_60` | 60-period rolling standard deviation (ddof=1) of adjusted-close daily returns on the canonical axis |
| `f_raw_volume_zscore_60` | `(volume - mean60) / std60` (ddof=1; null when std≤0 or fewer than 60 periods) |
| `f_raw_revenue_yoy` / `f_raw_net_income_yoy` / `f_raw_operating_income_yoy` / `f_raw_assets_yoy` | Year-over-year on the as-of snapshot: `current/prior - 1`; prior prefers the same `(fiscal_year-1, fiscal_period)`, otherwise the nearest period end within ±15 days of report_period_end − 1 year; null when prior is missing or 0 |
| `f_raw_days_since_filing` | `signal date - available_as_of` (days) |

**f_raw: macro (33)**, `float32`, aligned position-by-position on the canonical axis:

| Series (11) | Derived columns |
|---|---|
| `BAMLH0A0HYM2`, `CPIAUCSL`, `CPILFESL`, `DCOILWTICO`, `DEXUSEU`, `DGS10`, `DGS2`, `FEDFUNDS`, `PAYEMS`, `UNRATE`, `VIXCLS` | 3 columns each: `f_raw_m_<S>` (level), `f_raw_m_<S>_d1`, `f_raw_m_<S>_d5` (1/5-position differences on the canonical axis, no look-ahead) |

**f_cs: cross-sectional standardization (15)**, `float32`, one per the 15 stock-level `f_raw` columns:

| Column | Semantics |
|---|---|
| `f_cs_return_1d` … `f_cs_days_since_filing` | On each date's cross-section, average rank over non-null raw values → `p = (rank - 0.5) / n` → inverse normal `Φ⁻¹(p)` (Acklam approximation); null when raw is null |

- Cross-section scope: all stocks included that day (including `is_common=false` rows); macro columns do not participate (constant within a date).
- The method string of record is `manifest.json.feature_contract.cross_sectional_method`.

**miss: missing indicators (48)**, `uint8` (1 = missing):

- One for each of the 48 `f_raw` columns; name = `miss_` + raw name with the `f_raw_` prefix removed (e.g. `f_raw_return_1d` → `miss_return_1d`, `f_raw_m_DGS10_d1` → `miss_m_DGS10_d1`).
- Rule: non-finite values (±inf/NaN) are converted to missing during feature generation, then flagged via `isna()` (see [AGENT.md](../../AGENT.md) invariant 7).

**Labels (32)**, `float32`:

| Column | Semantics |
|---|---|
| `target_return_{1..30}d` | `adj_open(t+1+h) / adj_open(t+1) - 1`; `adj_open = open × adj_close / close`; h is the positional offset on the canonical axis |
| `excess_5d` / `excess_21d` | `target_return_hd - same-date equal-weight mean`; the mean counts only rows with `is_common=true` and `flag_extreme_label=0` (no SPY benchmark) |

- A label requires both the entry and exit bars to exist with positive finite `open/close/adj_close/adj_open`; intermediate bars are not required, and no filling is done.
- Labels are naturally null at the dataset tail (entry/exit out of range) and across trading-halt gaps.

### 3.3 flag_extreme_label

- `1` = any 1..30-day label window (t+1 through t+1+h) crosses a source price jump: the ratio of `adj_open` between adjacent sessions falls outside `[0.5, 2.0]` (e.g. an unadjusted reverse split).
- Rows are retained; they are excluded from the `excess_*` benchmark mean; the training side should drop or down-weight them. Current count 120,510 (`manifest.json.data_quality_flags`).

### 3.4 meta.parquet

| Column | dtype | Semantics |
|---|---|---|
| `asset_id` | large_string | Ticker |
| `is_common` | bool | Same as samples |
| `first_date` / `last_date` | date32 | This ticker's first/last signal session in the sample |
| `n_rows` | int64 | Rows retained after purge |
| `missing_frac` | double | Raw-feature missing fraction for this ticker |

- 6,532 rows = 6,534 (tickers with a market.csv) − 2 (AYA, FUND, excluded by `exclusions_v1.json`).

### 3.5 splits.json

| Field | Semantics |
|---|---|
| `fit` / `select` / `screen` / `reserve` | Closed interval `[start, end]` (signal-session dates) |
| `purge_sessions` | `30` (maximum label horizon) |
| `purge_semantics` | Full purge rule text (below) |
| `purged_windows` | `{select, screen, reserve}` → boundary-window details (table below) |
| `rows_by_split` | `{split: {before_purge, purged, retained}}` |
| `rows_removed_by_split` | `{split: purged}`, redundant but convenient for verification |

Current baseline:

| split | Window | before_purge | purged | retained |
|---|---|---|---:|---:|---:|
| fit | 1990-01-02 – 2018-12-31 | 15,374,917 | 120,987 | 15,253,930 |
| select | 2019-01-01 – 2020-12-31 | 2,105,325 | 139,158 | 1,966,167 |
| screen | 2021-01-01 – 2024-12-31 | 5,351,300 | 182,398 | 5,168,902 |
| reserve | 2025-01-01 – 2025-12-31 | 1,549,670 | 0 | 1,549,670 |

| `purged_windows` key | Purged split | boundary_date | Window | sessions | rows_removed |
|---|---|---|---|---|---:|---:|
| `select` | fit | 2019-01-02 | 2018-11-14 – 2018-12-31 | 31 | 120,987 |
| `screen` | select | 2021-01-04 | 2020-11-17 – 2020-12-31 | 31 | 139,158 |
| `reserve` | screen | 2025-01-02 | 2024-11-15 – 2024-12-31 | 31 | 182,398 |

- Rule: labels enter at t+1 and exit at t+1+30, so the **31 signal sessions before each boundary** are removed at build time; only boundaries that have canonical sessions in the following split window are purged, and naturally missing rows at the dataset tail are kept. **Consumers do not purge again.**

### 3.6 manifest.json

Top-level fields:

| Field | Semantics |
|---|---|
| `schema_version` | `"samples_v1"`; any column/semantic change must bump it and update this document |
| `row_counts` | `{samples, meta, by_year:{...}}` |
| `date_range` | `{start,end}` signal-session range of the samples |
| `feature_list` | Ordered list of 111 feature columns (48 raw + 15 cs + 48 miss) |
| `feature_contract` | `raw_features` / `cross_sectional_features` / `missing_indicators` (raw→miss mapping) / `cross_sectional_method` / `cross_sectional_scope` / `macro_differences` |
| `label_semantics` | Full text of the label formulas, horizon definition, and excess benchmark |
| `build_params` | Build parameters and canonical statistics (table below) |
| `data_quality_flags` | `flag_extreme_label: {dtype, rule, handling, count}` |
| `known_biases` | 4 known biases (survivorship / FRED latest revised / adj_open not executable / `is_common` is a heuristic) |
| `exclusions_applied` | `{file, asset_ids, dropped_asset_ids_present}` |
| `input_inventory` | Input census: `stock_directories`=7,662, `market_ticker_files`=6,534, `directories_without_market_csv`=1,128 + list, `financial_ticker_files`=6,534, `calendar_denominator_scope` |
| `is_common_column` | Notes that `is_common=false` rows are retained and still participate in cross-sectional ranks |
| `benchmark` | Notes that SPY is absent and excess is the same-date equal-weight mean |
| `outputs` | `{relative_path: {sha256, rows, bytes}}` for all artifacts; `rows` is present for parquet only |
| `manifest_hash_note` | Self-reference exclusion note: `manifest.json` is not in its own `outputs` |

Current `build_params` values:

| key | Current value |
|---|---|
| `canonical_axis_rule` | dates with ≥ `canonical_min_tickers` `is_common` tickers present |
| `canonical_min_tickers` / `canonical_minimum_common_tickers` | 500 / 500 |
| `common_tickers_in_denominator` | 6,168 |
| `canonical_session_count` | 9,067 |
| `rank_batch_sessions` / `staging_tickers` | 40 / 50 |
| `parquet_compression` / `sample_partitioning` | snappy / `samples/year=YYYY/part-00000.parquet` |
| `derived_windows` | 60 and 120 canonical sessions (past/current data only) |
| `financial_asof` | latest snapshot with `available_as_of ≤ signal date`; full YoY matching rule |
| `purge_sessions` / `purge_semantics` | 30 / same as §3.5 |
| `rows_by_split` | same as §3.5 |

Verification example (consumers check by hash; ~15s for the full 6.3G on this machine):

```python
import hashlib, json
from pathlib import Path

root = Path("data/output")
manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
for relative, record in manifest["outputs"].items():
    actual = hashlib.sha256((root / relative).read_bytes()).hexdigest()
    assert actual == record["sha256"], relative
```

### 3.7 qc_report.{json,md}

| Field | Semantics |
|---|---|
| `rows_total` / `rows_per_year` | Row counts |
| `label_stats` | Per label, two sets (`all` / `clean`, the latter excluding flagged rows) of `n/mean/std/p50/p99/exact_zero_fraction` |
| `missingness_per_feature` | Missing count and fraction for every raw/cs feature |
| `cross_section_size_per_year` | Per year, cross-section `dates/min/median/max` |
| `purge` | `purge_sessions` / `semantics` / `rows_by_split` |
| `extreme_labels` | `flag_extreme_label` count, rule, up to 100 example rows |
| `ticker_failures` | Build-time failure details (currently empty) |

## 4. Baseline figures and verification

| Metric | Current baseline |
|---|---|
| samples rows / date range / canonical sessions | 23,938,669 / 1990-01-02 – 2025-12-31 / 9,067 |
| stock directories / with market.csv / without market data | 7,662 / 6,534 / 1,128 (warrants/units/shells, missing at the source) |
| SEC: tickers with companyfacts / no data (404) | 7,479 / 183 tickers (165 unique CIKs; funds, ETFs, foreign issuers often have no companyfacts; these tickers still get an all-`missing` financials.csv) |
| meta rows | 6,532 |
| flag_extreme_label | 120,510 |
| purge (fit/select/screen) | 120,987 / 139,158 / 182,398 |
| output file count | 41 (36 year partitions + meta + splits + manifest + qc×2); `manifest.outputs` therefore holds 40 hashes |

If these numbers change after a rebuild: first use [AGENT.md](../../AGENT.md) invariant 5 to decide whether purge/label logic changed; a rebuild that only changes financial features must be bit-for-bit unchanged.
