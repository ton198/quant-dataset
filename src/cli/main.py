"""Command-line entry point for raw downloads and filing report samples."""

from __future__ import annotations

import argparse
import logging
from datetime import date
from pathlib import Path

from download.manager import run_download


def _date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD") from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="quant-dataset", description="Download raw provider data and clean SEC reports"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    download = subparsers.add_parser("download", help="download SEC, Yahoo, and FRED data")
    download.add_argument(
        "--stage",
        choices=["market", "financials", "macros", "all"],
        action="append",
        default=None,
        help="stage to run; repeat to select multiple; default: all three raw downloads",
    )
    download.add_argument("--start", type=_date, help="inclusive market start date (YYYY-MM-DD)")
    download.add_argument("--end", type=_date, help="inclusive market end date (YYYY-MM-DD)")
    download.add_argument("--tickers", help="comma-separated ticker subset")
    download.add_argument("--force", action="store_true", help="ignore existing download progress")
    download.add_argument(
        "--dry-run", action="store_true", help="show planned work without network or writes"
    )
    download.add_argument(
        "--data-dir",
        type=Path,
        default=Path("data"),
        help="base directory for raw and progress files",
    )
    from filings.cli import add_filings_parser

    add_filings_parser(subparsers)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse CLI arguments and return the selected command's exit code."""
    args = _parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if args.command == "filings":
        from filings.cli import run_filings_command

        return run_filings_command(args)
    if args.command != "download":
        return 2
    stages = args.stage or ["all"]
    if "all" in stages:
        stages = ["market", "financials", "macros"]
    else:
        stages = list(dict.fromkeys(stages))
    if "market" in stages and (args.start is None or args.end is None):
        _parser().error("--start and --end are required when market stage is selected")
    if args.start and args.end and args.end < args.start:
        _parser().error("--end must be on or after --start")
    tickers = (
        [item.strip().upper() for item in args.tickers.split(",") if item.strip()]
        if args.tickers
        else None
    )
    return run_download(
        repo_root=Path.cwd(),
        stages=stages,
        start=args.start,
        end=args.end,
        force=args.force,
        dry_run=args.dry_run,
        tickers=tickers,
        data_dir=args.data_dir,
    )


if __name__ == "__main__":
    raise SystemExit(main())
