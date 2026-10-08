**English** | [简体中文](filings.zh-CN.md)

# SEC filing archive guide

This is a separate, bounded workflow for cataloging cached SEC submissions, acquiring selected filing documents into an explicit archive, parsing selected documents, verifying the archive, and querying tables that actually exist. It does not change the existing `download --stage financials` cache, legacy organized financial outputs, or the finance-free `samples` builder. The preserved daily `data/output/` artifact still has its historical `samples_v3` manifest label; it is not the current builder contract.

Non-XBRL financial extraction has an explicit non-publishing `filings extract-financials` command for a frozen list of eligible archived HTML documents. It uses bounded workers and checked window-local short refs; it does not replace the numeric parser or change archive scope. See the [CLI contract](cli.md#financial-extraction) and [developer guide](../developer/financial-extraction.md). Strict citation/schema checks are not independent financial accuracy or numerical coverage.

**Status (2026-10-04):** the bounded `catalog`, `download`, `parse`, `verify`, and `query` prototype is available. Two independent private archives have been exercised; they remain separate from one another and from the current `samples` product.

- The first-five archive is at manifest v7: 6,017 facts and 165 text sections. Its active parser rows include 6 full text parses, 4 full XBRL parses, and 1 unsupported legacy-cover XBRL parse. An exact repeat retained the same head. Evidence: `/tmp/opencode/filings_first_five_rerun_report.json`.
- The separate second archive is at manifest v15: 10 selected filings across 8 CIKs, 3,051 catalog filing rows and 1,253 document-registry rows (neither count means that every filing body was acquired), 25 active parser rows, 25,656 facts, and 95 text sections. Active statuses are 15 full text, 6 full XBRL, and 4 unsupported XBRL. Its RBC 40-F group has one primary-anchored parse with 7,543 original occurrences (40 primary, 7,503 EX2); the two prior solo parse IDs were retired. Omitting the group argument on resume restored the saved group, and both omitted-argument and explicit-group repeats were no-ops. Evidence: `/tmp/opencode/filings_second_batch_final_rerun/report.json`.

A full suite of 787 passed and 2 skipped is recorded at `/tmp/opencode/filings_second_batch_ixds_final_validation/`; only a formatter-only change to one parser test followed, and its 5 targeted tests passed. These finite parser outcomes do not establish broad issuer coverage, complete attachments, or financial correctness. In particular, RBC remains `partial` in raw coverage despite its full numeric group parse; the CNR 2002 40-F/A remains `partial` despite full TEXT 1.0.3 extraction and unsupported numeric parsing; Shopify 6-K remains `candidate`/`partial`. The Shopify body inspection is evidence only and was not persisted as an automatic exclusion rule. The daily `data/output/` v3 bundle remains separate, with the v1 output and frozen baseline preserved independently; release evidence is in `/tmp/opencode/v3_release_publication/report.json`.

## Install and choose a safe archive path

Catalog, download, and verify use the regular project installation. Parsing needs the optional `filings` extra (Arelle 2.46.0); archive SQL queries need the optional `query` extra (DuckDB). The ordinary pipeline install remains unchanged.

Choose one install command for the tools you need (the extra-specific commands are alternatives):

```bash
uv sync --frozen
# If you will parse filing documents:
uv sync --frozen --extra filings
# If you will query archive tables or sample bundles:
uv sync --frozen --extra query
# If you need both optional tools:
uv sync --frozen --extra filings --extra query
```

Choose a new explicit run root outside protected source/output locations. For example, `data/filings/pilot` is separate from the existing `data/raw/sec/financials` cache. Do not put the archive at, inside, or above `data/raw`, `data/organized`, `data/output`, `data/baselines`, or the protected v3 candidate path. The cache root itself is protected too. Never reuse `data/output` or a historical candidate as an archive.

```bash
ARCHIVE=data/filings/pilot
CACHE=data/raw/sec/financials
```

The catalog command reads only files already listed in the cache manifest; it makes no SEC request. The cache root must contain its existing `manifest.json` and the referenced verified resources. If the cache itself needs population, the separate existing `download --stage financials` workflow can fetch its Company Facts/submissions metadata; catalog never invokes it implicitly, and that stage does not fetch the filing documents used by the archive. If history pages are missing, catalog defaults to failing; `--allow-partial` is an explicit, visible choice, not a completeness fix.

## 1. Catalog cached submissions

```bash
quant-dataset filings catalog \
  --archive "$ARCHIVE" \
  --cache-root "$CACHE" \
  --cik 320193 \
  --start 2024-01-01 \
  --end 2024-12-31 \
  --form 10-K
```

- `--archive`, `--cache-root`, at least one `--cik`, and inclusive `--start`/`--end` are required. Repeat `--cik` or `--form` to add selections.
- Forms are an approved SEC subset. The command uses locally cached submissions and historical page resources only; it does not fetch missing catalog inputs.
- Visibility is mapped to the first XNYS session strictly after `filed_date`. It is not replaced with acceptance time or download time.
- `--resume` is only for the same persisted RunSpec. If CIK/date/form scope changes, choose a new run root; there is no force-overwrite scope switch. `--allow-partial` permits explicitly reported missing historical pages; it does not turn partial input into complete coverage.
- The archive copies verified source submissions into its own content-addressed raw storage. It does not modify the input cache.

The initial catalog describes filing identity and metadata. It does not yet contain an attachment inventory or claim that filing bytes are present.

## 2. Download a bounded set of filing documents

`filings download` only works on an existing, published catalog archive. It does not create a new archive or select a company-wide historical universe. By default it attempts at most five pending eligible filings; `--max-filings` accepts 1–50. Use repeated `--filing-id` values to target exact `cik10:accession_number` identities already in the archive. Unknown, excluded, duplicate, or over-limit identities are rejected before any fetch.

The SEC client needs a non-placeholder contact in `config/secrets.toml`:

```toml
[secrets]
sec_user_agent = "<replace with project name and real contact email>"
```

Replace the example with an approved, real contact value before running; the placeholder is intentionally invalid. The archive command reads only `sec_user_agent`; it does **not** require the unrelated FRED key. Use `--secrets PATH` for a different TOML file. Credentials and User-Agent values are not included in reports.

```bash
quant-dataset filings download --archive "$ARCHIVE" --max-filings 5
# Or, after obtaining an exact filing_id from a query:
quant-dataset filings download --archive "$ARCHIVE" --filing-id "$FILING_ID"
```

This is a bounded SEC-only operation with the client's SEC host policy; there is no arbitrary URL option. The selected scope retains inventory/index evidence and fetches selected primary and necessary XBRL resources, not every attachment or a website mirror. Existing raw submissions/cache files are not rewritten. The output reports processed IDs, selection status, acquired/unavailable/error counts, blocked IDs, and the published manifest version. Blocked, unavailable, or error outcomes return a nonzero status even though a partial-result summary may be printed. A `needs_review` selection by itself is informational and is not a success claim.

6-K filings remain candidates; inventory evidence may identify a financial subset for bounded acquisition, but that does not establish complete foreign-issuer coverage or change every 6-K into an included filing. Attachment selection is conservative metadata-based triage, not comprehensive content classification: an unrecognized exhibit can remain a candidate, and a press-release label alone is not proof that a filing is non-financial. The command does not fetch all attachments.

### Raw pilot result

The first-five acquisition pilot used Apple 2010 10-K/A, Apple 2024 10-Q/10-K, and TSMC 2024 20-F/6-K. The catalog has 823 filing-metadata rows—**not** 823 downloaded reports. Acquisition fetched 26 selected original-document payloads plus 10 inventory/index metadata sources; 423 document-registry rows are **not** 423 raw payloads. In the v7 rerun all five selected filing scopes reached `scoped_complete`. The TSMC 6-K was included after checking the actual detail index and its EX-99.1 financial-statement exhibit; only selected documents are covered, not all accession attachments. These acquisition states do not imply comprehensive filing or financial coverage.

### Real parser pilots (2026-10-04)

The five-filing archive is persisted at v7 with 6,017 facts and 165 text sections. Its active parser rows are six full text, four full XBRL, and one unsupported legacy-cover numeric parse; the exact repeat retained the same head. The unsupported result means no numeric occurrences were accepted from that non-XBRL source, not that it reports zero financial values. Evidence: `/tmp/opencode/filings_first_five_rerun_report.json`.

A separate ten-filing batch (eight selected CIKs) is persisted in `/tmp/opencode/filings-pilot-diverse-v1` at v15. It has 25 active parse rows: 15 full text, six full XBRL, and four unsupported XBRL; together they contain 25,656 facts and 95 text sections. The 3,051 catalog filings and 1,253 document-registry rows are metadata inventories, not all downloaded filing bodies. Case details are in `/tmp/opencode/filings_second_batch_final_rerun/report.json`.

| Case | XBRL result | Text result | Remaining scope limit |
| --- | --- | --- | --- |
| RBC 2023 40-F | full / 7,543 occurrences | three full text sections | One primary-anchored IXDS group retains 40 primary and 7,503 EX2 source occurrences; raw coverage remains `partial`. The two prior solo numeric parse IDs were retired. |
| CNR 2002 40-F/A | unsupported / 0 | full / 1 section | Plain SGML TEXT payload span is bytes `[70, 136699)` (136,629 bytes); raw coverage remains `partial`. |
| Bank of America 2023 10-K | full / 8,199 | full / 1 | Structural/source validation only. |
| Microsoft 2024 10-Q | full / 1,236 | full / 1 | Structural/source validation only. |
| Wells Fargo 2023 10-Q/A | full / 59 | full / 1 | The amendment remains its own filing identity. |
| Microsoft 2008 10-K | unsupported / 0 | full / 80 | Original HTML remains available; unsupported is not an economic zero. |
| Nokia 2023 20-F | full / 4,205 | full / 1 | Structural/source validation only. |
| SAP 2023 20-F | full / 4,414 | full / 1 | Structural/source validation only. |
| CNR 2024 6-K | unsupported / 0 | full / 4 | Selected-document scope is `scoped_complete`; this does not cover every attachment. |
| Shopify 2024 6-K | unsupported / 0 | full / 1 | Still `candidate`/`partial`. Temporary EX-99.1 inspection found a CEO automatic securities disposition plan, but no scope-review API or persisted automatic exclusion was added. |

Inline XBRL grouping is a prototype Python API, not a CLI flag. A declared or saved group is resumed as a default-target, source-bound document set; omission does not silently revert members to solo parsing. Fact `document_id` is the primary invocation anchor, while `source_uri`, `document_hash`, and `provenance.source_document_id` identify the physical source. A grouped parser `full` status does not promote raw coverage or establish financial correctness.

The second-batch v15 result was audited after commit because the execution driver's report serialization hit a local variable-name error after parsing and repeat checks; the report was reconstructed from the committed snapshot. Both omitted-argument and same-explicit-group repeats kept the v15 head and wrote no parser/dependency rows. The final source gate was 787 passed and 2 skipped; only `tests/test_filing_inline_document_set.py` was formatted afterward and its five targeted tests passed. Evidence and execution logs are under `/tmp/opencode/filings_second_batch_ixds_final_validation/` and `/tmp/opencode/filings_second_batch_final_rerun/`.

The two pilot archives remain separate and independent of `data/output`. Parser/occurrence counts describe these selected source scopes only, not all SEC filings, all accession attachments, an issuer universe, or economically correct statements.

## 3. Parse selected archived documents

`filings parse` requires an existing archive and a separate, existing workspace directory. It processes at most five ready filings by default; `--max-filings` accepts 1–50 and repeated `--filing-id` values select exact identities already in the archive. It never creates an archive, expands the catalog scope, or downloads filing documents. Each selected package is copied and hash-checked in the workspace; do not place that workspace inside or above protected source/output/cache/baseline/candidate paths.

The default is **offline-only**: no secrets file is read and no dependency callback is provided. Local missing-taxonomy references can therefore produce a failed/partial parse attempt rather than zero facts. If a taxonomy resource is needed, explicitly enable the bounded preparation mode:

```bash
ARCHIVE=/path/to/existing/catalog-run
WORKSPACE=$(mktemp -d /tmp/opencode/filings-workspace.XXXXXX)
FILING_ID=0000320193:0000320193-24-000081  # Example only; use an ID from your own archive.

# Offline first: never fetches taxonomy resources implicitly.
quant-dataset filings parse --archive "$ARCHIVE" --workspace-root "$WORKSPACE" \
  --filing-id "$FILING_ID" --max-filings 1

# Explicitly permits bounded taxonomy-resource fetching before Arelle's offline parse.
quant-dataset filings parse --archive "$ARCHIVE" --workspace-root "$WORKSPACE" \
  --filing-id "$FILING_ID" --max-filings 1 --prepare-dependencies
```

`--prepare-dependencies` is the only mode that reads `[secrets].sec_user_agent` (default `config/secrets.toml`, overridable with `--secrets`) and constructs the taxonomy client. No FRED key is required. The built-in exact-host allowlist is `www.sec.gov`, `data.sec.gov`, `xbrl.sec.gov`, `xbrl.fasb.org`, `www.xbrl.org`, `xbrl.ifrs.org`, and `www.w3.org`; repeated `--taxonomy-host` values can **narrow** that seven-host set, not add arbitrary hosts. Host/secrets flags without `--prepare-dependencies` are rejected. Approved HTTP-origin taxonomy URLs are transported over HTTPS with original and transport URLs recorded; an unresolved or out-of-allowlist dependency fails safely rather than enabling Arelle network access. Preparation is paced at a minimum 0.2 seconds between SEC requests and bounded to 500 resources / 128 MiB per filing in the current helper; Arelle always runs offline.

The JSON summary reports the snapshot version, selected/processed/skipped IDs, parser status counts, fact/section rows, dependency records, and bounded diagnostic codes. Partial or failed parse attempts return nonzero. Unsupported-only output is labeled `completed_with_unsupported`, not `completed`; PDF facts/text are not synthesized. Full extraction means only that the parser's structural/source checks passed, not SEC/EFM compliance, financial correctness, or complete reporting coverage. XBRL facts preserve source lexical values, context/unit/taxonomy/dimension provenance and exact string-valued numeric representations. Missing or invalid numbers remain null/invalid with diagnostics, never synthetic zeroes. Text extraction is best effort: legacy readable HTML may yield text without XBRL facts, PDF is retained but unsupported, and section headings are heuristic anchors rather than byte offsets. No feature encoding, ratios, training, or daily-sample joins are performed.

### Inline XBRL document sets (prototype Python API)

When present, required original inline documents in one filing may be processed as one default-target document set only when typed, source-bound cross-file references connect them. Independent reports are not combined merely because namespaces or IDs overlap. The Python processing API accepts exact archive `document_id` values:

```python
parse_archive(
    archive,
    protected_paths=protected_paths,
    workspace_root=workspace,
    filing_ids=(filing_id,),
    inline_document_sets={filing_id: (primary_document_id, exhibit_document_id)},
)
```

The primary is the anchor, followed by remaining members in original source-URL order. There is no new CLI flag. A persisted, validated group plan is restored when `inline_document_sets` is omitted; omission does not revert the group to solo parses. One numeric parse row is anchored to the primary. For each fact, `facts.document_id` identifies that invocation anchor, while `source_uri`, `document_hash`, and `provenance.source_document_id` identify the physical member that supplied the occurrence. Join physical ownership by filing, exact source URI, and raw hash, and verify group membership. Named/multiple targets and ambiguous ownership are unsupported. A full grouped parse describes extracted occurrences only; it does not upgrade the filing's raw-coverage status.

## 4. Verify an archive

```bash
quant-dataset filings verify --archive "$ARCHIVE"
```

Verification checks the published manifest and its explicitly listed raw and Parquet files, hashes/sizes, schemas, and core filing/document row consistency. Its report is an archive-integrity result, **not** evidence that all expected filings or financial facts are present, that every filing is financially complete, or that financial statements are correct. Partial inventory, unavailable documents, candidate scope, and review states remain meaningful outcomes.

## 5. Query available archive tables

Install the optional `query` extra first. Queries return CSV on stdout and query metadata (including archive format and manifest version) on stderr. Catalog/download/parse/verify return JSON summaries with the published manifest version. `--limit` defaults to 20 and accepts 1–1,000 output rows.

Set `AS_OF_DATE` to the date you want to inspect (replace the placeholder with a real `YYYY-MM-DD` value):

```bash
AS_OF_DATE='YYYY-MM-DD'
quant-dataset filings query --archive "$ARCHIVE" \
  --sql "SELECT filing_id, form, filed_date, effective_visible_session, scope_status, inventory_status, raw_coverage_status FROM filings WHERE effective_visible_session <= CAST('$AS_OF_DATE' AS DATE) ORDER BY filed_date, filing_id" \
  --limit 20
```

To inspect the recorded document inventory:

```bash
quant-dataset filings query --archive "$ARCHIVE" \
  --sql "SELECT f.filing_id, d.role, d.original_filename, d.selection_status, d.fetch_status FROM filings AS f LEFT JOIN documents AS d USING (filing_id) WHERE f.effective_visible_session <= CAST('$AS_OF_DATE' AS DATE) ORDER BY f.filed_date, d.original_filename"
```

Only tables actually listed in the current manifest are registered as views: `filings`, `documents`, `facts`, `sections`, `parses`, and `dependencies`. No missing table is replaced by an empty placeholder. Catalog/download create or update `filings` and `documents`; parse creates or updates parser outputs and its parse/dependency attempt records. For example, inspect bounded parse statuses and diagnostics:

```bash
quant-dataset filings query --archive "$ARCHIVE" \
  --sql "SELECT filing_id, document_id, parser_name, status, validation_scope, errors_json FROM parses ORDER BY filing_id, document_id, parser_name" \
  --limit 50
```

Dependency attempts retain original/final/transport URI provenance and failure codes; inspect them without downloading anything:

```bash
quant-dataset filings query --archive "$ARCHIVE" \
  --sql "SELECT filing_id, requested_url, final_url, transport_url, status, diagnostic_code FROM dependencies ORDER BY filing_id, requested_url" \
  --limit 50
```

When `facts` exists, query actual source occurrences and string-valued numbers rather than assuming every filing has a value:

```bash
quant-dataset filings query --archive "$ARCHIVE" \
  --sql "SELECT filing_id, fact_qname, raw_value, normalized_numeric, context_id, context_json, unit_id, unit_json FROM facts ORDER BY filing_id, object_index" \
  --limit 50
```

When `sections` exists, inspect the extracted text directly; it is heuristic normalized text, not exact byte offsets:

```bash
quant-dataset filings query --archive "$ARCHIVE" \
  --sql "SELECT filing_id, section_kind, heading, content_text, source_xpath, source_ordinal FROM sections ORDER BY filing_id, source_ordinal" \
  --limit 20
```

`normalized_numeric` is nullable and string-valued; a null/invalid parse is not a zero. Context and unit provenance preserve period and dimension details rather than guaranteeing a single financial number per filing. The current ticker map is not a complete historical CIK/ticker point-in-time map, and these archive tables are not joined to daily v3 labels/features.

The query path streams file hashes and checks manifest lineage plus Parquet footer schemas/row counts; it does not materialize every table as an Arrow table. This is descriptor-level verification, **not** the full row-acceptance performed by `verify`. SQL must be one trusted local SELECT/CTE; DDL, DML, multiple statements, and PRAGMA are rejected. DuckDB external access is disabled and the query reads only approved archive table files. This is local inspection, not a public SQL sandbox. `--limit` bounds returned rows, not the work an aggregate or sort may do.

There is no automatic “latest filing” or point-in-time decision. Amendments remain separate filing IDs; queries do not merge an amendment into its original or pick a latest value. For an as-of question, set your own date and write an explicit predicate on `effective_visible_session`, for example `effective_visible_session <= CAST('$AS_OF_DATE' AS DATE)`. This field follows the conservative first-XNYS-session-strictly-after-filed-date rule; it is not a guarantee that every source was processed by that date. Use CIK/accession as filing identity. Current ticker mappings are not a complete historical ticker/CIK point-in-time map.

## What remains incomplete

The parser does not create financial ratios, encoder features, training inputs, or joins to daily sample labels. HTML text sections are heuristic and are not byte offsets. PDFs remain archived source bytes with an explicit unsupported status; no text/numeric values are invented. XBRL parser `full` means its structural/source checks passed, not SEC/EFM compliance or financial-statement correctness. For malformed/invalid facts, the lexical source can remain available while numeric interpretation is null/invalid; it must not be treated as zero.

This remains a bounded parser prototype, not broad issuer coverage. In particular, the 2010 AAPL XBRL failure is unresolved without review and approval of its out-of-allowlist dependency; do not widen the host list implicitly. Exact rerun/idempotence has not been tested while retryable attempts remain. Historical ticker/CIK point-in-time mapping, future filings, economic/financial correctness, and full-company or full-attachment coverage are not claimed. The preserved `data/output/` artifact is a historical `samples_v3` publication, separate from this private archive; the pilot did not modify or merge into it. No finance-model columns, sample bundle rebuild, or encoder training were added by this archive pilot.
