"""CLI for SEC report-sample building and validation."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("value must be a positive integer") from exc
    if number < 1:
        raise argparse.ArgumentTypeError("value must be a positive integer")
    return number


def add_filings_parser(subparsers: Any) -> None:
    filings = subparsers.add_parser("filings", help="build and validate SEC report samples")
    commands = filings.add_subparsers(dest="filings_command", required=True)
    build = commands.add_parser("build-report-samples", help="download and clean SEC reports")
    build.add_argument("--out", type=Path, default=Path("data/financial_reports"))
    build.add_argument("--work-dir", type=Path)
    build.add_argument("--cache-root", type=Path, default=Path("data/raw/sec/financials"))
    build.add_argument("--limit", type=_positive_int)
    build.add_argument("--filing-id", action="append", dest="filing_ids")
    build.add_argument("--workers", type=_positive_int, default=4)
    build.add_argument("--resume", action="store_true")
    validate = commands.add_parser(
        "validate-report-samples", help="validate a report sample output"
    )
    validate.add_argument("--out", type=Path, default=Path("data/financial_reports"))
    validate.add_argument("--cache-root", type=Path, default=Path("data/raw/sec/financials"))


def run_filings_command(args: argparse.Namespace) -> int:
    from .report_samples import ReportSamplesError, build_report_samples, validate_report_samples

    try:
        if args.filings_command == "build-report-samples":
            result = build_report_samples(
                cache_root=args.cache_root,
                out=args.out,
                work_dir=args.work_dir,
                limit=args.limit,
                filing_ids=args.filing_ids,
                workers=args.workers,
                resume=args.resume,
            )
        elif args.filings_command == "validate-report-samples":
            result = validate_report_samples(args.out, cache_root=args.cache_root)
        else:
            return 2
    except (ReportSamplesError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0
