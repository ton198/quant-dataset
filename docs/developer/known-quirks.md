**English** | [简体中文](known-quirks.zh-CN.md)

# Known Quirks and Data-Source Realities

Use this page to distinguish data-source behavior from defects. The maintained `samples` contract is finance-free; it does not read financial files, finance metadata, or calculate financial coverage. Existing `financials.csv` and `financial_events_v1` are separate organized artifacts. This page makes no unmeasured coverage, performance, or release-pass claims.

Related: [download.md](download.md), [samples.md](samples.md), [data-contracts.md](data-contracts.md), [../user/data-format.md](../user/data-format.md).

## 1. Current and historical quirks

| Area | Behavior | Scope / what to do |
|---|---|---|
| Legacy `financials.csv` | One latest whole-filing snapshot is selected per session. A later non-financial filing can replace that row's concept values with null; values are not carried forward per concept. | Existing organizer behavior, unchanged. This CSV is not a samples input. |
| `financial_events_v1` | Existing event/fact Parquets retain selected submission events and facts from a nine-concept whitelist. | Separate organized structured extract, not all XBRL, not full filing text, and not a `samples` input. The separate filing archive is another workflow and artifact boundary. |
| Retired finance-feature proposal | An unimplemented proposal once described financial sample columns and coverage rules. | Not an active sample contract or build path; do not use it to add financial inputs, aliases, or gates. Existing Company Facts and `financial_events_v1` documentation remains independent and intact. |
| Bank and issuer concept gaps | The selected nine-concept whitelist does not include every issuer-specific revenue/profit concept; for example, bank interest/fee tags may be outside it, and XOM's selected source has no `OperatingIncomeLoss`. Later comparative facts are not automatically backfilled into an earlier filing event. | A limitation of the separate legacy extract. There is no current samples financial coverage metric. Preserve source uncertainty; do not claim these extracts are full XBRL. |
| Missing SEC resources | Existing organize code distinguishes confirmed empty/no-input cases from missing, malformed, or incomplete resources. | Applies to separate organized financial outputs. samples does not inspect SEC resource status; its financial status is `not_applicable`. |
| No market data | A ticker without `market.csv` cannot contribute market-based sample rows. | Check market inputs and sample manifest; no current sample ticker-count measurement is claimed here. |
| Multiple tickers per CIK | The universe is ticker-based while CIK identifies the issuer; share classes can share a CIK. | Do not assume a one-to-one mapping. Existing organizer outputs are per ticker, with CIK/source provenance. A future archive should use CIK+accession as filing identity and keep dated ticker mapping separately. |
| Historical v1 incorrect CIK sourcing for GAM/MFM/PEO | A v1 organizer fallback selected raw payloads when the hashed filename happened to contain requested CIK digits without checking the payload's inner CIK. The three v1 `financials.csv` files were byte-identical (333,320 bytes; SHA-256 `a1a8468c7f487ca04eaa31ae9b641b06995039ad2f31845c91f6df5a09be72ba`) and used inner CIKs `0002003977`, `934549`, and `0001780731`. | Historical v1 data-quality bug. Do not use those legacy financial histories as issuer truth. Existing organizer code later added normalized exact CIK matching; samples excludes financials entirely. |
| FRED histories and revisions | FRED series start at different dates; values are latest-revised rather than vintage. The organizer estimates release timing from reference period plus about one month and a later session. | Existing macro behavior. Use `miss_m_*` and treat early gaps/revisions as data-source limitations; no sample macro coverage rate is claimed here. |
| `is_common` | Common-stock classification is a ticker-suffix heuristic, not a security master. Non-common rows are retained and participate in cross-sectional ranks. | Filter explicitly if a strict common-stock universe is needed. |
| `excess_*` benchmark | There is no SPY benchmark. `excess_5d/21d` subtracts the same-date equal-weight target mean from rows with `is_common=true` and `flag_extreme_label=0`; non-common rows may still receive an excess value. | By contract. Rebuild a different benchmark downstream if needed; do not call this cap-weighted market excess. |
| Adjusted-open labels | `adj_open = open × adj_close / close` is an adjusted price, not an executable fill. | Labels omit costs/slippage and should not be treated as simulated trades. |
| Extreme-label rows | A row is flagged if its label window crosses an adjacent-session adjusted-open ratio outside `[0.5, 2.0]`; flagged rows remain in samples but are excluded from the excess benchmark mean. | Annotation, not row deletion. Use the candidate QC/manifest count; no current sample count is asserted here. |
| Former financial age columns | `f_raw_days_since_filing`, `f_raw_days_since_financials`, and `f_raw_days_since_oldest_financial_input` belonged to prior sample schemas. | Removed from samples. The v1 12.40% missing-rate figure for `days_since_filing` is a historical baseline only. |
| Existing download progress | The parsed `preserve_progress_on_success` config option is not used by the manager; the progress file remains. | Existing behavior. Use the existing `--force`/`--force-rebuild` semantics rather than relying on this option. |
| Yahoo interval files | Re-downloading the same ticker/range overwrites its named CSV; different ranges can coexist and are concatenated/deduplicated by organize. | Existing behavior; raw files are not hand-edited. |

## 2. Financial-source separation

No financial-feature proposal is active for sample construction. The existing Company Facts cache, legacy `financials.csv`, and selected `financial_events_v1` extract retain their own source and organizer contracts; the separate filing archive has its own scope. None is implicitly joined into `samples`, and neither the selected extract nor the filing archive should be described as complete XBRL coverage or complete filing text.

## 3. Adding or reviewing a quirk

1. Prove the symptom with specific source/output artifacts; do not rely on memory.
2. Determine whether it is a source limitation or implementation defect. Route code tasks via [AGENT.md](../../AGENT.md).
3. Label every historical metric by schema/version. Do not create unmeasured sample rates or imply a release gate passed.
4. Keep English/Chinese copies synchronized. For proposed archive scope, update [financial-filing-archive.md](financial-filing-archive.md) only; do not implement a fetcher/parser as a documentation change.
