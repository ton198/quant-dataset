**English** | [简体中文](data-contracts.zh-CN.md)

# Data Contracts

This document records the data-layer contracts (`data/raw/` → `data/organized/` → sample outputs). The maintained sample contract in §3 is the single finance-free `samples` schema. Existing sample bundles are preserved unchanged as historical artifacts: the 2026-10-03 publication at `data/output/` retains its original manifest value `schema_version="samples_v3"`, and the former 147-column output remains at `data/output-v1-backup-20261003T172214933236Z`; the frozen baseline is separate. These records describe existing data only and do not announce a new publication or current validation. The obsolete unimplemented financial-feature proposal is not part of this contract.

For the consumer-side guide see [../user/data-format.md](../user/data-format.md); for sample-build implementation details see [samples.md](samples.md); for download and organize implementation details see [download.md](download.md).

## 0. Contract layers

| Layer | Path | Contract strength | Change impact |
|---|---|---|---|
| raw | `data/raw/` | Payloads are content-addressed and append-only; `manifest.json` records key → versions | Appends do not affect downstream; but organized data does not automatically follow |
| organized | `data/organized/` | The contract in this section; each output records input/output sha256 in `_meta.json` | Sample candidates consume market/macro panels; keep existing bundles and baselines untouched |
| output | Preserved historical `data/output/`; CLI's fresh-candidate default is `data/samples-output` | New builds use the single `samples` contract; manifests record output hashes/bytes/rows | Never target preserved bundles, backups, or baselines; choose a fresh unused path |

## 1. data/raw/

### 1.1 Yahoo market data

```
data/raw/yahoo/<TICKER>/<start>_<end>.csv        # start/end are download --start/--end
```

- Columns (written by yfinance as-is, `auto_adjust=false, actions=true`): `date`(index), `Adj Close`, `Close`, `Dividends`, `High`, `Low`, `Open`, `Stock Splits`, `Volume`.
- **Not content-addressed**: re-downloading the same ticker over the same range overwrites the same-name file; failed/empty downloads write nothing — so "no directory" indicates no market data for that ticker. No current sample ticker count is claimed here.
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

- Row counts vary with IPO/delisting/data gaps; do not infer current universe size from an old sample bundle. A later organize end date can extend a file.
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
- Row count is the session-calendar length at organize time. This file retains its legacy daily **whole-snapshot replacement** semantics: `available_as_of` stores `filed_date`, non-financial filings can replace all concept values in the row, and values are not carried forward per concept. The `samples` builder does not read this CSV or use it as an input.

### 2.2.1 Existing organized financial event/fact extract (separate from samples)

The two Parquet tables and their `_meta.json` registration form the existing `financial_events_v1` artifact. They are generated from SEC submissions and Company Facts before the legacy daily snapshot is selected; they coexist with, and do not replace or redefine, `financials.csv`. This selected nine-concept structured extract is not a complete XBRL archive or a full filing-text archive. The `samples` builder does not read these files or their financial metadata.

| Artifact | Grain and key fields |
|---|---|
| `financial_events.parquet` | One valid filing event per row, including events with no financial facts. Fields include stable `event_id`, `asset_id`, zero-padded `cik10`, `accession_number`, `filed_date`, `effective_visible_session`, `form`, `is_amendment`, report/fiscal period metadata and key source, `quality_status`, submission path/hash/locator. |
| `financial_facts.parquet` | One normalized concept/actual-period/version per row. Fields include `fact_version_id`, `event_id`, `concept`, finite float64 `value`, `unit`, `taxonomy`, `tag`, `period_kind`, `period_start`, `report_period_end`, `duration_days`, resolved fiscal key/source, filing/effective dates, fact accession, `match_method`, and fact path/hash/locator. |

Both tables use fixed Arrow schemas, including when empty. Event IDs use CIK + accession when present; accession-less IDs use source hash + record locator and are counted. Facts must point to events. Candidates are finite-screened before tag priority; this artifact's financial consumption accepts USD. Flow periods are `quarter` only for verified 10-Q duration 70–125 days, `annual` only for verified 10-K duration 300–400 days; YTD/other spans are not single quarters. Missing starts can remain `unknown` disclosures but cannot qualify for growth or flow-ratio formulas. Stock concepts are `instant`. Unmatched/ambiguous sources, conflicts, invalid dates, unsupported units, non-finite values and invalid ranges are rejected and counted, never silently assigned a disclosure order based on download time.

`effective_visible_session` is the first XNYS session strictly after `filed_date`. Raw source completion identity includes both CIK and `raw_input_inventory_sha256`; `_meta.json.financial_events.complete` is fail-closed and requires valid resource states, calendar mapping, files, schema, output hashes, and event/fact relationships. A confirmed no-input case uses schema-correct empty tables with a reason; missing, malformed or incomplete input is not a valid empty artifact. For example, no usable resource records for a CIK are represented by `empty_reason="no_usable_input_for_cik"` and `submissions/companyfacts=no_input`. A valid Company Facts placeholder/empty-facts payload may instead be recorded as nonfatal `companyfacts=no_usable_facts`: submissions still provide filing events, while the fact table may be empty. The `input_resource_status` object records `manifest`, `submissions`, and `companyfacts`. `rejection_counts` provides diagnostics for raw manifest/payload validity, filing dates and CIK identity, source/accession matching, taxonomy/unit/numeric/finite checks, period validity, fact/fiscal-key conflicts, and effective-session mapping. The counter-key names and aggregation details are implementation diagnostics, not a stable contract; consumers must not depend on individual keys.

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
| `row_counts` | `market_input`/`market_output`/`market_dropped_non_session`, legacy `financials_input`/`financials_output`, `financial_events`, `financial_facts`, or `macro_output` |
| `financial_events` | Nested artifact record: `contract_version`, `complete`, `status`, `empty_reason`, `cik10`, `input_hashes`, `raw_input_inventory`, `raw_input_inventory_sha256`, `input_resource_status`, `input_resource_diagnostics`, `calendar`, `output_calendar`, `quality_counts`, `rejection_counts`, and `events`/`facts` path/hash/rows |
| `cik10` / `raw_input_inventory_sha256` | Top-level completion identity mirrored from the filing-event record; it binds completion to both issuer and raw SEC input inventory |
| `known_issues` | Manual/programmatic notes on semantics |

- `_ticker_is_organized` requires the current `financial_events_v1` contract, `complete=true`, matching CIK and raw-input fingerprint, valid status fields, schema-correct event/fact tables, and matching output hash/row registrations before skipping. Legacy CSV presence alone is not a completion signal.

## 3. Single active sample contract (`samples`)

The maintained contract for new sample builds is finance-free `samples`. The existing 2026-10-03 `data/output/` bundle is an unchanged historical artifact whose manifest still says `schema_version="samples_v3"`; this section does not relabel or republish it. The former 147-column output and frozen baseline remain separately preserved. `financial_status=not_applicable`, when present in a legacy artifact, is not a financial-coverage pass.

### 3.1 Inputs and financial separation

The builder reads organized `stocks/<TICKER>/market.csv`, `shared/macro.csv`, and the configured exclusions. It does not read `financials.csv`, `financial_events.parquet`, `financial_facts.parquet`, or financial `_meta.json` records. It performs no financial preflight, financial coverage analysis, or financial input inventory. Financial status is `not_applicable`. Existing SEC organized files remain untouched and available separately.

### 3.2 Closed schema and canonical order

| Group | Count | Contents |
|---|---:|---|
| Keys / flags | 4 | `date`, `asset_id`, `is_common`, `flag_extreme_label` |
| Raw | 43 | 10 stock-level market/derived features + 33 macro features |
| Cross-sectional | 10 | Same-date transform of the 10 stock-level raw features; no macro CS |
| Missing indicators | 43 | One `miss_*` for each raw column |
| `manifest.feature_list` | **96** | 43 raw + 10 CS + 43 MISS |
| Labels | 32 | 30 forward return labels + `excess_5d` and `excess_21d` |
| Physical sample columns | **132** | 4 keys/flags + 96 features + 32 labels |

Column order: four keys/flags → 43 raw → 10 CS → 43 MISS → 32 labels. Physical Arrow dtypes are `date32`, `large_string` for `asset_id`, bool for `is_common`, uint8 for flags/MISS, and float32 for numeric features/labels. All 51 prior financial columns are removed: 17 finance raw + 17 finance CS + 17 finance MISS. The active schema contains no filing-age, financial-age, level, growth, ratio, or QoQ fields.

The 10 stock raw columns are the existing six market passthroughs (`return_1d/5d/20d`, `volatility_20`, `volume_ratio_20`, `intraday_range`) and four derived values (`momentum_60/120`, `volatility_60`, `volume_zscore_60`). The 33 macro columns remain 11 series × level / canonical-position `_d1` / `_d5`.

### 3.3 Preserved behavior and metadata

The current behavioral contract defines `(date, asset_id)` keys, flags, 32 label values and null masks, splits/purge, extreme-label behavior, ticker-failure semantics, and the non-financial raw/CS/MISS values. Validate formulas and edge cases with independent fixtures and explicit invariants; a historical cross-generation projection is not the correctness oracle. The canonical calendar, macro alignment, cross-sectional ranking and label formulas are specified below.

`meta.parquet.missing_frac` is computed after purge over exactly the 43 raw non-financial features: `null raw cells / (retained_rows × 43)`. It excludes CS/MISS, labels, keys/flags, and financial data.

Each new manifest declares the single contract as `schema_version="samples"`, with the exact ordered 96-column `feature_list`, `semantic_contract`, schema/semantic fingerprints, consumed market/macro/exclusion inputs, `label_semantics`, and build parameters. It does not list or inspect financial artifacts. QC records row, label, raw/CS/MISS, cross-section, purge, extreme-label, and ticker-failure summaries; there is no financial feature coverage section. `input_provenance.files` records the paths, streamed SHA-256 values, and byte counts of every consumed input; these are rechecked before manifest publication. `code_identity` records hashes/bytes for all five samples package files and versions for NumPy, pandas, and PyArrow. `input_inventory` is counts-only, not a content inventory. Every registered output records SHA-256, bytes, and rows.

### 3.4 Safe candidate and release verification

The CLI defaults to `--out data/samples-output`. `workspace_root` defaults to the current working directory and may be specified with `--workspace-root` (an existing directory); it is never inferred from the installed source package. The default exclusions file is `<workspace_root>/config/universes/exclusions_v1.json`; an explicit `--exclusions-file` path is relative to CWD. Missing exclusions are an error, not an empty list. Protect `<workspace_root>/data/` and related raw/output/baselines paths; when organized inputs follow `<data>/organized`, protect recognized raw/output/baseline/archive siblings beside `<data>` independently of `workspace_root`. Reject symlink paths/ancestors, non-empty destinations, and destinations overlapping protected paths or actual inputs. Use a fresh unused candidate path, never the preserved output or a baseline. Verify each candidate using independent current-contract fixtures and invariants: exact columns/order/dtypes, absence of financial inputs/columns, label and split/purge edge cases, the 43-raw `missing_frac` denominator, safe output paths, actual input provenance, and output hashes.

The archived 2026-10-03 report records facts about that publication only. Its reused test result and historical projection are not current source validation or a correctness oracle. Do not infer performance, financial correctness, or a release pass for a new candidate; it needs its own recorded verification.


## 4. Existing archived sample artifacts (read-only)

| Path | Recorded artifact fact | Handling |
|---|---|---|
| `data/output/` | The 2026-10-03 publication retained `schema_version="samples_v3"`, 23,938,669 rows, 6,532 tickers, 132 physical columns, and 96 features. | Preserve its files and manifest unchanged; this record does not announce a new publication. |
| `data/output-v1-backup-20261003T172214933236Z/` | Former 147-column sample output. | Preserve as historical data; not a version-selectable product or compatibility target. |
| `data/baselines/samples_v1_financial_upgrade/` | Frozen historical baseline. | Preserve unchanged as a read-only archived reference. |

The schema labels embedded in these artifacts describe their original contents only. Do not rename, rewrite, or relabel existing output data to make it appear to have been generated under the new `samples` contract. The current-contract correctness suite uses independent fixtures, not these archived datasets as a long-lived oracle.
