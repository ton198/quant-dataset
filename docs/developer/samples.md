**English** | [简体中文](samples.zh-CN.md)

# Sample Building Internals (`src/build_samples.py`)

> Scope: changes to labels (`target_*`/`excess_*`), features (`f_raw_*`/`f_cs_*`/`miss_*`), the canonical calendar, purge/splits, and the output bundle layout.
> Related: [data-contracts.md](data-contracts.md) (field-level contracts and baseline numbers), [AGENT.md](../../AGENT.md) §2 invariant 5, [architecture.md](architecture.md), [known-quirks.md](known-quirks.md).
> This document tracks the 1,176-line implementation in `src/build_samples.py`; every function name below can be found there.

## 1. Entry points and top-level flow

```text
CLI: quant-dataset build-samples [--data-dir data/organized] [--out data/output] [--exclusions-file ...]
      └─ src/cli/main.py: build_samples(args.data_dir, args.out, args.exclusions_file)
          └─ build_samples(data_dir, output_dir, exclusions_file=None, *,
                           canonical_min_tickers=500, rank_batch_sessions=40, staging_tickers=50)
```

The default exclusions file is `config/universes/exclusions_v1.json` (resolved inside `build_samples` relative to `__file__`; the CLI also falls back to it). The top-level flow:

1. Validate that `stocks/` and `shared/macro.csv` exist; `_load_exclusions` reads and validates the exclusion list (upper-case, deduplicated, duplicates raise `ValueError`).
2. Scan `stocks/*/market.csv`: skip exclusions; classify with `_is_common`; count per day using **is_common tickers that have a market.csv**.
3. canonical calendar = dates where the ticker count is `>= canonical_min_tickers` (default 500); an empty result raises `ValueError`. `_macro_matrix` aligns macro.csv to that axis.
4. `_purge_windows(calendar)` precomputes the three boundary embargo windows → `purge_split_by_date`.
5. `_clean_outputs(output)` removes old samples/staging/manifest files, etc.
6. For each batch (`staging_tickers`, default 50), `_ticker_samples` runs → purge rows are dropped immediately by split → rows are sorted by `(date, asset_id)` and cut into chunks of `rank_batch_sessions` (default 40 sessions), written to the temporary `output/samples-stage-*/chunk=*/stage-*.parquet` directory.
7. Each chunk is read back: `_add_cross_sectional_features` → `miss_*` generation → `excess_5d/21d` computation → rows accumulated by year and appended to `output/samples/year=YYYY/part-00000.parquet` with `pq.ParquetWriter`.
8. Write `meta.parquet`, `splits.json`, `qc_report.json`/`.md`, and finally `_hash_output_files` + `manifest.json`. A ticker-level exception lands in `failures`/QC without stopping the rest; zero total rows raises `ValueError`.

## 2. Constants and the feature/label inventory

| Constant | Contents | Count |
|---|---|---|
| `MACRO_SERIES` | `BAMLH0A0HYM2, CPIAUCSL, CPILFESL, DCOILWTICO, DEXUSEU, DGS10, DGS2, FEDFUNDS, PAYEMS, UNRATE, VIXCLS` | 11 |
| `MARKET_RAW` | `f_raw_return_1d/5d/20d`, `f_raw_volatility_20`, `f_raw_volume_ratio_20`, `f_raw_intraday_range` (straight from market.csv) | 6 |
| `DERIVED_RAW` | `f_raw_momentum_60/120`, `f_raw_volatility_60`, `f_raw_volume_zscore_60` (computed at canonical-session positions) | 4 |
| `FINANCIAL_RAW` | `f_raw_revenue_yoy`, `f_raw_net_income_yoy`, `f_raw_operating_income_yoy`, `f_raw_assets_yoy`, `f_raw_days_since_filing` | 5 |
| `MACRO_RAW` | `f_raw_m_<series>`, `f_raw_m_<series>_d1`, `f_raw_m_<series>_d5` | 33 |
| `RAW_FEATURES` | `STOCK_RAW + MACRO_RAW` (`STOCK_RAW = MARKET_RAW+DERIVED_RAW+FINANCIAL_RAW`) | 48 |
| `CS_FEATURES` | `f_cs_*` generated for `STOCK_RAW` only; macro columns are date constants and **do not participate** in cross-sectional ranks | 15 |
| `MISS_FEATURES` | `miss_*` for all `RAW_FEATURES` | 48 |
| `LABELS` | `target_return_1d..30d` + `excess_5d` + `excess_21d` | 32 |
| `_PURGE_SESSIONS` | 30 (the longest label window) | — |
| `_SPLIT_TRANSITIONS` | `select` 2019-01-01 / `screen` 2021-01-01 / `reserve` 2025-01-01 (fit has no start; it is implied by data_start) | 3 boundaries |

## 3. How the canonical calendar, batching, and determinism relate

- The calendar is the global axis: `market["date"]` is mapped onto axis positions with `calendar.get_indexer`; bars off the axis produce no samples (`organize_market` already filtered by its organize calendar, and this pass filters by the canonical calendar again).
- Only tickers with `stocks/*/market.csv` are iterated; tickers without bars (1,128 of them) never enter the sample build. `ticker_map` uses upper-cased asset_ids, and duplicate directory names raise `ValueError`.
- `date_counts` counts only `_is_common=True` tickers, so the ">=500" denominator is common tickers, not the full universe (the manifest records this as `build_params.common_tickers_in_denominator`).
- Cross-sectional ranks are computed **within a single date chunk (40 sessions)**; a chunk contains the rows of every already-built ticker for those dates, so the result equals a full cross-section rank. `rank_batch_sessions` only affects memory/IO, never values.
- Ticker processing order = the order of `glob("*/market.csv")` (directory-name lexicographic); output is always `sort_values(["date","asset_id"], kind="mergesort")`; JSON uses `sort_keys=True`.

## 4. Core function map

| Function | Responsibility | Key behavior/gotcha |
|---|---|---|
| `build_samples` | Top-level orchestration | All kwargs only affect performance/calendar thresholds, never value definitions; `_clean_outputs` clears every old artifact under `output_dir` |
| `_clean_outputs` | Deletes `samples/`, `samples-stage-*`, and old `meta.parquet/manifest.json/splits.json/qc_report.*`, then recreates `samples/` | Temporary staging lives inside `output/` (same filesystem); `TemporaryDirectory` reclaims it on exception |
| `_ticker_samples` | One ticker → long-table rows (features + labels + flag) | Requires all `_MARKET_REQUIRED` columns and no duplicate dates; `valid_bar` requires positive finite open/close, finite factor, finite adjusted_open; missing labels are NaN, never 0 |
| `_financial_features` | Builds the 5 `FINANCIAL_RAW` features from `financials.csv` | See §5.3; a missing path returns all-NaN instead of raising |
| `_macro_matrix` | macro.csv → 33 float32 columns on the canonical axis | Missing columns/duplicate dates raise `ValueError`; `d1/d5` use positional `diff` on the canonical axis with no forward-fill |
| `_add_cross_sectional_features` | Per date, average-rank the 15 `STOCK_RAW` → `_inverse_normal` → `f_cs_*` | The denominator is the non-null count for that date+feature; NaN stays out of the ranks and remains NaN; writes into the frame in place |
| `_inverse_normal` | Acklam inverse-normal approximation (valid on (0,1) only) | Probability `(rank-0.5)/count`; the median maps exactly to 0; out-of-range/NaN → NaN |
| `_positive_finite` | `isfinite & >0` mask | Shared validity test for label windows and momentum |
| `_aligned_numeric` | Places a ticker-local array into a full-calendar-length position array | Positions not covered are NaN |
| `_rolling_std` | `rolling(window, min_periods=window).std(ddof=1)` | Too-short windows → NaN, no partial windows |
| `_purge_windows` | Computes the 31-session embargo window at each split boundary | Boundary = `calendar.searchsorted(split_start, side="left")`; a boundary at the axis start/outside the axis skips that window |
| `_public_purge_windows` | Drops the internal `_positions` for splits.json | `rows_removed` is filled in by `purge_rows_by_split` |
| `_label_qc` | Per-label all/clean stats (clean excludes `flag_extreme_label=1`) | If the clean side is entirely NaN, an empty array is used and `_stats_block` returns n=0 |
| `_stats_block` | n/mean/std/p50/p99/exact_zero_fraction | std uses ddof=1; n=1 gives std=0 |
| `_hash_output_files` | sha256/bytes/rows for every file under output except `manifest.json` | samples partitions take rows from the `year=` directory; `meta.parquet` takes the meta row count |
| `_arrow_table` | DataFrame → Arrow, converting `date` to `date32` | This is what pins the output schema's date type |
| `_write_json` | `json.dumps(indent=2, sort_keys=True, allow_nan=False)` | `allow_nan=False`: any NaN reaching splits/manifest/qc fails immediately |
| `_is_common` / `_load_exclusions` / `_normal_date_column` / `_sha256` | Utilities | `_is_common` is suffix-based only; `_normal_date_column` raises `ValueError` on any invalid date |

## 5. Feature implementation details

### 5.1 Market features (first half of `_ticker_samples`)

- Passthrough columns: `return_1d/5d/20d`, `volatility_20`, `volume_ratio_20`, `intraday_range`, via `pd.to_numeric(errors="coerce")` → float32.
- `factor = adj_close / close` (NaN when close is 0/non-finite); `adjusted_open = open * factor`; `valid_bar` requires open/close/factor/adjusted_open all positive and finite.
- `momentum_60/120 = adjusted_close[t] / adjusted_close[t-60/120] - 1`; both endpoints must be `_positive_finite`; fewer than 60/120 axis positions → NaN.
- `volatility_60`: first define the canonical adjacent-session return across the full axis from this ticker's aligned adjusted_close (NaN where the ticker has no bar), then `rolling(60).std`, then select the ticker's own axis positions; any missing axis position in the window keeps the result NaN.
- `volume_zscore_60 = (volume - rolling_mean_60) / rolling_std_60` (NaN when std is 0 or non-finite).
- Macro columns are taken directly as `macro.iloc[positions][feature]`, with no per-ticker ffill.
- Final cleanup: ±inf/NaN in every raw feature → NaN; `miss_*` is generated from `isna()` in §5.5.

### 5.2 Labels (second half of the same function)

- Definition: `adjusted_open = open × adj_close / close`; for signal session `t`, **entry = canonical axis position t+1**, **exit = t+1+h** (h=1..30).
- `target_return_hd = adjusted_open[t+1+h] / adjusted_open[t+1] - 1`, requiring both entry and exit to exist (`entry_positions < size`, `exit_positions < size`) and `valid_bar` to hold; otherwise NaN. **No intermediate-bar requirement, no substitution, no filling.**
- Non-positive/non-finite open/close/factor, invalid adj_open, or entry/exit beyond the axis → NaN label. Naturally NaN rows at the end of the dataset are kept (purge only removes boundary windows).

### 5.3 Financial as-of and YoY (`_financial_features`)

1. Read `financials.csv`; require `available_as_of`, `report_period_end`, and `_FINANCIAL_VALUES = ("revenue","net_income","operating_income","assets")`; rows with an invalid `available_as_of` are dropped.
2. **Snapshot semantics**: organized financials are daily snapshots; after a stable sort by `(_available, _source_order)`, `drop_duplicates("_available", keep="last")` keeps the last row per availability date — the filing information actually visible that day. Missing `fiscal_year`/`fiscal_period` columns are treated as NaN (for compatibility with older files).
3. **YoY main path**: `fiscal_history[(fiscal_year, fiscal_period)]` stores the most recent snapshot seen for that key; the prior for the current row is the snapshot with `fiscal_year-1` and the same `fiscal_period`. A ratio is computed only when current and prior are both non-null and prior != 0.
4. **Fallback path** (no fiscal identifiers, or the key cannot be found): look for candidates among report_period_ends of the **previous report year** with `|candidate - (end - 1 year)| <= 15 days`, take the closest; ties choose the earlier period-end (sort key `(abs(diff), candidate_date)`).
5. Visibility: `searchsorted(_available, signal_date, side="right") - 1`, i.e. **latest available_as_of <= signal date**; `f_raw_days_since_filing = signal_date - available_as_of` (calendar days).
6. Gotcha: the fiscal main path only recognizes the most recently seen key. If the newest snapshot carries fy/fp but all four values are missing (for example the whitelist matched no concept), the key points at that empty snapshot and YoY does not fall back to an older snapshot with the same key (see [known-quirks.md](known-quirks.md)).

### 5.4 Cross-sectional features (`_add_cross_sectional_features`)

- Per date and per `STOCK_RAW`: `rank(method="average")` → `p = (rank - 0.5) / non_null_count` → `_inverse_normal(p)` → float32.
- Macro columns are explicitly excluded (identical for every ticker on a date, so ranks are meaningless); `is_common=False` rows still participate; NaN stays NaN.
- Difference from `excess_*`: `excess_5d/21d = target - same-day equal-weight mean of is_common & flag==0 rows` (computed per date within the chunk; NaN targets are skipped automatically).

### 5.5 Missingness and flags

- `miss_* = raw.isna().astype(uint8)`, generated for all 48 raw features (not for cross-sectional features).
- `flag_extreme_label`: first compute the adjacent-axis `adjusted_open[t]/adjusted_open[t-1]` ratio across the full axis (only when both endpoints are `valid_bar`); a ratio `>2.0` or `<0.5` marks a glitch. For each signal row, check whether any glitch falls inside its label-window axis positions `[t+2, t+31]` (the 30 windows from entry t+1 to exit t+1+h) → `uint8(0/1)`. Rows are kept, never dropped; the `excess_*` benchmark mean excludes flag=1 rows.

## 6. Purge and splits

- Three boundaries: the split starts of select/screen/reserve (2019-01-01 / 2021-01-01 / 2025-01-01). Each boundary is the first canonical session `>= start`; the window = the `31` signal sessions before the boundary (`_PURGE_SESSIONS + 1`: 30 label sessions + 1, because the exit is at t+1+30).
- Build-time removal: every ticker row with `date ∈ purge_split_by_date` is dropped and counted in `rows_by_split[previous split].purged`; `before_purge/retained` are recorded alongside.
- `splits.json` fields: `fit/select/screen/reserve` start/end, `purge_sessions=30`, `purge_semantics`, `purged_windows` (key = the later split name: boundary_date, first/last_purged_session, sessions, rows_removed), `rows_by_split`, `rows_removed_by_split`.
- A boundary at the axis start/outside the axis (e.g. no session after reserve) → no window is generated; naturally NaN label rows at the end of the dataset are **not purged**.
- Current baseline (`data/output/splits.json`): purged fit=120,987, select=139,158, screen=182,398, reserve=0; `flag_extreme_label=120,510`; 23,938,669 total rows (consistent with [AGENT.md](../../AGENT.md) §2 invariant 5).

## 7. Output bundle

| Artifact | Contents | Key points |
|---|---|---|
| `samples/year=YYYY/part-00000.parquet` | Column order: `date, asset_id, is_common, flag_extreme_label, RAW_FEATURES(48), CS_FEATURES(15), MISS_FEATURES(48), LABELS(32)` | `date=date32`; float32 features/labels; `is_common` bool; `flag_extreme_label`/`miss_*` uint8; snappy |
| `meta.parquet` | Per asset_id: `is_common, first_date, last_date, n_rows, missing_frac` | Sorted by asset_id; missing_frac is computed after purge as the NaN rate of RAW_FEATURES (cross-sectional features excluded) |
| `splits.json` | The split and purge record from §6 | `_write_json` sorted keys |
| `qc_report.json` / `.md` | rows_total/rows_per_year, all/clean stats for the 32 labels, per-feature missingness, cross-section size by year, purge, extreme-label count + up to 100 examples, ticker failures | clean = excluding `flag_extreme_label=1` |
| `manifest.json` | `schema_version="samples_v1"`, `outputs` (sha256/bytes/rows for every file except itself), `row_counts`, `date_range`, `feature_list`, `feature_contract`, `label_semantics`, `build_params`, `data_quality_flags`, `known_biases`, `exclusions_applied`, `input_inventory`, `is_common_column`, `benchmark`, `manifest_hash_note` | `outputs` excludes `manifest.json` (no self-referential hash); row baselines are in [data-contracts.md](data-contracts.md) |

## 8. Determinism and reproducibility

- Row order: ticker lexicographic order + stable `(date, asset_id)` sort; chunk/year partition boundaries are fixed (40 sessions, calendar year).
- JSON: `sort_keys=True`; parquet: fixed schema, `row_group_size=8192` (staging).
- The manifest records sha256+bytes+rows for every output; identical input bytes + identical dependency versions + identical kwargs → identical manifest (`splits.json`, `meta.parquet`, and `manifest.json` contain no generation timestamp; timestamps only appear in the organize layer's `_meta.json`).
- Note: the `_`-prefixed internal column `_date_chunk` only exists in staging files and is dropped before samples are written.

## 9. Change warnings (mandatory after touching labels/features/splits)

1. Run the locked tests: `PYTHONPATH=src .venv/bin/python -m pytest tests/test_build_samples.py -q` (7 tests covering labels, YoY, purge, inf→miss, dtypes, cross-section).
2. After a rebuild, check the structural invariants ([AGENT.md](../../AGENT.md) §2 item 5): 23,938,669 total rows; purge 120,987/139,158/182,398; flag 120,510. **Financial-feature-only changes must keep these bit-for-bit identical**; changes are only allowed from purge/label-logic changes, and must sync [data-contracts.md](data-contracts.md).
3. Validate schema/types (`tests/test_build_samples.py::test_outputs_use_date32_and_float32`): `date32`, float32 features and labels, uint8 `flag_extreme_label`/`miss_*`, no `f_cs_m_*`.
4. Verify every output's sha256 against `data/output/manifest.json.outputs`; make sure `qc_report.json` missingness/label stats show no unusual jumps.
5. There is no ticker/year CLI filter: for a small real-data check, call `build_samples(data_dir, out, canonical_min_tickers=<small>, staging_tickers=<small>, rank_batch_sessions=<small>)` directly, or copy a few `stocks/<T>` directories plus `shared/macro.csv` into a temp directory; the default 500 floor requires a large enough sample set.

## 10. Known biases (as stated by the manifest)

- Survivorship: the universe is the current SEC list, with no delisted tickers.
- FRED values are latest revised, not vintage.
- `adj_open` is an adjusted price, not an executable fill; label returns are not realizable fills.
- `is_common` is a suffix heuristic (`-UN/-WT/-P/-R/-U`, etc.) and does not replace a security master; among the 6,532 asset_ids in the samples, 364 are `is_common=False`.
- No SPY: `excess_*` subtracts the same-day equal-weight common mean (excluding extreme-flag rows).

## 11. Verification command cheat sheet

```bash
# Sample-logic unit tests
PYTHONPATH=src .venv/bin/python -m pytest tests/test_build_samples.py -q

# Full rebuild (default data/organized → data/output)
PYTHONPATH=src .venv/bin/python -m cli.main build-samples

# Quick structural baseline check
.venv/bin/python - <<'PY'
import json

m = json.load(open("data/output/manifest.json"))
s = json.load(open("data/output/splits.json"))
q = json.load(open("data/output/qc_report.json"))
assert m["row_counts"]["samples"] == sum(m["row_counts"]["by_year"].values())
assert q["rows_total"] == m["row_counts"]["samples"]
print(
    m["row_counts"]["samples"],
    s["rows_removed_by_split"],
    q["extreme_labels"]["flag_extreme_label_count"],
)
PY
```
