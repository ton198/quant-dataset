"""Regression coverage for Samples filesystem safety and strict QC JSON output."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pandas as pd
import pytest

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from samples.builder import build_samples
from samples.validation import SamplesValidationError, validate_fresh_output

FIXTURES = Path(__file__).parent / "fixtures" / "samples" / "current"


def _organized_data_root(root: Path) -> tuple[Path, Path]:
    data_root = root / "data"
    organized = data_root / "organized"
    organized.mkdir(parents=True)
    return data_root, organized


@pytest.mark.parametrize("workspace_is_foreign", [False, True], ids=["same", "foreign"])
@pytest.mark.parametrize(
    "sibling_name", ["daily-output-backup-2024", "samples-output-prior"], ids=["backup", "output"]
)
def test_existing_data_siblings_are_protected_independent_of_workspace_root(
    tmp_path: Path, workspace_is_foreign: bool, sibling_name: str
) -> None:
    actual_root = tmp_path / "actual-workspace"
    data_root, organized = _organized_data_root(actual_root)
    protected_sibling = data_root / sibling_name
    protected_sibling.mkdir()
    (protected_sibling / "keep.bin").write_bytes(b"preserve")

    if workspace_is_foreign:
        workspace = tmp_path / "foreign-workspace"
        workspace.mkdir()
    else:
        workspace = actual_root

    with pytest.raises(SamplesValidationError, match="protected workspace data path"):
        validate_fresh_output(
            protected_sibling / "unwritten-child", organized, workspace_root=workspace
        )
    assert (protected_sibling / "keep.bin").read_bytes() == b"preserve"


def test_data_sibling_scan_protects_resolved_symlink_targets(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    data_root, organized = _organized_data_root(workspace)
    target = tmp_path / "external-backup"
    target.mkdir()
    (data_root / "custom-store").symlink_to(target, target_is_directory=True)

    with pytest.raises(SamplesValidationError, match="protected workspace data path"):
        validate_fresh_output(target / "unwritten-child", organized, workspace_root=workspace)


def test_nonexistent_fresh_samples_output_is_allowed_with_protected_siblings(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    data_root, organized = _organized_data_root(workspace)
    for name in ("raw", "release-backup-previous", "archive"):
        (data_root / name).mkdir()
    output = data_root / "samples-output"

    assert not output.exists()
    assert validate_fresh_output(output, organized, workspace_root=workspace) == output.resolve()
    assert not output.exists()


def test_workspace_root_must_exist_and_be_a_directory(tmp_path: Path) -> None:
    _, organized = _organized_data_root(tmp_path / "workspace")
    output = tmp_path / "new-output"
    missing_workspace = tmp_path / "missing-workspace"
    with pytest.raises(
        SamplesValidationError, match="workspace root must exist and be a directory"
    ):
        validate_fresh_output(output, organized, workspace_root=missing_workspace)

    workspace_file = tmp_path / "workspace-file"
    workspace_file.write_text("not a directory", encoding="utf-8")
    with pytest.raises(
        SamplesValidationError, match="workspace root must exist and be a directory"
    ):
        validate_fresh_output(output, organized, workspace_root=workspace_file)


def test_extreme_examples_encode_missing_target_as_json_null(tmp_path: Path) -> None:
    workspace = tmp_path / "fixture-copy"
    shutil.copytree(FIXTURES, workspace)
    market_path = workspace / "organized" / "stocks" / "AAA" / "market.csv"
    market = pd.read_csv(market_path)
    signal = market.loc[8, "date"]
    # Keep the missing date on the canonical calendar through another common ticker.
    ccc_dir = workspace / "organized" / "stocks" / "CCC"
    ccc_dir.mkdir()
    shutil.copy2(
        workspace / "organized" / "stocks" / "BBB" / "market.csv",
        ccc_dir / "market.csv",
    )

    # The missing bar is the label entry for signal row 8. The later fivefold open
    # jump still flags that row as extreme, so its one-session return remains null.
    market = market.drop(index=9).reset_index(drop=True)
    market.loc[10, "open"] *= 5.0
    market.to_csv(market_path, index=False)

    output = tmp_path / "fresh-output"
    build_samples(
        workspace / "organized",
        output,
        workspace / "config" / "universes" / "exclusions_v1.json",
        workspace_root=workspace,
        canonical_min_tickers=2,
        staging_tickers=1,
        rank_batch_sessions=6,
    )

    samples = pd.concat(
        [
            pd.read_parquet(path)
            for path in sorted((output / "samples").glob("year=*/*.parquet"))
        ],
        ignore_index=True,
    )
    sample_row = samples.loc[
        samples["asset_id"].eq("AAA") & samples["date"].eq(pd.Timestamp(signal).date())
    ].iloc[0]
    assert sample_row["flag_extreme_label"] == 1
    assert pd.isna(sample_row["target_return_1d"])

    qc = json.loads((output / "qc_report.json").read_text(encoding="utf-8"))
    example = next(
        row
        for row in qc["extreme_labels"]["examples"]
        if row["asset_id"] == "AAA" and row["date"] == signal
    )
    assert example["target_return_1d"] is None
    json.dumps(qc, allow_nan=False)
