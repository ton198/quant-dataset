# Changelog

All notable changes to this repository are documented in this file. The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/).

**English** | [简体中文](CHANGELOG.zh-CN.md)

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
