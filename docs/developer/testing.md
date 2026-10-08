**English** | [简体中文](testing.zh-CN.md)

# Test Layout, How to Run, and Verification Gates

> Scope: adding/changing tests, deciding whether a change can merge, and reproducing assigned verification.
> Related: [AGENT.md](../../AGENT.md) §1 routing table, [download.md](download.md), [samples.md](samples.md), [known-quirks.md](known-quirks.md).
>
> **Historical evidence note:** Exact test counts, skips, and smoke criteria below describe earlier repository snapshots only; they are not current `samples` validation results. The current sample test approach uses independent current-contract fixtures and invariants, not cross-generation projection or financial calibration tools. Use current test paths from the worktree when running assigned checks.

## 1. How to run and the baseline

```bash
# Standard command (run from the repository root)
PYTHONPATH=src .venv/bin/python -m pytest tests/ -q
# Equivalent: each test file inserts src/ into sys.path itself, so this also works
.venv/bin/python -m pytest -q

# Single assigned file (replace with the path present in the worktree)
PYTHONPATH=src .venv/bin/python -m pytest tests/<assigned-test-file>.py -q
PYTHONPATH=src .venv/bin/python -m pytest tests/test_download.py -q -k cli
```

| Metric | Baseline (measured) |
|---|---|
| Collection | 63 tests |
| Result | **61 passed, 2 skipped** (about 40 seconds locally, hardware-dependent) |
| Warnings | `test_organize_parallel.py`/`test_ticker_overrides.py` emit `DeprecationWarning: fork() ... multi-threaded` under ProcessPool; expected |
| Dependencies | Fully offline: no real HTTP; runs without `secrets.toml` (config tests use tmp files) |

Note: the test process does not depend on a real `data/`; only `test_load_sources_parses_provider_settings` and the CLI dry-run subprocess read the repository's `config/sources.toml` (no network).

## 2. Historical test file map (snapshot only; counts may have changed)

| File | Count | Coverage | Representative tests |
|---|---|---|---|
| `tests/test_download.py` | 26 | `config` (4), `progress` (6), `acquire_lock` (1), `organize_market` quality/derived columns (6, including 2 skips), `organize_financials` as-of visibility (4), CLI (5) | `test_progress_atomic_save_and_load_round_trip`, `test_market_adjustment_factor_and_one_day_return`, `test_financial_filing_becomes_visible_on_next_calendar_session`, `test_cli_market_stage_requires_dates` |
| `tests/test_build_samples.py` | 7 | End-to-end sample build, label formulas, cross-sectional ranks, `miss_*`, purge, inf→NaN, dtype/schema, YoY main path/fallback/precedence | `test_training_sample_build_labels_features_exclusions_and_splits`, `test_build_purges_label_windows_at_split_boundaries`, `test_fiscal_identifiers_take_precedence_over_closer_report_end` |
| `tests/test_organize_financials.py` | 11 | `_owned_paths` does not read unrelated JSON/fallback, missing rows still written with no input, ticker cache, `_CONCEPTS` whitelist, old/new tag priority, Apple-style fiscal fallback, three submission shapes, ragged rejection, page-only visibility, accession dedup | `test_submission_rows_parses_recent_table_and_column_page_shapes`, `test_column_oriented_payload_rejects_ragged_arrays`, `test_page_only_old_filing_fact_becomes_available_at_its_filing_date` |
| `tests/test_organize_parallel.py` | 8 | Parallel = serial consistency, completed tickers skipped, partial/corrupt outputs rebuilt, shared CIK with independent outputs, empty CIK, bars-less ticker with CIK, failure isolation | `test_process_results_match_serial_organization`, `test_corrupt_meta_or_missing_recorded_output_is_reorganized`, `test_one_ticker_failure_does_not_block_other_tickers` |
| `tests/test_progress_lock.py` | 6 | Stale PID reclaim, live PID blocks, normal release, empty/invalid PID (3 parameterized) | `test_stale_pid_lock_is_reclaimed`, `test_live_pid_lock_is_not_reclaimed` |
| `tests/test_ticker_overrides.py` | 5 | XOM override forward/reverse, missing-file fallback, CLI flag, `--force-rebuild` semantics | `test_xom_forward_lookup_uses_vetted_cik_override`, `test_missing_override_file_preserves_snapshot_mappings`, `test_force_rebuild_reorganizes_completed_tickers` |

## 3. What the two skips actually are

Both come from `tests/test_download.py` and deliberately record unimplemented behavior rather than an environment problem:

| Test | Skip reason (actual meaning) |
|---|---|
| `test_market_zero_close_flag_case_is_not_supported` | `close == 0` is currently classified as `invalid_ohlc` by `_market_quality`: only `close < 0` becomes `negative_price`, while `close == 0` hits the later `close <= 0` invalid branch. If the product needs close=0 as its own class, change the code + test |
| `test_market_non_session_quality_flag_is_not_supported` | `organize_market` filters out non-calendar dates before assigning `quality_flag`, so no "non-session" row exists to flag; non-sessions only show up in the `market_dropped_non_session` counter |

If you change `_market_quality` or the organize filter order, these two skips become real behavior-change points: either implement and unskip them, or confirm they stay skipped and update this document.

## 4. Fixture conventions

| Convention | Description |
|---|---|
| Temp directories | Always `tmp_path`; tests never write the repository's `data/` (the only exception is the read-only `load_sources`) |
| Fake raw construction | Download and organizer tests use local-shaped fixtures; current sample fixtures live under `tests/fixtures/samples/current/` and define small fixed inputs plus independent expected values. |
| Direct internal calls | Tests may call organizer or sample-package helpers directly, bypassing the CLI for point assertions; use current imports and test real file-access behavior rather than retaining no-op compatibility helpers. |
| monkeypatch | Only for paths and IO tracing: `universe.TICKER_CIK_OVERRIDES_PATH` (ticker_overrides) and a wrapped `Path.read_text` (organize_financials' "does not read unrelated files" assertion) |
| Cache isolation | Tests touching `_TICKER_CACHE` use `_reset_ticker_cache(raw_dir)` (or clear it entirely) |
| Network | Zero real requests; the network branches of `market.fetch_market`/`financials.fetch_financials`/`macros.fetch_macros`/`universe.fetch_universe` **currently have no test coverage** (see §6) |
| Subprocess CLI | `_run_cli` uses `sys.executable -m cli.main` with an injected `PYTHONPATH=src`, cwd fixed to the repository root, timeout=10s |

## 5. Existing-source smoke examples (only when assigned; not a samples release gate)

### 5.1 Changing financial extraction (`organize_financials.py`)

```bash
# Prerequisite: raw/sec/financials and raw/sec/universe already have caches; market is not involved
PYTHONPATH=src .venv/bin/python -m cli.main download --stage organize --force-rebuild --tickers AAPL,XOM
```

Recorded organizer smoke expectations (historical snapshot; re-check against current fixtures before treating as an acceptance result):

| Check | Expectation |
|---|---|
| AAPL `financials.csv` | First non-null `revenue` has `report_period_end ≈ 2009-06-27` (back to around 2009); `operating_income/net_income/assets` all have values |
| XOM `financials.csv` | First visible `revenue` row has `date=2012-02-27`, fiscal FY2011; `operating_income` empty throughout (XOM has no such tag; expected) |
| Row count | `financials_output` = number of organize-calendar sessions (currently 9,252) |
| Provenance | `_meta.json.inputs` is non-empty and its sha256 matches the raw files; with no owned inputs, inputs is empty but the output is still written |

Only after this passes run the full `download --stage all` (or at least organize for every ticker).

### 5.2 Changing sample logic (`samples.builder`)

```bash
# Run only the current-contract sample tests assigned for the change.
# Confirm migrated paths under tests/; fixture definitions live under tests/fixtures/samples/current/.

# If a candidate build is assigned, use a new path; never target data/output or a baseline.
CANDIDATE_DIR=/tmp/opencode/candidate-samples-finance-free
test ! -e "$CANDIDATE_DIR" && test ! -L "$CANDIDATE_DIR" || { echo "Choose a new, unused candidate directory" >&2; exit 1; }
PYTHONPATH=src .venv/bin/python -m cli.main build-samples \
  --workspace-root "$PWD" --data-dir data/organized --out "$CANDIDATE_DIR"
```

The maintained `samples` contract is finance-free: 43 raw + 10 CS + 43 MISS = 96 features and 132 physical columns. Current focused sample tests include `tests/test_samples_current_contract.py`, `tests/test_samples_semantic_regression.py`, `tests/test_samples_integrity_regression.py`, `tests/test_samples_safety_regression.py`, `tests/test_query_samples.py`, `tests/test_verification_tools.py`, and `tests/test_build_samples.py` as relevant; small fixtures live in `tests/fixtures/samples/current/`. Test independently authored expected values for labels and edge cases, exact schema/order/dtypes, 43-raw `missing_frac`, keys/flags/splits/purge, ticker-failure and finance-isolation invariants. Verify deterministic outputs, input content provenance, strict manifest/output verification, and workspace/data sibling path protections against implementation. Use only a fresh candidate path; removed projection tools are not a correctness gate. The old 735-passed/2-skipped and archived projection records are historical only. Do not claim a final PASS without evidence from the assigned current run.

### 5.3 Changing download/progress/overrides

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_download.py tests/test_progress_lock.py tests/test_ticker_overrides.py -q
PYTHONPATH=src .venv/bin/python -m cli.main download --stage all --start 1990-01-01 --end 2025-12-31 --dry-run
```

Acceptance: the dry run only prints the plan and writes nothing; after an override change, XOM's old/new CIK bidirectional mapping and force-rebuild behavior match the assertions in `test_ticker_overrides.py`.

## 6. Minimum test requirements for new changes

| Change | Minimum test | Status |
|---|---|---|
| Add/adjust a concept tag | Update the `_CONCEPTS` tuple assertion + add a fixture fact that verifies priority | Template exists |
| Change `_submission_rows` shape support | One case per shape; ragged/empty-array rejection cases; accession dedup case | Template exists |
| Change fiscal fallback | Apple-style non-calendar fiscal year + FY determination without fy/fp | Template exists |
| Change as-of/visibility | Filing not visible on its own day, visible the next day; amendment quality_status | Template exists |
| Change overrides/force-rebuild | Forward, reverse, missing-file fallback, CLI flag, force-rebuild of completed items | Template exists |
| Change progress/locking | Round trip, force/resume, stale/live/malformed lock | Template exists |
| Change market quality/derived columns | One valid bar + one invalid high + negative price + adjustment_factor/return | Template exists |
| Change labels/features | Independent expected-value cases in `tests/fixtures/samples/current/`; new columns assert schema/dtype and null-indicator behavior | Current-contract fixture suite |
| Change purge/splits | 31-session window, `rows_by_split` identity, end-of-data NaN retention | Template exists |
| Add a network download branch | **Currently empty**: at minimum add offline tests for "cache hit does not re-download", "corrupt manifest fallback", and "ragged/non-JSON error" (monkeypatch urlopen or inject fixture files) | To be added |
| Determinism | Suggested: "rebuilding twice from identical input yields identical manifest.outputs hashes" | Not automated |

Principle: new tests must be offline, use only `tmp_path`, and assert internal state directly; do not loosen existing assertions to fit a change — first decide whether it is a behavior regression or a contract change (contract changes must sync [data-contracts.md](data-contracts.md) and [../user/cli.md](../user/cli.md)).

## 7. Troubleshooting

| Symptom | Cause/fix |
|---|---|
| `ModuleNotFoundError: cli`/`download` | Not running from the repository root, or missing `PYTHONPATH=src` (test files insert src themselves, but manual CLI runs need it) |
| CLI subprocess test times out | `_run_cli` uses a fixed timeout=10s; can happen under heavy local load — rerun to confirm |
| `fork() may lead to deadlocks` warning | Known warning from ProcessPool + a multi-threaded interpreter; does not affect results and needs no fix |
| `test_load_sources_parses_provider_settings` fails | Someone changed key values in `config/sources.toml` (provider / rate_limit / series order); update the test or accept it as a contract change |
| Lock tests behave oddly on NFS/tmpfs | Locking relies on inode identity and `os.kill`; a local-disk `tmp_path` is a prerequisite |
