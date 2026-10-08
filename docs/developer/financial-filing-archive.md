**English** | [简体中文](financial-filing-archive.zh-CN.md)

# SEC Filing Archive: Implemented Boundary and Deferred Processing

**Status (2026-10-04): bounded archive/processing is verified, but production-grade financial coverage is not established.** Two independent private prototypes remain separate: the first-five archive `/tmp/opencode/filings-pilot-v1` is at manifest v7 with 6,017 facts, 165 sections, six full text parses, four full XBRL parses, one unsupported legacy-cover numeric parse, and an exact repeat/no-op; the selected second batch `/tmp/opencode/filings-pilot-diverse-v1` is at v15 with 25,656 facts, 95 sections, 25 active parser rows (15 full text, six full XBRL, four unsupported XBRL). The second batch is ten selected filings across eight CIKs, not a census. Its RBC 40-F group has one primary-anchored full parse with 7,543 original occurrences (40 primary, 7,503 EX2), replacing the two old solo numeric parses; its raw coverage remains partial. Outcomes and case-level limitations are in §7 and the linked reports. The final source suite was 787 passed and two skipped; only the pure parser test file was subsequently formatted, with its five targeted tests passing. These are bounded parser results, not financial correctness or comprehensive coverage. The daily `data/output/` release remains the independent finance-free v3; the v1 backup and frozen baseline remain separate and untouched. See [samples.md](samples.md).

## 1. Current state and scope boundary

The existing `data/raw/sec/financials/` cache contains SEC Company Facts, submissions, and historical submissions-page JSON. The existing organize path also produces `financials.csv` and a selected `financial_events_v1` structured extract. The latter includes filing-event rows and facts from a nine-concept whitelist; it is not all XBRL facts or a full filing-document archive.

A separate archive boundary now exists. `filings catalog` copies locally cached submission resources into an explicit archive; `filings download` acquires a bounded set of selected filing inventories/documents; `filings parse` processes present selected packages; `filings verify` checks the published archive/core rows; and `filings query` exposes manifest-listed tables that exist. This is separate from the Company Facts cache and does not write to `samples_v3`. The archive must not be copied into each daily sample row; any future join remains an optional downstream operation.

Parser modules and schemas are wired through the bounded `filings parse` CLI. Current real-parser evidence is in two independent archives: the first-five archive v7 has 6,017 facts/165 sections; the second ten-filing archive v15 has 25,656 facts/95 sections and includes a single persisted RBC IXDS group parse. The v15 active parser inventory is 25 rows (15 full text, six full XBRL, four unsupported XBRL); statuses are parser/document outcomes, not counts of economically complete filings. Active fact owners are checked against exact source URI/hash and `provenance.source_document_id`; grouped facts remain anchored to the primary parse ID. Group notes bind the default target, exact members, cross-file evidence and dependency fingerprint. Detailed results, archive separation, and outstanding raw-scope review limits appear in §7.

**Separate non-XBRL extraction lane:** source-neutral domain/evidence/runtime scaffolding is now in progress, but no supported real-filing extraction, `filings extract` CLI, optional financial tables, or archive publication path exists. This work does not alter archive snapshots, the existing parser schemas, `financial_events_v1`, or `samples`. See [Financial extraction: current implementation and offline boundary](financial-extraction.md); fake/fixture tests are not real-source evidence.

## 2. Identity, raw preservation, and candidate layout

### Filing identity

- Use `(cik10, accession_number)` as the filing primary key. Preserve the CIK as a ten-digit string.
- Ticker is a time-varying market/security label, not filing identity. Keep a ticker↔CIK mapping table with effective/observed time and source/provenance, because issuers can have multiple tickers/share classes and mappings can change.
- An amendment has its own accession and filing record. Never overwrite the original filing with an amendment or collapse the two by report period.
- Preserve `filed_date`; record `acceptance_datetime` when present in source material. Retrieval time is provenance only and is not a public-availability timestamp.

### Implemented archive storage boundary

Each run uses an explicit archive root separate from `data/raw/sec/financials/`. Its current layout includes:

```text
<archive-root>/
  run.json
  manifest.json                     # published head; source of current snapshot truth
  ledger.jsonl                      # attempt/audit events, not publication truth
  raw/sha256/<prefix>/<sha256>      # immutable source/resource bytes
  snapshots/<snapshot-id>/manifest.json
  tables/<table>/<snapshot-id>/part-00000.parquet
```

`manifest.json` names immutable snapshots and exact Parquet/raw paths; readers do not glob for data. Catalog/download write the core `filings` and `documents` tables. Parse may publish `facts` and `sections` using the schemas owned by `parsing_models`, plus processing-owned `parses` and `dependencies` tables. All four parser tables are optional manifest-listed outputs; missing tables have no empty placeholders. User SQL views are available only if that table exists in the current snapshot. The actual storage/schema implementation is authoritative; the later occurrence list is a contract guide, not a promise of universal extraction.

### Deferred derived products

Parsed/indexed products remain separate from original archive bytes. The `filings parse` CLI publishes parser results for bounded, ready packages, while DuckDB remains an optional local query client rather than a service or required migration. The prototype materializes the active small archive tables for full-replacement commits; it is not a billion-row streaming/production scale claim.

## 3. Document inventory and fetch scope

The current bounded acquisition inventories accession resources using SEC index/detail metadata and records document names/roles, URLs, selection/fetch states, and hashes for fetched bytes. It retains inventory/index evidence and fetches selected required primary documents plus recognized XBRL resources when selected. It does **not** fetch every exhibit or mirror the filing directory. The metadata-driven selector is conservative triage, not complete content classification.

The processing package copies only present, selected, required parser documents with original SEC basenames and verifies the copies against committed hashes. Text parsing is limited to selected primary/financial exhibits. A filing may use one primary-anchored IXDS model when exact, present original inline documents in that filing are connected by typed, unambiguous cross-file context/unit/continuation/tuple/relationship references. The Python API accepts `parse_archive(..., inline_document_sets={filing_id: (primary_document_id, other_member_id, ...)})`; there is no CLI group flag. Member order is primary first and then original source URL order. Independent self-contained reports remain standalone; shared IDs/namespaces alone do not group them. Persisted group plans are validated and restored if the argument is omitted; omission never silently returns members to solo parsing. One active numeric parse row is anchored to the primary, while physical occurrence ownership is traced through source URI/hash/relative path and `provenance.source_document_id`. Named/multiple targets or ambiguous ownership are rejected. A valid grouped attempt that fails during dependency preparation is persisted as a retryable failed/partial group with a source-bound error graph and retires old solo member facts; it is not marked terminal. A classic XBRL instance is used only if no inline source is selected. Schemas/linkbases are dependency inputs, not filing-text/fact entrypoints. PDF/text support states remain explicit rather than being converted to empty successful output.

- `filings download` processes at most 5 filings by default and 50 maximum; explicit IDs must already exist in the catalog archive. Results distinguish acquired, unavailable, error, blocked, candidate, out-of-scope, and `needs_review` states.
- 6-K remains candidate unless the metadata-based scope gate has sufficient inventory evidence for an included subset; this does not prove comprehensive foreign-issuer coverage. In the first-five v7 pilot, TSMC 6-K was included only after the actual detail index and EX-99.1 financial-statement exhibit were checked, and its selected raw scope reached `scoped_complete`. In the second batch, Shopify 6-K remains candidate/partial; a temporary EX-99.1 body review found a CEO automatic securities disposition plan, but no scope-review API or automatic exclusion was implemented.
- Unknown attachments can remain candidates. A press-release label by itself is not sufficient evidence that a filing is non-financial. Do not interpret out-of-scope selection as a complete content classification.

Keep primary-document versus full-attachment scope explicit. Do not claim a “full-text archive” or complete filing coverage unless the scope and completeness are measured and reported.

## 4. Implemented XBRL fact-occurrence processing boundary

`filings parse` can publish occurrence rows under parser-owned `FACT_SCHEMA`; the schema is defined in `src/filings/parsing_models.py` and may evolve with parser versioning. Results retain raw lexical source strings, transformed/normalized values when available, context/unit XML and dimension provenance, source hash/locator, parser/version/status, and Arelle diagnostics. Invalid facts remain occurrences with validation errors and nullable interpreted numbers; they are not dropped or changed to zero. This is structural/source-preserving parsing, not SEC/EFM certification, economic validation, or one normalized result per concept.

The real Apple 2024 10-Q parser attempt yielded 781 full XBRL fact rows with no parser error codes. Runtime is pinned to Arelle 2.46.0 and locally packaged SEC inline-transformation support sourced from two official Arelle/EDGAR implementation files at commit `47a372d099168f8669d20a8a6bbe5cb16bbf71ac`; source and license hashes were verified. Transform source is not fetched at parse time. This is only transformation support—not a whole SEC EFM validation plugin or whole-filing compliance check. It enables neither HTTP-loaded plugin code nor an arbitrary user-supplied plugin path.

Preserve fact **occurrences**, not just one normalized value per concept. A derived occurrence should retain enough fields to reconstruct what the source said and how it was interpreted, for example:

- filing key (`cik10`, accession) and source document payload hash;
- taxonomy namespace URI and local tag name (retain source prefix as display/provenance when useful);
- source locator: document name/hash plus byte/element/DOM or XBRL locator, line/fragment where available;
- original lexical value and optional parsed numeric value; do not discard the lexical form;
- unit reference and resolved unit representation;
- context reference and entity identifier;
- instant date or duration start/end, preserving the explicit period kind;
- explicit and typed dimensions, with member QName/namespace and typed-member content preserved rather than flattened into a single label;
- `decimals`, `precision` when present, and `xsi:nil`/nil status;
- inline XBRL transformation/format, sign, scale, continuation chain, and the transformed numeric value when applicable;
- parser/tool name and version, extraction timestamp for provenance, and parse status.

Do not force the facts into the current nine-concept `_CONCEPTS` whitelist, collapse custom taxonomy tags, aggregate dimension variants, or silently choose one duplicate. Duplicate/overlapping occurrences are an auditable source property. Any future normalization or concept mapping should be a separately versioned derived view that points back to the occurrence records and original payload hashes.

Company Facts still does not cover every custom taxonomy or dimensional fact. The legacy raw cache contains Company Facts/submissions/history-page JSON only; those resources do not substitute for filing HTML/iXBRL or filing-specific XBRL document sets. Parsed archive facts are tied to the selected source documents/dependencies actually present, and are not asserted complete for an issuer or period.

## 5. Implemented text extraction limits

Original filing bytes remain immutable. The text parser can publish normalized HTML/text sections through parser-owned `SECTION_SCHEMA`, keyed by filing/document/parse identity and source hash. PDF extraction is not implemented; PDFs remain source bytes with an explicit unsupported status. Heading sections are heuristic anchors, not byte offsets. A text `full` status means text extraction/source checks completed, not financial or semantic completeness. Every section records extraction/parser provenance and status.

Each section row records filing/document/parse identity, source hashes, parser version, section kind, heading/content when available, structural XPath/ordinal anchors, extraction scope, and status. HTML text is normalized readable text; heuristic heading anchors are not byte offsets. Non-text/PDF inputs are explicitly unsupported rather than treated as successful empty text. The current parser status set distinguishes `full`, `partial`, `unsupported`, and `failed`; unsupported input is not a parser failure, and neither status proves financial completeness.

Track **byte/document inventory completeness** separately from **text/fact extraction coverage**. A filing can have all selected bytes present but incomplete text/fact extraction, or intentionally leave attachments unfetched. Do not report one aggregate completeness boolean that hides these states.

## 6. Visibility, user-authored as-of filters, and joins

- Catalog stores `effective_visible_session` as the first XNYS session strictly after `filed_date`; a weekend/holiday filing maps to the first later session. This metadata is not a `samples_v3` feature.
- Preserve acceptance datetime as source metadata; it does not replace the visibility rule or become the query default.
- `filings query` does not automatically filter by visibility date, select “latest,” or merge amendments. The user must write an explicit predicate such as `effective_visible_session <= CAST('<AS_OF_DATE>' AS DATE)` for the date they are testing. This filters catalog metadata but does not guarantee that all source documents or processing were complete by that date.
- Filings are keyed by `(cik10, accession_number)`; amendments remain independent. Historical ticker/CIK mappings require effective dates and provenance and are not supplied as a complete PIT mapping by this workflow. Do not use a current ticker string as filing identity.
- Any later downstream join to daily samples remains optional and must have an explicit as-of rule. Do not repeat full document text in daily rows.

## 7. Deferred work and current validation limits

- The modular catalog/acquisition/parse/verification/query CLI exists. Offline fixtures cover miniature catalog → mocked acquisition → package → taxonomy preparation → offline parse → snapshot flows. The two real bounded archives below are finite integration evidence, not broad reporting coverage.
- **First-five archive:** `/tmp/opencode/filings-pilot-v1`, manifest v7. It has 6,017 active facts, 165 text sections, six full text parses, four full XBRL parses, and one unsupported legacy-cover numeric parse. The exact repeat kept the same v7 head. The catalog's 823 filing-metadata rows are not 823 downloaded reports, and the archive remains separate from `data/output`.
- **Second-batch archive:** `/tmp/opencode/filings-pilot-diverse-v1`, manifest v15, parented to v14. Ten explicitly selected filings across eight CIKs resulted in 25 active parse rows (15 full text, six full XBRL, four unsupported XBRL), 25,656 facts, 95 text sections, and 159 dependency rows. The 3,051 filing metadata rows and 1,253 document-registry rows are not an all-filings/all-bodies acquisition claim. It is a separate prototype archive, not merged with v7, `data/output`, or the baseline.
- The initial October 3 snapshot v5 figures (5,729 facts / 164 sections and retryable/failed attempts) are historical and were superseded by the same archive's verified v7 rerun. Current first-five v7 evidence is 6,017 facts / 165 sections, with six full text rows, four full XBRL rows, one unsupported legacy-cover numeric parse, and a same-head exact repeat. The retained unsupported cover is not a numeric zero.
- The second-batch v15 active case matrix is: RBC 40-F IXDS group full/7,543 (40 primary, 7,503 EX2); Bank of America 10-K full/8,199; Microsoft 10-Q full/1,236; Wells Fargo 10-Q/A full/59; Nokia 20-F full/4,205; SAP 20-F full/4,414; CNR 2002 40-F/A text full with numeric unsupported; Microsoft 2008 10-K text full (80 sections) with numeric unsupported; CNR 6-K text full (four sections) with numeric unsupported; Shopify 6-K remains candidate/partial with full text and unsupported numeric parsing. These per-source results and the group provenance are in `/tmp/opencode/filings_second_batch_final_rerun/report.json`.
- The RBC group is persisted as one default-target parse anchored to its original primary. Its two old solo numeric parse IDs were retired; the secondary member has no synthetic numeric parse row. Physical facts bind through exact `source_uri`, `document_hash`, and `provenance.source_document_id`. The group parse does not promote the RBC raw-coverage state from `partial`. Both omitted-argument resume and the same explicit group declaration were exact v15 no-ops with zero CAS callbacks.
- CNR 2002 40-F/A's plain-text payload span is bytes `[70, 136699)` (136,629 bytes): TEXT 1.0.3 is full after the documented boundary-whitespace trim; XBRL is unsupported with zero facts. Its raw coverage remains partial. Shopify's temporary body evidence identifies an EX-99.1 CEO automatic-securities-disposition-plan announcement, not company results, but the audited scope-review API is deferred; its archive filing therefore remains candidate/partial and is not automatically excluded. See `/tmp/opencode/filings_second_batch_body_evidence/report.json`.
- AAPL 10-Q transforms use pinned Arelle 2.46.0 plus the two locally packaged official Arelle/EDGAR SEC inline-transform implementation files at commit `47a372d099168f8669d20a8a6bbe5cb16bbf71ac`; source/license hashes were verified, and source is not fetched at parse time. This does not enable HTTP-loaded plugin code or arbitrary user-supplied plugin paths, and it is not whole EFM validation.
- Final source gate: 787 passed, 2 skipped, with Ruff checks; the only subsequent source-tree delta was formatting `tests/test_filing_inline_document_set.py`, whose five targeted tests passed. Reports/logs: `/tmp/opencode/filings_second_batch_ixds_final_validation/`, `/tmp/opencode/filings_first_five_rerun_report.json`, `/tmp/opencode/filings_second_batch_final_rerun/report.json`, and `/tmp/opencode/filings_second_batch_final_rerun/run.log`. The second-batch active parse/report was verified after publication; its execution driver's report serialization hit a local variable-name error after the parse and repeats, so the report was reconstructed from the committed snapshot plus guarded repeat checks. The archive v15 source gate remains validated and no real-source bytes were rewritten. Runtime dependencies remain pinned in `pyproject.toml`/`uv.lock`; this docs-only sync makes no code change or commit.
- The daily release is separate and is committed at `data/output` as v3; its previous v1 tree remains at `data/output-v1-backup-20261003T172214933236Z`, with the frozen baseline preserved independently. Publication evidence: `/tmp/opencode/v3_release_publication/report.json` and `data/.output-publication-20261003T172214933236Z.json`. This archive pilot did not rebuild v3, add finance-model columns, or train an encoder, and was not merged into the daily bundle. No SEC-scale coverage, economic correctness, future filing coverage, or historical ticker/CIK PIT guarantee is claimed.
- No financial features are added to samples_v3; no training, historical ticker/CIK reconstruction, or encoder task is part of this workflow. Current archive CIK/accession identity is not a historical ticker map.
- No embeddings are stored or generated now. If a later task proposes embeddings, keep them in a separate derived store/table keyed at minimum by filing identity, encoder/model identifier and version, input document/text hash, and preprocessing/chunking version. They must be optional downstream artifacts, not embedded in raw source records or daily sample columns.
- Full-attachment policy, extraction completeness measures, retention policy, and financial acceptance criteria remain future decisions. Downloaded source bytes or parser `full` status do not prove financial-statement correctness.

## 8. Related contracts

- User commands, safe paths, and status limits: [filings.md](../user/filings.md).
- Active finance-free sample output: [samples.md](samples.md), [data-contracts.md](data-contracts.md).
- Existing SEC Company Facts/submissions and selected structured organizer behavior: [download.md](download.md).
- Separate non-XBRL extraction work and its current offline-only boundary: [financial-extraction.md](financial-extraction.md).
- Consumer schema and safe candidate build: [../user/data-format.md](../user/data-format.md), [../user/cli.md](../user/cli.md).
