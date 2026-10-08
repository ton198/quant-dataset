# AGENT navigation

**This repository is an offline market/macro data pipeline and sample-bundle package.** The maintained sample product is one finance-free `samples` contract, with no sample product-generation selector or compatibility aliases. Existing output and baseline paths are preserved read-only: the 2026-10-03 `data/output/` publication retains its original `schema_version="samples_v3"` manifest (23,938,669 rows, 6,532 tickers, 132 columns, 96 features), the former 147-column output remains at `data/output-v1-backup-20261003T172214933236Z`, and the frozen baseline remains separate. These are archived artifact facts, not current source/test results or a new publication. Never overwrite, migrate, or delete them. Existing Company Facts and `financial_events_v1` artifacts remain independent of samples.

**English** | [简体中文](AGENT.zh-CN.md)

## 1. Task routing table

| Task type | Required reading (≤2) | Code entry points | How to verify |
|---|---|---|---|
| Sample labels, non-financial features, splits, or output schema | [samples-architecture.md](docs/developer/samples-architecture.md), [data-contracts.md](docs/developer/data-contracts.md) | Current API: `src/samples/{builder,query,contracts,validation}.py` | Run only assigned current-contract fixture/invariant tests; verify schema/fingerprints, consumed-input provenance and output hashes; build only to a fresh, non-protected `--out` path. Do not use a cross-generation projection as a correctness oracle. |
| SEC download/organize behavior and legacy financial artifacts | [download.md](docs/developer/download.md), [data-contracts.md](docs/developer/data-contracts.md) | `src/download/{financials,organize_financials,financial_events}.py`; `src/download/manager.py` | Run only assigned offline tests or organize smoke. These separate inputs are not read by the `samples` builder; preserve `financial_events_v1` as its own artifact contract. |
| SEC filing archive catalog/acquisition/parsing/verification/query | [filings.md](docs/user/filings.md), [financial-filing-archive.md](docs/developer/financial-filing-archive.md) | `src/filings/{cli,workflow,archive,acquisition,processing,query}.py` | Keep archive separate from the `samples` builder and legacy Company Facts cache; use explicit root/workspace and bounded IDs. Parse is offline by default; taxonomy fetch requires explicit opt-in/host allowlist. The independent five-filing pilot is persisted but partial; no broad issuer/economic correctness claim. |
| Download / data sources (Yahoo, SEC Company Facts/submissions, FRED, universe, overrides) | [download.md](docs/developer/download.md), [architecture.md](docs/developer/architecture.md) | `src/download/{universe,market,financials,macros,organize_financials}.py`; `config/sources.toml`; `config/universes/ticker_cik_overrides.json` | `pytest tests/test_download.py tests/test_ticker_overrides.py -q`; dry-run if assigned. Existing SEC download scope is unchanged and does not fetch full filing HTML. |
| CLI options or behavior | [cli.md](docs/user/cli.md), [filings.md](docs/user/filings.md) | `src/cli/main.py:_parser`, `main`; `src/filings/cli.py`, `processing.py`; current query API `samples.query.query_samples` | `--help` and assigned focused CLI/query checks; keep download/catalog/verify independent of optional extras and keep parse taxonomy fetch opt-in. CLI command names remain `build-samples` and `query-samples`. |
| Read or query a sample bundle | [query.md](docs/user/query.md), [data-format.md](docs/user/data-format.md) | Current API `src/samples/query.py`; `src/cli/main.py` | Query accepts only the strict current `samples` manifest/schema and validates all registered output hashes/bytes/rows; it rejects historical bundles and does not change inputs. Integrity checking is not independent release/semantic verification. |
| Data-quality issue (missing market/macro values, labels, exclusions, ticker→CIK) | [known-quirks.md](docs/developer/known-quirks.md), [data-contracts.md](docs/developer/data-contracts.md) | `config/universes/exclusions_v1.json`, `ticker_cik_overrides.json`; matching organize/build functions | Assigned focused tests; compare the relevant manifest/QC projections without treating historical finance diagnostics as current sample features. |
| Tests and offline fixtures | [testing.md](docs/developer/testing.md) | `tests/` | Run only the validation assigned by the task owner. The old 735-passed/2-skipped and 0-difference publication records are historical evidence only; they were not produced by current code or by this documentation change. Never report them as new test results. |
| Candidate schema / invariant verification | [samples-architecture.md](docs/developer/samples-architecture.md), [data-contracts.md](docs/developer/data-contracts.md) | `src/samples/{contracts,validation,builder}.py` and independent current fixtures in `tests/fixtures/samples/current/` | Verify registry counts, canonical column order, finance-free inputs, semantic invariants, consumed-input provenance and output protections. Do not use removed cross-generation projection or financial calibration tools as a gate. |

## 2. Invariants

1. **Raw source files are not hand-edited.** Existing raw SEC, FRED, and universe payloads use content-addressed storage; Yahoo interval files may be replaced by their existing downloader. The separate filing archive has bounded catalog/download/parse/verify/query commands; keep archive/workspace roots outside protected source/output/cache locations. Parse defaults offline; external taxonomy preparation is explicit and host-allowlisted. This archive is not a `samples` input. See [filings.md](docs/user/filings.md).
2. **Determinism:** identical inputs and build parameters must produce semantically identical outputs; stable sorting and manifest hashes remain required.
3. **No lookahead:** labels enter at the `t+1` open and exit at the `t+1+h` open (`adj_open = open × adj_close / close`); macro differences use positions on the canonical axis. No filing date or financial data is a `samples` input.
4. **Hashes and provenance:** `manifest.json.outputs` records sha256/bytes/rows for every generated output except the manifest's self-reference. `input_provenance.files` records streamed sha256/bytes for every consumed input and rechecks them before manifest publication; `input_inventory` is counts-only.
5. **Current-contract correctness:** test sample formulas and boundary behavior against small independently authored expected-value fixtures, plus stable-key, dtype/schema, split/purge, missingness, determinism, security, and failure invariants. Do not make a historical cross-generation projection the correctness oracle. The archived 2026-10-03 publication report is not a current test result and does not imply financial coverage or economic correctness.
6. **Purge is applied at build time:** 31 signal sessions before each applicable split boundary are dropped; consumers do not purge again.
7. **Non-finite values become missing:** non-finite raw feature values are normalized to null with the matching `miss_*` indicator.
8. **CLI contract sync:** changes to `download`, `build-samples`, `query-samples`, or any `filings` subcommand must update [cli.md](docs/user/cli.md); changes to archive scope/status must also update [filings.md](docs/user/filings.md) and its Chinese version. Bundle-query changes also update [query.md](docs/user/query.md).
9. **Financial separation:** `samples` must not read financial files or financial `_meta.json` records, run financial preflight/coverage/inventory, or emit financial features. If a financial-status field is retained, `not_applicable` is not a coverage claim. Existing `financials.csv` and selected `financial_events_v1` structured extracts remain separate organized data; they are not complete XBRL/filing archives and are not sample inputs. Do not delete or migrate existing raw, organized, output, or baseline data.
10. **Workspace and safe candidate build:** `workspace_root` defaults to CWD and may be set with `--workspace-root`; do not infer it from package installation paths. Default exclusions come from `<workspace_root>/config/universes/exclusions_v1.json`; missing config is an error. Explicit `--exclusions-file` paths resolve relative to CWD. Protect `<workspace_root>/data/` and related raw/output/baselines paths; normal `<data>/organized` inputs also protect recognized raw/output/baseline/archive siblings beside `<data>` independently of workspace root. Reject symlink paths/ancestors, non-empty targets, and overlap with protected paths or actual inputs. The CLI default is `--out data/samples-output`; build only to a fresh unused path and never overwrite preserved bundles or baselines.
11. **Release evidence:** each new publication requires its own recorded current-contract verification and provenance. Historical publication evidence is not inherited by new builds; do not claim performance, correctness, or a release PASS without an identified run.
12. **Optional query layer:** sample storage remains source files and Parquet bundles. Do not add SQLite, migrate data, or create a default persistent DuckDB database/catalog for `query-samples`; the command uses the optional query extra, accepts only the strict current `samples` contract, validates manifest-registered output hashes/bytes/rows, and leaves inputs unchanged. Preserved historical bundles are not supported by `query-samples`; inspect them with Python/PyArrow. Python reads and the regular pipeline install remain supported without the query extra.

## 3. Command cheat sheet

```bash
# Run only the task-assigned current-contract samples tests from the current tests/ tree.

# Build a samples candidate only in a new, separate directory.
# Choose a different path if this one already exists; do not delete old artifacts to reuse it.
WORKSPACE_ROOT=$PWD
CANDIDATE_DIR=/tmp/opencode/candidate-samples-finance-free
test ! -e "$CANDIDATE_DIR" && test ! -L "$CANDIDATE_DIR" || { echo "Choose a new, unused, non-symlink candidate directory" >&2; exit 1; }
PYTHONPATH=src .venv/bin/python -m cli.main build-samples \
  --workspace-root "$WORKSPACE_ROOT" --data-dir data/organized --out "$CANDIDATE_DIR"

# Existing download/organize commands are separate and are not needed to build this candidate.
# Download and filing-archive acquisition are separate workflows, not sample-build inputs.
```

## 4. Documentation map

| Doc | One-liner |
|---|---|
| [architecture.md](docs/developer/architecture.md) / [architecture.zh-CN.md](docs/developer/architecture.zh-CN.md) | Pipeline stages and separation of sample inputs from SEC financial artifacts |
| [data-contracts.md](docs/developer/data-contracts.md) / [data-contracts.zh-CN.md](docs/developer/data-contracts.zh-CN.md) | Raw/organized contracts and the single `samples` schema; retained bundle labels are archival facts |
| [download.md](docs/developer/download.md) / [download.zh-CN.md](docs/developer/download.zh-CN.md) | Existing Yahoo/SEC Company Facts/submissions/FRED download and organize behavior |
| [samples-architecture.md](docs/developer/samples-architecture.md) | Single-product samples package, contracts, cleanup boundary, and migration gates |
| [source-layout-audit.md](docs/developer/source-layout-audit.md) | Current source tree/import evidence before migration |
| [samples.md](docs/developer/samples.md) / [samples.zh-CN.md](docs/developer/samples.zh-CN.md) | Finance-free sample behavior and current-contract verification cues |
| [financial-filing-archive.md](docs/developer/financial-filing-archive.md) / [financial-filing-archive.zh-CN.md](docs/developer/financial-filing-archive.zh-CN.md) | Archive and processing boundary, raw pilot evidence, and remaining parser validation |
| [testing.md](docs/developer/testing.md) / [testing.zh-CN.md](docs/developer/testing.zh-CN.md) | Offline test conventions and current-contract verification guidance |
| [known-quirks.md](docs/developer/known-quirks.md) / [known-quirks.zh-CN.md](docs/developer/known-quirks.zh-CN.md) | Data-source realities and explicitly historical finance-path behavior |
| [cli.md](docs/user/cli.md) / [cli.zh-CN.md](docs/user/cli.zh-CN.md) | CLI commands, safe candidate output, and existing download behavior |
| [filings.md](docs/user/filings.md) / [filings.zh-CN.md](docs/user/filings.zh-CN.md) | Bounded SEC filing archive commands, setup, and limits |
| [query.md](docs/user/query.md) / [query.zh-CN.md](docs/user/query.zh-CN.md) | Optional read-only SQL queries over existing Parquet sample bundles |
| [data-format.md](docs/user/data-format.md) / [data-format.zh-CN.md](docs/user/data-format.zh-CN.md) | Single `samples` output format and preserved legacy bundle facts |
| [recommended-usage.md](docs/user/recommended-usage.md) / [recommended-usage.zh-CN.md](docs/user/recommended-usage.zh-CN.md) | Downstream use of a `samples` candidate and caveats |

## 5. Maintenance conventions

- When docs and implementation disagree, report the discrepancy to the task owner and update the relevant docs in the same change; do not silently rewrite protected data or implementation outside your assigned lane.
- **Bilingual sync:** update both language versions together; English is authoritative if a discrepancy remains.
- Treat `samples` as one contract; mention `samples_v1`/`samples_v3` only where needed to identify actual preserved historical artifacts. Do not revive the superseded finance-feature proposal or its tools.
- Keep the existing SEC organizer's legacy behavior accurate: `financials.csv` retains its daily whole-snapshot semantics; `financial_events_v1` is a selected nine-concept structured extract, not a full filing archive.
- Preserve measurement honesty: no estimated performance, coverage, or release PASS claims.
