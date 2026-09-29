**English** | [简体中文](recommended-usage.zh-CN.md)

# Recommended Usage (Training Side)

For downstream training code that consumes `data/output/`. Column definitions and file details are in [data-format.md](data-format.md); the CLI and rebuild workflow are in [cli.md](cli.md).

## 1. Five-step workflow

### Step 1: split with `splits.json` — do not purge again

- Use `fit` for training, `select` for tuning/model selection, and `screen` for out-of-sample validation; touch `reserve` only once, for the final evaluation.
- The 31 signal sessions before each of the three boundaries are already removed at build time (`purged_windows`), so labels never cross split boundaries; purging again downstream throws away roughly 440K more rows.
- If you need time-series cross-validation inside `fit`, split by contiguous date ranges (e.g. rolling by year) — never shuffle at random.

### Step 2: use `excess_5d` / `excess_21d` as the primary targets

- Both already subtract the same-date equal-weight benchmark of `is_common` stocks, so modeling them directly avoids mistaking market beta for alpha; the repo has no SPY, and this is the only benchmark definition available.
- The remaining `target_return_1d..30d` serve as auxiliary tasks (shared representation across horizons, auxiliary regularization); do not mix them with the excess targets in the same head.
- Labels are naturally missing at the tail of the dataset (not enough future prices) — expected; do not impute them.

### Step 3: use the 111 feature columns (48 f_raw + 15 f_cs + 48 miss)

- For stock-level features, prefer `f_cs_*`: same-date cross-sectional rank → inverse normal, comparable across days by construction, which avoids scale drift.
- Macro columns exist only as `f_raw_*` (constant within a day, so they have no cross-section); `_d1`/`_d5` are positional differences on the canonical axis.
- Model `miss_*` explicitly (0/1 feature or mask); never fill NaN with 0.
- `is_common` works as a filter; if you keep non-common rows, at least use it as a feature and check the label distribution of those rows.

### Step 4: handle `flag_extreme_label`

- `=1` for 120,510 rows (≈0.5%): the label window crosses a price glitch (consecutive adjusted-open ratios outside [0.5, 2.0]) and the label is unreliable.
- Drop them (or down-weight heavily) during training; drop them in evaluation as well, otherwise a few glitches dominate the metrics.
- Note that the excess benchmark already excludes these rows; if you recompute the benchmark yourself, keep the same convention.

### Step 5: load by year

- The full float32 feature matrix takes about 10 GB in memory (23.9M × 95 float32 ≈ 9.1 GB plus key columns), so reading everything at once is impractical.
- Stream by year (or year range) instead: each partition is ~45–440 MB (snappy) and a single year fits in memory.
- Shuffling the file order per year in the training loop is enough; random sampling across years is hard on memory.

## 2. Loading examples

Direct duckdb query over the parquet glob (good for interactive exploration; `year` is a Hive partition column):

```python
import duckdb

con = duckdb.connect()
df = con.execute("""
    SELECT date, asset_id, excess_5d, excess_21d,
           f_cs_momentum_120, f_cs_volatility_20,
           f_raw_days_since_filing, miss_revenue_yoy
    FROM read_parquet('data/output/samples/year=*/part-00000.parquet',
                      hive_partitioning = true)
    WHERE year BETWEEN 2019 AND 2020
      AND is_common
      AND flag_extreme_label = 0
""").df()
```

Lazy pyarrow.dataset read with partition pushdown (`date` is `date32`, so filter values must be `datetime.date` — strings raise a no-kernel error):

```python
from datetime import date
import pyarrow.dataset as ds

dataset = ds.dataset("data/output/samples", format="parquet", partitioning="hive")
table = dataset.to_table(
    columns=[
        "date",
        "asset_id",
        "is_common",
        "flag_extreme_label",
        "excess_21d",
        "f_cs_momentum_120",
    ],
    filter=(ds.field("year") == 2024) & (ds.field("date") >= date(2024, 1, 1)),
)
df = table.to_pandas()
```

## 3. Biases and data-source realities

| Symptom | Cause | What to do |
|---|---|---|
| Survivorship bias | The universe is the current SEC list; there are no delisted stocks | Fit-period returns skew optimistic; treat `screen` as the real test and do not extrapolate absolute returns |
| FRED is not vintage | Only latest revised values are stored | Macro features contain hindsight revisions; run sensitivity checks on macro exposure |
| Adjusted open is not executable | `open × adj_close / close` is an adjusted price | Do not treat it as a fill price; model costs/slippage yourself |
| `is_common` is a heuristic | Decided by ticker suffix only | Build your own security master if you need a strict equity universe; non-common rows remain in the table |
| Financial coverage is sparse (≈96% missing) and non-random | Snapshot semantics: only the latest filing on that date is visible, and a missing field does not inherit older values; coverage is better around filing-dense periods and for large companies | Use `miss_*` + `f_raw_days_since_filing`; evaluate in coverage-stratified buckets |
| Early macro gaps | Some series are missing before the 2000s (e.g. `BAMLH0A0HYM2` is 87% missing) | Mask with `miss_m_*`; shorten the training window for macro-sensitive models |
| No SPY benchmark | The organized data contains no index | Excess is an equal-weight `is_common` mean benchmark; compare like with like |
| Overlapping label windows | Adjacent signal dates share future prices (autocorrelation) | Cluster evaluation by stock/date or hold out whole periods; do not use random K-fold |
| Canonical calendar ≠ exchange calendar | Only sessions with ≥500 `is_common` stocks are kept | When aligning external data, use the `date` axis of the sample table |

## 4. Common pitfalls

1. **Purging twice**: purge already happened at build time; cutting another 31 sessions downstream only wastes data (see `splits.json.purge_semantics`).
2. **Ignoring `miss_*`**: a null `f_raw_*` is not 0; filling NaN with 0 manufactures spurious signal, especially in the financial columns with very high missingness.
3. **Mixing cross-sectional features across days**: `f_cs_*` is meaningful only within the cross-section that generated it; do not pool-normalize across days, and do not mix ranks from different dates into the same batch statistics.
4. **Overlapping-label autocorrelation**: adjacent-day samples are highly correlated; random splits or early stopping overstate performance. Always evaluate with time- and stock-grouped splits.
5. **Treating `is_common=false` as common stock**: those rows (warrants, preferreds, etc.) still take part in cross-sectional ranks; selecting by `asset_id` alone silently mixes in unintended securities.
6. **Hand-editing `data/output/` artifacts**: `build-samples` clears and rebuilds everything on every run and refreshes sha256; make changes in code or `config/universes/exclusions_v1.json`, then rebuild.
