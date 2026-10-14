# Known Quirks and Limits

- The Yahoo universe is based on a current SEC exchange list; it does not provide delisted-stock history or a point-in-time ticker mapping.
- FRED data are latest-revised observations, not vintage releases. Series begin at different dates.
- SEC Company Facts/submissions cache is metadata and structured facts, not full filing HTML/iXBRL.
- Report-sample coverage is selection- and source-dependent. Unavailable documents, unsupported payloads, parse failures and partial coverage remain visible in diagnostics; validation does not imply complete issuer coverage or financial correctness.
- SEC request contact information must be valid and non-placeholder for real retrieval. Network tests always mock transport.
- Yahoo adjusted prices are not executable fills; labels are not a backtest or trading simulation.

Preserve existing user data and report only measurements observed in a named run. See [download internals](download.md), [data contracts](data-contracts.md), and [SEC report samples](../user/filings.md).
