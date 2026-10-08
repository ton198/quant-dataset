"""As-of SEC filing organization helpers used by the main organizer."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

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
            fields = {
                str(name).casefold(): index for index, name in enumerate(payload.get("fields", []))
            }
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
    tag: str,
    filed: str,
    fy: Any,
    fp: Any,
    end: str,
    form: str = "",
    accession_number: str = "",
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
            return (
                0 if in_quarter_range else 1,
                abs(duration - 91) if in_quarter_range else duration,
                start,
            )
        if filing_form == "10-K" and duration:
            annual_range = 300 <= duration <= 400
            return (
                0 if annual_range else 1,
                abs(duration - 365) if annual_range else -duration,
                start,
            )
        return (0, 0, start)

    return min(matches, key=preference)


def _fiscal_identifiers(
    filing: dict[str, Any],
    fiscal_index: dict[tuple[str, str], list[dict[str, Any]]],
    reported_facts: list[dict[str, Any]],
    all_filings: list[dict[str, Any]],
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
            str(item.get("reportDate", ""))
            for item in all_filings
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
        isinstance(accessions, list)
        and isinstance(filing_dates, list)
        and len(accessions) > 0
        and len(accessions) == len(filing_dates)
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
            filings.append(
                {
                    key: values[index]
                    for key, values in recent.items()
                    if isinstance(values, list) and len(values) > index
                }
            )
    fields, rows = payload.get("fields", []), payload.get("data", [])
    if isinstance(fields, list) and isinstance(rows, list):
        for row in rows:
            if isinstance(row, list):
                filings.append(
                    {str(key): row[index] for index, key in enumerate(fields) if index < len(row)}
                )
    if not filings:
        filings.extend(_column_oriented_rows(payload))
    return filings


def _canonical_cik(value: str) -> str | None:
    value = value.strip()
    if value.upper().startswith("CIK"):
        value = value[3:]
    if not value.isdigit():
        return None
    canonical = value.lstrip("0") or "0"
    return canonical if len(canonical) <= 10 else None


def _logical_key_cik(logical_key: str) -> str | None:
    """Parse the CIK field, never treating a numeric suffix as identity."""
    fields = logical_key.split(":")
    if len(fields) < 2:
        return None
    return _canonical_cik(fields[1])


def _resource_kind(logical_key: str, path: str = "") -> str:
    identity = f"{logical_key} {path}".casefold()
    if "companyfacts" in identity:
        return "companyfacts"
    if "submissions-page" in identity:
        return "submission_page"
    if "submission" in identity:
        return "submissions"
    return "unknown"


def _owned_resource_entries(
    root: Path, cik10: str
) -> tuple[list[tuple[Path, str, str, str]], str, dict[str, int]]:
    """Return CIK-owned references and fail-closed manifest diagnostics."""
    target = _canonical_cik(str(cik10))
    if target is None:
        return [], "malformed", {"invalid_cik": 1}

    manifest_path = root / "manifest.json"
    manifest_status = "unknown"
    manifest_rejections: Counter[str] = Counter()
    selected: list[tuple[Path, str, str, str]] = []
    manifest_logical_key_count = 0
    matched_logical_key_count = 0
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            manifest = None
            manifest_status = "unreadable"
        else:
            if not isinstance(manifest, dict):
                manifest_status = "malformed"
                manifest_rejections["malformed_raw_manifest"] += 1
            elif "resources" not in manifest:
                manifest_status = "malformed"
                manifest_rejections["manifest_missing_resources"] += 1
            else:
                raw_resources = manifest["resources"]
                if isinstance(raw_resources, list):
                    manifest_entries = raw_resources
                elif isinstance(raw_resources, dict):
                    manifest_entries = []
                    for logical_key, versions in raw_resources.items():
                        if not isinstance(logical_key, str) or not logical_key.strip():
                            manifest_status = "malformed"
                            manifest_rejections["manifest_resource_missing_logical_key"] += 1
                            continue
                        manifest_logical_key_count += 1
                        if _logical_key_cik(logical_key) == target:
                            matched_logical_key_count += 1
                        if isinstance(versions, dict):
                            version_records = [versions]
                        elif isinstance(versions, list):
                            version_records = versions
                        else:
                            manifest_status = "malformed"
                            manifest_rejections["manifest_resource_record_not_object"] += 1
                            continue
                        for record in version_records:
                            if not isinstance(record, dict):
                                manifest_status = "malformed"
                                manifest_rejections["manifest_resource_record_not_object"] += 1
                                continue
                            # A dict-keyed manifest carries logical_key outside
                            # the record; the key is authoritative if duplicated.
                            manifest_entries.append({**record, "logical_key": logical_key})
                else:
                    manifest_status = "malformed"
                    manifest_rejections["manifest_resources_not_list"] += 1
                    manifest_entries = []

                if manifest_status != "malformed":
                    manifest_status = "ok"
                for entry in manifest_entries:
                    if not isinstance(entry, dict):
                        manifest_status = "malformed"
                        manifest_rejections["manifest_resource_record_not_object"] += 1
                        continue
                    logical_key = entry.get("logical_key")
                    if not isinstance(logical_key, str) or not logical_key.strip():
                        manifest_status = "malformed"
                        manifest_rejections["manifest_resource_missing_logical_key"] += 1
                        continue
                    if isinstance(raw_resources, list):
                        manifest_logical_key_count += 1
                        if _logical_key_cik(logical_key) == target:
                            matched_logical_key_count += 1
                    if "path" not in entry or not isinstance(entry["path"], str) or not entry["path"]:
                        manifest_status = "malformed"
                        manifest_rejections["manifest_resource_invalid_path"] += 1
                        continue
                    if "sha256" in entry and not isinstance(entry["sha256"], str):
                        manifest_status = "malformed"
                        manifest_rejections["manifest_resource_invalid_sha256"] += 1
                        continue
                    if _logical_key_cik(logical_key) != target:
                        continue
                    raw_path = entry["path"]
                    path = root / raw_path
                    selected.append(
                        (
                            path,
                            _resource_kind(logical_key, raw_path),
                            logical_key,
                            "ok" if path.is_file() else "missing",
                        )
                    )

    if not selected and root.exists() and manifest_status not in {"unreadable", "malformed"}:
        filename_pattern = re.compile(
            rf"(?<![0-9])0*{re.escape(str(int(target)))}(?![0-9])"
        )
        for path in sorted(root.glob("*.json")):
            kind = _resource_kind("", path.name)
            if (
                path.name != "manifest.json"
                and kind in {"companyfacts", "submissions", "submission_page"}
                and filename_pattern.search(path.name)
            ):
                selected.append((path, kind, "", "ok"))
    if manifest_status == "ok" and manifest_logical_key_count and not matched_logical_key_count and not selected:
        manifest_rejections["no_cik_owned_resources"] += 1
    elif manifest_status == "ok" and matched_logical_key_count and not selected:
        manifest_rejections["empty_cik_resource_versions"] += 1

    # Keep distinct logical references for the raw inventory, but avoid scanning
    # and hashing a shared payload more than once.
    selected.sort(key=lambda item: (str(item[0]), item[1], item[2]))
    return selected, manifest_status, dict(manifest_rejections)


def _raw_input_inventory(
    root: Path, cik10: str | None
) -> tuple[
    list[dict[str, Any]], str, str, list[Path], dict[Path, str], dict[Path, set[str]], dict[str, int]
]:
    from .financial_events import inventory_fingerprint, normalize_cik

    normalized_cik = normalize_cik(cik10)
    if normalized_cik is None:
        if cik10 is None or str(cik10).strip() == "":
            inventory = [{"kind": "no_cik", "path": None, "status": "no_cik", "sha256": None}]
            return inventory, inventory_fingerprint(None, inventory), "no_cik", [], {}, {}, {}
        inventory = [
            {
                "kind": "invalid_cik",
                "cik_input": str(cik10),
                "path": None,
                "status": "malformed",
                "sha256": None,
            }
        ]
        return (
            inventory,
            inventory_fingerprint(None, inventory),
            "malformed",
            [],
            {},
            {},
            {"invalid_cik": 1},
        )

    entries, manifest_status, manifest_rejections = _owned_resource_entries(root, normalized_cik)
    manifest_path = root / "manifest.json"
    try:
        manifest_sha256 = _sha256(manifest_path) if manifest_path.is_file() else None
    except OSError:
        manifest_sha256 = None
    by_path: dict[Path, list[tuple[str, str, str]]] = {}
    for path, kind, logical_key, status in entries:
        by_path.setdefault(path, []).append((kind, logical_key, status))
    inventory: list[dict[str, Any]] = [
        {
            "kind": "manifest",
            "path": "manifest.json" if manifest_path.is_file() else None,
            "status": manifest_status,
            "sha256": manifest_sha256,
        }
    ]
    input_hashes: dict[Path, str] = {}
    expected_types: dict[Path, set[str]] = {}
    for path in sorted(by_path):
        refs = by_path[path]
        status = next((item[2] for item in refs if item[2] != "ok"), "ok")
        digest = None
        if status == "ok":
            try:
                digest = _sha256(path)
            except OSError:
                status = "unreadable"
        if digest is not None:
            input_hashes[path] = digest
        expected_types[path] = {kind for kind, _, _ in refs if kind != "unknown"}
        for kind, logical_key, _ in refs:
            inventory.append(
                {
                    "kind": kind,
                    "logical_key": logical_key or None,
                    "path": str(path.relative_to(root)) if path.is_relative_to(root) else str(path),
                    "status": status,
                    "sha256": digest,
                }
            )
    if not entries:
        inventory.append({"kind": "raw_inputs", "path": None, "status": "no_input", "sha256": None})
    inventory.sort(key=lambda item: (str(item.get("path")), str(item.get("kind")), str(item.get("logical_key"))))
    fingerprint = inventory_fingerprint(normalized_cik, inventory)
    return (
        inventory,
        fingerprint,
        manifest_status,
        sorted(input_hashes),
        input_hashes,
        expected_types,
        manifest_rejections,
    )


def raw_input_inventory(
    raw_dir: Path, cik10: str | None
) -> tuple[list[dict[str, Any]], str, dict[str, str]]:
    inventory, fingerprint, manifest_status, _, _, _, _ = _raw_input_inventory(
        raw_dir / "sec" / "financials", cik10
    )
    statuses = {"manifest": manifest_status, "submissions": "no_input", "companyfacts": "no_input"}
    for entry in inventory:
        kind = entry.get("kind")
        status_kind = "submissions" if kind == "submission_page" else kind
        if status_kind in {"submissions", "companyfacts"} and entry.get("status") in {
            "missing", "unreadable", "malformed"
        }:
            statuses[status_kind] = entry["status"]
        elif status_kind in {"submissions", "companyfacts"} and entry.get("status") == "ok":
            statuses[status_kind] = "ok"
    return inventory, fingerprint, statuses


def _owned_paths(root: Path, cik10: str) -> list[Path]:
    if _canonical_cik(str(cik10)) is None:
        return []
    entries, _, _ = _owned_resource_entries(root, cik10)
    return sorted({path for path, _, _, status in entries if status == "ok" and path.is_file()})


def _write_meta(
    path: Path,
    ticker: str,
    inputs: list[dict[str, str]],
    output: Path,
    organized_dir: Path,
    rows: int,
    input_count: int,
) -> None:
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(meta, dict):
            meta = {}
    except (OSError, ValueError):
        meta = {}
    old_inputs = {
        item.get("path"): item
        for item in meta.get("inputs", [])
        if isinstance(item, dict) and (inputs or "sec/financials/" not in str(item.get("path", "")))
    }
    old_inputs.update({item["path"]: item for item in inputs})
    output_record = {
        "path": str(output.relative_to(organized_dir.parent.parent)),
        "sha256": _sha256(output),
        "rows": rows,
    }
    old_outputs = {
        item.get("path"): item for item in meta.get("outputs", []) if isinstance(item, dict)
    }
    old_outputs[output_record["path"]] = output_record
    row_counts = meta.get("row_counts", {})
    if not isinstance(row_counts, dict):
        row_counts = {}
    row_counts.update({"financials_input": input_count, "financials_output": rows})
    meta.update(
        {
            "ticker": ticker,
            "generated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "cleaning_rules_version": "v1",
            "inputs": list(old_inputs.values()),
            "outputs": list(old_outputs.values()),
            "row_counts": row_counts,
            "known_issues": meta.get("known_issues", []),
        }
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        temporary.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _valid_companyfacts_payload(payload: dict[str, Any]) -> bool:
    facts = payload.get("facts")
    if not isinstance(facts, dict):
        return False
    for taxonomy_data in facts.values():
        if not isinstance(taxonomy_data, dict):
            return False
        for tag_data in taxonomy_data.values():
            if not isinstance(tag_data, dict) or "units" not in tag_data:
                return False
            units = tag_data["units"]
            if not isinstance(units, dict):
                return False
            if any(
                not isinstance(values, list)
                or any(not isinstance(fact, dict) for fact in values)
                for values in units.values()
            ):
                return False
    return True


def _valid_submission_payload(payload: dict[str, Any]) -> bool:
    filings = payload.get("filings")
    if isinstance(filings, dict) and "recent" in filings:
        recent = filings.get("recent")
        if not isinstance(recent, dict):
            return False
        accession = recent.get("accessionNumber")
        filed = recent.get("filingDate")
        if not isinstance(accession, list) or not isinstance(filed, list):
            return False
        length = len(accession)
        return len(filed) == length and all(
            len(values) == length for values in recent.values() if isinstance(values, list)
        ) and all(isinstance(value, list) for value in recent.values())
    fields, rows = payload.get("fields"), payload.get("data")
    if isinstance(fields, list) or isinstance(rows, list):
        if not isinstance(fields, list) or not isinstance(rows, list):
            return False
        return all(isinstance(row, list) and len(row) == len(fields) for row in rows)
    accession, filed = payload.get("accessionNumber"), payload.get("filingDate")
    if isinstance(accession, list) and isinstance(filed, list):
        length = len(accession)
        return len(filed) == length and all(
            len(values) == length for values in payload.values() if isinstance(values, list)
        )
    return False


def _merge_resource_status(statuses: dict[str, str], kind: str, status: str) -> None:
    current = statuses.get(kind, "no_input")
    priority = {
        "no_input": 0,
        "no_usable_facts": 1,
        "no_usable_submissions": 1,
        "misattributed": 1,
        "ok": 2,
        "missing": 3,
        "unreadable": 4,
        "malformed": 5,
    }
    if priority.get(status, 0) >= priority.get(current, 0):
        statuses[kind] = status


def organize_financials(
    cik10: str,
    raw_dir: Path,
    organized_dir: Path,
    calendar: list[date],
    output_ticker: str | None = None,
) -> Path:
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
    (
        raw_inventory,
        raw_inventory_sha256,
        manifest_status,
        paths,
        input_hashes_by_path,
        expected_types,
        manifest_rejections,
    ) = _raw_input_inventory(root, cik10)
    fact_payloads: list[dict[str, Any]] = []
    submission_payloads: list[tuple[int, str, Path, dict[str, Any]]] = []
    input_rejections: Counter[str] = Counter(manifest_rejections)
    input_resource_status = {
        "manifest": manifest_status,
        "submissions": "no_input",
        "companyfacts": "no_input",
    }
    input_resource_diagnostics: list[dict[str, Any]] = []
    for reason in ("no_cik_owned_resources", "empty_cik_resource_versions"):
        if input_rejections.get(reason, 0):
            input_resource_diagnostics.append(
                {
                    "kind": "manifest",
                    "status": "no_matching_cik_resources",
                    "reason": reason,
                    "count": input_rejections[reason],
                }
            )
    for item in raw_inventory:
        kind = item.get("kind")
        status_kind = "submissions" if kind == "submission_page" else kind
        if item.get("status") in {"missing", "unreadable"}:
            resource_state = item["status"]
            _merge_resource_status(input_resource_status, "manifest", resource_state)
            if status_kind in {"submissions", "companyfacts"}:
                _merge_resource_status(input_resource_status, status_kind, resource_state)
            input_rejections[
                "missing_manifest_resource" if resource_state == "missing" else "unreadable_raw_payload"
            ] += 1
            input_resource_diagnostics.append(
                {"kind": kind, "path": item.get("path"), "status": resource_state}
            )
    if manifest_status in {"unreadable", "malformed"}:
        diagnostic_key = f"{manifest_status}_raw_manifest"
        if input_rejections.get(diagnostic_key, 0) == 0:
            input_rejections[diagnostic_key] += 1
        input_resource_diagnostics.append(
            {"kind": "manifest", "path": "manifest.json", "status": manifest_status}
        )

    target_cik = str(int(cik10)) if cik10.isdigit() else cik10
    for path in paths:
        expected = expected_types.get(path, set())
        source_path = (
            str(path.relative_to(raw_dir.parent.parent))
            if path.is_relative_to(raw_dir.parent.parent)
            else str(path)
        )
        source_hash = input_hashes_by_path[path]
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            input_rejections["unreadable_raw_payload"] += 1
            for kind in expected or {"unknown"}:
                _merge_resource_status(input_resource_status, kind, "unreadable")
            input_resource_diagnostics.append(
                {"kind": sorted(expected) or ["unknown"], "path": source_path, "status": "unreadable"}
            )
            continue
        if not isinstance(payload, dict):
            input_rejections["invalid_raw_payload_shape"] += 1
            for kind in expected or {"unknown"}:
                status_kind = "submissions" if kind == "submission_page" else kind
                if kind == "submission_page":
                    input_rejections["ragged_submission_page"] += 1
                    _merge_resource_status(input_resource_status, status_kind, "no_usable_submissions")
                else:
                    _merge_resource_status(input_resource_status, status_kind, "malformed")
            input_resource_diagnostics.append(
                {"kind": sorted(expected) or ["unknown"], "path": source_path, "status": "malformed"}
            )
            continue

        if "facts" in payload:
            actual_kind = "companyfacts"
        elif "submission_page" in expected:
            actual_kind = "submission_page"
        elif "companyfacts" in expected:
            # SEC uses a parseable empty object as a placeholder for filers with
            # no Company Facts/XBRL payload.
            actual_kind = "companyfacts"
        else:
            actual_kind = "submissions"
        status_kind = "submissions" if actual_kind == "submission_page" else actual_kind

        payload_cik = payload.get("cik")
        if payload_cik is not None and str(payload_cik).lstrip("0") != target_cik.lstrip("0"):
            input_rejections["raw_payload_cik_mismatch"] += 1
            for kind in expected or {status_kind}:
                status_type = "submissions" if kind == "submission_page" else kind
                _merge_resource_status(input_resource_status, status_type, "misattributed")
            input_resource_diagnostics.append(
                {
                    "kind": sorted(expected) or [status_kind],
                    "path": source_path,
                    "status": "misattributed",
                    "expected_cik": target_cik,
                    "payload_cik": payload_cik,
                }
            )
            continue

        if actual_kind == "companyfacts" and (
            "facts" not in payload
            or (isinstance(payload.get("facts"), dict) and not payload["facts"])
        ):
            input_rejections["no_usable_facts"] += 1
            _merge_resource_status(input_resource_status, "companyfacts", "no_usable_facts")
            input_resource_diagnostics.append(
                {"kind": "companyfacts", "path": source_path, "status": "no_usable_facts"}
            )
            continue

        if actual_kind == "companyfacts":
            valid = _valid_companyfacts_payload(payload)
        else:
            valid = _valid_submission_payload(payload)
        if expected and actual_kind not in expected:
            valid = False
            input_rejections["raw_payload_type_mismatch"] += 1
        if not valid:
            if actual_kind == "submission_page":
                input_rejections["ragged_submission_page"] += 1
                _merge_resource_status(input_resource_status, "submissions", "no_usable_submissions")
                input_resource_diagnostics.append(
                    {"kind": actual_kind, "path": source_path, "status": "rejected_page"}
                )
                continue
            if actual_kind == "submissions":
                input_rejections["invalid_submissions_payload_shape"] += 1
            else:
                input_rejections["invalid_companyfacts_payload_shape"] += 1
            _merge_resource_status(input_resource_status, status_kind, "malformed")
            input_resource_diagnostics.append(
                {"kind": actual_kind, "path": source_path, "status": "malformed"}
            )
            continue
        _merge_resource_status(input_resource_status, status_kind, "ok")
        if actual_kind == "companyfacts":
            fact_payloads.append(
                {
                    **payload,
                    "_source_fact_path": source_path,
                    "_source_fact_sha256": source_hash,
                }
            )
        else:
            submission_payloads.append((_submission_rank(payload), str(path), path, payload))

    # Historical submission pages overlap the main ``filings.recent`` block for
    # their newest accessions; dedupe by accession number, preferring the main
    # submissions payload and keeping a deterministic rank-then-path order.
    filings: list[dict[str, Any]] = []
    seen_accessions: set[str] = set()
    for _, _, source_path, payload in sorted(submission_payloads, key=lambda item: (item[0], item[1])):
        for row_index, row in enumerate(_submission_rows(payload)):
            accession = str(row.get("accessionNumber") or "")
            if accession and accession in seen_accessions:
                continue
            if accession:
                seen_accessions.add(accession)
            try:
                stored_path = str(source_path.relative_to(raw_dir.parent.parent))
            except ValueError:
                stored_path = str(source_path)
            filings.append(
                {
                    **row,
                    "_source_submission_path": stored_path,
                    "_source_submission_sha256": input_hashes_by_path[source_path],
                    "_source_submission_locator": f"record[{row_index}]",
                }
            )

    has_sec_inputs = any(
        item.get("kind") not in {"manifest", "raw_inputs", "no_cik"}
        for item in raw_inventory
    )
    if has_sec_inputs and input_resource_status.get("submissions") == "no_input":
        _merge_resource_status(input_resource_status, "submissions", "no_usable_submissions")
        input_rejections["no_submission_resource_for_cik"] += 1
        input_resource_diagnostics.append(
            {
                "kind": "submissions",
                "path": None,
                "status": "no_usable_submissions",
                "reason": "no_submission_reference_for_cik",
            }
        )

    # Build the richer event/version artifacts before the legacy per-session
    # latest-snapshot selection below; never reconstruct these tables from CSV.
    from .financial_events import (
        commit_artifact_meta,
        commit_incomplete_diagnostic,
        organize_artifact,
    )

    ticker = (output_ticker or _ticker_for_cik(raw_dir, cik10) or cik10).upper()
    artifact = organize_artifact(
        cik10,
        ticker,
        raw_dir,
        organized_dir,
        filings,
        fact_payloads,
        paths,
        output_calendar=calendar,
        initial_rejections=dict(input_rejections),
        input_hashes_by_path=input_hashes_by_path,
        raw_input_inventory=raw_inventory,
        input_resource_status=input_resource_status,
        input_resource_diagnostics=input_resource_diagnostics,
        commit_meta=False,
    )

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
                    if end
                    else None
                )
                if selected_fact is not None:
                    break
            if selected_fact is not None:
                reported_facts.append(selected_fact)
                concept_values[output] = float(selected_fact["val"])
            else:
                concept_values[output] = None
        fiscal_year, fiscal_period = _fiscal_identifiers(
            filing,
            fiscal_index,
            reported_facts,
            filings,
        )
        report_rows.append(
            {
                "filed": filed,
                "available_at": filed,
                "accession_number": accession_number,
                "form": form,
                "is_amendment": form.endswith("/A"),
                "fiscal_year": fiscal_year,
                "fiscal_period": fiscal_period,
                "report_period_end": end,
                **concept_values,
            }
        )
    report_rows.sort(key=lambda item: (item["available_at"], item["accession_number"]))
    report_filed_dates = [date.fromisoformat(item["filed"]) for item in report_rows]
    output_rows: list[dict[str, Any]] = []
    fields = (
        "available_at",
        "accession_number",
        "form",
        "is_amendment",
        "fiscal_year",
        "fiscal_period",
        "report_period_end",
        *_CONCEPTS,
    )
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
                    item.get("available_at")
                    and str(item["available_at"]) < selected["available_at"]
                    and not item.get("is_amendment")
                    and item.get("report_period_end") == selected.get("report_period_end")
                    for item in report_rows
                )
                if prior_original:
                    values["quality_status"] = (
                        "ok"
                        if any(selected.get(key) is not None for key in _CONCEPTS)
                        else "missing"
                    )
                else:
                    values["quality_status"] = "amendment_only"
            else:
                values["quality_status"] = (
                    "ok" if any(selected.get(key) is not None for key in _CONCEPTS) else "missing"
                )
        else:
            values = {key: None for key in (*fields, "days_since_filing")}
            values["quality_status"] = "missing"
        output_rows.append(
            {"date": session, "available_as_of": values.pop("available_at"), **values}
        )
    columns = [
        "date",
        "available_as_of",
        "accession_number",
        "form",
        "is_amendment",
        "fiscal_year",
        "fiscal_period",
        "report_period_end",
        "days_since_filing",
        *_CONCEPTS,
        "quality_status",
    ]
    data = pd.DataFrame(output_rows, columns=columns)
    data["fiscal_year"] = pd.array(data["fiscal_year"], dtype="Int64")
    output = organized_dir / "stocks" / ticker / "financials.csv"
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{output.name}.", suffix=".tmp", dir=output.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        data.to_csv(temporary, index=False, date_format="%Y-%m-%d")
        os.replace(temporary, output)
    except Exception as exc:
        artifact.update({"complete": False, "status": "legacy_csv_write_failed", "write_error": str(exc)})
        commit_incomplete_diagnostic(output.parent, ticker, artifact, artifact.get("_input_records", []))
        raise
    finally:
        temporary.unlink(missing_ok=True)
    inputs = [
        {
            "path": (
                str(path.relative_to(raw_dir.parent.parent))
                if path.is_relative_to(raw_dir.parent.parent)
                else str(path)
            ),
            "sha256": input_hashes_by_path[path],
        }
        for path in paths
    ]
    try:
        _write_meta(
            output.parent / "_meta.json", ticker, inputs, output, organized_dir, len(data), len(filings)
        )
    except Exception as exc:
        artifact.update({"complete": False, "status": "legacy_meta_write_failed", "write_error": str(exc)})
        commit_incomplete_diagnostic(output.parent, ticker, artifact, artifact.get("_input_records", []))
        raise
    artifact_inputs = artifact.pop("_input_records", [])
    try:
        commit_artifact_meta(output.parent, ticker, artifact, artifact_inputs)
    except Exception as exc:
        artifact.update({"complete": False, "status": "final_meta_commit_failed", "write_error": str(exc)})
        commit_incomplete_diagnostic(output.parent, ticker, artifact, artifact_inputs)
        raise
    if not artifact.get("complete"):
        raise ValueError(
            "financial-events artifact is incomplete: "
            f"{artifact.get('rejection_counts', {})}"
        )
    return output
