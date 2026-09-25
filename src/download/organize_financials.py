"""As-of SEC filing organization helpers used by the main organizer."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from .errors import OrganizeError


_CONCEPTS: dict[str, tuple[str, ...]] = {
    "revenue": ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax"),
    "gross_profit": ("GrossProfit",),
    "operating_income": ("OperatingIncomeLoss",),
    "net_income": ("NetIncomeLoss",),
    "operating_cash_flow": ("NetCashProvidedByUsedInOperatingActivities",),
    "capital_expenditure": ("PaymentsToAcquirePropertyPlantAndEquipment",),
    "assets": ("Assets",),
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

    mappings = (ticker_to_cik, cik_to_ticker)
    _TICKER_CACHE[cache_key] = mappings
    return mappings


def _ticker_for_cik(raw_dir: Path, cik10: str) -> str | None:
    return _ticker_mappings(raw_dir)[1].get(cik10)


def _facts_for_period(payload: dict[str, Any], tag: str, filed: str,
                      fy: Any, fp: Any, end: str) -> float | None:
    units = payload.get("facts", {}).get("us-gaap", {}).get(tag, {}).get("units", {})
    for values in units.values():
        matches = [item for item in values if str(item.get("filed", "")) == filed
                   and str(item.get("end", "")) == end
                   and (fy is None or str(item.get("fy", "")) == str(fy))
                   and (fp is None or str(item.get("fp", "")) == str(fp))]
        if matches:
            try:
                return float(matches[-1]["val"])
            except (KeyError, TypeError, ValueError):
                return None
    return None


def _submission_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
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

    ``output_ticker`` directs shared-CIK companies to an independent ticker
    output directory; omitted callers retain the legacy CIK-to-ticker lookup.
    """

    root = raw_dir / "sec" / "financials"
    paths = _owned_paths(root, cik10)
    filings: list[dict[str, Any]] = []
    fact_payloads: list[dict[str, Any]] = []
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
        elif "filings" in payload or "fields" in payload:
            filings.extend(_submission_rows(payload))

    report_rows: list[dict[str, Any]] = []
    for filing in filings:
        filed = str(filing.get("filingDate", ""))
        if not filed:
            continue
        end = str(filing.get("reportDate", ""))
        fy, fp = filing.get("fy"), filing.get("fp")
        concept_values: dict[str, float | None] = {}
        for output, tags in _CONCEPTS.items():
            found = None
            for tag in tags:
                for payload in fact_payloads:
                    found = _facts_for_period(payload, tag, filed, fy, fp, end) if end else None
                    if found is not None:
                        break
                if found is not None:
                    break
            concept_values[output] = found
        report_rows.append({
            "filed": filed, "available_at": filed,
            "accession_number": str(filing.get("accessionNumber", "")),
            "form": str(filing.get("form", "")),
            "is_amendment": str(filing.get("form", "")).endswith("/A"),
            "fiscal_year": fy, "fiscal_period": fp, "report_period_end": end, **concept_values,
        })
    report_rows.sort(key=lambda item: (item["available_at"], item["accession_number"]))
    output_rows: list[dict[str, Any]] = []
    fields = ("available_at", "accession_number", "form", "is_amendment", "fiscal_year", "fiscal_period",
              "report_period_end", *_CONCEPTS)
    for session in sorted(calendar):
        visible = [item for item in report_rows if date.fromisoformat(item["filed"]) < session]
        if visible:
            selected = visible[-1]
            values: dict[str, Any] = {key: selected.get(key) for key in fields}
            values["days_since_filing"] = (session - date.fromisoformat(selected["filed"])).days
            prior_original = any(
                item.get("available_at") and str(item["available_at"]) < selected["available_at"]
                and not item.get("is_amendment")
                and item.get("report_period_end") == selected.get("report_period_end")
                for item in report_rows
            )
            if selected.get("is_amendment") and not prior_original:
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
