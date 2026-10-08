# Changelog

All notable changes to this repository are documented in this file. The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/).

**English** | [简体中文](CHANGELOG.zh-CN.md)

## [Unreleased]

### Changed

- Financial extraction runs an async bounded queue (`run_extraction_async` with a synchronous `run_extraction` wrapper): separate local-processing (`[extraction].workers`, default 4), model (`[extraction].model_workers`, default 1) and prefetch (`[extraction].prefetch_windows`, default 4) capacities, with `model_workers` never exceeding `workers` or `prefetch_windows`. Source-ordered results, a single journal-writer thread and no automatic retries are preserved.
- Unknown-outcome transport failures (`UnknownRequestError`: timeouts, connection errors) stop admission of new sends; `RequestStateError` (unknown prior state, identity collision) fails only its window while other journal failures stay fatal; identical in-batch windows wait and replay the persisted response with one provider call. Sync-or-async per-window completion callbacks never retrigger work. Progress reports queued, in-flight and durably saved responses separately from completed window outcomes. Prior 32-way provider overlap needs explicit `model_workers=32` (config or `--model-workers`) with `workers`/`prefetch_windows` of at least 32.
- The `filings extract-financials` CLI streams phase and window progress to stderr, keeps its JSON summary on stdout, and supports `--no-progress`; window totals remain unknown until evidence planning finishes.
- Prompt revision `open-financial-v2` states exact numeric lexical, numeric multiplier, configured policy and current-window quote requirements without relaxing validation. A private three-window DeepSeek comparison returned 164/165 valid records and 161/162 valid numeric records; these are contract checks, not accuracy or recall. Bilingual extraction/CLI docs and configuration examples are synchronized.
- Introduce checked window-local short references (`open-financial-v3`), exact request-bound decoding for every quote field, and durable reference maps in request identity/journals. Unknown or mistyped refs remain unresolved; original source validation and raw responses are preserved.
- Exercise the checked-ref protocol through a new 20-filing, 598-window CLI batch: 240 schema-valid responses had no unknown short refs. The batch remains incomplete due primarily to provider balance exhaustion (331 HTTP 402) and dynamic concurrency limits (23 HTTP 429); outputs and failure indexes are preserved without retry.

## [0.1.0] - 2026-09-29

### Added

- Sample-building pipeline: the `download` / `organize` / `build-samples` stages produce the frozen `samples_v1` package (23,938,669 rows × 147 columns, covering 1990–2025), with every output registered by hash in `manifest.json`.
- SEC financial ingestion: a filing-concept whitelist, `fiscal_year` / `fiscal_period` identifiers, paged parsing of historical filings, and a `ticker→CIK` override (XOM).
- Purge embargo and lookahead-free labels: the 31 signal sessions before each of the fit/select/screen split boundaries are embargoed at build time; labels enter at the `t+1` open and exit at the `t+1+h` open.
- User docs (`docs/user/`: CLI, data format, recommended usage) and developer docs (`docs/developer/`: architecture, data contracts, testing, and more), plus the `AGENT.md` navigation.

### Fixed

- Label leakage across splits: label windows near a split boundary no longer cross into the next split (purge embargo).
- YoY period matching: match by `fiscal_year` / `fiscal_period` keys first, so Q1 is not matched against Q2.
- Non-finite feature values: ±inf/NaN in raw features are uniformly converted to missing and given `miss_*` flags.
- Missing revenue concepts: widened the revenue whitelist to include old/narrow us-gaap concepts such as `SalesRevenueNet`.
- XOM CIK mismatch: the universe snapshot pointed at the wrong entity; switched to the historical filer CIK and added override support.
- Truncated filing history: parse the columnar submissions pages beyond `filings.recent`, restoring visibility of early 10-K/10-Q filings.
