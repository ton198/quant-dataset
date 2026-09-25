# quant-dataset

`quant-dataset` prepares and publishes frozen quantitative datasets. It is a
data repository and preparation library, not a model-training repository.

The package exposes deterministic preparation of chronological role anchors,
token demand, and causal same-asset windows. The download command is:

```text
quant-dataset download --data-dir PATH --start YYYY-MM-DD --end YYYY-MM-DD
```

The older `quant-dataset-prepare` entry point remains available.

## Session calendar snapshot

`data/input/metadata/session_calendar.csv` is a fixed, one-column snapshot of
XNYS regular US-listed-equity cash sessions for 1990-01-01 through 2025-12-31
(inclusive). Session labels use `America/New_York`; full-day closures are
absent and early closes remain sessions. The accompanying schedule and
provenance files are the audit record. Regenerate and compare a snapshot with:

```text
quant-dataset build-calendar --start 1990-01-01 --end 2025-12-31 --calendar XNYS \
  --output-path data/input/metadata/session_calendar.csv \
  --provenance-path data/input/metadata/session_calendar_provenance.json \
  --schedule-path data/input/metadata/session_calendar_schedule.csv \
  --comparison-path data/input/metadata/session_calendar_comparison.json \
  --existing-path data/input/metadata/session_calendar.csv
```

Use `--dry-run` to inspect hashes and counts without writing. The calendar is
global: an individual suspension does not change it, and a union of
per-asset trading dates must never be used as an asset's calendar. Rebuild
downstream labels/fundamentals when the calendar date set changes, so their
manifests bind the exact calendar bytes.

The four canonical files are published with a final
`session_calendar_bundle.json` commit marker in the same metadata directory.
Consumers must reject a missing marker or any member hash mismatch; verify the
canonical bundle with `quant-dataset build-calendar --verify-bundle
data/input/metadata`. Custom output paths remain supported as standalone files;
canonical output uses the marker as its completeness signal. A failed write
therefore cannot be mistaken for a complete canonical bundle.

Compatibility note: `build_windows` and `load_single_fold` now default to
`strict=True`. Callers must pass an explicit, unique session calendar containing
every anchor. During migration only, pass `strict=False` to retain the old
feature-date fallback; this emits `DeprecationWarning` and should not be used
for reproducible production builds.

Compact labels remain unchanged: they contain one `target_return_*d` column per
horizon and no per-horizon exit columns. Use the public
`materialize_samples(labels, calendar, horizon=...)` API, or `prepare` with
`--horizon`, to derive one-horizon samples using exact calendar positions:
`entry = signal + 1 session`, `exit = signal + 1 + horizon sessions`, and
`known_at` means `exit_open`. Weekend/holiday gaps are skipped by positional
calendar arithmetic; no natural-day shift or filling is performed. The CLI can
write an explicit derived table with `--write-derived-samples PATH` and an
adjacent manifest. `--labels-manifest` and
`--require-calendar-bundle --calendar-bundle-dir DIRECTORY` enable fail-closed
hash verification.

`download` also retrieves SEC structured financial data for each universe entity
with a CIK: Company Facts, submissions metadata, and the historical submission
index files listed by submissions. It does not download filing documents such as
10-K or 10-Q originals. SEC requests require `--sec-user-agent` or the
`QUANT_DATASET_SEC_USER_AGENT` environment variable. Use
`--skip-sec-financials` to run Yahoo-only downloads; a ticker CSV used for SEC
financials must include `cik` or `cik10`.

## Reproducibility

Inputs are caller-controlled CSV tables and an explicit JSON exclusion
manifest. Preparation validates required columns, dates, duplicate keys,
history availability, label purging, and exclusion membership. It emits
deterministically ordered CSV/JSON results. `migration/MIGRATION_RECEIPT.json`
records the copy-only migration, source/target mappings, sizes, and SHA256
hashes for the immutable artifacts.

## Layout

- `data/raw/sec/financials/`: immutable, content-addressed SEC Company Facts JSON payloads and manifest.
- `data/input/`: semantic source tables read by downstream builds.
  - `data/input/metadata/`: `XNYS session_calendar_*.{csv,json}` and `sec_ticker_universe.csv`.
  - `data/input/sec/universe_960/`: `identity.csv`, `coverage.json`, and `download_manifest.json` for the 960-entity source universe.
  - `data/input/sec/universe_958_projected/`: the same manifest set with a 958-row `identity.csv`.
  - `data/input/yahoo/aggregate/min5y/aggregate_yahoo_ce58120c8a1fb08e/`: canonical daily data for labels.
- `data/artifacts/`: processed outputs.
  - `data/artifacts/staged/yahoo/aggregate/min5y/aggregate_yahoo_projection_35fc8128d49d03b6/`: 958-universe projection.
  - `data/artifacts/curated/fundamentals/core_v1/`: `fundamental_observations.csv`, `fundamental_feature_changes.csv`, `skipped_entities.csv`, and `manifest.json`.
  - `data/artifacts/features/yahoo/features_e1ef0f729906bdab/`: `features.csv` and manifest; `data/artifacts/samples/yahoo/samples_54ef1eabc4b791d0/`: `samples.csv` and manifest.
  - `data/artifacts/labels/multi_horizon_v1/`: `multi_horizon_labels.csv` (future 1d..30d returns) and `labels_manifest.json`.
  - `data/artifacts/prepared/research_958/`: `fit.csv`, `reserve.csv`, `screen.csv`, `select.csv`, `demand_token_keys.csv`, and `summary.json`.
- `data/provenance/migrations/`: relocation receipts and future run manifests.
- `data/_archive/2026-09-23_legacy_sec_capture/`: cold storage outside the four-layer design, including `artifacts/sec/excluded_958/` and `provenance/sec/source_960/`.
- `config/universes/`: explicit universe and exclusion definitions.
- `migration/`: immutable migration manifests and receipt.
- `src/quant_dataset/`: reusable preparation and window code.

`_archive/2026-09-23_legacy_sec_capture/` 保留了 `excluded_958` / `source_960` 两套历史字面命名，因为它们的 manifest 用相对路径互相引用，改名会破坏校验。

Large data paths are ignored by Git intentionally; code, configuration, docs,
and migration records remain trackable.
