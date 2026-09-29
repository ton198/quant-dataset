"""As-of SEC filing organization helpers used by the main organizer."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from .errors import OrganizeError
from .universe import apply_ticker_cik_mapping_overrides


# Ordered from the standard/current presentation to narrower or legacy tags.
# The first tag with a fact for the reported filing period wins.
_CONCEPTS: dict[str, tuple[str, ...]] = {
    "revenue": (
        "Revenues",
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "SalesRevenueNet",
        "SalesRevenueGoodsNet",
        "SalesRevenueServicesNet",
        "SalesRevenueGoodsGross",
        "SalesRevenueServicesGross",
        "RevenuesNetOfInterestExpense",
        "FinancialServicesRevenue",
        "InsuranceServicesRevenue",
        "RevenueNotFromContractWithCustomer",
        "SalesRevenueOilGas",
    ),
    "gross_profit": ("GrossProfit",),
    "operating_income": ("OperatingIncomeLoss",),
    "net_income": (
        "NetIncomeLoss",
        "ProfitLoss",
        "NetIncomeLossAvailableToCommonStockholdersBasic",
        "NetIncomeLossAvailableToCommonStockholdersDiluted",
    ),
    "operating_cash_flow": ("NetCashProvidedByUsedInOperatingActivities",),
    "capital_expenditure": ("PaymentsToAcquirePropertyPlantAndEquipment",),
    "assets": ("Assets", "AssetsNet"),
    "liabilities": ("Liabilities",),
    "equity": ("StockholdersEquity",),
}

_TICKER_CACHE: dict[Path, tuple[dict[str, str], dict[str, str]]] = {}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _reset_ticker_cache(raw_dir: Path | None = None) -> None:
    """Clear the cached universe mappings, optionally for one raw data directory."""
    if raw_dir is None:
        _TICKER_CACHE.clear()
    else:
        _TICKER_CACHE.pop(raw_dir.resolve(), None)


def _ticker_mappings(raw_dir: Path) -> tuple[dict[str, str], dict[str, str]]:
    cache_key = raw_dir.resolve()
    cached = _TICKER_CACHE.get(cache_key)
    if cached is not None:
        return cached

    ticker_to_cik: dict[str, str] = {}
    cik_to_ticker: dict[str, str] = {}
    directory = raw_dir / "sec" / "universe"
    for path in sorted(directory.glob("*.json")) if directory.exists() else []:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            fields = {str(name).casefold(): index for index, name in enumerate(payload.get("fields", []))}
            cik_index, ticker_index = fields.get("cik", 0), fields.get("ticker", 2)
            for row in payload.get("data", []):
                cik10 = str(row[cik_index]).zfill(10)
                ticker = str(row[ticker_index]).upper() if row[ticker_index] else ""
                if ticker:
                    ticker_to_cik.setdefault(ticker, cik10)
                    cik_to_ticker.setdefault(cik10, ticker)
        except (OSError, ValueError, KeyError, IndexError, TypeError):
            continue

    apply_ticker_cik_mapping_overrides(ticker_to_cik, cik_to_ticker)
    mappings = (ticker_to_cik, cik_to_ticker)
    _TICKER_CACHE[cache_key] = mappings
    return mappings


def _ticker_for_cik(raw_dir: Path, cik10: str) -> str | None:
    return _ticker_mappings(raw_dir)[1].get(cik10)


def _build_fact_index(
    fact_payloads: list[dict[str, Any]],
) -> tuple[
    dict[str, dict[tuple[str, str], list[dict[str, Any]]]],
    dict[tuple[str, str], list[dict[str, Any]]],
]:
    """Index Company Facts by ``(filed, end)`` for O(1) filing lookups.

    Historical submission pages multiply the number of filings processed per
    ticker, so scanning every fact of every tag per filing is no longer viable.
    Two indexes are built in the original iteration order (payload order, then
    sorted tag, then sorted unit, then array order): ``by_tag`` buckets facts by
    ``(filed, end)`` per tag for ``_fact_for_period``; ``by_filing`` keeps every
    fact carrying ``fy``/``fp`` for the fiscal-identifier fallback.
    """
    by_tag: dict[str, dict[tuple[str, str], list[dict[str, Any]]]] = {}
    by_filing: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for payload in fact_payloads:
        us_gaap = payload.get("facts", {}).get("us-gaap", {})
        if not isinstance(us_gaap, dict):
            continue
        for tag in sorted(us_gaap):
            units = us_gaap[tag].get("units", {})
            if not isinstance(units, dict):
                continue
            for unit in sorted(units):
                values = units[unit]
                if not isinstance(values, list):
                    continue
                for item in values:
                    if not isinstance(item, dict):
                        continue
                    filed = str(item.get("filed", ""))
                    end = str(item.get("end", ""))
                    if not filed or not end:
                        continue
                    by_tag.setdefault(tag, {}).setdefault((filed, end), []).append(item)
                    if item.get("fy") is not None and item.get("fp"):
                        by_filing.setdefault((filed, end), []).append(item)
    return by_tag, by_filing


def _fact_for_period(
    fact_index: dict[str, dict[tuple[str, str], list[dict[str, Any]]]],
    tag: str, filed: str, fy: Any, fp: Any, end: str,
    form: str = "", accession_number: str = "",
) -> dict[str, Any] | None:
    """Return the fact best representing this filing's reported period.

    SEC Company Facts can contain both quarter-to-date and year-to-date values
    for one 10-Q filing. Prefer a standalone quarter for 10-Qs and a roughly
    annual duration for 10-Ks; instant facts such as assets are matched by end.
    """
    filing_form = form.removesuffix("/A")
    matches: list[dict[str, Any]] = []
    for item in fact_index.get(tag, {}).get((filed, end), []):
        if fy is not None and str(item.get("fy", "")) != str(fy):
            continue
        if fp is not None and str(item.get("fp", "")) != str(fp):
            continue
        item_form = str(item.get("form", "")).removesuffix("/A")
        if filing_form and item_form and item_form != filing_form:
            continue
        item_accession = str(item.get("accn", ""))
        if accession_number and item_accession and item_accession != accession_number:
            continue
        try:
            float(item["val"])
        except (KeyError, TypeError, ValueError):
            continue
        start = str(item.get("start", ""))
        try:
            duration = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
        except ValueError:
            duration = 0
        # Do not label a year-to-date fact as a quarterly value (or a
        # standalone quarter as an annual value) merely because both facts
        # share the same filing date and end date.
        if filing_form == "10-Q" and duration and not 70 <= duration <= 125:
            continue
        if filing_form == "10-K" and duration and not 300 <= duration <= 400:
            continue
        matches.append(item)
    if not matches:
        return None

    def preference(item: dict[str, Any]) -> tuple[int, int, str]:
        start = str(item.get("start", ""))
        try:
            duration = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
        except ValueError:
            duration = 0
        if filing_form == "10-Q" and duration:
            in_quarter_range = 70 <= duration <= 125
            return (0 if in_quarter_range else 1,
                    abs(duration - 91) if in_quarter_range else duration, start)
        if filing_form == "10-K" and duration:
            annual_range = 300 <= duration <= 400
            return (0 if annual_range else 1,
                    abs(duration - 365) if annual_range else -duration, start)
        return (0, 0, start)

    return min(matches, key=preference)


def _fiscal_identifiers(
    filing: dict[str, Any], fiscal_index: dict[tuple[str, str], list[dict[str, Any]]],
    reported_facts: list[dict[str, Any]], all_filings: list[dict[str, Any]],
) -> tuple[int | None, str | None]:
    """Get fiscal year/period from a fact covering the report, then SEC metadata.

    Company Facts ``fy``/``fp`` describe the fact's reported period (including
    quarterly filings after a non-calendar fiscal year end), so they take
    precedence over filing context. Form type/report date provide a documented
    fallback for older or incomplete payloads.
    """
    for fact in reported_facts:
        if fact.get("fy") is not None and fact.get("fp"):
            try:
                return int(fact["fy"]), str(fact["fp"])
            except (TypeError, ValueError):
                continue

    filed = str(filing.get("filingDate", ""))
    end = str(filing.get("reportDate", ""))
    accession_number = str(filing.get("accessionNumber", ""))
    for fact in fiscal_index.get((filed, end), []):
        if accession_number and fact.get("accn") and str(fact["accn"]) != accession_number:
            continue
        try:
            return int(fact["fy"]), str(fact["fp"])
        except (TypeError, ValueError):
            continue

    filing_fy, filing_fp = filing.get("fy"), filing.get("fp")
    if filing_fy is not None and filing_fp:
        try:
            return int(filing_fy), str(filing_fp)
        except (TypeError, ValueError):
            pass

    form = str(filing.get("form", "")).removesuffix("/A")
    if not end:
        return None, None
    try:
        report_end = date.fromisoformat(end)
    except ValueError:
        return None, None

    if form == "10-K":
        return report_end.year, "FY"
    if form != "10-Q":
        return None, None

    # If facts lack fy/fp, use the closest earlier annual report as an anchor;
    # count distinct quarterly report ends since it rather than using calendar
    # quarters, which would mislabel issuers such as Apple.
    annual_ends: list[tuple[date, int]] = []
    for prior_filing in all_filings:
        if str(prior_filing.get("form", "")).removesuffix("/A") != "10-K":
            continue
        try:
            annual_end = date.fromisoformat(str(prior_filing.get("reportDate", "")))
        except ValueError:
            continue
        if annual_end < report_end:
            prior_year = prior_filing.get("fy")
            try:
                fiscal_year = int(prior_year) if prior_year is not None else annual_end.year
            except (TypeError, ValueError):
                fiscal_year = annual_end.year
            annual_ends.append((annual_end, fiscal_year))
    if annual_ends:
        annual_end, annual_year = max(annual_ends)
        quarter_ends = {
            str(item.get("reportDate", "")) for item in all_filings
            if str(item.get("form", "")).removesuffix("/A") == "10-Q"
            and annual_end < report_end
            and str(item.get("reportDate", "")) <= end
            and str(item.get("reportDate", "")) > annual_end.isoformat()
        }
        try:
            quarter_number = sorted(quarter_ends).index(end) + 1
        except ValueError:
            quarter_number = 0
        if 1 <= quarter_number <= 3:
            return annual_year + 1, f"Q{quarter_number}"

    # A quarter's exact fiscal slot cannot be inferred from its calendar date
    # alone; leave it null rather than mislabeling non-calendar fiscal years.
    return None, None


def _column_oriented_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Expand a column-oriented submissions page into one dict per filing.

    Historical ``submissions-page`` payloads store one parallel array per field
    (``accessionNumber``, ``filingDate``, ``reportDate``, ``form``, ...). Each
    row is the element-wise zip of those arrays; ragged payloads are rejected so
    misaligned arrays can never fabricate or shift filing dates.
    """
    columns = {str(key): values for key, values in payload.items() if isinstance(values, list)}
    accessions = columns.get("accessionNumber")
    filing_dates = columns.get("filingDate")
    if not isinstance(accessions, list) or not isinstance(filing_dates, list):
        return []
    length = len(accessions)
    if length == 0 or len(filing_dates) != length:
        return []
    if any(len(values) != length for values in columns.values()):
        return []
    return [{key: values[index] for key, values in columns.items()} for index in range(length)]


def _is_column_oriented(payload: dict[str, Any]) -> bool:
    """Cheap shape probe for column-oriented submission page payloads."""
    accessions, filing_dates = payload.get("accessionNumber"), payload.get("filingDate")
    return (
        isinstance(accessions, list) and isinstance(filing_dates, list)
        and len(accessions) > 0 and len(accessions) == len(filing_dates)
    )


def _submission_rank(payload: dict[str, Any]) -> int:
    """Dedup priority: the main ``filings.recent`` block wins over older pages."""
    filings = payload.get("filings")
    if isinstance(filings, dict) and isinstance(filings.get("recent"), dict):
        return 0
    if isinstance(payload.get("fields"), list) or isinstance(payload.get("data"), list):
        return 1
    return 2


def _submission_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Return filing rows from any supported SEC submissions payload shape.

    Three shapes exist in the raw store and are assembled here: the main
    submissions payload (``filings.recent`` parallel arrays), the generic
    ``fields``/``data`` table, and column-oriented historical
    ``submissions-page`` payloads. Overlapping accessions are deduplicated by
    the caller in ``_submission_rank`` order.
    """
    filings: list[dict[str, Any]] = []
    recent = payload.get("filings", {}).get("recent", {})
    if isinstance(recent, dict):
        accessions = recent.get("accessionNumber", [])
        for index in range(len(accessions) if isinstance(accessions, list) else 0):
            filings.append({key: values[index] for key, values in recent.items()
                            if isinstance(values, list) and len(values) > index})
    fields, rows = payload.get("fields", []), payload.get("data", [])
    if isinstance(fields, list) and isinstance(rows, list):
        for row in rows:
            if isinstance(row, list):
                filings.append({str(key): row[index] for index, key in enumerate(fields) if index < len(row)})
    if not filings:
        filings.extend(_column_oriented_rows(payload))
    return filings


def _owned_paths(root: Path, cik10: str) -> list[Path]:
    try:
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        manifest = {}
    entries = manifest.get("resources", []) if isinstance(manifest, dict) else []
    if isinstance(entries, dict):
        entries = [{"logical_key": key, **record}
                   for key, versions in entries.items()
                   for record in (versions if isinstance(versions, list) else [versions])
                   if isinstance(record, dict)]
    paths = {
        str(entry.get("path", "")) for entry in entries
        if isinstance(entry, dict) and (
            str(entry.get("logical_key", "")).endswith(cik10)
            or f":{cik10}:" in str(entry.get("logical_key", ""))
        )
    }
    result = sorted(root / item for item in paths if item and (root / item).is_file())
    if not result and root.exists():
        result = sorted(
            path for path in root.glob("*.json")
            if path.name != "manifest.json" and cik10 in path.name
        )
    return result


def _write_meta(path: Path, ticker: str, inputs: list[dict[str, str]],
                output: Path, organized_dir: Path, rows: int, input_count: int) -> None:
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(meta, dict):
            meta = {}
    except (OSError, ValueError):
        meta = {}
    old_inputs = {
        item.get("path"): item for item in meta.get("inputs", [])
        if isinstance(item, dict) and (
            inputs or "sec/financials/" not in str(item.get("path", ""))
        )
    }
    old_inputs.update({item["path"]: item for item in inputs})
    output_record = {
        "path": str(output.relative_to(organized_dir.parent.parent)),
        "sha256": _sha256(output), "rows": rows,
    }
    old_outputs = {item.get("path"): item for item in meta.get("outputs", []) if isinstance(item, dict)}
    old_outputs[output_record["path"]] = output_record
    row_counts = meta.get("row_counts", {})
    if not isinstance(row_counts, dict):
        row_counts = {}
    row_counts.update({"financials_input": input_count, "financials_output": rows})
    meta.update({"ticker": ticker, "generated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                 "cleaning_rules_version": "v1", "inputs": list(old_inputs.values()),
                 "outputs": list(old_outputs.values()), "row_counts": row_counts,
                 "known_issues": meta.get("known_issues", [])})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def organize_financials(cik10: str, raw_dir: Path, organized_dir: Path,
                         calendar: list[date], output_ticker: str | None = None) -> Path:
    """As-of join SEC company facts and filing metadata to session dates.

    Concept tags are tried in the priority order listed in ``_CONCEPTS``;
    the first fact available for each reported filing period is retained. For
    annual revenue, net income, operating income, and assets the complete
    documented order is ``Revenues``, ``RevenueFromContractWithCustomerExcludingAssessedTax``,
    ``RevenueFromContractWithCustomerIncludingAssessedTax``, ``SalesRevenueNet``,
    ``SalesRevenueGoodsNet``, ``SalesRevenueServicesNet``, ``SalesRevenueGoodsGross``,
    ``SalesRevenueServicesGross``, ``RevenuesNetOfInterestExpense``,
    ``FinancialServicesRevenue``, ``InsuranceServicesRevenue``,
    ``RevenueNotFromContractWithCustomer``, ``SalesRevenueOilGas`` (revenue);
    ``NetIncomeLoss``, ``ProfitLoss``, ``NetIncomeLossAvailableToCommonStockholdersBasic``,
    ``NetIncomeLossAvailableToCommonStockholdersDiluted`` (net income);
    ``OperatingIncomeLoss``; and ``Assets``, ``AssetsNet``.

    ``fiscal_year`` and ``fiscal_period`` identify the period covered by each
    fact, preferring Company Facts ``fy``/``fp`` for the exact reported filing
    facts. When absent they fall back to submission fiscal metadata, then use
    10-K/report dates or count 10-Q ends from the prior annual report (so a
    September year-end issuer such as Apple is not assigned calendar quarters).

    ``output_ticker`` directs shared-CIK companies to an independent ticker
    output directory; omitted callers retain the legacy CIK-to-ticker lookup.
    """

    root = raw_dir / "sec" / "financials"
    paths = _owned_paths(root, cik10)
    fact_payloads: list[dict[str, Any]] = []
    submission_payloads: list[tuple[int, dict[str, Any]]] = []
    target_cik = str(int(cik10)) if cik10.isdigit() else cik10
    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        payload_cik = payload.get("cik")
        if payload_cik is not None and str(payload_cik).lstrip("0") != target_cik.lstrip("0"):
            continue
        if isinstance(payload.get("facts"), dict):
            fact_payloads.append(payload)
        elif "filings" in payload or "fields" in payload or _is_column_oriented(payload):
            submission_payloads.append((_submission_rank(payload), payload))

    # Historical submission pages overlap the main ``filings.recent`` block for
    # their newest accessions; dedupe by accession number, preferring the main
    # submissions payload and keeping a deterministic rank-then-path order.
    filings: list[dict[str, Any]] = []
    seen_accessions: set[str] = set()
    for _, payload in sorted(submission_payloads, key=lambda item: item[0]):
        for row in _submission_rows(payload):
            accession = str(row.get("accessionNumber") or "")
            if accession and accession in seen_accessions:
                continue
            if accession:
                seen_accessions.add(accession)
            filings.append(row)

    fact_index, fiscal_index = _build_fact_index(fact_payloads)
    report_rows: list[dict[str, Any]] = []
    for filing in filings:
        filed = str(filing.get("filingDate", ""))
        if not filed:
            continue
        end = str(filing.get("reportDate", ""))
        fy, fp = filing.get("fy"), filing.get("fp")
        form = str(filing.get("form", ""))
        accession_number = str(filing.get("accessionNumber", ""))
        concept_values: dict[str, float | None] = {}
        reported_facts: list[dict[str, Any]] = []
        for output, tags in _CONCEPTS.items():
            selected_fact = None
            for tag in tags:
                selected_fact = (
                    _fact_for_period(fact_index, tag, filed, fy, fp, end, form, accession_number)
                    if end else None
                )
                if selected_fact is not None:
                    break
            if selected_fact is not None:
                reported_facts.append(selected_fact)
                concept_values[output] = float(selected_fact["val"])
            else:
                concept_values[output] = None
        fiscal_year, fiscal_period = _fiscal_identifiers(
            filing, fiscal_index, reported_facts, filings,
        )
        report_rows.append({
            "filed": filed, "available_at": filed,
            "accession_number": accession_number,
            "form": form,
            "is_amendment": form.endswith("/A"),
            "fiscal_year": fiscal_year, "fiscal_period": fiscal_period,
            "report_period_end": end, **concept_values,
        })
    report_rows.sort(key=lambda item: (item["available_at"], item["accession_number"]))
    report_filed_dates = [date.fromisoformat(item["filed"]) for item in report_rows]
    output_rows: list[dict[str, Any]] = []
    fields = ("available_at", "accession_number", "form", "is_amendment", "fiscal_year", "fiscal_period",
              "report_period_end", *_CONCEPTS)
    visible_count = 0
    for session in sorted(calendar):
        while visible_count < len(report_rows) and report_filed_dates[visible_count] < session:
            visible_count += 1
        if visible_count:
            selected = report_rows[visible_count - 1]
            values: dict[str, Any] = {key: selected.get(key) for key in fields}
            values["days_since_filing"] = (session - report_filed_dates[visible_count - 1]).days
            if selected.get("is_amendment"):
                prior_original = any(
                    item.get("available_at") and str(item["available_at"]) < selected["available_at"]
                    and not item.get("is_amendment")
                    and item.get("report_period_end") == selected.get("report_period_end")
                    for item in report_rows
                )
                if prior_original:
                    values["quality_status"] = "ok" if any(
                        selected.get(key) is not None for key in _CONCEPTS
                    ) else "missing"
                else:
                    values["quality_status"] = "amendment_only"
            else:
                values["quality_status"] = "ok" if any(selected.get(key) is not None for key in _CONCEPTS) else "missing"
        else:
            values = {key: None for key in (*fields, "days_since_filing")}
            values["quality_status"] = "missing"
        output_rows.append({"date": session, "available_as_of": values.pop("available_at"), **values})
    columns = ["date", "available_as_of", "accession_number", "form", "is_amendment", "fiscal_year",
               "fiscal_period", "report_period_end", "days_since_filing", *_CONCEPTS, "quality_status"]
    data = pd.DataFrame(output_rows, columns=columns)
    data["fiscal_year"] = pd.array(data["fiscal_year"], dtype="Int64")
    ticker = (output_ticker or _ticker_for_cik(raw_dir, cik10) or cik10).upper()
    output = organized_dir / "stocks" / ticker / "financials.csv"
    output.parent.mkdir(parents=True, exist_ok=True)
    data.to_csv(output, index=False, date_format="%Y-%m-%d")
    inputs = []
    for path in paths:
        try:
            relative = str(path.relative_to(raw_dir.parent.parent))
        except ValueError:
            relative = str(path)
        inputs.append({"path": relative, "sha256": _sha256(path)})
    _write_meta(output.parent / "_meta.json", ticker, inputs, output, organized_dir, len(data), len(filings))
    return output
