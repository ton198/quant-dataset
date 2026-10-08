**English** | [简体中文](recommended-usage.zh-CN.md)

# Recommended Usage for `samples`

This guide covers the single finance-free `samples` contract for new candidates and preserved historical bundles. The existing `data/output/` publication retains its original `schema_version="samples_v3"` manifest; the former 147-column output is retained at `data/output-v1-backup-20261003T172214933236Z`, while the frozen baseline remains separate. Check each bundle's actual manifest and schema before consuming it. Historical schema labels describe artifacts; they are not product selectors or compatibility promises. See [data-format.md](data-format.md) for the contract and [cli.md](cli.md) for safe candidate construction.

## 1. Read and validate the candidate

- Read the selected bundle's manifest and schema, then verify its registered output hashes. New builds use the single `samples` contract; do not infer a contract from a product-version selector.
- Sample rows are identified by `(date, asset_id)`. Use `splits.json`; the build-time purge has already removed applicable boundary windows, so do not purge again downstream.
- The active schema has 132 physical columns: 4 keys/flags, 43 non-financial raw features, 10 same-date cross-sectional features, 43 missing indicators, and 32 labels. `manifest.feature_list` has 96 feature columns.
- There are no financial features or finance-derived missingness flags in the `samples` contract. Existing SEC files in `data/organized/` are separate artifacts, not sample inputs. If an older manifest includes financial status `not_applicable`, that is not a financial-coverage result.
- `null` is not zero. Keep each `miss_*` feature when selecting/imputing its matching raw value; do not silently fill missing values with zero.
- `f_cs_*` values are same-date ranks; interpret them within their generating cross-section rather than as a global time-series scale. Macro columns remain raw-only because they are shared within a date.

## 2. Labels and row filters

- Existing labels are unchanged: `target_return_1d..30d`, `excess_5d`, and `excess_21d`. `excess_*` subtract the same-date equal-weight mean over `is_common=true` and `flag_extreme_label=0` rows; there is no SPY benchmark.
- Use `splits.json` for temporal separation. Keep `reserve` for a final evaluation, and avoid random row splits because adjacent labels overlap in future prices.
- `flag_extreme_label=1` marks a label window that crosses an adjacent-session adjusted-open ratio outside `[0.5, 2.0]`. Rows remain in the bundle; choose a documented downstream handling rule and apply it consistently in evaluation.
- Labels are naturally null when future entry/exit bars are unavailable. Do not impute labels.

## 3. Read a small result

For a quick command-line query, install the optional `query` extra and select only the rows and columns you need. `query-samples` strictly validates and accepts only the current `samples` contract; it rejects preserved historical bundles, including `data/output/` (`samples_v3`). Inspect historical bundles with Python/PyArrow, or query a fresh current-contract candidate.

```bash
uv sync --frozen --extra query
BUNDLE=/tmp/opencode/candidate-samples-finance-free
quant-dataset query-samples --bundle "$BUNDLE" \
  --sql "SELECT date, asset_id, is_common FROM samples WHERE date >= DATE '2019-01-01' AND date < DATE '2020-01-01' ORDER BY date, asset_id" \
  --limit 5
```

The bundle remains Parquet, and Python users can still read historical artifacts directly with PyArrow. The CLI query view accepts only the current `samples` contract and uses physical bundle columns; it does not add a virtual `year` column from partition-folder names. See the [query guide](query.md) for installation, output format, and limits.

## 4. Data-source caveats

| Caveat | Implication |
|---|---|
| Survivorship bias | The universe is based on a current SEC ticker list and has no delisted-stock history. Treat this as a limitation of the data, not evidence of future returns. |
| FRED is not vintage | Macro values use the existing latest-revised input and release-date approximation; historical values may differ from what was known then. |
| Adjusted open is not executable | Labels use `open × adj_close / close`; they are not simulated fills and do not include costs or slippage. |
| `is_common` is a heuristic | It is based on ticker suffixes, not a security master; non-common rows are retained. |
| Macro coverage varies | Series start dates and missingness differ. Use the matching `miss_m_*` flags and inspect the candidate QC report; no v3 coverage rates are claimed here. |
| No SPY benchmark | Excess labels use the equal-weight common-stock target mean described above. |

The legacy `financials.csv` retains its historical whole-snapshot replacement behavior, and `financial_events_v1` remains a selected nine-concept organized extract. Neither is read by `samples`; neither should be mistaken for a full filing archive. Separate filing pilots remain independent of the sample contract; see [the filing guide](filings.md) for their scope and limitations.

## 5. Common pitfalls

1. Do not purge again after loading a candidate.
2. Do not confuse `miss_*` with labels or fill a null raw value with zero by default.
3. Do not compare `f_cs_*` values across dates as though they were globally standardized measurements.
4. Do not treat `is_common=false` as common stock; those rows may still participate in cross-sectional ranks.
5. Do not treat a fresh candidate as published until its own current-contract verification is recorded. The existing `data/output/` and former 147-column output are preserved historical artifacts, not outputs of this documentation change.
6. Use independently authored current-contract fixtures and assigned invariants; a cross-generation projection is not a correctness oracle. Historical release evidence is not a new verification result.
