**English** | [简体中文](download.zh-CN.md)

# Download and Organize Internals

> Scope: changes to `src/download/`, `config/sources.toml`, `config/universes/*`, progress/locking, and SEC financial extraction.
> Related: [architecture.md](architecture.md) (data flow and module boundaries), [data-contracts.md](data-contracts.md) (field contracts), [known-quirks.md](known-quirks.md) (data-source realities), [testing.md](testing.md) (verification).
> Existing download/organize behavior below describes `src/download/*.py`, `src/cli/main.py`, and `config/sources.toml`; that behavior is unchanged by the active `samples` contract. The selected financial event/fact output is a separate nine-concept structured extract, not a full filing archive and not a samples input. The bounded filing archive is a separate implemented workflow; see [financial-filing-archive.md](financial-filing-archive.md) for its scope and current evidence.

## 1. Data flow and entry points

```text
CLI: quant-dataset download --stage {market|financials|macros|organize|all} [--start --end --tickers --force --force-rebuild --dry-run --data-dir --workers]
      └─ src/cli/main.py: main → run_download(repo_root=Path.cwd(), ...)
          └─ src/download/manager.py: run_download
               ├─ load_sources/load_secrets (config/sources.toml + config/secrets.toml)
               ├─ acquire_lock (data/.download_progress.lock)
               ├─ market    → raw/yahoo/<TICKER>/<start>_<end>.csv
               ├─ financials→ raw/sec/financials/<sha256>.json (companyfacts + submissions + historical pages)
               ├─ macros    → raw/fred/<SERIES>/<sha256>.json
                └─ organize  → organized/stocks/<TICKER>/{market.csv,financials.csv,
                                  financial_events.parquet,financial_facts.parquet,_meta.json}
                              organized/shared/{macro.csv,_meta.json}
```

The financial organizer outputs shown above are separate files. The active `samples` builder reads only organized market/macro panels and exclusions; it does not read financial files or financial `_meta.json` records.

- `STAGES = ("market", "financials", "macros", "organize")`; the CLI expands `--stage all` into all four.
- `--data-dir` (default `data`) remaps `raw_dir/organized_dir/progress_file/progress_tmp_file/progress_lock_file` through `manager._with_data_dir`. Relative paths resolve against `repo_root`, and `repo_root` also determines where `config/` is read from, so the command must run from the repository root.
- The `market` stage requires `--start/--end` (`run_download` raises `ConfigError`; the CLI also reports a parser error first). `--dry-run` returns 0 immediately after loading configuration — no writes, no network requests.
- `--force` resets the progress worklist (re-download); `--force-rebuild` only affects organize (it skips the completion check and rebuilds existing outputs). The two are independent.

## 2. Module responsibilities

| Module | Responsibility | Key symbols |
|---|---|---|
| `manager.py` | Orchestrates the 4 stages, progress persistence, process lock, parallel organize, failure counting | `run_download`, `STAGES`, `_organize_tickers`, `_ticker_is_organized`, `_with_data_dir` |
| `universe.py` | Fetch/cache the SEC exchange universe; apply ticker→CIK overrides forward and backward | `fetch_universe`, `_rows`, `load_ticker_cik_overrides`, `apply_ticker_cik_mapping_overrides` |
| `market.py` | Yahoo daily bars (one request interval per ticker) | `fetch_market` |
| `financials.py` | Download companyfacts, submissions, and historical submission pages; content addressing + manifest | `fetch_financials`, `_fetch`, `_read_manifest`, `_manifest_mapping` |
| `macros.py` | Download FRED observation series; content addressing + manifest; per-series progress | `fetch_macros` |
| `organize.py` | Market cleaning/derived columns, macro session alignment, merged `_meta.json` writes | `organize_market`, `organize_macros`, `_market_quality`, `write_meta` |
| `organize_financials.py` | SEC facts → legacy session snapshots plus pre-snapshot filing-event artifact generation | `organize_financials`, `_CONCEPTS`, `_submission_rows`, `_fact_for_period`, `_fiscal_identifiers` |
| `financial_events.py` | Normalized filing events and finite fact-version Parquet tables under `financial_events_v1` | `organize_artifact`, `write_tables`, `artifact_is_valid`, `write_empty_artifact` |
| `progress.py` | Atomic progress writes + PID-based exclusive lock + stale-lock reclaim | `Progress`, `initialize`, `save_atomic`, `acquire_lock` |
| `config.py` | Load `secrets.toml` / `sources.toml` into dataclasses | `load_secrets`, `load_sources`, `SourcesConfig` |
| `errors.py` | Domain exceptions | `ConfigError`, `DownloadError`, `OrganizeError` |

## 3. On-disk layout

| Path | Written by | Naming/semantics |
|---|---|---|
| `data/raw/yahoo/<TICKER>/<start>_<end>.csv` | `market.fetch_market` | Interval-based filename; re-downloading the same interval overwrites, different intervals accumulate, and organize deduplicates by date (keep last) |
| `data/raw/sec/universe/<sha256>.json` | `universe.fetch_universe` | Filename must equal the content sha256, otherwise the cache is invalid and the file is re-downloaded |
| `data/raw/sec/financials/<sha256>.json` + `manifest.json` | `financials.fetch_financials` | One flat content-addressed directory shared by all CIKs; manifest is `{"resources": {logical_key: [record,...]}}` |
| `data/raw/fred/<SERIES>/<sha256>.json` + `manifest.json` | `macros.fetch_macros` | Same as above, with `logical_key = observations:<series>` |
| `data/organized/stocks/<TICKER>/market.csv` | `organize_market` | Sessions inside the calendar; includes `quality_flag` and derived columns |
| `data/organized/stocks/<TICKER>/financials.csv` | `organize_financials` | Legacy one-row-per-session latest whole-filing snapshot; this contract remains unchanged |
| `data/organized/stocks/<TICKER>/financial_events.parquet` | `financial_events.organize_artifact` | One valid filing event per row, including filings with no financial facts |
| `data/organized/stocks/<TICKER>/financial_facts.parquet` | `financial_events.organize_artifact` | Normalized finite USD concept facts by source filing and actual period/version |
| `data/organized/stocks/<TICKER>/_meta.json` | `organize._write_meta` / `financial_events.commit_artifact_meta` | Records legacy and event/fact outputs; nested `financial_events` contract, completeness, CIK/raw-input identity, resource states, quality/rejection counts, calendar, hashes and row counts |
| `data/organized/shared/macro.csv` | `organize_macros` | Wide table, session-aligned + ffill |
| `data/.download_progress` (+ `.tmp`, `.lock`) | `progress.py` | Per-item status; the lock file contains the PID |

## 4. SEC financial ingestion: legacy snapshot plus filing-event artifact

### 4.1 Download layer: `fetch_financials(cik10, cfg, secrets, raw_dir)`

Fixed request sequence (`financials.py`):

1. `companyfacts:{cik}` → `cfg.financials.company_facts_url_template` (default `https://data.sec.gov/api/xbrl/companyfacts/CIK{cik10}.json`);
2. `submissions:{cik}` → `submissions_url_template`;
3. every `name` in `submissions.get("filings", {}).get("files", [])` shaped like `CIK{cik10}-submissions-*.json` → `https://data.sec.gov/submissions/<name>`, with `logical_key = submissions-page:{cik}:{name}`.

Mechanics:

- `_fetch` handles the User-Agent (`secrets.sec_user_agent`), `Accept: application/json`, `retries` with exponential backoff (≤ 8 s), and a per-request `rate_limit_seconds` throttle. Failures raise `DownloadError`.
- `obtain` checks the manifest first: a matching `(logical_key, url)` whose file sha256 still matches is reused as-is; otherwise it downloads, validates the JSON, writes `<sha256>.json` (an existing file is not rewritten), and appends a record (`fetched_at_utc`, `attempts`, `byte_size`, `status: done`).
- The manifest is persisted atomically with `.json.tmp` + `replace`; `resources` is reshaped from a list into `{logical_key: [records]}`.
- The return value includes cache-hit paths, so organize works offline: with a complete raw cache the financials stage is all `done` and organize only reads local files.
- One CIK's files can be reused by several tickers (multiple share classes of the same company); `fetch_financials` takes a CIK, and the ticker is only the manager's unit of iteration.

### 4.2 Filing parsing layer: `_submission_rows(payload)` handles three shapes

| Shape | Detection | Parser | Notes |
|---|---|---|---|
| `filings.recent` (current main submissions payload) | `filings` and `filings.recent` are both dicts (what `_submission_rank` checks) | inline expansion in `_submission_rows` | parallel arrays zipped by index; `_submission_rank=0` |
| `fields`/`data` (generic 2-D table) | both keys are lists | inline dict conversion | only the intersection of `fields` and each row's length is taken (missing columns omitted, extra values ignored); `_submission_rank=1` |
| Column-oriented historical pages (`accessionNumber`/`filingDate` etc. as parallel arrays) | `_is_column_oriented(payload)` | `_column_oriented_rows` | if any column has a different length (ragged), the whole payload is rejected and `[]` is returned; `_submission_rank=2` |

- Parse order: `filings.recent` and `fields`/`data` are each expanded and appended whenever present; only when neither produced any rows does parsing fall back to column-oriented pages. When multiple shapes coexist, the caller deduplicates by `_submission_rank`, so `recent` always wins.
- Deduplication (inside `organize_financials`): `submission_payloads` is stably sorted by `_submission_rank`, then rows are deduplicated in order by `accessionNumber` — `recent` always beats pages; within the same rank the order follows `_owned_paths` filenames (content hashes), which keeps the result deterministic and reproducible. Rows without an accession are kept, not deduplicated.
- `_owned_paths(root, cik10)`: first reads manifest records whose `logical_key` ends with `cik10` or contains `:{cik10}:`; only when the manifest is missing/empty does it fall back to `*.json` filenames containing `cik10`. A payload-level check also rejects a payload whose `payload["cik"]`, when present, does not equal the target CIK (compared after stripping leading zeros), so a wrong payload cannot slip in.

### 4.3 Concept extraction layer: `_CONCEPTS` whitelist + `_fact_for_period`

`_CONCEPTS: dict[output column, tuple[tag, ...]]` lists tags in priority order, newest first; each output column takes the **first** tag that matches a fact. Every key becomes a column of `financials.csv`.

| Output column | Priority sequence |
|---|---|
| `revenue` (13) | `Revenues` → `RevenueFromContractWithCustomerExcludingAssessedTax` → `RevenueFromContractWithCustomerIncludingAssessedTax` → `SalesRevenueNet` → `SalesRevenueGoodsNet` → `SalesRevenueServicesNet` → `SalesRevenueGoodsGross` → `SalesRevenueServicesGross` → `RevenuesNetOfInterestExpense` → `FinancialServicesRevenue` → `InsuranceServicesRevenue` → `RevenueNotFromContractWithCustomer` → `SalesRevenueOilGas` |
| `net_income` (4) | `NetIncomeLoss` → `ProfitLoss` → `NetIncomeLossAvailableToCommonStockholdersBasic` → `NetIncomeLossAvailableToCommonStockholdersDiluted` |
| `operating_income` | `OperatingIncomeLoss` |
| `assets` | `Assets` → `AssetsNet` |
| Others | `gross_profit`=`GrossProfit`; `operating_cash_flow`=`NetCashProvidedByUsedInOperatingActivities`; `capital_expenditure`=`PaymentsToAcquirePropertyPlantAndEquipment`; `liabilities`=`Liabilities`; `equity`=`StockholdersEquity` |

`_build_fact_index(fact_payloads)` builds two indexes in one O(facts) pass so later lookups are O(1):

- `by_tag[tag][(filed, end)] → [fact,...]`: iteration order is payload order → sorted tag → sorted unit → array order, which is deterministic;
- `by_filing[(filed, end)] → [fact with fy/fp,...]`: only used as a fallback by `_fiscal_identifiers`.

`_fact_for_period(fact_index, tag, filed, fy, fp, end, form, accession_number)` filters and ranks candidates:

1. only facts in the `(filed, end)` bucket exactly matching the filing row are considered (`end` is the filing's `reportDate`);
2. when the filing row has a non-empty fy/fp, the fact's fy/fp must be equal; when the fact's form is non-empty, it must equal the filing form after stripping `/A`; when an accession is present, `accn` must match; `val` must convert to float;
3. **duration filter stops YTD facts being read as single quarters**: for `10-Q` facts with a `start`, only 70–125 days are kept; for `10-K`, only 300–400 days; instant facts (no `start`, e.g. `Assets`) are unrestricted;
4. preference by min: `10-Q` picks the duration closest to 91 days, `10-K` the closest to 365, instant facts sort first, ties break on the `start` string. A `10-Q` YTD fact (e.g. 180+ days) or an over-short window (< 70 days) is already excluded in step 3 and never reaches the preference ordering.

`_fiscal_identifiers(filing, fiscal_index, reported_facts, all_filings)` decides in this order:

1. any selected fact (`reported_facts`) carrying `fy`+`fp` → use it directly;
2. the fy/fp of the fact in `fiscal_index[(filed, reportDate)]` whose `accn` matches;
3. the filing row's own `fy`/`fp` (only some older submissions carry them);
4. `10-K` → `(reportDate.year, "FY")`;
5. `10-Q` → anchor on the **nearest earlier 10-K**: fiscal_year is that 10-K's fy (or its report year), and the quarter number is the rank (1–3) of this report date among the distinct 10-Q `reportDate`s after the anchor up to this period end → `(annual_year+1, Qn)`. This is why Apple-style non-calendar fiscal years are not mislabeled as calendar quarters;
6. everything else (including a 10-Q that cannot be anchored) → `(None, None)`: leaving it blank beats mislabeling.

### 4.4 Legacy daily-CSV as-of session alignment (`organize_financials` main loop)

- `report_rows` is sorted by `(available_at, accession_number)`, where `available_at = filingDate`; `report_filed_dates` is the parallel list.
- The sorted calendar is scanned with a **two-pointer prefix sweep**: `while report_filed_dates[visible_count] < session: visible_count += 1`, then `report_rows[visible_count-1]` is selected. A filing therefore becomes visible only on the session **after** its filing date (strict `<`): the filing date itself never uses that information, conservatively aligning to the disclosure time.
- Each output row carries the full row of the latest visible filing (including `form`, `accession_number`, `fiscal_year`, `fiscal_period`, `report_period_end`, all concept columns, and `days_since_filing`). **Non-financial filings (8-K/Form 4/424B*, etc.) also become the "latest visible row"**, with all concept columns empty — values do not carry forward across filings; see [known-quirks.md](known-quirks.md).
- `days_since_filing = (session - that filing's filingDate).days` (calendar days, not sessions).
- Amendments: `is_amendment = form.endswith("/A")`. `quality_status` = if an earlier original filing with the same `report_period_end` exists → `"ok"` when any concept has a value, otherwise `"missing"`; if no original exists → `"amendment_only"`; for a non-amendment → `"ok"` with values / `"missing"` without. An amendment never resurrects an earlier original filing.
- Output column order is fixed: `date, available_as_of, accession_number, form, is_amendment, fiscal_year(Int64), fiscal_period, report_period_end, days_since_filing, <keys of _CONCEPTS in order>, quality_status`; dates are written as `%Y-%m-%d`.
- `_write_meta` merges inputs keyed by path; when this run has no owned inputs, old `sec/financials/` inputs are dropped so provenance never points at raw files that no longer exist (test-covered).

### 4.5 Existing organized event/fact extract (`financial_events_v1`; not a full archive)

`organize_financials` builds the existing `financial_events_v1` artifact from selected raw SEC submissions and Company Facts **before** it chooses the legacy daily snapshot. The legacy `financials.csv` schema and whole-row replacement behavior remain unchanged; the artifact coexists with it and is never reconstructed from it. Its facts are limited to the existing nine-concept whitelist; it is not a complete XBRL or filing-text archive. `samples` does not read this artifact or the financial metadata.

| Table | Grain and stable fields |
|---|---|
| `financial_events.parquet` | One valid event per filing, including non-financial/no-fact filings. Fixed fields: `event_id`, `asset_id`, `cik10`, `accession_number`, `filed_date`, `effective_visible_session`, `form`, `is_amendment`, `report_period_end`, `fiscal_year`, `fiscal_period`, `fiscal_key_source`, `quality_status`, `source_submission_path`, `source_submission_sha256`, `source_submission_locator`. |
| `financial_facts.parquet` | One normalized candidate per source event, concept and actual period/version. Fixed fields: `fact_version_id`, `event_id`, `concept`, float64 `value`, `unit`, `taxonomy`, `tag`, `period_kind`, `period_start`, `report_period_end`, `duration_days`, `fiscal_year`, `fiscal_period`, `fiscal_key_source`, `filed_date`, `effective_visible_session`, `fact_accession_number`, `match_method`, `source_fact_path`, `source_fact_sha256`, `source_fact_locator`. |

Both tables use fixed Arrow schemas even when empty. Event IDs use normalized zero-padded CIK + accession when accession exists; otherwise the source submission hash + record locator form a stable identity and an accession-absence diagnostic is counted; its counter key is not a stable contract. Facts link to one event. Fact accession matching is exact; when the fact has no accession, only a unique `(filed_date, report_period_end, base form)` event match is allowed. Ambiguous/unmatched candidates are rejected, as are contradictory values for one event/tag/unit/actual period; download time is not used to order disclosures.

Extraction iterates the existing nine-concept `_CONCEPTS` whitelist. Numeric conversion and finite screening happen **before** tag-priority selection, so a higher-priority NaN/inf cannot hide a lower-priority finite fact. Currency is not inferred or converted; only USD candidates are retained. The artifact records tag, taxonomy, unit, source locator, and period metadata. `assets`, `liabilities`, and `equity` must be instant facts. A flow with verified start/end is `quarter` only for a 10-Q duration of 70–125 days, or `annual` only for a 10-K duration of 300–400 days (inclusive day count); YTD/other duration facts stay `unknown` and cannot enter growth/flow-ratio formula cohorts. Missing-start flow facts may remain finite `unknown` disclosures. Fiscal-key conflicts, invalid/ambiguous source matches, non-USD units, non-numeric/non-finite values, invalid ranges, unsupported taxonomies, and rejected filings are counted.

`effective_visible_session` is mapped on the XNYS calendar as the first session strictly **after** `filed_date`; the filing date itself is never visible. Mapping failure is counted and prevents a complete artifact. The output-calendar range remains separate from the expanded effective-session mapping calendar. All events with the same effective session are applied before features for that session are emitted. Amendments remain independent events/versions and start affecting the history only at their own effective session; empty amendments do not delete prior facts. `quality_status` is metadata, not a filter on whether valid finite facts can be used.

`_meta.json.financial_events` records `contract_version="financial_events_v1"`, `complete`, `status`, `empty_reason`, `cik10`, `input_hashes`, `raw_input_inventory`, `raw_input_inventory_sha256`, `input_resource_status`, `input_resource_diagnostics`, `calendar`, `output_calendar`, `quality_counts`, `rejection_counts`, and `events`/`facts` path, sha256, and row counts. Top-level `_meta.json` also records `cik10`, `raw_input_inventory_sha256`, `row_counts.financial_events`/`financial_facts`, and output registrations for both Parquets.

`input_resource_status` has required `manifest`, `submissions`, and `companyfacts` entries. Hard states such as `missing`, `unreadable`, `malformed`, and `unknown` make the artifact incomplete; a confirmed no-CIK/no-input case is represented explicitly and may be complete with schema-correct empty files. Event/fact files are committed atomically, and completion metadata is registered last. The completion identity includes both CIK and raw SEC inventory fingerprint; changed raw resources require reorganization. `rejection_counts` reports diagnostics across raw resource/payload validity, source/date/accession matching, unit/numeric/finite checks, period and fiscal-key conflicts, and effective-session mapping. Individual counter-key names and aggregation are implementation diagnostics, not a stable contract; consumers must not depend on them.

## 5. ticker→CIK overrides and caching

- File: `config/universes/ticker_cik_overrides.json`, `{"schema": ..., "notes": {...}, "overrides": {"XOM": "0000034088"}}`. Tickers are upper-cased, CIKs zero-padded to 10 digits; invalid entries are skipped.
- Forward: after parsing a snapshot, `universe._rows` replaces `TickerRow.cik10` from the overrides (`fetch_universe`'s cache-hit path also goes through `_rows`, so cache and network agree).
- Reverse: `organize_financials._ticker_mappings(raw_dir)` scans `raw/sec/universe/*.json` to build `ticker→cik` and `cik→ticker` (both `setdefault`, so when several tickers share a CIK the reverse map keeps the first one seen), then calls `apply_ticker_cik_mapping_overrides` to correct both maps in sync (the old CIK is removed only if it still points back to that ticker). The result is cached per `raw_dir.resolve()` in the module-level `_TICKER_CACHE`; tests clear one directory with `_reset_ticker_cache(raw_dir)`.
- A missing override file means no overrides, and behavior is exactly as before the mechanism existed (test-covered).
- `organize_financials(..., output_ticker=ticker)` is passed explicitly by the manager, so every ticker sharing a CIK writes its own directory (`test_shared_cik_tickers_write_independent_outputs_in_parallel`); `_ticker_for_cik` only serves legacy callers that omit `output_ticker`.
- After changing an override you must rebuild the affected tickers with `--force-rebuild --stage organize`; XOM's CIK was corrected from the 2025 holding company `0002115436` to the historical filer `0000034088`, and raw cache under the old CIK is not used by the new CIK (re-run the financials stage).

## 6. Progress and locking (`progress.py`)

| Structure | Contents |
|---|---|
| `Progress` | `run_id`, `started_at_utc`, `universe_source`, `stages: {stage: {item: status}}` |
| status | `pending` / `done` / `failed:<reason>` |
| `initialize(stages, source, force, ...)` | `force=True` or no file → a brand-new worklist; otherwise existing state is kept, new items get `pending`, and `universe_source` is refreshed when non-empty |
| `pending(state, stage)` | returns every non-`done` item (failures are retried) |
| `save_atomic` | `.tmp` write + `flush`/`fsync` + `os.rename` + directory `fsync` (directory fsync failure only warns) |

- Locking: `acquire_lock` creates the `.lock` with `O_CREAT|O_EXCL` and immediately writes the PID. On failure, `_reclaim_stale_lock` decides: an empty file with mtime < 0.05 s is considered "being written" and kept; a missing/invalid PID past that window is stale; a live PID (`os.kill(pid,0)`; no permission counts as alive) → raises `Another download run holds lock`. Removal compares inode identity first (`_unlink_if_same_inode`) so someone else's lock is never deleted; release only unlinks when inode + PID still point at the owner.
- `run_download` holds this lock for the whole run (universe fetch plus all four stages); **organize's ProcessPool children do not download**, they only read raw.

## 7. Parallel organize (`manager._organize_tickers`)

- Completion check `_ticker_is_organized`: `_meta.json` parses, `ticker` matches, and all output paths exist; a ticker with a CIK must have the legacy `financials.csv` and a valid `financial_events_v1` artifact. `artifact_is_valid` checks `complete`, contract version, expected CIK, raw-input inventory fingerprint, fail-closed resource statuses, both Parquet hashes/row counts/schemas, and event/fact relationships. An old meta or CSV alone can never skip the new artifact rebuild.
- The worker (`_organize_ticker_worker`) first `unlink`s the old `_meta.json` (so a partial state cannot be mistaken for completion), then runs `organize_market` (only logs and returns None when there is no Yahoo CSV) and `organize_financials` (when a CIK exists); the two operations collect errors separately and neither blocks other tickers.
- `ProcessPoolExecutor(max_workers=workers, initializer=_initialize_organize_worker, initargs=(session_calendar,))`: the session calendar is copied once per process at initialization; workers never construct `exchange_calendars` themselves.
- Returns and logs `(succeeded, skipped, failed)`; a `failed>0` does not stop other tickers, but the run's final exit code is 1.
- Organize calendar: `start or 1990-01-01` through `end or today`. For an organize-only run without `--tickers`, the selected tickers fall back to the directories under `raw/yahoo/` (`_raw_tickers`); when tickers are given, they are organized one by one, and a ticker absent from the universe only attempts market data (CIK is None).

## 8. Market and macro organize

`organize_market(ticker, raw_dir, organized_dir, calendar)`:

- Reads all `raw/yahoo/<TICKER>/*.csv` and concatenates them; column names are casefolded and spaces become underscores; accepts `date`/`datetime` and `adj_close`/`adjclose`; a missing `open/high/low/close/volume` raises `OrganizeError` (adj_close falls back to close when absent).
- Rows whose `date` fails to parse are dropped → sorted by date, same-day keep last → only dates in the calendar are kept (counted in `market_dropped_non_session`).
- `quality_flag` (`_market_quality`): any price < 0 → `negative_price`; `close<=0`, `volume<0`, `high<max(open,close,low)`, `low>min(open,close,high)`, or a non-finite value → `invalid_ohlc`; otherwise `ok` (close == 0 falls into `invalid_ohlc`, not `negative_price`).
- Derived columns: `adjustment_factor = adj_close/close`; `return_1d/5d/20d` = pct_change of close; `volatility_20` = rolling(20).std(ddof=1) of return_1d; `volume_ratio_20 = volume / rolling(20).mean()`; `intraday_range = (high-low)/close`.
- The `market.csv` column order is the `columns` list inside `organize_market`; `_meta.json` records input/output sha256 plus row counts.

`organize_macros(raw_dir, organized_dir, calendar)`:

- Walks `raw/fred/*/*.json` and uses the directory name as the column name; for each observation `approximate_release = reference month + 1 month, same day` (clamped to the month end) → it becomes visible on the first session **strictly after** that date; observations with no visible session are dropped.
- The wide table is sorted by session, then each column is `reindex`ed and `ffill`ed. `_meta.json.known_issues` always states: FRED responses omit release timestamps, visibility is approximated by "reference period + 1 month + 1 session", and values are latest revised, not vintage.

## 9. Extension points

| What you want to change | Where | What must be updated together |
|---|---|---|
| Add a financial concept to the separate organized extract | `organize_financials._CONCEPTS` (output column → ordered tag tuple) | `tests/test_organize_financials.py::test_financial_concept_priority_whitelist_includes_old_and_new_us_gaap_tags` + a fixture fact. `samples` does not consume finance; adding a financial sample feature requires a separate contract decision, not this organizer change. |
| Add a data-source stage | `manager.STAGES` + a new block in `run_download` + `progress_stages`; CLI `--stage` choices | the new stage's failure counting and progress status; [../user/cli.md](../user/cli.md) (invariant 8) |
| Add provider configuration | dataclass in `config.py` + `load_sources` fields + `config/sources.toml` | `tests/test_download.py` config tests |
| Add a ticker→CIK correction | `overrides` in `config/universes/ticker_cik_overrides.json` | `tests/test_ticker_overrides.py`; re-download financials + `--force-rebuild --stage organize` |
| Exclude a ticker | `config/universes/exclusions_v1.json` (`asset_id`, deduplicated, must be upper-case) | the sample layer's `_load_exclusions` validates duplicates; after rebuilding samples compare manifest `exclusions_applied` |

## 10. Existing organizer limitations (not samples coverage or release metrics)

- **Bank concepts remain outside the selected whitelist**: bank-specific interest/fee concepts such as `InterestAndDividendIncomeOperating` are not part of the existing nine-concept extract. The event/fact artifact cannot produce facts outside `_CONCEPTS`. No current samples bank coverage is measured because finance is not a sample input.
- **The selected whitelist has known issuer/concept gaps**: for example, XOM's selected source lacks `OperatingIncomeLoss`; comparative facts in later filings are not normalized backward into prior event rows. This is an existing organizer limitation, not a samples field or coverage claim; see [known-quirks.md](known-quirks.md).
- **Missing or confirmed-empty SEC resources**: the existing organizer distinguishes confirmed no-input cases from missing/malformed resources and incomplete mappings. These resource states apply to separate organized financial artifacts; the samples builder does not inspect them.
- **No Yahoo bars**: a ticker without `market.csv` cannot produce market-based sample rows. No current sample ticker count is asserted here.
- **Large filing histories**: event volume can vary by issuer. Measure counts and organize resource use from a specifically identified run; no current sample financial-input or runtime measurement is asserted.
- **Macro-series coverage**: FRED histories differ. Check the relevant raw resource status and manifest before diagnosing an acquisition issue; no current sample missing rate is asserted here.
- **Input-history boundary**: the existing SEC cache is not a complete historical vintage archive and has no archived filing HTML/iXBRL set. Missing raw facts, later comparative-period facts, non-USD facts, ambiguous matches, and concepts outside the whitelist remain unavailable.

## 11. How to verify

```bash
# 1) Offline unit tests (no network)
PYTHONPATH=src .venv/bin/python -m pytest tests/test_download.py tests/test_organize_financials.py \
  tests/test_organize_parallel.py tests/test_progress_lock.py tests/test_ticker_overrides.py -q

# 2) Plan only, write nothing
PYTHONPATH=src .venv/bin/python -m cli.main download --stage all --start 1990-01-01 --end 2025-12-31 --dry-run

# 3) Existing organizer smoke (only when an organizer task is assigned; not a sample-build step)
# Reuses raw cache; does not fetch market/financials/macros.
PYTHONPATH=src .venv/bin/python -m cli.main download --stage organize --force-rebuild \
  --tickers AMZN,JPM,AAPL,XOM --start 1990-01-01 --end 2025-12-31
# Inspect the existing financial_events_v1 metadata/hashes and event/fact provenance.
# AMZN filed_date 2025-08-01 is not visible on 08-01; legacy organizer mapping is next-session based.
# financials.csv retains its daily whole-snapshot semantics; neither organizer output is a samples input.

# 4) Check provenance / content addressing for any ticker
.venv/bin/python - <<'PY'
import json

with open("data/organized/stocks/AAPL/_meta.json") as handle:
    meta = json.load(handle)
print([item["path"] for item in meta["inputs"]])
PY
# raw filenames = sha256; after organize, cross-check them against manifest.json via _meta.json inputs
```

- After changing download/progress/locking run `tests/test_download.py tests/test_progress_lock.py`; after changing extraction run `tests/test_organize_financials.py` plus the smoke; after changing an override run `tests/test_ticker_overrides.py` and inspect the XOM outputs.
- Dependency order: the financials stage depends on the universe (for CIKs); organize depends on the raw cache (market may be missing, financials may be missing). For an offline rebuild, do not pass the `market`/`financials` stages.
