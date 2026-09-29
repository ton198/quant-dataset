"""Command-line entry point for the download-only pipeline."""

from __future__ import annotations

import argparse
import logging
from datetime import date
from pathlib import Path

from build_samples import build_samples
from download.manager import run_download


def _date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD") from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="quant-dataset", description="Download and organize market data")
    subparsers = parser.add_subparsers(dest="command", required=True)
    download = subparsers.add_parser("download", help="download SEC, Yahoo, and FRED data")
    download.add_argument("--stage", choices=["market", "financials", "macros", "organize", "all"],
                          action="append", default=None, help="stage to run; repeat to select multiple")
    download.add_argument("--start", type=_date, help="inclusive market start date (YYYY-MM-DD)")
    download.add_argument("--end", type=_date, help="inclusive market end date (YYYY-MM-DD)")
    download.add_argument("--tickers", help="comma-separated ticker subset")
    download.add_argument("--force", action="store_true", help="ignore existing download progress")
    download.add_argument(
        "--force-rebuild", action="store_true",
        help="regenerate all selected ticker organization outputs, even when complete",
    )
    download.add_argument("--dry-run", action="store_true", help="show planned work without network or writes")
    download.add_argument("--data-dir", type=Path, default=Path("data"),
                          help="base directory for raw, organized, and progress files")
    download.add_argument("--workers", type=int, default=16,
                          help="process workers for per-ticker organization (default: 16)")
    samples = subparsers.add_parser("build-samples", help="build the training sample long table")
    samples.add_argument("--data-dir", type=Path, default=Path("data/organized"),
                         help="organized data directory (default: data/organized)")
    samples.add_argument("--out", type=Path, default=Path("data/output"),
                         help="sample bundle output directory (default: data/output)")
    samples.add_argument("--exclusions-file", type=Path,
                         help="exclusion JSON (defaults to config/universes/exclusions_v1.json)")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse CLI arguments and return the download manager's exit code."""
    args = _parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if args.command == "build-samples":
        result = build_samples(args.data_dir, args.out, args.exclusions_file)
        logging.getLogger(__name__).info(
            "Built %s rows (%s to %s) at %s",
            result["rows"], result["date_start"], result["date_end"], result["output_dir"],
        )
        return 1 if result["failures"] else 0
    if args.command != "download":
        return 2
    stages = args.stage or ["all"]
    if "all" in stages:
        stages = ["market", "financials", "macros", "organize"]
    else:
        stages = list(dict.fromkeys(stages))
    if "market" in stages and (args.start is None or args.end is None):
        _parser().error("--start and --end are required when market stage is selected")
    if args.start and args.end and args.end < args.start:
        _parser().error("--end must be on or after --start")
    if args.workers < 1:
        _parser().error("--workers must be at least 1")
    tickers = [item.strip().upper() for item in args.tickers.split(",") if item.strip()] if args.tickers else None
    return run_download(repo_root=Path.cwd(), stages=stages, start=args.start, end=args.end,
                        force=args.force, dry_run=args.dry_run, tickers=tickers,
                        data_dir=args.data_dir, workers=args.workers,
                        force_rebuild=args.force_rebuild)


if __name__ == "__main__":
    raise SystemExit(main())
