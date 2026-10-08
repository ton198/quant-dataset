"""Regression tests for SEC financial input selection and universe caching."""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from download.organize_financials import (
    _CONCEPTS,
    _is_column_oriented,
    _owned_paths,
    _reset_ticker_cache,
    _submission_rows,
    _ticker_for_cik,
    organize_financials,
)


def _financials_root(tmp_path: Path) -> Path:
    root = tmp_path / "raw" / "sec" / "financials"
    root.mkdir(parents=True)
    return root


def _write_company_facts_fixture(
    root: Path,
    filings: list[dict[str, object]],
    facts_by_tag: dict[str, list[dict[str, object]]],
    page_filings: list[dict[str, object]] | None = None,
) -> None:
    """Write small Company Facts/submissions fixtures under the ownership manifest."""
    cik = 42
    facts_path = root / "companyfacts.json"
    submission_path = root / "submissions.json"
    facts = {
        "cik": cik,
        "facts": {
            "us-gaap": {tag: {"units": {"USD": values}} for tag, values in facts_by_tag.items()}
        },
    }
    submission = {
        "cik": cik,
        "filings": {
            "recent": {
                key: [filing.get(key) for filing in filings]
                for key in ("filingDate", "reportDate", "form", "accessionNumber", "fy", "fp")
            }
        },
    }
    facts_path.write_text(json.dumps(facts), encoding="utf-8")
    submission_path.write_text(json.dumps(submission), encoding="utf-8")
    resources = [
        {"logical_key": "companyfacts:0000000042", "path": facts_path.name},
        {"logical_key": "submissions:0000000042", "path": submission_path.name},
    ]
    if page_filings is not None:
        page_path = root / "submissions-page-001.json"
        page = {
            key: [filing.get(key) for filing in page_filings]
            for key in ("filingDate", "reportDate", "form", "accessionNumber")
        }
        page_path.write_text(json.dumps(page), encoding="utf-8")
        resources.append(
            {
                "logical_key": "submissions-page:0000000042:CIK0000000042-submissions-001.json",
                "path": page_path.name,
            }
        )
    (root / "manifest.json").write_text(
        json.dumps({"resources": resources}),
        encoding="utf-8",
    )


def _fact(
    filing: dict[str, object],
    value: float,
    *,
    start: str | None = None,
    fy: int | None = None,
    fp: str | None = None,
) -> dict[str, object]:
    """Build a Company Facts value tied to one accession and report date."""
    fact: dict[str, object] = {
        "filed": filing["filingDate"],
        "end": filing["reportDate"],
        "form": filing["form"],
        "accn": filing["accessionNumber"],
        "val": value,
    }
    if start is not None:
        fact["start"] = start
    if fy is not None:
        fact["fy"] = fy
    if fp is not None:
        fact["fp"] = fp
    return fact


def _artifact_filing(
    filed: str,
    end: str | None,
    accession: str,
    form: str = "10-Q",
    *,
    fy: int | None = None,
    fp: str | None = None,
) -> dict[str, object]:
    filing: dict[str, object] = {
        "filingDate": filed,
        "reportDate": end,
        "form": form,
        "accessionNumber": accession,
    }
    if fy is not None:
        filing["fy"] = fy
    if fp is not None:
        filing["fp"] = fp
    return filing


def _artifact_fact(
    filing: dict[str, object],
    value: object,
    *,
    start: str | None = None,
    fy: int | None = None,
    fp: str | None = None,
    include_accession: bool = True,
    **overrides: object,
) -> dict[str, object]:
    fact: dict[str, object] = {
        "filed": filing["filingDate"],
        "end": filing["reportDate"],
        "form": filing["form"],
        "val": value,
    }
    if include_accession:
        fact["accn"] = filing.get("accessionNumber")
    if start is not None:
        fact["start"] = start
    if fy is not None:
        fact["fy"] = fy
    if fp is not None:
        fact["fp"] = fp
    fact.update(overrides)
    return fact


def _write_artifact_inputs(
    root: Path,
    filings: list[dict[str, object]],
    facts_by_tag: dict[str, dict[str, list[dict[str, object]]]],
    *,
    page_filings: list[dict[str, object]] | None = None,
) -> None:
    """Write SEC cache payloads supporting event/fact artifact contract tests."""
    cik = 42
    facts_path = root / "companyfacts.json"
    submission_path = root / "submissions.json"
    facts = {
        "cik": cik,
        "facts": {
            "us-gaap": {
                tag: {"units": units}
                for tag, units in facts_by_tag.items()
            }
        },
    }
    keys = list(dict.fromkeys(key for filing in filings for key in filing))
    submission = {
        "cik": cik,
        "filings": {
            "recent": {key: [filing.get(key) for filing in filings] for key in keys}
        },
    }
    facts_path.write_text(json.dumps(facts), encoding="utf-8")
    submission_path.write_text(json.dumps(submission), encoding="utf-8")
    resources = [
        {"logical_key": "companyfacts:0000000042", "path": facts_path.name},
        {"logical_key": "submissions:0000000042", "path": submission_path.name},
    ]
    if page_filings is not None:
        page_path = root / "submissions-page-001.json"
        page_keys = list(dict.fromkeys(key for filing in page_filings for key in filing))
        page_path.write_text(
            json.dumps({key: [filing.get(key) for filing in page_filings] for key in page_keys}),
            encoding="utf-8",
        )
        resources.append(
            {
                "logical_key": "submissions-page:0000000042:CIK0000000042-submissions-001.json",
                "path": page_path.name,
            }
        )
    (root / "manifest.json").write_text(json.dumps({"resources": resources}), encoding="utf-8")


def test_owned_paths_manifest_miss_does_not_read_unrelated_json(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A manifest miss only considers filenames and does not read unrelated data."""
    root = _financials_root(tmp_path)
    (root / "manifest.json").write_text(json.dumps({"resources": []}), encoding="utf-8")
    unrelated = [root / f"{index:064x}.json" for index in range(4)]
    for path in unrelated:
        path.write_text('{"cik": 999}', encoding="utf-8")

    read_paths: list[Path] = []
    read_text = Path.read_text

    def tracked_read_text(path: Path, *args, **kwargs) -> str:
        read_paths.append(path)
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", tracked_read_text)

    assert _owned_paths(root, "0000000042") == []
    assert read_paths == [root / "manifest.json"]
    assert not set(unrelated).intersection(read_paths)


def test_owned_paths_fallback_keeps_matching_filename_only(tmp_path: Path) -> None:
    """Files named with the padded CIK remain discoverable without manifest entries."""
    root = _financials_root(tmp_path)
    (root / "manifest.json").write_text(json.dumps({"resources": []}), encoding="utf-8")
    matching = root / "orphan-0000000042-companyfacts.json"
    matching.write_text('{"cik": 42}', encoding="utf-8")
    (root / "unrelated-companyfacts.json").write_text('{"cik": 7}', encoding="utf-8")

    assert _owned_paths(root, "0000000042") == [matching]


def test_missing_sec_financials_still_writes_missing_rows_and_empty_provenance(
    tmp_path: Path,
) -> None:
    """No SEC inputs produce the regular all-missing CSV and no unrelated meta inputs."""
    root = _financials_root(tmp_path)
    (root / "manifest.json").write_text(json.dumps({"resources": []}), encoding="utf-8")
    for index in range(3):
        (root / f"unrelated-{index}.json").write_text('{"cik": 999}', encoding="utf-8")

    ticker_meta = tmp_path / "organized" / "stocks" / "0000000042" / "_meta.json"
    ticker_meta.parent.mkdir(parents=True)
    ticker_meta.write_text(
        json.dumps(
            {
                "inputs": [
                    {
                        "path": "data/raw/sec/financials/previously-unrelated.json",
                        "sha256": "stale",
                    },
                    {"path": "data/raw/yahoo/0000000042/prices.csv", "sha256": "valid"},
                ]
            }
        ),
        encoding="utf-8",
    )

    _reset_ticker_cache()
    output = organize_financials(
        "0000000042", tmp_path / "raw", tmp_path / "organized", [date(2024, 1, 2)]
    )
    frame = pd.read_csv(output)
    metadata = json.loads(output.parent.joinpath("_meta.json").read_text(encoding="utf-8"))

    assert len(frame) == 1
    assert frame.loc[0, "quality_status"] == "missing"
    assert pd.isna(frame.loc[0, "available_as_of"])
    assert pd.isna(frame.loc[0, "fiscal_year"])
    assert pd.isna(frame.loc[0, "fiscal_period"])
    assert metadata["inputs"] == [
        {"path": "data/raw/yahoo/0000000042/prices.csv", "sha256": "valid"}
    ]
    assert metadata["row_counts"]["financials_input"] == 0
    artifact = metadata["financial_events"]
    assert artifact["contract_version"] == "financial_events_v1"
    assert artifact["complete"] is True
    assert artifact["empty_reason"] == "no_submission_inputs"
    assert artifact["input_resource_status"]["submissions"] == "no_input"
    assert artifact["input_resource_status"]["companyfacts"] == "no_input"
    assert artifact["raw_input_inventory_sha256"]
    assert artifact["events"]["rows"] == artifact["facts"]["rows"] == 0
    assert (output.parent / "financial_events.parquet").is_file()
    assert (output.parent / "financial_facts.parquet").is_file()
    assert pd.read_parquet(output.parent / "financial_events.parquet").empty
    assert pd.read_parquet(output.parent / "financial_facts.parquet").empty


def test_ticker_lookup_caches_universe_mapping(tmp_path: Path, monkeypatch) -> None:
    """The first CIK lookup reads universe files; subsequent lookups use the cache."""
    universe = tmp_path / "raw" / "sec" / "universe"
    universe.mkdir(parents=True)
    universe_file = universe / "tickers.json"
    universe_file.write_text(
        json.dumps({"fields": ["cik", "name", "ticker"], "data": [[42, "Example", "exm"]]}),
        encoding="utf-8",
    )

    read_paths: list[Path] = []
    read_text = Path.read_text

    def tracked_read_text(path: Path, *args, **kwargs) -> str:
        if path.parent == universe:
            read_paths.append(path)
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", tracked_read_text)
    _reset_ticker_cache(tmp_path / "raw")

    assert _ticker_for_cik(tmp_path / "raw", "0000000042") == "EXM"
    assert _ticker_for_cik(tmp_path / "raw", "0000000042") == "EXM"
    assert read_paths == [universe_file]

    _reset_ticker_cache(tmp_path / "raw")
    assert _ticker_for_cik(tmp_path / "raw", "0000000042") == "EXM"
    assert read_paths == [universe_file, universe_file]


def test_financial_concept_priority_whitelist_includes_old_and_new_us_gaap_tags() -> None:
    """Documented concept order covers current and legacy Company Facts tags."""
    assert _CONCEPTS["revenue"] == (
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
    )
    assert _CONCEPTS["net_income"] == (
        "NetIncomeLoss",
        "ProfitLoss",
        "NetIncomeLossAvailableToCommonStockholdersBasic",
        "NetIncomeLossAvailableToCommonStockholdersDiluted",
    )
    assert _CONCEPTS["operating_income"] == ("OperatingIncomeLoss",)
    assert _CONCEPTS["assets"] == ("Assets", "AssetsNet")


def test_company_facts_priority_uses_new_and_legacy_concepts_and_emits_periods(
    tmp_path: Path,
) -> None:
    """Current tags win and legacy tags fill gaps with fact-reported periods."""
    root = _financials_root(tmp_path)
    first = {
        "filingDate": "2020-01-31",
        "reportDate": "2019-12-28",
        "form": "10-Q",
        "accessionNumber": "first",
        "fy": None,
        "fp": None,
    }
    middle = {
        "filingDate": "2020-05-01",
        "reportDate": "2020-03-28",
        "form": "10-Q",
        "accessionNumber": "middle",
        "fy": None,
        "fp": None,
    }
    second = {
        "filingDate": "2021-01-30",
        "reportDate": "2020-10-03",
        "form": "10-K",
        "accessionNumber": "second",
        "fy": None,
        "fp": None,
    }
    facts = {
        "Revenues": [_fact(first, 120, start="2019-09-29", fy=2020, fp="Q1")],
        "RevenueFromContractWithCustomerExcludingAssessedTax": [
            _fact(first, 110, start="2019-09-29", fy=2020, fp="Q1"),
        ],
        "SalesRevenueGoodsNet": [_fact(middle, 250, start="2019-12-29", fy=2020, fp="Q2")],
        "SalesRevenueNet": [_fact(second, 500, start="2019-09-29", fy=2020, fp="FY")],
        "NetIncomeLoss": [_fact(first, 20, start="2019-09-29", fy=2020, fp="Q1")],
        "ProfitLoss": [
            _fact(first, 19, start="2019-09-29", fy=2020, fp="Q1"),
            _fact(middle, 39, start="2019-12-29", fy=2020, fp="Q2"),
        ],
        "NetIncomeLossAvailableToCommonStockholdersBasic": [
            _fact(second, 50, start="2019-09-29", fy=2020, fp="FY"),
        ],
        "OperatingIncomeLoss": [
            _fact(first, 15, start="2019-09-29", fy=2020, fp="Q1"),
            _fact(middle, 35, start="2019-12-29", fy=2020, fp="Q2"),
            _fact(second, 40, start="2019-09-29", fy=2020, fp="FY"),
        ],
        "Assets": [_fact(first, 300, fy=2020, fp="Q1")],
        "AssetsNet": [
            _fact(middle, 600, fy=2020, fp="Q2"),
            _fact(second, 700, fy=2020, fp="FY"),
        ],
    }
    _write_company_facts_fixture(root, [first, middle, second], facts)

    output = organize_financials(
        "0000000042",
        tmp_path / "raw",
        tmp_path / "organized",
        [date(2020, 2, 1), date(2020, 5, 2), date(2021, 2, 1)],
        output_ticker="TEST",
    )
    frame = pd.read_csv(output)

    assert frame.loc[0, "revenue"] == 120
    assert frame.loc[0, "net_income"] == 20
    assert frame.loc[0, "operating_income"] == 15
    assert frame.loc[0, "assets"] == 300
    assert frame.loc[0, "fiscal_year"] == 2020
    assert frame.loc[0, "fiscal_period"] == "Q1"
    assert frame.loc[1, "revenue"] == 250
    assert frame.loc[1, "net_income"] == 39
    assert frame.loc[1, "operating_income"] == 35
    assert frame.loc[1, "assets"] == 600
    assert frame.loc[1, "fiscal_year"] == 2020
    assert frame.loc[1, "fiscal_period"] == "Q2"
    assert frame.loc[2, "revenue"] == 500
    assert frame.loc[2, "net_income"] == 50
    assert frame.loc[2, "operating_income"] == 40
    assert frame.loc[2, "assets"] == 700
    assert frame.loc[2, "fiscal_year"] == 2020
    assert frame.loc[2, "fiscal_period"] == "FY"


def test_fiscal_period_fallback_uses_reported_apple_style_fiscal_quarters(
    tmp_path: Path,
) -> None:
    """When facts omit fy/fp, 10-K dates anchor the non-calendar fiscal year."""
    root = _financials_root(tmp_path)
    annual = {
        "filingDate": "2024-11-01",
        "reportDate": "2024-09-28",
        "form": "10-K",
        "accessionNumber": "annual",
        "fy": None,
        "fp": None,
    }
    quarter = {
        "filingDate": "2025-01-31",
        "reportDate": "2024-12-28",
        "form": "10-Q",
        "accessionNumber": "quarter",
        "fy": None,
        "fp": None,
    }
    _write_company_facts_fixture(
        root,
        [annual, quarter],
        {
            "SalesRevenueNet": [
                _fact(annual, 100, start="2023-10-01"),
                _fact(quarter, 25, start="2024-09-29"),
                _fact(quarter, 75, start="2023-09-30"),
            ],
        },
    )

    output = organize_financials(
        "0000000042",
        tmp_path / "raw",
        tmp_path / "organized",
        [date(2024, 11, 2), date(2025, 2, 2)],
        output_ticker="TEST",
    )
    frame = pd.read_csv(output)

    assert frame["revenue"].tolist() == [100, 25]
    assert frame["fiscal_year"].tolist() == [2024, 2025]
    assert frame["fiscal_period"].tolist() == ["FY", "Q1"]
    assert "fiscal_year" in frame.columns
    assert "fiscal_period" in frame.columns

    _reset_ticker_cache(tmp_path / "raw")


def test_submission_rows_parses_recent_table_and_column_page_shapes() -> None:
    """All three raw submission payload shapes assemble into filing row dicts."""
    recent = {
        "filings": {
            "recent": {
                "accessionNumber": ["r-1", "r-2"],
                "filingDate": ["2024-02-01", "2024-05-01"],
                "reportDate": ["2023-12-31", "2024-03-31"],
                "form": ["10-K", "10-Q"],
                "fy": [2023, 2024],
                "fp": ["FY", "Q1"],
            }
        }
    }
    table = {
        "fields": ["accessionNumber", "filingDate", "reportDate", "form"],
        "data": [["t-1", "2010-02-26", "2009-12-31", "10-K"]],
    }
    page = {
        "accessionNumber": ["p-1", "p-2"],
        "filingDate": ["2005-09-26", "2006-03-01"],
        "reportDate": ["2005-09-23", "2005-12-31"],
        "form": ["4", "10-K"],
        "size": [10025, 42],
    }

    recent_rows = _submission_rows(recent)
    assert [row["accessionNumber"] for row in recent_rows] == ["r-1", "r-2"]
    assert recent_rows[1]["filingDate"] == "2024-05-01"
    assert recent_rows[0]["fy"] == 2023

    assert _submission_rows(table) == [
        {
            "accessionNumber": "t-1",
            "filingDate": "2010-02-26",
            "reportDate": "2009-12-31",
            "form": "10-K",
        }
    ]

    page_rows = _submission_rows(page)
    assert [row["accessionNumber"] for row in page_rows] == ["p-1", "p-2"]
    assert page_rows[0]["filingDate"] == "2005-09-26"
    assert page_rows[0]["reportDate"] == "2005-09-23"
    assert page_rows[1]["filingDate"] == "2006-03-01"
    assert page_rows[1]["form"] == "10-K"
    assert page_rows[1]["size"] == 42
    assert _is_column_oriented(page)
    assert not _is_column_oriented(recent)
    assert not _is_column_oriented(table)


def test_column_oriented_payload_rejects_ragged_arrays() -> None:
    """Mismatched parallel arrays are rejected instead of fabricating rows."""
    assert _submission_rows({"accessionNumber": ["a", "b"], "filingDate": ["2020-01-01"]}) == []
    assert (
        _submission_rows(
            {
                "accessionNumber": ["a"],
                "filingDate": ["2020-01-01"],
                "form": [],
            }
        )
        == []
    )
    assert _submission_rows({"accessionNumber": [], "filingDate": []}) == []


def test_page_only_old_filing_fact_becomes_available_at_its_filing_date(
    tmp_path: Path,
) -> None:
    """A fact reachable only through a historical page appears on the as-of date."""
    root = _financials_root(tmp_path)
    old = {
        "filingDate": "2010-02-26",
        "reportDate": "2009-12-31",
        "form": "10-K",
        "accessionNumber": "old-annual",
    }  # type: dict[str, object]
    _write_company_facts_fixture(
        root,
        [],
        {
            "Revenues": [_fact(old, 500, start="2009-01-01", fy=2009, fp="FY")],
        },
        page_filings=[old],
    )

    output = organize_financials(
        "0000000042",
        tmp_path / "raw",
        tmp_path / "organized",
        [date(2010, 1, 4), date(2010, 3, 1)],
        output_ticker="TEST",
    )
    frame = pd.read_csv(output)
    metadata = json.loads(output.parent.joinpath("_meta.json").read_text(encoding="utf-8"))

    assert pd.isna(frame.loc[0, "revenue"])
    assert frame.loc[1, "revenue"] == 500
    assert frame.loc[1, "available_as_of"] == "2010-02-26"
    assert frame.loc[1, "report_period_end"] == "2009-12-31"
    assert frame.loc[1, "fiscal_year"] == 2009
    assert frame.loc[1, "fiscal_period"] == "FY"
    assert metadata["row_counts"]["financials_input"] == 1


def test_overlapping_page_rows_dedupe_prefers_recent_submissions(
    tmp_path: Path,
) -> None:
    """A page duplicate of a recent accession never overrides the recent entry."""
    root = _financials_root(tmp_path)
    annual = {
        "filingDate": "2010-02-26",
        "reportDate": "2009-12-31",
        "form": "10-K",
        "accessionNumber": "same-annual",
        "fy": 2009,
        "fp": "FY",
    }  # type: dict[str, object]
    other = {
        "filingDate": "2006-03-01",
        "reportDate": "2005-12-31",
        "form": "10-K",
        "accessionNumber": "page-only",
    }  # type: dict[str, object]
    duplicate_from_page = {
        "filingDate": "2010-02-26",
        "reportDate": "2010-01-01",
        "form": "10-K",
        "accessionNumber": "same-annual",
    }  # type: dict[str, object]
    _write_company_facts_fixture(
        root,
        [annual],
        {
            "Revenues": [_fact(annual, 500, start="2009-01-01", fy=2009, fp="FY")],
        },
        page_filings=[duplicate_from_page, other],
    )

    output = organize_financials(
        "0000000042",
        tmp_path / "raw",
        tmp_path / "organized",
        [date(2010, 3, 1)],
        output_ticker="TEST",
    )
    frame = pd.read_csv(output)
    metadata = json.loads(output.parent.joinpath("_meta.json").read_text(encoding="utf-8"))

    assert metadata["row_counts"]["financials_input"] == 2
    assert frame.loc[0, "revenue"] == 500
    assert frame.loc[0, "report_period_end"] == "2009-12-31"
    assert frame.loc[0, "fiscal_year"] == 2009

    _reset_ticker_cache(tmp_path / "raw")


def test_event_artifacts_restore_covered_and_page_only_filings(tmp_path: Path) -> None:
    """All accession events survive daily snapshot replacement and page overlap."""
    root = _financials_root(tmp_path)
    annual = _artifact_filing("2020-02-01", "2019-12-31", "annual", "10-K", fy=2019, fp="FY")
    same_day = _artifact_filing("2020-02-01", "2018-12-31", "same-day", "10-K", fy=2018, fp="FY")
    same_day_nonfinancial = _artifact_filing("2020-02-01", None, "z-same-day", "8-K")
    later_nonfinancial = _artifact_filing("2020-02-02", None, "later-nonfinancial", "4")
    old = _artifact_filing("2019-02-01", "2018-12-31", "page-only", "10-K", fy=2018, fp="FY")
    stale_duplicate = _artifact_filing(
        "2020-02-01", "2018-12-31", "annual", "10-K", fy=2018, fp="FY"
    )
    _write_artifact_inputs(
        root,
        [annual, same_day, same_day_nonfinancial, later_nonfinancial],
        {
            "Revenues": {
                "USD": [
                    _artifact_fact(annual, 900, start="2019-01-01", fy=2019, fp="FY"),
                    _artifact_fact(old, 800, start="2018-01-01", fy=2018, fp="FY"),
                ]
            }
        },
        page_filings=[stale_duplicate, old],
    )

    output = organize_financials(
        "0000000042", tmp_path / "raw", tmp_path / "organized", [date(2020, 2, 3)], output_ticker="TEST"
    )
    directory = output.parent
    events = pd.read_parquet(directory / "financial_events.parquet")
    facts = pd.read_parquet(directory / "financial_facts.parquet")
    daily = pd.read_csv(output)

    assert set(events["accession_number"].dropna()) == {
        "annual", "same-day", "z-same-day", "later-nonfinancial", "page-only"
    }
    assert len(events[events["filed_date"] == date(2020, 2, 1)]) == 3
    annual_event = events.loc[events["accession_number"] == "annual"].iloc[0]
    assert "submissions.json" in annual_event["source_submission_path"]
    assert annual_event["report_period_end"] == date(2019, 12, 31)
    assert "submissions-page" in events.loc[
        events["accession_number"] == "page-only", "source_submission_path"
    ].iloc[0]
    assert facts.loc[facts["event_id"] == annual_event["event_id"], "value"].tolist() == [900.0]
    assert pd.isna(daily.loc[0, "revenue"])
    assert daily.loc[0, "accession_number"] == "later-nonfinancial"

    meta = json.loads((directory / "_meta.json").read_text(encoding="utf-8"))
    artifact = meta["financial_events"]
    assert artifact["contract_version"] == "financial_events_v1"
    assert artifact["complete"] is True
    assert artifact["events"]["rows"] == 5
    assert artifact["facts"]["rows"] == 2
    assert artifact["events"]["sha256"]
    assert artifact["input_hashes"]
    assert artifact["calendar"]["mapping_start"] == "2019-02-01"
    assert artifact["calendar"]["mapping_end"] == "2020-02-16"
    assert artifact["output_calendar"]["last_session"] == "2020-02-03"
    assert artifact["calendar"]["mapping_end"] > artifact["output_calendar"]["last_session"]


def test_effective_session_is_strictly_after_weekends_and_xnys_holiday(tmp_path: Path) -> None:
    """The filing date, weekends and exchange holidays never become visibility dates."""
    root = _financials_root(tmp_path)
    filings = [
        _artifact_filing("2020-01-15", None, "wed", "8-K"),
        _artifact_filing("2020-01-17", None, "fri", "8-K"),
        _artifact_filing("2020-01-18", None, "sat", "8-K"),
        _artifact_filing("2020-01-20", None, "holiday", "8-K"),
    ]
    _write_artifact_inputs(root, filings, {})

    output = organize_financials(
        "0000000042",
        tmp_path / "raw",
        tmp_path / "organized",
        [date(2020, 1, 15), date(2020, 1, 16), date(2020, 1, 21)],
        output_ticker="TEST",
    )
    events = pd.read_parquet(output.parent / "financial_events.parquet").set_index("accession_number")

    assert events.loc["wed", "effective_visible_session"] == date(2020, 1, 16)
    assert events.loc["fri", "effective_visible_session"] == date(2020, 1, 21)
    assert events.loc["sat", "effective_visible_session"] == date(2020, 1, 21)
    assert events.loc["holiday", "effective_visible_session"] == date(2020, 1, 21)
    assert pd.isna(events.loc["wed", "report_period_end"])


def test_amendment_versions_are_independent_and_amendment_only_facts_survive(
    tmp_path: Path,
) -> None:
    """Finite, empty and amendment-only filings never overwrite prior fact versions."""
    root = _financials_root(tmp_path)
    original = _artifact_filing("2020-02-01", "2019-12-31", "original", "10-K", fy=2019, fp="FY")
    amendment = _artifact_filing("2020-03-01", "2019-12-31", "amendment", "10-K/A", fy=2019, fp="FY")
    empty_amendment = _artifact_filing(
        "2020-04-01", "2019-12-31", "empty-amendment", "10-K/A", fy=2019, fp="FY"
    )
    amendment_only = _artifact_filing(
        "2020-05-01", "2018-12-31", "amendment-only", "10-K/A", fy=2018, fp="FY"
    )
    _write_artifact_inputs(
        root,
        [original, amendment, empty_amendment, amendment_only],
        {
            "Revenues": {
                "USD": [
                    _artifact_fact(original, 100, start="2019-01-01", fy=2019, fp="FY"),
                    _artifact_fact(amendment, 110, start="2019-01-01", fy=2019, fp="FY"),
                    _artifact_fact(amendment_only, 80, start="2018-01-01", fy=2018, fp="FY"),
                ]
            }
        },
    )

    output = organize_financials(
        "0000000042", tmp_path / "raw", tmp_path / "organized", [date(2020, 5, 4)], output_ticker="TEST"
    )
    events = pd.read_parquet(output.parent / "financial_events.parquet").set_index("accession_number")
    facts = pd.read_parquet(output.parent / "financial_facts.parquet")

    assert events.loc["original", "quality_status"] == "ok"
    assert events.loc["amendment", "quality_status"] == "ok"
    assert events.loc["empty-amendment", "quality_status"] == "missing"
    assert events.loc["amendment-only", "quality_status"] == "amendment_only"
    assert facts["value"].tolist() == [100.0, 110.0, 80.0]
    assert not facts["event_id"].str.endswith(":empty-amendment").any()
    amendment_only_event = events.loc["amendment-only", "event_id"]
    assert facts.loc[facts["event_id"] == amendment_only_event, "value"].item() == 80.0


def test_period_classification_finite_tag_priority_units_and_conflicts(tmp_path: Path) -> None:
    """Finite USD facts win across tags and period classes retain auditable semantics."""
    root = _financials_root(tmp_path)
    quarter = _artifact_filing("2020-07-30", "2020-06-30", "q2", "10-Q", fy=2020, fp="Q2")
    annual = _artifact_filing("2021-02-10", "2020-12-31", "fy", "10-K", fy=2020, fp="FY")
    quarter_start = "2020-04-01"
    q_ytd_start = "2020-01-01"
    facts = {
        "Revenues": {
            "USD": [_artifact_fact(quarter, float("inf"), start=quarter_start, fy=2020, fp="Q2")]
        },
        "SalesRevenueNet": {
            "USD": [
                _artifact_fact(
                    quarter, 200, start=quarter_start, fy=2020, fp="Q2", include_accession=False
                ),
                _artifact_fact(quarter, 350, start=q_ytd_start, fy=2020, fp="Q2"),
                _artifact_fact(annual, 1000, start="2020-01-01", fy=2020, fp="FY"),
            ],
            "EUR": [_artifact_fact(quarter, 999, start=quarter_start, fy=2020, fp="Q2")],
        },
        "Assets": {
            "USD": [
                _artifact_fact(quarter, 500, fy=2020, fp="Q2"),
                _artifact_fact(quarter, 501, start=quarter_start, fy=2020, fp="Q2"),
            ]
        },
        "OperatingIncomeLoss": {
            "USD": [
                _artifact_fact(quarter, 20, start=quarter_start, fy=2020, fp="Q2"),
                _artifact_fact(quarter, 21, start=quarter_start, fy=2020, fp="Q2"),
            ]
        },
    }
    _write_artifact_inputs(root, [quarter, annual], facts)

    output = organize_financials(
        "0000000042", tmp_path / "raw", tmp_path / "organized", [date(2021, 2, 11)], output_ticker="TEST"
    )
    facts_out = pd.read_parquet(output.parent / "financial_facts.parquet")
    meta = json.loads((output.parent / "_meta.json").read_text(encoding="utf-8"))
    q_facts = facts_out.loc[facts_out["event_id"].str.endswith(":q2")]
    revenue = q_facts.loc[q_facts["concept"] == "revenue"]
    assets = q_facts.loc[q_facts["concept"] == "assets"]

    assert revenue.loc[revenue["period_start"] == date(2020, 4, 1), "value"].item() == 200.0
    assert revenue.loc[revenue["period_start"] == date(2020, 4, 1), "tag"].item() == "SalesRevenueNet"
    assert revenue.loc[revenue["period_start"] == date(2020, 4, 1), "match_method"].item() == "unique_filed_end_form"
    assert revenue.loc[revenue["period_start"] == date(2020, 4, 1), "period_kind"].item() == "quarter"
    assert revenue.loc[revenue["period_start"] == date(2020, 4, 1), "unit"].item() == "USD"
    assert revenue.loc[revenue["period_start"] == date(2020, 4, 1), "source_fact_locator"].str.endswith("/0").any()
    assert revenue.loc[revenue["period_start"] == date(2020, 1, 1), "period_kind"].item() == "unknown"
    assert revenue.loc[revenue["period_start"] == date(2020, 1, 1), "duration_days"].item() == 182
    assert assets["period_kind"].tolist() == ["instant"]
    assert facts_out.loc[
        facts_out["event_id"].str.endswith(":fy") & (facts_out["concept"] == "revenue"),
        "period_kind",
    ].item() == "annual"
    assert not (q_facts["concept"] == "operating_income").any()
    assert meta["financial_events"]["rejection_counts"]["non_finite_value"] == 1
    assert meta["financial_events"]["rejection_counts"]["non_usd_unit"] == 1
    assert meta["financial_events"]["rejection_counts"]["instant_fact_has_duration"] == 1
    assert meta["financial_events"]["rejection_counts"]["conflicting_fact_versions"] == 1


def test_ambiguous_accessionless_fact_is_rejected_and_counted(tmp_path: Path) -> None:
    """A fact missing accession is rejected when filed/end/form maps to two events."""
    root = _financials_root(tmp_path)
    one = _artifact_filing("2020-07-30", "2020-06-30", "one", "10-Q", fy=2020, fp="Q2")
    two = _artifact_filing("2020-07-30", "2020-06-30", "two", "10-Q", fy=2020, fp="Q2")
    _write_artifact_inputs(
        root,
        [one, two],
        {
            "Revenues": {
                "USD": [
                    _artifact_fact(
                        one, 100, start="2020-04-01", fy=2020, fp="Q2", include_accession=False
                    )
                ]
            }
        },
    )

    output = organize_financials(
        "0000000042", tmp_path / "raw", tmp_path / "organized", [date(2020, 7, 31)], output_ticker="TEST"
    )
    facts = pd.read_parquet(output.parent / "financial_facts.parquet")
    meta = json.loads((output.parent / "_meta.json").read_text(encoding="utf-8"))

    assert facts.empty
    assert meta["financial_events"]["complete"] is True
    assert meta["financial_events"]["rejection_counts"]["fact_missing_accession_ambiguous"] == 1


def test_event_artifact_rebuild_is_deterministic_and_has_no_generation_rows(tmp_path: Path) -> None:
    """Repeated offline organization yields identical parquet bytes and semantic rows."""
    import hashlib

    root = _financials_root(tmp_path)
    annual = _artifact_filing("2020-02-01", "2019-12-31", "annual", "10-K", fy=2019, fp="FY")
    _write_artifact_inputs(
        root,
        [annual],
        {"Revenues": {"USD": [_artifact_fact(annual, 500, start="2019-01-01", fy=2019, fp="FY")]}},
    )
    organized = tmp_path / "organized"
    output = organize_financials("0000000042", tmp_path / "raw", organized, [date(2020, 2, 3)], output_ticker="TEST")
    event_path = output.parent / "financial_events.parquet"
    fact_path = output.parent / "financial_facts.parquet"
    first_hashes = [hashlib.sha256(path.read_bytes()).hexdigest() for path in (event_path, fact_path)]
    first_meta = json.loads((output.parent / "_meta.json").read_text(encoding="utf-8"))

    organize_financials("0000000042", tmp_path / "raw", organized, [date(2020, 2, 3)], output_ticker="TEST")
    second_hashes = [hashlib.sha256(path.read_bytes()).hexdigest() for path in (event_path, fact_path)]
    events = pd.read_parquet(event_path)
    facts = pd.read_parquet(fact_path)

    assert first_hashes == second_hashes
    assert "generated_at_utc" not in events.columns
    assert "generated_at_utc" not in facts.columns
    assert first_meta["financial_events"]["events"]["rows"] == 1
    assert facts["value"].tolist() == [500.0]


def test_ragged_submission_page_is_rejected_and_diagnosed(tmp_path: Path) -> None:
    """Malformed page arrays do not fabricate filings and are counted in metadata."""
    root = _financials_root(tmp_path)
    filing = _artifact_filing("2020-02-01", None, "recent", "8-K")
    _write_artifact_inputs(root, [filing], {}, page_filings=[filing])
    page_path = root / "submissions-page-001.json"
    page_path.write_text(
        json.dumps(
            {
                "accessionNumber": ["page-a", "page-b"],
                "filingDate": ["2019-02-01"],
                "reportDate": ["2018-12-31", "2019-12-31"],
                "form": ["10-K", "10-K"],
            }
        ),
        encoding="utf-8",
    )

    output = organize_financials(
        "0000000042", tmp_path / "raw", tmp_path / "organized", [date(2020, 2, 3)], output_ticker="TEST"
    )
    directory = output.parent
    events = pd.read_parquet(directory / "financial_events.parquet")
    meta = json.loads((directory / "_meta.json").read_text(encoding="utf-8"))

    assert events["accession_number"].tolist() == ["recent"]
    assert meta["financial_events"]["complete"] is True
    assert meta["financial_events"]["input_resource_status"]["submissions"] == "ok"
    assert meta["financial_events"]["rejection_counts"]["ragged_submission_page"] == 1


def test_failed_xnys_mapping_is_diagnostic_and_never_complete(tmp_path: Path, monkeypatch) -> None:
    """A calendar mapping failure persists an incomplete artifact diagnostic."""
    from download import financial_events

    root = _financials_root(tmp_path)
    filing = _artifact_filing("2020-02-01", None, "event", "8-K")
    _write_artifact_inputs(root, [filing], {})

    def fail_calendar(*args, **kwargs):
        raise RuntimeError("calendar unavailable")

    monkeypatch.setattr(financial_events.xcals, "get_calendar", fail_calendar)
    with pytest.raises(ValueError, match="effective_session_mapping_failed"):
        organize_financials(
            "0000000042", tmp_path / "raw", tmp_path / "organized", [date(2020, 2, 3)], output_ticker="TEST"
        )

    meta = json.loads(
        (tmp_path / "organized" / "stocks" / "TEST" / "_meta.json").read_text(encoding="utf-8")
    )
    artifact = meta["financial_events"]
    assert artifact["complete"] is False
    assert artifact["rejection_counts"]["effective_session_mapping_failed"] == 1
    assert artifact["calendar"]["mapping_error"] == "calendar unavailable"


def test_missing_accession_event_has_stable_source_id_and_future_filing_is_not_anchor(
    tmp_path: Path,
) -> None:
    """Missing accessions stay locatable; a future annual filing cannot key history."""
    root = _financials_root(tmp_path)
    earlier = _artifact_filing("2020-05-01", "2020-03-31", "temporary", "10-Q")
    earlier.pop("accessionNumber")
    later = _artifact_filing("2021-02-10", "2019-12-31", "later-annual", "10-K", fy=2020, fp="FY")
    _write_artifact_inputs(
        root,
        [earlier, later],
        {
            "Revenues": {
                "USD": [
                    _artifact_fact(earlier, 100, start="2020-01-01", include_accession=False),
                    _artifact_fact(later, 500, start="2020-01-01", fy=2020, fp="FY"),
                ]
            }
        },
    )

    output = organize_financials(
        "0000000042", tmp_path / "raw", tmp_path / "organized", [date(2021, 2, 11)], output_ticker="TEST"
    )
    events = pd.read_parquet(output.parent / "financial_events.parquet")
    facts = pd.read_parquet(output.parent / "financial_facts.parquet")
    meta = json.loads((output.parent / "_meta.json").read_text(encoding="utf-8"))
    earlier_event = events.loc[events["filed_date"] == date(2020, 5, 1)].iloc[0]

    assert pd.isna(earlier_event["accession_number"])
    assert earlier_event["event_id"].startswith("0000000042:missing:")
    assert earlier_event["source_submission_sha256"]
    assert earlier_event["source_submission_locator"] == "record[0]"
    assert pd.isna(earlier_event["fiscal_year"])
    assert pd.isna(earlier_event["fiscal_period"])
    earlier_fact = facts.loc[facts["event_id"] == earlier_event["event_id"]].iloc[0]
    assert earlier_fact["match_method"] == "unique_filed_end_form"
    assert meta["financial_events"]["rejection_counts"]["missing_accession_number"] == 1


def test_corrupt_submissions_is_incomplete_not_confirmed_no_data(tmp_path: Path) -> None:
    """An unreadable required submissions payload cannot publish a complete empty artifact."""
    root = _financials_root(tmp_path)
    filing = _artifact_filing("2020-02-01", None, "event", "8-K")
    _write_artifact_inputs(root, [filing], {})
    (root / "submissions.json").write_text("{broken", encoding="utf-8")

    with pytest.raises(ValueError, match="financial-events artifact is incomplete"):
        organize_financials(
            "0000000042", tmp_path / "raw", tmp_path / "organized", [date(2020, 2, 3)], output_ticker="TEST"
        )

    directory = tmp_path / "organized" / "stocks" / "TEST"
    meta = json.loads((directory / "_meta.json").read_text(encoding="utf-8"))
    artifact = meta["financial_events"]
    assert artifact["complete"] is False
    assert artifact["input_resource_status"]["submissions"] == "unreadable"
    assert artifact["rejection_counts"]["unreadable_raw_payload"] == 1
    assert artifact["events"]["rows"] == artifact["facts"]["rows"] == 0
    assert pd.read_parquet(directory / "financial_events.parquet").empty


def test_corrupt_companyfacts_is_incomplete_not_confirmed_empty_facts(tmp_path: Path) -> None:
    """An unreadable facts payload leaves its filing visible but blocks completion."""
    root = _financials_root(tmp_path)
    filing = _artifact_filing("2020-02-01", "2019-12-31", "annual", "10-K", fy=2019, fp="FY")
    _write_artifact_inputs(
        root,
        [filing],
        {"Revenues": {"USD": [_artifact_fact(filing, 50, start="2019-01-01", fy=2019, fp="FY")] }},
    )
    (root / "companyfacts.json").write_text("not-json", encoding="utf-8")

    with pytest.raises(ValueError, match="financial-events artifact is incomplete"):
        organize_financials(
            "0000000042", tmp_path / "raw", tmp_path / "organized", [date(2020, 2, 3)], output_ticker="TEST"
        )

    directory = tmp_path / "organized" / "stocks" / "TEST"
    meta = json.loads((directory / "_meta.json").read_text(encoding="utf-8"))
    artifact = meta["financial_events"]
    assert artifact["complete"] is False
    assert artifact["input_resource_status"]["submissions"] == "ok"
    assert artifact["input_resource_status"]["companyfacts"] == "unreadable"
    assert artifact["rejection_counts"]["unreadable_raw_payload"] == 1
    assert pd.read_parquet(directory / "financial_events.parquet")["accession_number"].tolist() == ["annual"]
    assert pd.read_parquet(directory / "financial_facts.parquet").empty


def test_malformed_companyfacts_shape_is_incomplete(tmp_path: Path) -> None:
    """A decoded but structurally invalid Company Facts document is not no-facts."""
    root = _financials_root(tmp_path)
    filing = _artifact_filing("2020-02-01", "2019-12-31", "annual", "10-K", fy=2019, fp="FY")
    _write_artifact_inputs(root, [filing], {})
    (root / "companyfacts.json").write_text(
        json.dumps({"cik": 42, "facts": []}), encoding="utf-8"
    )

    with pytest.raises(ValueError, match="financial-events artifact is incomplete"):
        organize_financials(
            "0000000042", tmp_path / "raw", tmp_path / "organized", [date(2020, 2, 3)], output_ticker="TEST"
        )

    directory = tmp_path / "organized" / "stocks" / "TEST"
    artifact = json.loads((directory / "_meta.json").read_text(encoding="utf-8"))["financial_events"]
    assert artifact["complete"] is False
    assert artifact["input_resource_status"]["companyfacts"] == "malformed"
    assert artifact["rejection_counts"]["invalid_companyfacts_payload_shape"] == 1
    assert pd.read_parquet(directory / "financial_events.parquet")["accession_number"].tolist() == ["annual"]
    assert pd.read_parquet(directory / "financial_facts.parquet").empty


def test_no_companyfacts_payload_is_a_valid_empty_facts_state(tmp_path: Path) -> None:
    """A valid submissions payload can complete when Company Facts is absent."""
    root = _financials_root(tmp_path)
    filing = _artifact_filing("2020-02-01", "2019-12-31", "annual", "10-K", fy=2019, fp="FY")
    _write_artifact_inputs(root, [filing], {})
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["resources"] = [
        item for item in manifest["resources"] if not item["logical_key"].startswith("companyfacts:")
    ]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    (root / "companyfacts.json").unlink()

    output = organize_financials(
        "0000000042", tmp_path / "raw", tmp_path / "organized", [date(2020, 2, 3)], output_ticker="TEST"
    )
    directory = output.parent
    artifact = json.loads((directory / "_meta.json").read_text(encoding="utf-8"))["financial_events"]
    assert artifact["complete"] is True
    assert artifact["input_resource_status"]["submissions"] == "ok"
    assert artifact["input_resource_status"]["companyfacts"] == "no_input"
    assert pd.read_parquet(directory / "financial_events.parquet")["accession_number"].tolist() == ["annual"]
    assert pd.read_parquet(directory / "financial_facts.parquet").empty


def test_missing_manifest_payload_is_incomplete_and_diagnosed(tmp_path: Path) -> None:
    """A manifest reference to a deleted raw payload is not filtered into no-data."""
    root = _financials_root(tmp_path)
    filing = _artifact_filing("2020-02-01", None, "event", "8-K")
    _write_artifact_inputs(root, [filing], {})
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for record in manifest["resources"]:
        if record["logical_key"].startswith("companyfacts:"):
            record["path"] = "missing-companyfacts.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="financial-events artifact is incomplete"):
        organize_financials(
            "0000000042", tmp_path / "raw", tmp_path / "organized", [date(2020, 2, 3)], output_ticker="TEST"
        )

    directory = tmp_path / "organized" / "stocks" / "TEST"
    artifact = json.loads((directory / "_meta.json").read_text(encoding="utf-8"))["financial_events"]
    assert artifact["complete"] is False
    assert artifact["input_resource_status"]["companyfacts"] == "missing"
    assert artifact["rejection_counts"]["missing_manifest_resource"] == 1
    assert any(
        item["kind"] == "companyfacts" and item["status"] == "missing"
        for item in artifact["input_resource_diagnostics"]
    )


def test_valid_empty_submission_payload_is_confirmed_no_data(tmp_path: Path) -> None:
    """A well-formed empty SEC response is distinguishable from a broken payload."""
    root = _financials_root(tmp_path)
    facts_path = root / "companyfacts.json"
    submissions_path = root / "submissions.json"
    facts_path.write_text(json.dumps({"cik": 42, "facts": {"us-gaap": {}}}), encoding="utf-8")
    submissions_path.write_text(
        json.dumps(
            {
                "cik": 42,
                "filings": {
                    "recent": {
                        "accessionNumber": [],
                        "filingDate": [],
                        "reportDate": [],
                        "form": [],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "resources": [
                    {"logical_key": "companyfacts:0000000042", "path": facts_path.name},
                    {"logical_key": "submissions:0000000042", "path": submissions_path.name},
                ]
            }
        ),
        encoding="utf-8",
    )

    output = organize_financials(
        "0000000042", tmp_path / "raw", tmp_path / "organized", [date(2020, 2, 3)], output_ticker="TEST"
    )
    artifact = json.loads(output.with_name("_meta.json").read_text(encoding="utf-8"))["financial_events"]
    assert artifact["complete"] is True
    assert artifact["empty_reason"] == "no_matching_sec_filings"
    assert artifact["input_resource_status"]["submissions"] == "ok"
    assert artifact["input_resource_status"]["companyfacts"] == "ok"
    assert artifact["events"]["rows"] == artifact["facts"]["rows"] == 0


@pytest.mark.parametrize(
    ("filing_fy", "filing_fp", "fact_fy", "fact_fp", "expected_year", "expected_period", "expected_source"),
    [
        (2020, "Q1", 2020, "Q1", 2020, "Q1", "fact"),
        (None, None, None, None, None, None, "unknown"),
        (2020, None, None, None, None, None, "unknown"),
        (2019, None, None, "Q2", None, None, "unknown"),
        (2019, "Q2", 2020, None, None, None, "conflict"),
    ],
)
def test_event_and_fact_fiscal_keys_are_indivisible_and_auditable(
    tmp_path: Path,
    filing_fy: int | None,
    filing_fp: str | None,
    fact_fy: int | None,
    fact_fp: str | None,
    expected_year: int | None,
    expected_period: str | None,
    expected_source: str,
) -> None:
    """Complete, missing, partial and conflicting fiscal metadata stays consistent."""
    root = _financials_root(tmp_path)
    filing = _artifact_filing(
        "2020-05-01", "2020-03-31", "quarter", "10-Q", fy=filing_fy, fp=filing_fp
    )
    _write_artifact_inputs(
        root,
        [filing],
        {
            "Revenues": {
                "USD": [
                    _artifact_fact(
                        filing,
                        100,
                        start="2020-01-01",
                        fy=fact_fy,
                        fp=fact_fp,
                    )
                ]
            }
        },
    )

    output = organize_financials(
        "0000000042", tmp_path / "raw", tmp_path / "organized", [date(2020, 5, 4)], output_ticker="TEST"
    )
    events = pd.read_parquet(output.parent / "financial_events.parquet")
    facts = pd.read_parquet(output.parent / "financial_facts.parquet")
    event = events.iloc[0]
    fact = facts.iloc[0]

    assert event["fiscal_year"] == expected_year or pd.isna(event["fiscal_year"])
    assert event["fiscal_period"] == expected_period or pd.isna(event["fiscal_period"])
    assert event["fiscal_key_source"] == expected_source
    assert fact["fiscal_year"] == expected_year or pd.isna(fact["fiscal_year"])
    assert fact["fiscal_period"] == expected_period or pd.isna(fact["fiscal_period"])
    assert fact["fiscal_key_source"] == expected_source
    if expected_source == "conflict":
        artifact = json.loads(output.with_name("_meta.json").read_text(encoding="utf-8"))["financial_events"]
        assert artifact["rejection_counts"]["fiscal_key_conflicts"] == 1


def test_complete_fact_key_backfills_sibling_fact_missing_its_key(tmp_path: Path) -> None:
    """An event key inferred from one fact is reused for every key-incomplete fact."""
    root = _financials_root(tmp_path)
    filing = _artifact_filing("2020-05-01", "2020-03-31", "quarter", "10-Q")
    _write_artifact_inputs(
        root,
        [filing],
        {
            "Revenues": {"USD": [_artifact_fact(filing, 100, start="2020-01-01")]},
            "NetIncomeLoss": {
                "USD": [_artifact_fact(filing, 10, start="2020-01-01", fy=2020, fp="Q1")]
            },
        },
    )

    output = organize_financials(
        "0000000042", tmp_path / "raw", tmp_path / "organized", [date(2020, 5, 4)], output_ticker="TEST"
    )
    events = pd.read_parquet(output.parent / "financial_events.parquet")
    facts = pd.read_parquet(output.parent / "financial_facts.parquet").set_index("concept")

    assert events.loc[0, "fiscal_year"] == 2020
    assert events.loc[0, "fiscal_period"] == "Q1"
    assert events.loc[0, "fiscal_key_source"] == "fact"
    assert facts.loc["revenue", "fiscal_year"] == 2020
    assert facts.loc["revenue", "fiscal_period"] == "Q1"
    assert facts.loc["revenue", "fiscal_key_source"] == "fact"


def test_invalid_starts_future_ends_and_non_us_gaap_tags_are_rejected(tmp_path: Path) -> None:
    """Invalid temporal metadata and same-name tags outside us-gaap never become facts."""
    root = _financials_root(tmp_path)
    filing = _artifact_filing("2020-07-30", "2020-06-30", "quarter", "10-Q", fy=2020, fp="Q2")
    _write_artifact_inputs(
        root,
        [filing],
        {
            "Revenues": {
                "USD": [
                    _artifact_fact(filing, 10, start="bad-date", fy=2020, fp="Q2"),
                    _artifact_fact(filing, 20, fy=2020, fp="Q2", end="2020-08-01"),
                ]
            }
        },
    )
    facts_path = root / "companyfacts.json"
    payload = json.loads(facts_path.read_text(encoding="utf-8"))
    payload["facts"]["ifrs-full"] = {
        "Revenues": {
            "units": {
                "USD": [
                    _artifact_fact(filing, 30, start="2020-04-01", fy=2020, fp="Q2")
                ]
            }
        }
    }
    facts_path.write_text(json.dumps(payload), encoding="utf-8")

    output = organize_financials(
        "0000000042", tmp_path / "raw", tmp_path / "organized", [date(2020, 7, 31)], output_ticker="TEST"
    )
    artifact = json.loads(output.with_name("_meta.json").read_text(encoding="utf-8"))["financial_events"]
    assert artifact["complete"] is True
    assert pd.read_parquet(output.parent / "financial_facts.parquet").empty
    assert artifact["rejection_counts"]["unparseable_period_start"] == 1
    assert artifact["rejection_counts"]["report_period_end_after_filed_date"] == 1
    assert artifact["rejection_counts"]["non_us_gaap_taxonomy"] == 1


def test_amendment_status_requires_same_base_form_and_report_end(tmp_path: Path) -> None:
    """A same-end 8-K is not an original filing for a 10-K/A quality decision."""
    root = _financials_root(tmp_path)
    filings = [
        _artifact_filing("2020-01-30", "2019-12-31", "unrelated-8k", "8-K"),
        _artifact_filing("2020-02-10", "2019-12-31", "only-amendment", "10-K/A"),
    ]
    _write_artifact_inputs(root, filings, {})

    output = organize_financials(
        "0000000042", tmp_path / "raw", tmp_path / "organized", [date(2020, 2, 11)], output_ticker="TEST"
    )
    events = pd.read_parquet(output.parent / "financial_events.parquet").set_index("accession_number")
    artifact = json.loads(output.with_name("_meta.json").read_text(encoding="utf-8"))["financial_events"]

    assert events.loc["only-amendment", "quality_status"] == "amendment_only"
    assert artifact["quality_counts"]["event_quality_amendment_only"] == 1


def test_raw_payload_hash_is_computed_once_for_many_submission_rows(
    tmp_path: Path, monkeypatch
) -> None:
    """All filing locators in one payload reuse its single precomputed digest."""
    import importlib

    module = importlib.import_module("download.organize_financials")
    root = _financials_root(tmp_path)
    filings = [
        _artifact_filing("2020-02-01", None, f"event-{index}", "8-K")
        for index in range(8)
    ]
    _write_artifact_inputs(root, filings, {})
    submission_path = root / "submissions.json"
    original = module._sha256
    calls: list[Path] = []

    def tracked(path: Path) -> str:
        if path == submission_path:
            calls.append(path)
        return original(path)

    monkeypatch.setattr(module, "_sha256", tracked)
    output = organize_financials(
        "0000000042", tmp_path / "raw", tmp_path / "organized", [date(2020, 2, 3)], output_ticker="TEST"
    )
    events = pd.read_parquet(output.parent / "financial_events.parquet")

    assert len(events) == 8
    assert len(calls) == 1
    assert events["source_submission_sha256"].nunique() == 1


@pytest.mark.parametrize("interruption", ["second_parquet", "legacy_csv", "final_meta"])
def test_interrupted_organization_never_completes_and_keeps_mapping_diagnostic(
    tmp_path: Path, monkeypatch, interruption: str
) -> None:
    """Each commit boundary leaves a false completion and the calendar failure reason."""
    import importlib
    from download import manager

    financial_events = importlib.import_module("download.financial_events")
    root = _financials_root(tmp_path)
    filing = _artifact_filing("2020-02-01", None, "event", "8-K")
    _write_artifact_inputs(root, [filing], {})

    def fail_calendar(*args, **kwargs):
        raise RuntimeError("calendar unavailable")

    monkeypatch.setattr(financial_events.xcals, "get_calendar", fail_calendar)
    if interruption == "second_parquet":
        original_write = financial_events._write_parquet_atomic

        def fail_facts(path, records, schema):
            if path.name == "financial_facts.parquet":
                raise OSError("injected facts write failure")
            return original_write(path, records, schema)

        monkeypatch.setattr(financial_events, "_write_parquet_atomic", fail_facts)
    elif interruption == "legacy_csv":
        def fail_csv(self, *args, **kwargs):
            raise OSError("injected CSV write failure")

        monkeypatch.setattr(pd.DataFrame, "to_csv", fail_csv)
    else:
        original_meta = financial_events._write_meta_atomic
        calls = 0

        def fail_final_meta(path, metadata):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("injected final meta failure")
            return original_meta(path, metadata)

        monkeypatch.setattr(financial_events, "_write_meta_atomic", fail_final_meta)

    with pytest.raises(OSError):
        organize_financials(
            "0000000042", tmp_path / "raw", tmp_path / "organized", [date(2020, 2, 3)], output_ticker="TEST"
        )

    directory = tmp_path / "organized" / "stocks" / "TEST"
    metadata = json.loads((directory / "_meta.json").read_text(encoding="utf-8"))
    artifact = metadata["financial_events"]
    assert artifact["complete"] is False
    assert artifact["rejection_counts"]["effective_session_mapping_failed"] == 1
    assert not manager._ticker_is_organized(
        "TEST",
        tmp_path / "raw",
        tmp_path / "organized",
        require_financials=True,
        expected_cik="0000000042",
        check_cik=True,
    )


def test_cross_payload_duplicate_dedup_and_conflict_rejection(tmp_path: Path) -> None:
    """Identical copied facts dedupe across payloads; disagreeing copies are rejected."""
    filing = _artifact_filing("2020-02-01", "2019-12-31", "annual", "10-K", fy=2019, fp="FY")
    fact = _artifact_fact(filing, 100, start="2019-01-01", fy=2019, fp="FY")
    for conflict in (False, True):
        case_root = tmp_path / ("conflict" if conflict else "duplicate")
        root = case_root / "raw" / "sec" / "financials"
        root.mkdir(parents=True)
        _write_artifact_inputs(root, [filing], {"Revenues": {"USD": [fact]}})
        first_payload = json.loads((root / "companyfacts.json").read_text(encoding="utf-8"))
        copied_payload = json.loads(json.dumps(first_payload))
        if conflict:
            copied_payload["facts"]["us-gaap"]["Revenues"]["units"]["USD"][0]["val"] = 101
        duplicate_path = root / "companyfacts-copy.json"
        duplicate_path.write_text(json.dumps(copied_payload), encoding="utf-8")
        manifest_path = root / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["resources"].append(
            {"logical_key": "companyfacts:0000000042:copy", "path": duplicate_path.name}
        )
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        output = organize_financials(
            "0000000042", case_root / "raw", case_root / "organized", [date(2020, 2, 3)], output_ticker="TEST"
        )
        directory = case_root / "organized" / "stocks" / "TEST"
        facts = pd.read_parquet(directory / "financial_facts.parquet")
        artifact = json.loads((directory / "_meta.json").read_text(encoding="utf-8"))["financial_events"]
        if conflict:
            assert facts.empty
            assert artifact["rejection_counts"]["conflicting_fact_versions"] == 1
        else:
            assert output is not None
            assert len(facts) == 1
            assert facts["value"].item() == 100.0
            assert not artifact["rejection_counts"].get("conflicting_fact_versions")


def test_owned_manifest_cik_is_field_matched_and_zero_padding_normalized(tmp_path: Path) -> None:
    """CIK 42 owns both 42/0000000042 aliases, but never CIK 142's suffix match."""
    root = _financials_root(tmp_path)
    annual = _artifact_filing("2020-02-01", "2019-12-31", "annual", "10-K", fy=2019, fp="FY")
    _write_artifact_inputs(
        root,
        [annual],
        {"Revenues": {"USD": [_artifact_fact(annual, 100, start="2019-01-01", fy=2019, fp="FY")]}},
    )

    original_manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    facts_alias = root / "companyfacts-unpadded.json"
    facts_alias.write_text((root / "companyfacts.json").read_text(encoding="utf-8"), encoding="utf-8")
    facts_142 = root / "companyfacts-142.json"
    facts_142.write_text(
        json.dumps({"cik": 142, "facts": {"us-gaap": {}}}), encoding="utf-8"
    )
    submissions_142 = root / "submissions-142.json"
    submissions_142.write_text(
        json.dumps(
            {
                "cik": 142,
                "filings": {
                    "recent": {
                        "accessionNumber": [],
                        "filingDate": [],
                        "reportDate": [],
                        "form": [],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    original_manifest["resources"].extend(
        [
            {"logical_key": "companyfacts:42", "path": facts_alias.name},
            {"logical_key": "companyfacts:0000000142", "path": facts_142.name},
            {"logical_key": "submissions:0000000142", "path": submissions_142.name},
        ]
    )
    # The padded resource is represented by the original fixture entry; rewrite
    # one of its equivalent aliases to exercise both normalizations explicitly.
    for entry in original_manifest["resources"]:
        if entry["logical_key"] == "companyfacts:0000000042":
            entry["logical_key"] = "companyfacts:0000000042:canonical"
    (root / "manifest.json").write_text(json.dumps(original_manifest), encoding="utf-8")

    selected = _owned_paths(root, "0000000042")
    assert _owned_paths(root, "42") == selected
    assert set(selected) == {
        root / "companyfacts.json",
        facts_alias,
        root / "submissions.json",
    }
    output = organize_financials(
        "0000000042", tmp_path / "raw", tmp_path / "organized", [date(2020, 2, 3)], output_ticker="TEST"
    )
    directory = output.parent
    artifact = json.loads((directory / "_meta.json").read_text(encoding="utf-8"))["financial_events"]
    assert artifact["complete"] is True
    assert artifact["input_resource_status"]["companyfacts"] == "ok"
    assert not artifact["rejection_counts"].get("raw_payload_cik_mismatch")
    assert pd.read_parquet(directory / "financial_events.parquet")["accession_number"].tolist() == ["annual"]
    assert pd.read_parquet(directory / "financial_facts.parquet")["value"].tolist() == [100.0]


def test_empty_cik_never_falls_back_to_other_cik_resources(tmp_path: Path) -> None:
    """Empty and invalid CIKs are always isolated from every raw SEC payload."""
    root = _financials_root(tmp_path)
    resources = []
    for cik in ("0000000042", "0000000142", "0000000999", "0000000001", "0000000007"):
        path = root / f"companyfacts-{cik}.json"
        path.write_text("{}", encoding="utf-8")
        resources.append({"logical_key": f"companyfacts:{cik}", "path": path.name})
    (root / "manifest.json").write_text(json.dumps({"resources": resources}), encoding="utf-8")

    assert _owned_paths(root, "") == []
    assert _owned_paths(root, "bad") == []
    assert _owned_paths(root, "12345678901") == []


def test_cik_variants_produce_one_ten_digit_artifact_identity(tmp_path: Path) -> None:
    """Unpadded and over-zero-padded valid CIK strings normalize identically."""
    from download.financial_events import normalize_cik

    assert [normalize_cik(item) for item in ("42", "00000000042", "0000000042")] == [
        "0000000042",
        "0000000042",
        "0000000042",
    ]
    root = _financials_root(tmp_path)
    annual = _artifact_filing("2020-02-01", "2019-12-31", "annual", "10-K", fy=2019, fp="FY")
    _write_artifact_inputs(
        root,
        [annual],
        {"Revenues": {"USD": [_artifact_fact(annual, 100, start="2019-01-01", fy=2019, fp="FY")]}},
    )
    identities = []
    event_ids = []
    for cik in ("42", "00000000042", "0000000042"):
        output = organize_financials(
            cik, tmp_path / "raw", tmp_path / "organized", [date(2020, 2, 3)], output_ticker="TEST"
        )
        meta = json.loads(output.with_name("_meta.json").read_text(encoding="utf-8"))
        identities.append(meta["cik10"])
        event_ids.append(pd.read_parquet(output.with_name("financial_events.parquet"))["event_id"].tolist())
    assert identities == ["0000000042"] * 3
    assert event_ids[0] == event_ids[1] == event_ids[2]


@pytest.mark.parametrize("invalid_cik", ["bad", "12345678901"])
def test_invalid_cik_is_audited_and_cannot_complete(
    tmp_path: Path, invalid_cik: str
) -> None:
    """Nonnumeric and overlong CIKs are explicit incomplete inputs, not empty success."""
    case = tmp_path / invalid_cik.replace("/", "_")
    root = _financials_root(case)
    (root / "manifest.json").write_text(json.dumps({"resources": []}), encoding="utf-8")

    with pytest.raises(ValueError, match="financial-events artifact is incomplete"):
        organize_financials(
            invalid_cik,
            case / "raw",
            case / "organized",
            [date(2020, 2, 3)],
            output_ticker="TEST",
        )
    artifact = json.loads(
        (case / "organized" / "stocks" / "TEST" / "_meta.json").read_text(encoding="utf-8")
    )["financial_events"]
    assert artifact["complete"] is False
    assert artifact["cik10"] is None
    assert artifact["rejection_counts"]["invalid_cik"] == 1
    assert artifact["input_resource_status"]["manifest"] == "malformed"


def test_dict_manifest_accepts_single_record_values(tmp_path: Path) -> None:
    """A dict-valued logical-key entry is also a supported manifest record shape."""
    root = _financials_root(tmp_path)
    facts_path = root / "facts.json"
    submissions_path = root / "submissions.json"
    facts_path.write_text("{}", encoding="utf-8")
    submissions_path.write_text("{}", encoding="utf-8")
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "resources": {
                    "companyfacts:0000000042": {"path": facts_path.name, "sha256": "a" * 64},
                    "submissions:0000000042": {"path": submissions_path.name, "sha256": "b" * 64},
                }
            }
        ),
        encoding="utf-8",
    )

    assert _owned_paths(root, "0000000042") == [facts_path, submissions_path]


@pytest.mark.parametrize("resources", [{}, []])
def test_both_explicit_empty_manifest_shapes_confirm_no_raw_input(
    tmp_path: Path, resources: object
) -> None:
    """Empty dict and list resource maps are explicit, valid no-input evidence."""
    root = _financials_root(tmp_path)
    (root / "manifest.json").write_text(json.dumps({"resources": resources}), encoding="utf-8")

    output = organize_financials(
        "0000000042", tmp_path / "raw", tmp_path / "organized", [date(2020, 2, 3)], output_ticker="TEST"
    )
    artifact = json.loads(output.with_name("_meta.json").read_text(encoding="utf-8"))["financial_events"]
    assert artifact["complete"] is True
    assert artifact["input_resource_status"]["manifest"] == "ok"
    assert artifact["input_resource_status"]["submissions"] == "no_input"
    assert artifact["input_resource_status"]["companyfacts"] == "no_input"


def test_production_dict_manifest_generates_valid_nonempty_artifact(tmp_path: Path) -> None:
    """Production logical-key to record-list manifests organize through manager validation."""
    import hashlib
    from download import financial_events, manager

    root = _financials_root(tmp_path)
    filing = _artifact_filing("2020-02-01", "2019-12-31", "annual", "10-K", fy=2019, fp="FY")
    _write_artifact_inputs(
        root,
        [filing],
        {"Revenues": {"USD": [_artifact_fact(filing, 500, start="2019-01-01", fy=2019, fp="FY")]}},
    )
    old_manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    production_resources: dict[str, list[dict[str, object]]] = {}
    for entry in old_manifest["resources"]:
        path = root / entry["path"]
        production_resources.setdefault(entry["logical_key"], []).append(
            {
                "attempts": 1,
                "byte_size": path.stat().st_size,
                "fetched_at_utc": "2026-09-24T11:51:46.928031Z",
                "path": entry["path"],
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "status": "done",
                "url": f"https://data.sec.gov/{entry['logical_key']}",
            }
        )
    (root / "manifest.json").write_text(
        json.dumps({"resources": production_resources}), encoding="utf-8"
    )

    output = organize_financials(
        "0000000042", tmp_path / "raw", tmp_path / "organized", [date(2020, 2, 3)], output_ticker="TEST"
    )
    directory = output.parent
    metadata = json.loads((directory / "_meta.json").read_text(encoding="utf-8"))
    artifact = metadata["financial_events"]
    events = pd.read_parquet(directory / "financial_events.parquet")
    facts = pd.read_parquet(directory / "financial_facts.parquet")

    assert artifact["complete"] is True
    assert artifact["input_resource_status"] == {
        "manifest": "ok", "submissions": "ok", "companyfacts": "ok"
    }
    assert events["accession_number"].tolist() == ["annual"]
    assert facts["concept"].tolist() == ["revenue"]
    assert facts["value"].tolist() == [500.0]
    assert financial_events.artifact_is_valid(
        directory,
        metadata,
        raw_dir=tmp_path / "raw",
        expected_cik="0000000042",
    )
    assert manager._ticker_is_organized(
        "TEST",
        tmp_path / "raw",
        tmp_path / "organized",
        require_financials=True,
        expected_cik="0000000042",
        check_cik=True,
    )


def test_dict_manifest_multiple_versions_select_deterministically_and_keep_source(tmp_path: Path) -> None:
    """All same-key records are considered in stable path order with source provenance."""
    import hashlib

    root = _financials_root(tmp_path)
    chosen = _artifact_filing("2020-02-01", "2019-12-31", "same-accession", "10-K", fy=2019, fp="FY")
    stale = _artifact_filing("2020-02-01", "2018-12-31", "same-accession", "10-K", fy=2018, fp="FY")
    _write_artifact_inputs(
        root,
        [stale],
        {"Revenues": {"USD": [_artifact_fact(chosen, 500, start="2019-01-01", fy=2019, fp="FY")]}},
    )
    companyfacts_path = root / "companyfacts.json"
    a_submission_path = root / "a-submissions.json"
    a_submission_path.write_text(
        json.dumps(
            {
                "cik": 42,
                "filings": {
                    "recent": {
                        key: [chosen.get(key)]
                        for key in ("accessionNumber", "filingDate", "reportDate", "form", "fy", "fp")
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    submissions_path = root / "submissions.json"
    resources: dict[str, list[dict[str, object]]] = {}
    for logical_key, path in (
        ("companyfacts:0000000042", companyfacts_path),
        ("submissions:0000000042", submissions_path),
        ("submissions:0000000042", a_submission_path),
    ):
        resources.setdefault(logical_key, []).append(
            {
                "path": path.name,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "attempts": 1,
                "byte_size": path.stat().st_size,
                "status": "done",
                "url": f"https://data.sec.gov/{logical_key}",
            }
        )
    # Reverse version order to prove selection does not depend on manifest list order.
    resources["submissions:0000000042"].reverse()
    (root / "manifest.json").write_text(json.dumps({"resources": resources}), encoding="utf-8")

    output = organize_financials(
        "0000000042", tmp_path / "raw", tmp_path / "organized", [date(2020, 2, 3)], output_ticker="TEST"
    )
    events = pd.read_parquet(output.parent / "financial_events.parquet")
    facts = pd.read_parquet(output.parent / "financial_facts.parquet")
    artifact = json.loads(output.with_name("_meta.json").read_text(encoding="utf-8"))["financial_events"]

    assert artifact["complete"] is True
    assert len(events) == 1
    assert events.loc[0, "report_period_end"] == date(2019, 12, 31)
    assert events.loc[0, "source_submission_path"].endswith("a-submissions.json")
    assert events.loc[0, "source_submission_sha256"] == hashlib.sha256(a_submission_path.read_bytes()).hexdigest()
    assert facts["value"].tolist() == [500.0]


def test_null_manifest_is_malformed_and_cannot_confirm_no_input(tmp_path: Path) -> None:
    """A JSON null manifest is an invalid resource inventory, not explicit emptiness."""
    root = _financials_root(tmp_path)
    (root / "manifest.json").write_text("null", encoding="utf-8")

    with pytest.raises(ValueError, match="financial-events artifact is incomplete"):
        organize_financials(
            "0000000042", tmp_path / "raw", tmp_path / "organized", [date(2020, 2, 3)], output_ticker="TEST"
        )

    artifact = json.loads(
        (tmp_path / "organized" / "stocks" / "TEST" / "_meta.json").read_text(encoding="utf-8")
    )["financial_events"]
    assert artifact["complete"] is False
    assert artifact["input_resource_status"]["manifest"] == "malformed"
    assert artifact["events"]["rows"] == 0
    assert artifact["rejection_counts"]["malformed_raw_manifest"] == 1


@pytest.mark.parametrize(
    ("manifest_payload", "expected_rejections"),
    [
        ({}, {"manifest_missing_resources": 1}),
        (
            {
                "resources": {
                    "submissions:0000000042": 17,
                    "companyfacts:0000000042": None,
                }
            },
            {"manifest_resource_record_not_object": 2},
        ),
        ({"resources": None}, {"manifest_resources_not_list": 1}),
        (
            {"resources": [17]},
            {"manifest_resource_record_not_object": 1},
        ),
        (
            {"resources": [{"path": "submissions.json"}]},
            {"manifest_resource_missing_logical_key": 1},
        ),
    ],
)
def test_malformed_manifest_shapes_never_publish_or_reuse_empty_artifact(
    tmp_path: Path,
    manifest_payload: dict[str, object],
    expected_rejections: dict[str, int],
) -> None:
    """Missing/wrong manifest schema is malformed; neither artifact nor manager accepts it."""
    from download import manager
    from download import financial_events

    root = _financials_root(tmp_path)
    (root / "manifest.json").write_text(json.dumps(manifest_payload), encoding="utf-8")

    with pytest.raises(ValueError, match="financial-events artifact is incomplete"):
        organize_financials(
            "0000000042", tmp_path / "raw", tmp_path / "organized", [date(2020, 2, 3)], output_ticker="TEST"
        )

    directory = tmp_path / "organized" / "stocks" / "TEST"
    metadata = json.loads((directory / "_meta.json").read_text(encoding="utf-8"))
    artifact = metadata["financial_events"]
    assert artifact["complete"] is False
    assert artifact["input_resource_status"]["manifest"] == "malformed"
    for rejection, expected_count in expected_rejections.items():
        assert artifact["rejection_counts"][rejection] == expected_count
    assert not financial_events.artifact_is_valid(
        directory,
        metadata,
        raw_dir=tmp_path / "raw",
        expected_cik="0000000042",
    )
    assert not manager._ticker_is_organized(
        "TEST",
        tmp_path / "raw",
        tmp_path / "organized",
        require_financials=True,
        expected_cik="0000000042",
        check_cik=True,
    )


@pytest.mark.parametrize(
    "placeholder",
    [{}, {"cik": 42}, {"facts": {}}, {"cik": 42, "facts": {}}],
)
def test_companyfacts_no_xbrl_placeholders_are_nonfatal(
    tmp_path: Path, placeholder: dict[str, object]
) -> None:
    """A parseable empty/missing facts response is audited without blocking events."""
    from download import financial_events, manager
    root = _financials_root(tmp_path)
    filing = _artifact_filing("2020-02-01", None, "nonfinancial", "8-K")
    _write_artifact_inputs(root, [filing], {})
    (root / "companyfacts.json").write_text(json.dumps(placeholder), encoding="utf-8")

    output = organize_financials(
        "0000000042", tmp_path / "raw", tmp_path / "organized", [date(2020, 2, 3)], output_ticker="TEST"
    )
    directory = output.parent
    artifact = json.loads((directory / "_meta.json").read_text(encoding="utf-8"))["financial_events"]

    assert artifact["complete"] is True
    assert artifact["input_resource_status"]["companyfacts"] == "no_usable_facts"
    assert artifact["input_resource_status"]["submissions"] == "ok"
    assert artifact["rejection_counts"]["no_usable_facts"] == 1
    metadata = json.loads((directory / "_meta.json").read_text(encoding="utf-8"))
    assert financial_events.artifact_is_valid(
        directory, metadata, raw_dir=tmp_path / "raw", expected_cik="0000000042"
    )
    assert manager._ticker_is_organized(
        "TEST",
        tmp_path / "raw",
        tmp_path / "organized",
        require_financials=True,
        expected_cik="0000000042",
        check_cik=True,
    )
    assert pd.read_parquet(directory / "financial_events.parquet")["accession_number"].tolist() == [
        "nonfinancial"
    ]
    assert pd.read_parquet(directory / "financial_facts.parquet").empty


@pytest.mark.parametrize(
    ("facts_tree", "reason"),
    [
        ({"Revenues": {"units": {"USD": [17]}}}, "invalid_companyfacts_payload_shape"),
        ({"Revenues": {}}, "invalid_companyfacts_payload_shape"),
    ],
)
def test_malformed_companyfacts_units_cannot_confirm_empty_facts(
    tmp_path: Path, facts_tree: dict[str, object], reason: str
) -> None:
    """Fact arrays require object records and concept entries require a units node."""
    root = _financials_root(tmp_path)
    filing = _artifact_filing("2020-02-01", "2019-12-31", "annual", "10-K", fy=2019, fp="FY")
    _write_artifact_inputs(root, [filing], {})
    companyfacts_path = root / "companyfacts.json"
    payload = json.loads(companyfacts_path.read_text(encoding="utf-8"))
    payload["facts"]["us-gaap"] = facts_tree
    companyfacts_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="financial-events artifact is incomplete"):
        organize_financials(
            "0000000042", tmp_path / "raw", tmp_path / "organized", [date(2020, 2, 3)], output_ticker="TEST"
        )

    directory = tmp_path / "organized" / "stocks" / "TEST"
    artifact = json.loads((directory / "_meta.json").read_text(encoding="utf-8"))["financial_events"]
    assert artifact["complete"] is False
    assert artifact["input_resource_status"]["companyfacts"] == "malformed"
    assert artifact["rejection_counts"][reason] == 1
    assert pd.read_parquet(directory / "financial_facts.parquet").empty


def test_manifest_with_only_other_ciks_is_audited_complete_empty_input(tmp_path: Path) -> None:
    """A valid manifest with no resources owned by the requested CIK is D-class empty."""
    import hashlib
    from download import financial_events, manager

    root = _financials_root(tmp_path)
    facts_path = root / "other-companyfacts.json"
    submissions_path = root / "other-submissions.json"
    facts_path.write_text(json.dumps({"cik": 999, "facts": {"us-gaap": {}}}), encoding="utf-8")
    submissions_path.write_text(
        json.dumps(
            {
                "cik": 999,
                "filings": {
                    "recent": {
                        "accessionNumber": [],
                        "filingDate": [],
                        "reportDate": [],
                        "form": [],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    resources = {}
    for kind, path in (("companyfacts", facts_path), ("submissions", submissions_path)):
        resources[f"{kind}:0000000999"] = [
            {"path": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        ]
    (root / "manifest.json").write_text(json.dumps({"resources": resources}), encoding="utf-8")

    output = organize_financials(
        "0000000042", tmp_path / "raw", tmp_path / "organized", [date(2020, 2, 3)], output_ticker="TEST"
    )
    directory = output.parent
    artifact = json.loads((directory / "_meta.json").read_text(encoding="utf-8"))["financial_events"]

    assert artifact["complete"] is True
    assert artifact["empty_reason"] == "no_usable_input_for_cik"
    assert artifact["input_resource_status"] == {
        "manifest": "ok", "submissions": "no_input", "companyfacts": "no_input"
    }
    assert artifact["rejection_counts"]["no_cik_owned_resources"] == 1
    metadata = json.loads((directory / "_meta.json").read_text(encoding="utf-8"))
    assert financial_events.artifact_is_valid(
        directory, metadata, raw_dir=tmp_path / "raw", expected_cik="0000000042"
    )
    assert manager._ticker_is_organized(
        "TEST",
        tmp_path / "raw",
        tmp_path / "organized",
        require_financials=True,
        expected_cik="0000000042",
        check_cik=True,
    )
    assert pd.read_parquet(directory / "financial_events.parquet").empty
    assert pd.read_parquet(directory / "financial_facts.parquet").empty


def test_misattributed_cik_payloads_are_complete_audited_empty_input(tmp_path: Path) -> None:
    """Resources declared for a CIK but containing another CIK are audited, not imported."""
    root = _financials_root(tmp_path)
    filing = _artifact_filing("2020-02-01", "2019-12-31", "wrong-owner", "10-K", fy=2019, fp="FY")
    _write_artifact_inputs(
        root,
        [filing],
        {"Revenues": {"USD": [_artifact_fact(filing, 100, start="2019-01-01", fy=2019, fp="FY")]}},
    )
    for name in ("companyfacts.json", "submissions.json"):
        path = root / name
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["cik"] = 999
        path.write_text(json.dumps(payload), encoding="utf-8")

    output = organize_financials(
        "0000000042", tmp_path / "raw", tmp_path / "organized", [date(2020, 2, 3)], output_ticker="TEST"
    )
    directory = output.parent
    artifact = json.loads((directory / "_meta.json").read_text(encoding="utf-8"))["financial_events"]

    assert artifact["complete"] is True
    assert artifact["empty_reason"] == "no_usable_input_for_cik"
    assert artifact["input_resource_status"]["companyfacts"] == "misattributed"
    assert artifact["input_resource_status"]["submissions"] == "misattributed"
    assert artifact["rejection_counts"]["raw_payload_cik_mismatch"] == 2
    assert pd.read_parquet(directory / "financial_events.parquet").empty
    assert pd.read_parquet(directory / "financial_facts.parquet").empty


def test_organize_artifact_without_resource_states_fails_closed(tmp_path: Path) -> None:
    """Legacy callers that omit resource-state evidence cannot imply trusted emptiness."""
    import importlib

    financial_events = importlib.import_module("download.financial_events")
    artifact = financial_events.organize_artifact(
        "0000000042",
        "TEST",
        tmp_path / "raw",
        tmp_path / "organized",
        [],
        [],
        [],
        initial_rejections={"unreadable_raw_payload": 1},
        commit_meta=False,
    )
    assert artifact["complete"] is False
    assert artifact["input_resource_status"] == {
        "manifest": "unknown",
        "submissions": "unknown",
        "companyfacts": "unknown",
    }


@pytest.mark.parametrize(
    ("annual_end", "annual_filed", "annual_fy", "quarter_end", "quarter_filed", "expected_year"),
    [
        ("2020-01-31", "2020-03-01", 2019, "2020-04-30", "2020-06-01", 2020),
        ("2021-01-31", "2021-03-01", 2020, "2021-04-30", "2021-06-01", 2021),
    ],
)
def test_fiscal_anchor_uses_resolved_annual_fact_year_not_calendar_end_year(
    tmp_path: Path,
    annual_end: str,
    annual_filed: str,
    annual_fy: int,
    quarter_end: str,
    quarter_filed: str,
    expected_year: int,
) -> None:
    """A non-calendar annual fact key anchors a missing-key Q1 as FY+1."""
    root = _financials_root(tmp_path)
    annual_start = f"{annual_fy}-02-01"
    annual = _artifact_filing(
        annual_filed, annual_end, "annual", "10-K", fy=None, fp=None
    )
    quarter = _artifact_filing(quarter_filed, quarter_end, "quarter", "10-Q")
    quarter_start = f"{quarter_end[:4]}-02-01"
    _write_artifact_inputs(
        root,
        [annual, quarter],
        {
            "Revenues": {
                "USD": [
                    _artifact_fact(annual, 1000, start=annual_start, fy=annual_fy, fp="FY"),
                    _artifact_fact(quarter, 100, start=quarter_start),
                ]
            }
        },
    )

    output = organize_financials(
        "0000000042",
        tmp_path / "raw",
        tmp_path / "organized",
        [date.fromisoformat(quarter_filed)],
        output_ticker="TEST",
    )
    events = pd.read_parquet(output.parent / "financial_events.parquet").set_index("accession_number")
    facts = pd.read_parquet(output.parent / "financial_facts.parquet")
    quarter_event_id = events.loc["quarter", "event_id"]
    quarter_fact = facts.loc[facts["event_id"] == quarter_event_id].iloc[0]

    assert events.loc["annual", "fiscal_year"] == annual_fy
    assert events.loc["annual", "fiscal_period"] == "FY"
    assert events.loc["quarter", "fiscal_year"] == expected_year
    assert events.loc["quarter", "fiscal_period"] == "Q1"
    assert events.loc["quarter", "fiscal_key_source"] == "prior_annual_anchor"
    assert quarter_fact["fiscal_year"] == expected_year
    assert quarter_fact["fiscal_period"] == "Q1"


def test_same_day_annual_revision_is_not_visible_as_anchor(tmp_path: Path) -> None:
    """A same-day annual revision cannot replace the strictly earlier FY anchor."""
    root = _financials_root(tmp_path)
    old_annual = _artifact_filing("2020-03-01", "2020-01-31", "old-annual", "10-K", fy=2019, fp="FY")
    same_day_revision = _artifact_filing(
        "2020-06-01", "2020-01-31", "same-day-revision", "10-K/A", fy=2020, fp="FY"
    )
    quarter = _artifact_filing("2020-06-01", "2020-04-30", "quarter", "10-Q")
    _write_artifact_inputs(
        root,
        [old_annual, same_day_revision, quarter],
        {
            "Revenues": {
                "USD": [
                    _artifact_fact(old_annual, 1000, start="2019-02-01", fy=2019, fp="FY"),
                    _artifact_fact(same_day_revision, 1100, start="2019-02-01", fy=2020, fp="FY"),
                    _artifact_fact(quarter, 100, start="2020-02-01"),
                ]
            }
        },
    )

    output = organize_financials(
        "0000000042", tmp_path / "raw", tmp_path / "organized", [date(2020, 6, 2)], output_ticker="TEST"
    )
    events = pd.read_parquet(output.parent / "financial_events.parquet").set_index("accession_number")

    assert events.loc["old-annual", "fiscal_year"] == 2019
    assert events.loc["same-day-revision", "fiscal_year"] == 2020
    assert events.loc["quarter", "fiscal_year"] == 2020
    assert events.loc["quarter", "fiscal_period"] == "Q1"
    assert events.loc["quarter", "fiscal_key_source"] == "prior_annual_anchor"


def test_same_day_prior_quarter_is_excluded_from_quarter_sequence(tmp_path: Path) -> None:
    """A same-day earlier quarter is not visible when assigning the current slot."""
    root = _financials_root(tmp_path)
    annual = _artifact_filing("2020-02-01", "2019-12-31", "annual", "10-K", fy=2019, fp="FY")
    same_day_prior_q = _artifact_filing("2020-07-30", "2020-03-31", "same-day-q1", "10-Q")
    current_q = _artifact_filing("2020-07-30", "2020-06-30", "same-day-current", "10-Q")
    _write_artifact_inputs(
        root,
        [annual, same_day_prior_q, current_q],
        {
            "Revenues": {
                "USD": [
                    _artifact_fact(annual, 1000, start="2019-01-01", fy=2019, fp="FY"),
                    _artifact_fact(same_day_prior_q, 100, start="2020-01-01"),
                    _artifact_fact(current_q, 200, start="2020-04-01"),
                ]
            }
        },
    )

    output = organize_financials(
        "0000000042", tmp_path / "raw", tmp_path / "organized", [date(2020, 7, 31)], output_ticker="TEST"
    )
    events = pd.read_parquet(output.parent / "financial_events.parquet").set_index("accession_number")

    assert events.loc["same-day-q1", "fiscal_period"] == "Q1"
    assert events.loc["same-day-current", "fiscal_year"] == 2020
    assert events.loc["same-day-current", "fiscal_period"] == "Q1"


def test_anchor_index_matches_strict_naive_oracle_for_small_history(
    tmp_path: Path, monkeypatch
) -> None:
    """The indexed resolver matches a strict-filed-date naive reference exactly."""
    from datetime import timedelta
    from download import financial_events

    def fixed_session_map(filed_dates):
        return (
            {filed: filed + timedelta(days=1) for filed in set(filed_dates)},
            {"exchange": "XNYS", "first_session": None, "last_session": None, "mapping_start": None, "mapping_end": None},
        )

    monkeypatch.setattr(financial_events, "_map_effective_sessions", fixed_session_map)
    filings = [
        {"filingDate": "2019-02-01", "reportDate": "2018-12-31", "form": "10-K", "accessionNumber": "a0", "fy": 2018, "fp": "FY"},
        {"filingDate": "2020-03-01", "reportDate": "2018-12-31", "form": "10-K/A", "accessionNumber": "a1", "fy": 2019, "fp": "FY"},
        {"filingDate": "2020-06-01", "reportDate": "2019-12-31", "form": "10-K", "accessionNumber": "a2", "fy": 2019, "fp": "FY"},
        {"filingDate": "2020-06-01", "reportDate": "2020-03-31", "form": "10-Q", "accessionNumber": "q1"},
        {"filingDate": "2020-06-01", "reportDate": "2020-04-30", "form": "10-Q", "accessionNumber": "q2-same-day"},
        {"filingDate": "2020-08-01", "reportDate": "2020-07-31", "form": "10-Q", "accessionNumber": "q3"},
        {"filingDate": "2020-05-20", "reportDate": "2020-05-15", "form": "10-Q", "accessionNumber": "no-new-anchor"},
    ]
    source = [
        {
            **filing,
            "_source_submission_path": "raw/submissions.json",
            "_source_submission_sha256": "0" * 64,
            "_source_submission_locator": f"record[{index}]",
        }
        for index, filing in enumerate(filings)
    ]
    events, _, _, _ = financial_events._event_records(
        "0000000042", "TEST", source, [], tmp_path / "raw"
    )
    actual = {row["accession_number"]: (row["fiscal_year"], row["fiscal_period"]) for row in events}

    expected = {}
    for filing in filings:
        accession = filing["accessionNumber"]
        if str(filing["form"]).startswith("10-K"):
            expected[accession] = (filing["fy"], filing["fp"])
            continue
        filed = date.fromisoformat(filing["filingDate"])
        end = date.fromisoformat(filing["reportDate"])
        annual_candidates = []
        for prior in filings:
            if not str(prior["form"]).startswith("10-K"):
                continue
            prior_filed = date.fromisoformat(prior["filingDate"])
            prior_end = date.fromisoformat(prior["reportDate"])
            if prior_filed < filed and prior_end < end:
                annual_candidates.append((prior_end, prior_filed, int(prior["fy"])))
        if not annual_candidates:
            expected[accession] = (None, None)
            continue
        anchor_end = max(item[0] for item in annual_candidates)
        anchor_version = max(
            (item for item in annual_candidates if item[0] == anchor_end),
            key=lambda item: item[1],
        )
        quarter_ends = {
            date.fromisoformat(prior["reportDate"])
            for prior in filings
            if str(prior["form"]).startswith("10-Q")
            and anchor_end < date.fromisoformat(prior["reportDate"]) < end
            and date.fromisoformat(prior["filingDate"]) < filed
        }
        quarter_number = len(quarter_ends) + 1
        expected[accession] = (
            (anchor_version[2] + 1, f"Q{quarter_number}")
            if quarter_number <= 3
            else (None, None)
        )

    assert actual == expected
