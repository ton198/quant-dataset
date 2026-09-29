"""Regression tests for SEC financial input selection and universe caching."""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pandas as pd

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


def test_owned_paths_manifest_miss_does_not_read_unrelated_json(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A manifest miss only considers filenames and does not read unrelated data."""
    root = _financials_root(tmp_path)
    (root / "manifest.json").write_text(json.dumps({"resources": {}}), encoding="utf-8")
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
    (root / "manifest.json").write_text(json.dumps({"resources": {}}), encoding="utf-8")
    matching = root / "orphan-0000000042-companyfacts.json"
    matching.write_text('{"cik": 42}', encoding="utf-8")
    (root / "unrelated-companyfacts.json").write_text('{"cik": 7}', encoding="utf-8")

    assert _owned_paths(root, "0000000042") == [matching]


def test_missing_sec_financials_still_writes_missing_rows_and_empty_provenance(
    tmp_path: Path,
) -> None:
    """No SEC inputs produce the regular all-missing CSV and no unrelated meta inputs."""
    root = _financials_root(tmp_path)
    (root / "manifest.json").write_text(json.dumps({"resources": {}}), encoding="utf-8")
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
