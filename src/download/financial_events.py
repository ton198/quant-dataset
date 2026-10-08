"""Stable filing-event and normalized financial-fact artifact generation.

The daily ``financials.csv`` remains a legacy snapshot. This module builds its
separate event/version history directly from cached SEC submissions and Company
Facts payloads, before any daily snapshot selection takes place.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import exchange_calendars as xcals
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from .organize_financials import _CONCEPTS

CONTRACT_VERSION = "financial_events_v1"
_UNSET = object()

EVENT_SCHEMA = pa.schema(
    [
        ("event_id", pa.string()),
        ("asset_id", pa.string()),
        ("cik10", pa.string()),
        ("accession_number", pa.string()),
        ("filed_date", pa.date32()),
        ("effective_visible_session", pa.date32()),
        ("form", pa.string()),
        ("is_amendment", pa.bool_()),
        ("report_period_end", pa.date32()),
        ("fiscal_year", pa.int32()),
        ("fiscal_period", pa.string()),
        ("fiscal_key_source", pa.string()),
        ("quality_status", pa.string()),
        ("source_submission_path", pa.string()),
        ("source_submission_sha256", pa.string()),
        ("source_submission_locator", pa.string()),
    ]
)

FACT_SCHEMA = pa.schema(
    [
        ("fact_version_id", pa.string()),
        ("event_id", pa.string()),
        ("concept", pa.string()),
        ("value", pa.float64()),
        ("unit", pa.string()),
        ("taxonomy", pa.string()),
        ("tag", pa.string()),
        ("period_kind", pa.string()),
        ("period_start", pa.date32()),
        ("report_period_end", pa.date32()),
        ("duration_days", pa.int32()),
        ("fiscal_year", pa.int32()),
        ("fiscal_period", pa.string()),
        ("fiscal_key_source", pa.string()),
        ("filed_date", pa.date32()),
        ("effective_visible_session", pa.date32()),
        ("fact_accession_number", pa.string()),
        ("match_method", pa.string()),
        ("source_fact_path", pa.string()),
        ("source_fact_sha256", pa.string()),
        ("source_fact_locator", pa.string()),
    ]
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _stable_id(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def normalize_cik(cik10: str | None) -> str | None:
    if cik10 is None:
        return None
    value = str(cik10).strip()
    if not value:
        return None
    if value.upper().startswith("CIK"):
        value = value[3:]
    if not value.isdigit():
        return None
    significant = value.lstrip("0") or "0"
    if len(significant) > 10:
        return None
    return significant.zfill(10)


def inventory_fingerprint(cik10: str | None, inventory: list[dict[str, Any]]) -> str:
    payload = {
        "cik10": normalize_cik(cik10),
        "resources": sorted(inventory, key=lambda item: (str(item.get("path")), str(item.get("kind")))),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _iso_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _base_form(form: str) -> str:
    return form.removesuffix("/A").upper()


def _normalize_fiscal_period(value: Any) -> str | None:
    if value is None:
        return None
    value = str(value).upper()
    return value if value in {"Q1", "Q2", "Q3", "Q4", "FY"} else None


def _relative_path(path: Path, raw_dir: Path) -> str:
    try:
        return str(path.relative_to(raw_dir.parent.parent))
    except ValueError:
        return str(path)


def _map_effective_sessions(filed_dates: list[date]) -> tuple[dict[date, date], dict[str, Any]]:
    """Map filings on a calendar wide enough to include their next XNYS session."""
    if not filed_dates:
        return {}, {
            "exchange": "XNYS",
            "first_session": None,
            "last_session": None,
            "mapping_start": None,
            "mapping_end": None,
        }
    start = min(filed_dates)
    end = max(filed_dates) + timedelta(days=14)
    calendar = xcals.get_calendar(
        "XNYS", start=pd.Timestamp(start), end=pd.Timestamp(end)
    )
    sessions = [item.date() for item in calendar.sessions]
    mapped: dict[date, date] = {}
    for filed in sorted(set(filed_dates)):
        index = bisect_right(sessions, filed)
        if index >= len(sessions):
            raise ValueError(f"XNYS calendar has no session after filed_date {filed.isoformat()}")
        mapped[filed] = sessions[index]
    return mapped, {
        "exchange": "XNYS",
        "first_session": sessions[0].isoformat() if sessions else None,
        "last_session": sessions[-1].isoformat() if sessions else None,
        "mapping_start": start.isoformat(),
        "mapping_end": end.isoformat(),
    }


def _fiscal_metadata(source: dict[str, Any]) -> tuple[int | None, str | None]:
    """Parse one source's fiscal key using the normalized field interface."""
    try:
        year = int(source.get("fiscal_year")) if source.get("fiscal_year") is not None else None
    except (TypeError, ValueError, OverflowError):
        year = None
    period = _normalize_fiscal_period(source.get("fiscal_period"))
    return year, period


@dataclass
class _PriorFilingIndex:
    annual_ends: list[date]
    annual_visibility_dates: list[date]
    annual_visibility_roots: list[int]
    annual_tree_nodes: list[tuple[int, int, int | None]]
    quarter_filed_dates: dict[date, list[date]]
    quarter_ends: list[date]


def _persistent_annual_update(
    nodes: list[tuple[int, int, int | None]],
    root: int,
    low: int,
    high: int,
    position: int,
    fiscal_year: int,
) -> int:
    left, right, _ = nodes[root] if root else (0, 0, None)
    if high - low == 1:
        nodes.append((0, 0, fiscal_year))
        return len(nodes) - 1
    middle = (low + high) // 2
    if position < middle:
        left = _persistent_annual_update(nodes, left, low, middle, position, fiscal_year)
    else:
        right = _persistent_annual_update(nodes, right, middle, high, position, fiscal_year)
    nodes.append((left, right, None))
    return len(nodes) - 1


def _persistent_rightmost_annual(
    nodes: list[tuple[int, int, int | None]],
    root: int,
    low: int,
    high: int,
    end_exclusive: int,
) -> tuple[int, int] | None:
    if root == 0 or low >= end_exclusive:
        return None
    left, right, fiscal_year = nodes[root]
    if high - low == 1:
        return (low, fiscal_year) if fiscal_year is not None else None
    middle = (low + high) // 2
    if end_exclusive > middle:
        match = _persistent_rightmost_annual(
            nodes, right, middle, high, end_exclusive
        )
        if match is not None:
            return match
    return _persistent_rightmost_annual(nodes, left, low, middle, end_exclusive)


def _prior_filing_index(
    source_events: list[dict[str, Any]],
    preliminary_keys: dict[str, tuple[int | None, str | None, str]],
) -> _PriorFilingIndex:
    """Prebuild persistent as-of anchor roots and sorted quarter indexes."""
    annual_by_end: dict[date, list[tuple[date, str, int]]] = defaultdict(list)
    quarter_filed_dates: dict[date, list[date]] = defaultdict(list)
    for event in source_events:
        filed = event["filed_date"]
        end = event["report_period_end"]
        if end is None or end > filed:
            continue
        form = _base_form(event["form"])
        if form == "10-K":
            year, period, source = preliminary_keys[event["event_id"]]
            if year is not None and period == "FY" and source != "conflict":
                annual_by_end[end].append((filed, event["event_id"], year))
        elif form == "10-Q":
            quarter_filed_dates[end].append(filed)

    annual_ends = sorted(annual_by_end)
    end_positions = {end: position for position, end in enumerate(annual_ends)}
    annual_versions = sorted(
        (
            filed,
            event_id,
            end_positions[end],
            year,
        )
        for end, versions in annual_by_end.items()
        for filed, event_id, year in versions
    )
    nodes: list[tuple[int, int, int | None]] = [(0, 0, None)]
    visibility_dates: list[date] = []
    visibility_roots: list[int] = []
    root = 0
    index = 0
    while index < len(annual_versions):
        filed = annual_versions[index][0]
        while index < len(annual_versions) and annual_versions[index][0] == filed:
            _, _, end_position, fiscal_year = annual_versions[index]
            root = _persistent_annual_update(
                nodes, root, 0, len(annual_ends), end_position, fiscal_year
            )
            index += 1
        visibility_dates.append(filed)
        visibility_roots.append(root)

    for filed_dates in quarter_filed_dates.values():
        filed_dates.sort()
    return _PriorFilingIndex(
        annual_ends=annual_ends,
        annual_visibility_dates=visibility_dates,
        annual_visibility_roots=visibility_roots,
        annual_tree_nodes=nodes,
        quarter_filed_dates=quarter_filed_dates,
        quarter_ends=sorted(quarter_filed_dates),
    )


def _prior_filing_index_from_raw(filings: list[dict[str, Any]]) -> _PriorFilingIndex:
    """Compatibility index for direct fiscal-key helper callers."""
    events: list[dict[str, Any]] = []
    keys: dict[str, tuple[int | None, str | None, str]] = {}
    for index, filing in enumerate(filings):
        filed = _iso_date(filing.get("filingDate"))
        end = _iso_date(filing.get("reportDate"))
        if filed is None:
            continue
        event_id = f"raw:{index}"
        events.append(
            {
                "event_id": event_id,
                "filed_date": filed,
                "report_period_end": end,
                "form": str(filing.get("form") or ""),
            }
        )
        try:
            year = int(filing.get("fy")) if filing.get("fy") is not None else None
        except (TypeError, ValueError, OverflowError):
            year = None
        period = _normalize_fiscal_period(filing.get("fp"))
        if year is None and end is not None and _base_form(str(filing.get("form") or "")) == "10-K":
            year, period = end.year, "FY"
        keys[event_id] = (year, period, "submission" if year is not None and period else "unknown")
    return _prior_filing_index(events, keys)


def _empty_prior_filing_index() -> _PriorFilingIndex:
    return _PriorFilingIndex([], [], [], [(0, 0, None)], {}, [])


def _anchor_fiscal_key(
    filing: dict[str, Any],
    prior_index: _PriorFilingIndex,
) -> tuple[int | None, str | None, str]:
    filed = _iso_date(filing.get("filingDate"))
    end = _iso_date(filing.get("reportDate"))
    form = _base_form(str(filing.get("form") or ""))
    if not end:
        return None, None, "unknown"
    if form == "10-K":
        return end.year, "FY", "submission"
    if form != "10-Q" or not filed:
        return None, None, "unknown"

    # Persistent roots represent annual versions filed strictly before each
    # date. Even when every earlier report was filed in the future, one bisect
    # determines there is no visible anchor; no prior-end scan is needed.
    visibility_index = bisect_left(prior_index.annual_visibility_dates, filed) - 1
    end_bound = bisect_left(prior_index.annual_ends, end)
    if visibility_index < 0 or end_bound == 0:
        return None, None, "unknown"
    root = prior_index.annual_visibility_roots[visibility_index]
    match = _persistent_rightmost_annual(
        prior_index.annual_tree_nodes,
        root,
        0,
        len(prior_index.annual_ends),
        end_bound,
    )
    if match is None:
        return None, None, "unknown"
    anchor_position, anchor_year = match
    anchor_end = prior_index.annual_ends[anchor_position]
    first = bisect_right(prior_index.quarter_ends, anchor_end)
    stop = bisect_left(prior_index.quarter_ends, end)
    quarter = 1
    for index in range(first, stop):
        report_end = prior_index.quarter_ends[index]
        if bisect_left(prior_index.quarter_filed_dates[report_end], filed) > 0:
            quarter += 1
            if quarter > 3:
                return None, None, "prior_annual_anchor"
    return anchor_year + 1, f"Q{quarter}", "prior_annual_anchor"


def _fiscal_key(
    filing: dict[str, Any],
    candidate_facts: list[dict[str, Any]],
    prior_filings: list[dict[str, Any]],
    prior_index: _PriorFilingIndex | None = None,
) -> tuple[int | None, str | None, str]:
    """Resolve a fiscal key as an indivisible, source-auditable pair.

    Facts and submission metadata may fill one another only when their complete
    pairs agree. Partial or conflicting pairs never yield a hybrid key.
    """
    fact_pairs: set[tuple[int, str]] = set()
    metadata_pairs: list[tuple[int | None, str | None]] = []
    for fact in candidate_facts:
        pair = _fiscal_metadata(fact)
        metadata_pairs.append(pair)
        if pair[0] is not None and pair[1] is not None:
            fact_pairs.add((pair[0], pair[1]))
    submission = {
        "fiscal_year": filing.get("fy"),
        "fiscal_period": filing.get("fp"),
    }
    submission_pair = _fiscal_metadata(submission)
    metadata_pairs.append(submission_pair)

    known_years = {year for year, _ in metadata_pairs if year is not None}
    known_periods = {period for _, period in metadata_pairs if period is not None}
    if len(known_years) > 1 or len(known_periods) > 1 or len(fact_pairs) > 1:
        return None, None, "conflict"
    fact_pair = next(iter(fact_pairs), None)
    complete_submission = (
        (submission_pair[0], submission_pair[1])
        if submission_pair[0] is not None and submission_pair[1] is not None
        else None
    )
    if fact_pair and complete_submission and fact_pair != complete_submission:
        return None, None, "conflict"

    if fact_pair is not None:
        resolved = (*fact_pair, "fact")
    elif complete_submission is not None:
        resolved = (*complete_submission, "submission")
    else:
        if prior_index is None:
            prior_index = _prior_filing_index_from_raw(prior_filings)
        year, period, source = _anchor_fiscal_key(filing, prior_index)
        if year is None or period is None:
            return None, None, source
        resolved = (year, period, source)

    year, period, source = resolved
    if any(value_year is not None and value_year != year for value_year, _ in metadata_pairs):
        return None, None, "conflict"
    if any(value_period is not None and value_period != period for _, value_period in metadata_pairs):
        return None, None, "conflict"
    return year, period, source


def _event_records(
    cik10: str,
    ticker: str,
    filings: list[dict[str, Any]],
    fact_payloads: list[dict[str, Any]],
    raw_dir: Path,
    initial_rejections: dict[str, int] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int], dict[str, Any]]:
    rejections: Counter[str] = Counter(initial_rejections or {})
    counters: Counter[str] = Counter()
    normalized_cik = normalize_cik(cik10)
    source_events: list[dict[str, Any]] = []
    for filing in filings:
        filed = _iso_date(filing.get("filingDate"))
        if filed is None:
            rejections["unparseable_filed_date"] += 1
            continue
        accession = str(filing.get("accessionNumber") or "").strip() or None
        source_hash = str(filing.get("_source_submission_sha256") or "")
        source_path = str(filing.get("_source_submission_path") or "")
        locator = str(filing.get("_source_submission_locator") or "")
        if not accession:
            rejections["missing_accession_number"] += 1
            event_id = f"{normalized_cik or cik10}:missing:{_stable_id(source_hash, locator)}"
        else:
            event_id = f"{normalized_cik or cik10}:{accession}"
        end = _iso_date(filing.get("reportDate"))
        raw_filing = filing
        if end is not None and end > filed:
            rejections["report_period_end_after_filed_date"] += 1
            end = None
            raw_filing = {**filing, "reportDate": None}
        source_events.append(
            {
                "event_id": event_id,
                "asset_id": ticker.upper(),
                "cik10": normalized_cik or cik10,
                "accession_number": accession,
                "filed_date": filed,
                "form": str(filing.get("form") or ""),
                "is_amendment": str(filing.get("form") or "").upper().endswith("/A"),
                "report_period_end": end,
                "source_submission_path": source_path,
                "source_submission_sha256": source_hash,
                "source_submission_locator": locator,
                "raw_filing": raw_filing,
            }
        )

    try:
        session_map, calendar_info = _map_effective_sessions(
            [item["filed_date"] for item in source_events]
        )
    except Exception as exc:
        session_map = {}
        rejections["effective_session_mapping_failed"] += len(source_events)
        calendar_info = {
            "exchange": "XNYS",
            "first_session": None,
            "last_session": None,
            "mapping_start": min((item["filed_date"] for item in source_events), default=None),
            "mapping_end": None,
            "mapping_error": str(exc),
        }
        if isinstance(calendar_info["mapping_start"], date):
            calendar_info["mapping_start"] = calendar_info["mapping_start"].isoformat()
    for event in source_events:
        event["effective_visible_session"] = session_map.get(event["filed_date"])

    # Build fact -> event linkage; an accession-less fact can only be attached
    # when filed/end/base-form identifies precisely one event.
    events_by_accession: dict[str, list[dict[str, Any]]] = defaultdict(list)
    events_by_tuple: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for event in source_events:
        accession = event["accession_number"]
        if accession:
            events_by_accession[accession].append(event)
        end = event["report_period_end"]
        if end:
            key = (
                event["filed_date"].isoformat(),
                end.isoformat(),
                _base_form(event["form"]),
            )
            events_by_tuple[key].append(event)

    # Collect all candidate records before tag-priority selection, so a NaN or
    # infinity at a high-priority tag can never hide a finite lower tag.
    raw_candidates: list[dict[str, Any]] = []
    for payload in fact_payloads:
        facts = payload.get("facts", {})
        if not isinstance(facts, dict):
            continue
        source_path = str(payload.get("_source_fact_path") or "")
        source_sha = str(payload.get("_source_fact_sha256") or "")
        for taxonomy in sorted(facts):
            tax_facts = facts.get(taxonomy)
            if not isinstance(tax_facts, dict):
                continue
            if taxonomy != "us-gaap":
                for tags in _CONCEPTS.values():
                    for tag in tags:
                        tag_payload = tax_facts.get(tag)
                        units = tag_payload.get("units", {}) if isinstance(tag_payload, dict) else {}
                        if isinstance(units, dict):
                            rejections["non_us_gaap_taxonomy"] += sum(
                                len(items) for items in units.values() if isinstance(items, list)
                            )
                continue
            for concept, tags in _CONCEPTS.items():
                tag_rank = {tag: rank for rank, tag in enumerate(tags)}
                for tag in tags:
                    tag_payload = tax_facts.get(tag)
                    units = tag_payload.get("units", {}) if isinstance(tag_payload, dict) else {}
                    if not isinstance(units, dict):
                        continue
                    for unit in sorted(units):
                        items = units.get(unit)
                        if not isinstance(items, list):
                            continue
                        for index, item in enumerate(items):
                            if not isinstance(item, dict):
                                continue
                            item_end = _iso_date(item.get("end"))
                            filed = _iso_date(item.get("filed"))
                            if not item_end or not filed:
                                rejections["fact_missing_filed_or_end"] += 1
                                continue
                            if item_end > filed:
                                rejections["report_period_end_after_filed_date"] += 1
                                continue
                            accession = str(item.get("accn") or "").strip()
                            if accession:
                                matching = [
                                    event
                                    for event in events_by_accession.get(accession, [])
                                    if event["filed_date"] == filed
                                    and event["report_period_end"] == item_end
                                    and item.get("form")
                                    and _base_form(str(item["form"])) == _base_form(event["form"])
                                ]
                                method = "accession_exact"
                                if len(matching) != 1:
                                    rejections["fact_accession_unmatched_or_ambiguous"] += 1
                                    continue
                            else:
                                form = _base_form(str(item.get("form") or ""))
                                matching = events_by_tuple.get(
                                    (filed.isoformat(), item_end.isoformat(), form), []
                                )
                                method = "unique_filed_end_form"
                                if len(matching) != 1:
                                    rejections[
                                        "fact_missing_accession_ambiguous"
                                        if matching
                                        else "fact_missing_accession_unmatched"
                                    ] += 1
                                    continue
                            event = matching[0]
                            if str(unit) != "USD":
                                rejections["non_usd_unit"] += 1
                                continue
                            try:
                                value = float(item.get("val"))
                            except (TypeError, ValueError, OverflowError):
                                rejections["non_numeric_value"] += 1
                                continue
                            if not math.isfinite(value):
                                rejections["non_finite_value"] += 1
                                continue
                            raw_start = item.get("start")
                            has_start = raw_start not in (None, "")
                            start = _iso_date(raw_start)
                            if has_start and start is None:
                                rejections["unparseable_period_start"] += 1
                                continue
                            form = _base_form(event["form"])
                            duration: int | None = None
                            if concept in {"assets", "liabilities", "equity"}:
                                if has_start:
                                    rejections["instant_fact_has_duration"] += 1
                                    continue
                                period_kind = "instant"
                            else:
                                if start is None:
                                    period_kind = "unknown"
                                else:
                                    duration = (item_end - start).days + 1
                                    if duration <= 0:
                                        rejections["invalid_period_range"] += 1
                                        continue
                                    if form == "10-Q" and 70 <= duration <= 125:
                                        period_kind = "quarter"
                                    elif form == "10-K" and 300 <= duration <= 400:
                                        period_kind = "annual"
                                    else:
                                        # Includes valid YTD facts: finite and
                                        # auditable, but never mislabeled quarter.
                                        period_kind = "unknown"
                            try:
                                fiscal_year = (
                                    int(item["fy"]) if item.get("fy") is not None else None
                                )
                            except (TypeError, ValueError, OverflowError):
                                fiscal_year = None
                            fiscal_period = _normalize_fiscal_period(item.get("fp"))
                            raw_candidates.append(
                                {
                                    "event": event,
                                    "concept": concept,
                                    "value": value,
                                    "unit": str(unit),
                                    "taxonomy": str(taxonomy),
                                    "tag": tag,
                                    "tag_rank": tag_rank[tag],
                                    "period_start": start,
                                    "period_end": item_end,
                                    "duration_days": duration,
                                    "period_kind": period_kind,
                                    "fiscal_year": fiscal_year,
                                    "fiscal_period": fiscal_period,
                                    "fact_accession_number": accession or None,
                                    "match_method": method,
                                    "source_fact_path": source_path,
                                    "source_fact_sha256": source_sha,
                                    "source_fact_locator": f"facts/{taxonomy}/{tag}/units/{unit}/{index}",
                                }
                            )

    # Repeated raw copies of an identical fact are deduped. Disagreeing values
    # for one accession/tag/unit/period have no disclosure-order evidence and
    # are all rejected rather than ordered by download time.
    conflict_groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for candidate in raw_candidates:
        event = candidate["event"]
        conflict_groups[
            (
                event["event_id"],
                candidate["tag"],
                candidate["unit"],
                candidate["period_start"],
                candidate["period_end"],
            )
        ].append(candidate)
    resolved: list[dict[str, Any]] = []
    for candidates in conflict_groups.values():
        values = {item["value"] for item in candidates}
        if len(values) > 1:
            rejections["conflicting_fact_versions"] += 1
            continue
        candidates.sort(
            key=lambda item: (
                item["source_fact_path"],
                item["source_fact_locator"],
            )
        )
        resolved.append(candidates[0])

    # A concept can have more than one actual duration for the same report end
    # (e.g. standalone quarter and YTD). Tag priority is applied within each
    # actual period, after all candidates have passed numeric finite checks.
    by_actual_period: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for candidate in resolved:
        event = candidate["event"]
        by_actual_period[
            (
                event["event_id"],
                candidate["concept"],
                candidate["period_start"],
                candidate["period_end"],
                candidate["period_kind"],
            )
        ].append(candidate)
    selected: list[dict[str, Any]] = []
    for candidates in by_actual_period.values():
        candidates.sort(key=lambda item: (item["tag_rank"], item["tag"], item["taxonomy"]))
        selected.append(candidates[0])

    selected_by_event: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for candidate in selected:
        selected_by_event[candidate["event"]["event_id"]].append(candidate)

    # Resolve own fact/submission keys first. Annual reports can then seed the
    # historical anchor index with their actual fiscal year (including fact-
    # derived non-calendar fiscal years), rather than report_end.year.
    empty_index = _empty_prior_filing_index()
    preliminary_keys: dict[str, tuple[int | None, str | None, str]] = {}
    for event in source_events:
        preliminary_keys[event["event_id"]] = _fiscal_key(
            event["raw_filing"],
            selected_by_event.get(event["event_id"], []),
            filings,
            empty_index,
        )
    prior_index = _prior_filing_index(source_events, preliminary_keys)

    event_keys: dict[str, tuple[int | None, str | None, str]] = {}
    for event in source_events:
        candidates = selected_by_event.get(event["event_id"], [])
        key = _fiscal_key(event["raw_filing"], candidates, filings, prior_index)
        if key[2] == "conflict":
            rejections["fiscal_key_conflicts"] += 1
        event_keys[event["event_id"]] = key

    fact_rows: list[dict[str, Any]] = []
    for candidate in selected:
        event = candidate["event"]
        fiscal_year, fiscal_period, key_source = event_keys[event["event_id"]]
        fact_id = _stable_id(
            event["event_id"],
            candidate["concept"],
            candidate["taxonomy"],
            candidate["tag"],
            candidate["unit"],
            candidate["period_start"].isoformat() if candidate["period_start"] else "",
            candidate["period_end"].isoformat(),
            repr(candidate["value"]),
        )
        fact_rows.append(
            {
                "fact_version_id": fact_id,
                "event_id": event["event_id"],
                "concept": candidate["concept"],
                "value": candidate["value"],
                "unit": candidate["unit"],
                "taxonomy": candidate["taxonomy"],
                "tag": candidate["tag"],
                "period_kind": candidate["period_kind"],
                "period_start": candidate["period_start"],
                "report_period_end": candidate["period_end"],
                "duration_days": candidate["duration_days"],
                "fiscal_year": fiscal_year,
                "fiscal_period": fiscal_period,
                "fiscal_key_source": key_source,
                "filed_date": event["filed_date"],
                "effective_visible_session": event["effective_visible_session"],
                "fact_accession_number": candidate["fact_accession_number"],
                "match_method": candidate["match_method"],
                "source_fact_path": candidate["source_fact_path"],
                "source_fact_sha256": candidate["source_fact_sha256"],
                "source_fact_locator": candidate["source_fact_locator"],
            }
        )
        counters["facts_written"] += 1

    # An amendment is quality-classified against the same base form and report
    # end only. The index avoids a full event scan for every filing.
    original_dates: dict[tuple[str, date], list[date]] = defaultdict(list)
    for original in source_events:
        if not original["is_amendment"] and original["report_period_end"] is not None:
            original_dates[
                (_base_form(original["form"]), original["report_period_end"])
            ].append(original["filed_date"])
    for dates in original_dates.values():
        dates.sort()

    # Fiscal identifiers on events and facts share the same resolved event key.
    events: list[dict[str, Any]] = []
    for event in source_events:
        fy, fp, source = event_keys[event["event_id"]]
        own_facts = selected_by_event.get(event["event_id"], [])
        has_prior_original = False
        if event["is_amendment"] and event["report_period_end"] is not None:
            prior_dates = original_dates.get(
                (_base_form(event["form"]), event["report_period_end"]), []
            )
            has_prior_original = bool(prior_dates) and prior_dates[0] < event["filed_date"]
        if event["is_amendment"] and not has_prior_original:
            quality = "amendment_only"
        else:
            quality = "ok" if own_facts else "missing"
        events.append(
            {
                "event_id": event["event_id"],
                "asset_id": event["asset_id"],
                "cik10": event["cik10"],
                "accession_number": event["accession_number"],
                "filed_date": event["filed_date"],
                "effective_visible_session": event["effective_visible_session"],
                "form": event["form"],
                "is_amendment": event["is_amendment"],
                "report_period_end": event["report_period_end"],
                "fiscal_year": fy,
                "fiscal_period": fp,
                "fiscal_key_source": source,
                "quality_status": quality,
                "source_submission_path": event["source_submission_path"],
                "source_submission_sha256": event["source_submission_sha256"],
                "source_submission_locator": event["source_submission_locator"],
            }
        )
        counters[f"event_quality_{quality}"] += 1

    events.sort(key=lambda row: (row["filed_date"], row["accession_number"] or "", row["event_id"]))
    fact_rows.sort(
        key=lambda row: (
            row["filed_date"],
            row["event_id"],
            row["concept"],
            row["report_period_end"],
            row["period_start"] or date.min,
            row["fact_version_id"],
        )
    )
    return events, fact_rows, dict(rejections), {**calendar_info, "quality_counts": dict(counters)}


def _write_parquet_atomic(path: Path, records: list[dict[str, Any]], schema: pa.Schema) -> None:
    table = pa.Table.from_pylist(records, schema=schema)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        pq.write_table(table, temporary, compression="zstd", version="2.6")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_tables(directory: Path, events: list[dict[str, Any]], facts: list[dict[str, Any]]) -> dict[str, Any]:
    """Atomically write both typed parquet tables and return integrity records."""
    event_path = directory / "financial_events.parquet"
    facts_path = directory / "financial_facts.parquet"
    _write_parquet_atomic(event_path, events, EVENT_SCHEMA)
    _write_parquet_atomic(facts_path, facts, FACT_SCHEMA)
    return {
        "contract_version": CONTRACT_VERSION,
        "events": {
            "path": event_path.name,
            "sha256": _sha256(event_path),
            "rows": len(events),
        },
        "facts": {
            "path": facts_path.name,
            "sha256": _sha256(facts_path),
            "rows": len(facts),
        },
    }


def _write_meta_atomic(path: Path, metadata: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        temporary.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def commit_incomplete_diagnostic(
    directory: Path,
    ticker: str,
    artifact: dict[str, Any],
    input_records: list[dict[str, str]] | None = None,
) -> Path:
    """Persist a non-complete state before output writes or after a failure."""
    meta_path = directory / "_meta.json"
    try:
        metadata = json.loads(meta_path.read_text(encoding="utf-8"))
        if not isinstance(metadata, dict):
            metadata = {}
    except (OSError, ValueError, UnicodeDecodeError):
        metadata = {}
    inputs = {
        item.get("path"): item
        for item in metadata.get("inputs", [])
        if isinstance(item, dict) and isinstance(item.get("path"), str)
    }
    inputs.update(
        {
            item["path"]: item
            for item in (input_records or [])
            if isinstance(item, dict) and isinstance(item.get("path"), str)
        }
    )
    diagnostic = dict(artifact)
    diagnostic["complete"] = False
    metadata.update(
        {
            "ticker": ticker.upper(),
            "inputs": list(inputs.values()),
            "cik10": diagnostic.get("cik10"),
            "raw_input_inventory_sha256": diagnostic.get("raw_input_inventory_sha256"),
            "financial_events": diagnostic,
        }
    )
    _write_meta_atomic(meta_path, metadata)
    return meta_path


def commit_artifact_meta(
    directory: Path,
    ticker: str,
    artifact: dict[str, Any],
    input_records: list[dict[str, str]],
) -> Path:
    """Register an artifact last, preserving metadata written by market organizing."""
    meta_path = directory / "_meta.json"
    try:
        metadata = json.loads(meta_path.read_text(encoding="utf-8"))
        if not isinstance(metadata, dict):
            metadata = {}
    except (OSError, ValueError, UnicodeDecodeError):
        metadata = {}
    output_root = directory.parent.parent.parent.parent
    outputs = {
        item.get("path"): item
        for item in metadata.get("outputs", [])
        if isinstance(item, dict) and isinstance(item.get("path"), str)
    }
    for key in ("events", "facts"):
        entry = artifact[key]
        path = directory / entry["path"]
        try:
            relative = str(path.relative_to(output_root))
        except ValueError:
            relative = str(path)
        outputs[relative] = {"path": relative, "sha256": entry["sha256"], "rows": entry["rows"]}
        entry["path"] = relative
    old_inputs = {
        item.get("path"): item
        for item in metadata.get("inputs", [])
        if isinstance(item, dict) and isinstance(item.get("path"), str)
    }
    old_inputs.update({item["path"]: item for item in input_records})
    row_counts = metadata.get("row_counts", {})
    if not isinstance(row_counts, dict):
        row_counts = {}
    row_counts.update(
        {
            "financial_events": artifact["events"]["rows"],
            "financial_facts": artifact["facts"]["rows"],
        }
    )
    metadata.update(
        {
            "ticker": ticker.upper(),
            "generated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "inputs": list(old_inputs.values()),
            "outputs": list(outputs.values()),
            "row_counts": row_counts,
            "cik10": artifact.get("cik10"),
            "raw_input_inventory_sha256": artifact.get("raw_input_inventory_sha256"),
            "financial_events": artifact,
        }
    )
    _write_meta_atomic(meta_path, metadata)
    return meta_path


def write_empty_artifact(
    ticker: str,
    cik10: str | None,
    organized_dir: Path,
    *,
    reason: str,
    calendar: list[date] | None = None,
) -> dict[str, Any]:
    """Create schema-correct empty tables for confirmed no-input cases."""
    directory = organized_dir / "stocks" / ticker.upper()
    artifact = write_tables(directory, [], [])
    no_cik_inventory = [{"kind": "no_cik", "path": None, "status": "no_cik", "sha256": None}]
    artifact.update(
        {
            "complete": True,
            "empty_reason": reason,
            "cik10": normalize_cik(cik10),
            "input_hashes": {},
            "raw_input_inventory": no_cik_inventory,
            "raw_input_inventory_sha256": inventory_fingerprint(cik10, no_cik_inventory),
            "input_resource_status": {
                "manifest": "no_cik",
                "submissions": "no_cik",
                "companyfacts": "no_cik",
            },
            "input_resource_diagnostics": [],
            "calendar": {
                "exchange": "XNYS",
                "first_session": None,
                "last_session": None,
                "mapping_start": None,
                "mapping_end": None,
            },
            "output_calendar": {
                "first_session": min(calendar).isoformat() if calendar else None,
                "last_session": max(calendar).isoformat() if calendar else None,
                "session_count": len(set(calendar or [])),
            },
            "quality_counts": {},
            "rejection_counts": {},
        }
    )
    commit_artifact_meta(directory, ticker, artifact, [])
    return artifact


def organize_artifact(
    cik10: str,
    ticker: str,
    raw_dir: Path,
    organized_dir: Path,
    filings: list[dict[str, Any]],
    fact_payloads: list[dict[str, Any]],
    input_paths: list[Path],
    *,
    output_calendar: list[date] | None = None,
    initial_rejections: dict[str, int] | None = None,
    input_hashes_by_path: dict[Path, str] | None = None,
    raw_input_inventory: list[dict[str, Any]] | None = None,
    input_resource_status: dict[str, str] | None = None,
    input_resource_diagnostics: list[dict[str, Any]] | None = None,
    commit_meta: bool = True,
) -> dict[str, Any]:
    events, facts, rejections, calendar_info = _event_records(
        cik10, ticker, filings, fact_payloads, raw_dir, initial_rejections
    )
    directory = organized_dir / "stocks" / ticker.upper()
    input_records: list[dict[str, str]] = []
    input_hashes: dict[str, str] = {}
    known_hashes = input_hashes_by_path or {}
    for path in sorted(input_paths):
        record_path = _relative_path(path, raw_dir)
        digest = known_hashes.get(path) or _sha256(path)
        input_records.append({"path": record_path, "sha256": digest})
        input_hashes[record_path] = digest
    inventory = list(raw_input_inventory or [])
    fingerprint = inventory_fingerprint(cik10, inventory)
    if input_resource_status is None:
        statuses = {"manifest": "unknown", "submissions": "unknown", "companyfacts": "unknown"}
    else:
        statuses = dict(input_resource_status)
    for required_resource in ("manifest", "submissions", "companyfacts"):
        if required_resource not in statuses or not isinstance(statuses[required_resource], str):
            statuses[required_resource] = "unknown"
    hard_input_states = {"missing", "unreadable", "malformed", "unknown"}
    resource_failure = any(status in hard_input_states for status in statuses.values())
    complete = (
        not bool(rejections.get("effective_session_mapping_failed")) and not resource_failure
    )
    empty_reason = None
    if not events:
        audited_no_usable_reasons = {
            "no_cik_owned_resources",
            "empty_cik_resource_versions",
            "raw_payload_cik_mismatch",
            "no_usable_facts",
            "no_submission_resource_for_cik",
        }
        if audited_no_usable_reasons.intersection(rejections) or any(
            statuses.get(key) in {"misattributed", "no_usable_facts", "no_usable_submissions"}
            for key in ("submissions", "companyfacts")
        ):
            empty_reason = "no_usable_input_for_cik"
        elif statuses.get("submissions") == "no_input":
            empty_reason = "no_submission_inputs"
        else:
            empty_reason = "no_matching_sec_filings"
    artifact_summary: dict[str, Any] = {
        "contract_version": CONTRACT_VERSION,
        "complete": False,
        "status": "organizing",
        "empty_reason": empty_reason,
        "cik10": normalize_cik(cik10),
        "input_hashes": input_hashes,
        "raw_input_inventory": inventory,
        "raw_input_inventory_sha256": fingerprint,
        "input_resource_status": statuses,
        "input_resource_diagnostics": list(input_resource_diagnostics or []),
        "calendar": calendar_info,
        "output_calendar": {
            "first_session": min(output_calendar).isoformat() if output_calendar else None,
            "last_session": max(output_calendar).isoformat() if output_calendar else None,
            "session_count": len(set(output_calendar or [])),
        },
        "quality_counts": calendar_info.get("quality_counts", {}),
        "rejection_counts": rejections,
        "events": {"path": "financial_events.parquet", "rows": len(events)},
        "facts": {"path": "financial_facts.parquet", "rows": len(facts)},
    }
    commit_incomplete_diagnostic(directory, ticker, artifact_summary, input_records)
    try:
        write_tables_result = write_tables(directory, events, facts)
    except Exception as exc:
        artifact_summary.update({"status": "table_write_failed", "write_error": str(exc)})
        commit_incomplete_diagnostic(directory, ticker, artifact_summary, input_records)
        raise

    artifact = dict(artifact_summary)
    artifact["events"] = write_tables_result["events"]
    artifact["facts"] = write_tables_result["facts"]
    artifact["complete"] = complete
    artifact["status"] = "complete" if complete else "incomplete_input_or_mapping"
    if commit_meta:
        commit_artifact_meta(directory, ticker, artifact, input_records)
    else:
        artifact["_input_records"] = input_records
    return artifact


def artifact_is_valid(
    directory: Path,
    metadata: dict[str, Any],
    *,
    raw_dir: Path | None = None,
    expected_cik: str | None | object = _UNSET,
) -> bool:
    """Validate completion identity, input inventory, hashes and table contracts."""
    artifact = metadata.get("financial_events")
    if not isinstance(artifact, dict) or artifact.get("contract_version") != CONTRACT_VERSION:
        return False
    if artifact.get("complete") is not True:
        return False
    artifact_cik = normalize_cik(artifact.get("cik10"))
    metadata_cik = normalize_cik(metadata.get("cik10"))
    if artifact_cik != metadata_cik:
        return False
    if expected_cik is not _UNSET and artifact_cik != normalize_cik(expected_cik):
        return False
    inventory = artifact.get("raw_input_inventory")
    fingerprint = artifact.get("raw_input_inventory_sha256")
    if (
        not isinstance(inventory, list)
        or not all(isinstance(item, dict) for item in inventory)
        or not isinstance(fingerprint, str)
    ):
        return False
    if metadata.get("raw_input_inventory_sha256") != fingerprint:
        return False
    try:
        if inventory_fingerprint(artifact_cik, inventory) != fingerprint:
            return False
    except (TypeError, ValueError):
        return False
    statuses = artifact.get("input_resource_status")
    if (
        not isinstance(statuses, dict)
        or not {"manifest", "submissions", "companyfacts"}.issubset(statuses)
        or any(
            not isinstance(value, str)
            or value in {"missing", "unreadable", "malformed", "unknown"}
            for value in statuses.values()
        )
    ):
        return False
    if raw_dir is not None:
        from .organize_financials import raw_input_inventory

        try:
            current_inventory, current_fingerprint, _ = raw_input_inventory(raw_dir, artifact_cik)
        except (OSError, ValueError, TypeError):
            return False
        if current_fingerprint != fingerprint or current_inventory != inventory:
            return False
    input_hashes = artifact.get("input_hashes")
    if not isinstance(input_hashes, dict):
        return False
    if not isinstance(artifact.get("calendar"), dict) or not isinstance(
        artifact.get("output_calendar"), dict
    ):
        return False
    if not isinstance(artifact.get("quality_counts"), dict) or not isinstance(
        artifact.get("rejection_counts"), dict
    ):
        return False
    outputs = {
        item.get("path"): item
        for item in metadata.get("outputs", [])
        if isinstance(item, dict) and isinstance(item.get("path"), str)
    }
    table_paths: dict[str, Path] = {}
    expected = (("events", EVENT_SCHEMA), ("facts", FACT_SCHEMA))
    for key, schema in expected:
        record = artifact.get(key)
        if not isinstance(record, dict):
            return False
        expected_name = "financial_events.parquet" if key == "events" else "financial_facts.parquet"
        record_path = str(record.get("path", ""))
        if Path(record_path).name != expected_name:
            return False
        output_record = outputs.get(record_path)
        if (
            not isinstance(output_record, dict)
            or output_record.get("sha256") != record.get("sha256")
            or output_record.get("rows") != record.get("rows")
        ):
            return False
        path = directory / expected_name
        table_paths[key] = path
        if not path.is_file():
            return False
        try:
            if _sha256(path) != record.get("sha256"):
                return False
            parquet_file = pq.ParquetFile(path)
            if not parquet_file.schema_arrow.equals(schema, check_metadata=False):
                return False
            if parquet_file.metadata.num_rows != record.get("rows"):
                return False
        except (OSError, ValueError, pa.ArrowException):
            return False

    event_count = int(artifact["events"].get("rows", -1))
    fact_count = int(artifact["facts"].get("rows", -1))
    if event_count == 0 and fact_count != 0:
        return False
    if event_count == 0 and not artifact.get("empty_reason"):
        return False
    try:
        event_rows = pq.read_table(
            table_paths["events"],
            columns=["event_id", "cik10", "filed_date", "effective_visible_session"],
        ).to_pylist()
        event_by_id: dict[str, dict[str, Any]] = {}
        for row in event_rows:
            event_id = row["event_id"]
            if not event_id or event_id in event_by_id:
                return False
            if row["filed_date"] is None or row["effective_visible_session"] is None:
                return False
            if normalize_cik(row["cik10"]) != artifact_cik:
                return False
            event_by_id[event_id] = row
        fact_rows = pq.read_table(
            table_paths["facts"],
            columns=[
                "fact_version_id",
                "event_id",
                "value",
                "unit",
                "report_period_end",
                "filed_date",
                "effective_visible_session",
            ],
        ).to_pylist()
        fact_ids: set[str] = set()
        for row in fact_rows:
            event = event_by_id.get(row["event_id"])
            if event is None or not row["fact_version_id"] or row["fact_version_id"] in fact_ids:
                return False
            fact_ids.add(row["fact_version_id"])
            if (
                row["unit"] != "USD"
                or row["report_period_end"] is None
                or row["filed_date"] != event["filed_date"]
                or row["effective_visible_session"] != event["effective_visible_session"]
                or not math.isfinite(row["value"])
            ):
                return False
    except (OSError, ValueError, pa.ArrowException, TypeError):
        return False
    return True
