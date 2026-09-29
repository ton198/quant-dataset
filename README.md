# quant-dataset

A frozen quantitative training dataset of US equities (1990–2025), plus the reproducible pipeline that builds it.

[![CI](https://github.com/ton198/quant-dataset/actions/workflows/ci.yml/badge.svg)](https://github.com/ton198/quant-dataset/actions/workflows/ci.yml)

**English** | [简体中文](README.zh-CN.md)

## What this is

`data/output/` holds 23.9M rows of features that only use information available that day, plus the forward returns to predict.
"Frozen" means the schema (`samples_v1`) is fixed and every file is locked by sha256: a rebuild from the same inputs should match byte for byte.
This repo prepares data. It does not train models or run backtests — that belongs downstream.

| Fact | Value |
|---|---|
| Rows × columns | 23,938,669 × 147 |
| Stocks | 6,532 |
| Trading days | 9,067 canonical sessions |
| Coverage | 1990-01-02 – 2025-12-31 |
| Storage | 36 yearly Parquet partitions + `meta` / `splits` / `manifest` / `qc` |
| License | MIT |

A *canonical session* is the shared trading calendar used to align every stock: a day counts when at least 500 common stocks (`is_common`) traded.

## What one row is

One row = one stock (`asset_id`) on one signal day (`date`).
Features are what was knowable that day: 48 raw (`f_raw_*`: price/volume, financials, macro) + 15 cross-sectional (`f_cs_*`) + 48 missing flags (`miss_*`).
Labels are the future: 1–30 session returns, plus `excess_5d` / `excess_21d` (market-relative).

```text
signal day t             entry at t+1 open          exit at t+1+h open
    │                          │                            │
    │  features end at t       │  label window: h sessions
    └──────────────────────────┴────────────────────────────┘
      h = 1..30 canonical sessions; the main targets use h = 5 and 21
```

## 30-second quickstart

Read the output with DuckDB (a consumer-side tool, not a repo dependency):

```python
import duckdb

con = duckdb.connect()
df = con.execute("""
    SELECT date, asset_id, excess_5d, excess_21d,
           f_cs_momentum_120, f_cs_volatility_20
    FROM read_parquet('data/output/samples/year=*/part-00000.parquet',
                      hive_partitioning = true)
    WHERE year BETWEEN 2019 AND 2020
      AND is_common
      AND flag_extreme_label = 0
""").df()
```

Do not load all 23.9M rows at once — the float32 feature matrix is ~10 GB. Read by year.
More loaders and pitfalls: [docs/user/recommended-usage.md](docs/user/recommended-usage.md).

## How to use

| Step | Do |
|---|---|
| 1. Split | Use `splits.json`: fit (1990–2018) to train, select (2019–2020) to tune, screen (2021–2024) for out-of-sample, reserve (2025) for one final test. Purge is already applied at build time — do not purge again. |
| 2. Target | Train on `excess_5d` / `excess_21d`. They subtract the same-day equal-weight common-stock mean. Use `target_return_*` as auxiliary tasks only. |
| 3. Clean | Drop or down-weight rows with `flag_extreme_label = 1` (120,510 rows, ≈0.5%): their label window crosses a price glitch, so the returns are unreliable. |

Two things that bite most often:

- **Survivorship bias**: the universe is today's SEC list and contains no delisted stocks. Trust `screen`, not absolute returns.
- **Sparse financials**: ~96% missing, and missingness is not random (snapshot semantics — only the latest filing is visible). Model `miss_*`; never fill NaN with 0.

Full workflow: [docs/user/recommended-usage.md](docs/user/recommended-usage.md).

## Rebuild from scratch

Prerequisites: Python ≥3.10 (CI covers 3.10–3.13), access to Yahoo / SEC / FRED, and a `config/secrets.toml` with `sec_user_agent` (SEC requires contact info) and `fred_api_key`.

```bash
uv sync --frozen
cp config/secrets.example.toml config/secrets.toml   # then fill in both keys

PYTHONPATH=src .venv/bin/python -m cli.main download \
  --stage all --start 1990-01-01 --end 2025-12-31 --workers 16
PYTHONPATH=src .venv/bin/python -m cli.main build-samples

.venv/bin/python -m pytest
```

`download` resumes after interruptions. `build-samples` clears and rebuilds `data/output/` every run.
All flags and exit codes: [docs/user/cli.md](docs/user/cli.md).

## Repository layout

| Path | What |
|---|---|
| `data/` | `raw/` (immutable snapshots) → `organized/` (per-ticker CSVs) → `output/` (the frozen dataset) |
| `src/` | CLI entry, download/organize, `build_samples.py` |
| `config/` | Universe lists, `sources.toml`, local `secrets.toml` (gitignored) |
| `docs/` | `user/` guides and `developer/` contracts |
| `tests/` | Offline pytest suite (baseline: 61 passed / 2 skipped) |

## Documentation

| User guides | Developer docs |
|---|---|
| [CLI manual](docs/user/cli.md) — commands, secrets, exit codes | [AGENT.md](AGENT.md) — task routing for agents and contributors |
| [Data format](docs/user/data-format.md) — all 147 columns, label formulas | [architecture.md](docs/developer/architecture.md) — five-stage data flow |
| [Recommended usage](docs/user/recommended-usage.md) — training workflow, pitfalls | [data-contracts.md](docs/developer/data-contracts.md) — layer contracts, baselines |
| | [samples.md](docs/developer/samples.md) — canonical calendar, purge, partitions |
| | [download.md](docs/developer/download.md) — endpoints, resume, CIK overrides |
| | [known-quirks.md](docs/developer/known-quirks.md) — "looks like a bug, isn't" |
| | [testing.md](docs/developer/testing.md) — test layout, offline fixtures |

## Development

Baseline: `pytest` 61 passed / 2 skipped, `ruff check` and `ruff format --check` clean; CI runs the same on Python 3.10–3.13.
Contributions: [CONTRIBUTING.md](CONTRIBUTING.md). Agents: start at [AGENT.md](AGENT.md).

## Known limitations

- **Survivorship**: the universe is today's SEC ticker list; delisted stocks are absent, so history reads better than it was.
- **FRED is not vintage**: macro features use latest revised values, so they contain hindsight.
- **Adjusted open is not a fill price**: labels come from `open × adj_close / close`, not executable trades.
- **Financial coverage is sparse and non-random**: ~96% missing, skewed toward filing-dense periods and large caps.

More: [docs/developer/known-quirks.md](docs/developer/known-quirks.md).

## License

MIT — see [LICENSE](LICENSE).
