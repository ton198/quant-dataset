"""Consistency checks between the current builder and its public registry."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from samples import builder
from samples.contracts import (
    CS_FEATURES,
    FEATURE_LIST,
    LABELS,
    MISS_FEATURES,
    RAW_FEATURES,
    STOCK_RAW,
)


def test_cross_sectional_values_and_missing_mirrors_follow_current_registry() -> None:
    frame = pd.DataFrame(
        {
            "date": pd.to_datetime(["2024-01-02"] * 4),
            "asset_id": ["AAA", "BBB", "CCC", "DDD"],
        }
    )
    for column in STOCK_RAW:
        frame[column] = np.nan
    frame["f_raw_return_1d"] = np.asarray([0.1, 0.2, np.nan, 0.4], dtype=np.float32)

    builder._add_cross_sectional_features(frame)
    frame["miss_return_1d"] = frame["f_raw_return_1d"].isna().astype(np.uint8)

    assert frame["f_cs_return_1d"].isna().tolist() == [False, False, True, False]
    assert frame["miss_return_1d"].tolist() == [0, 0, 1, 0]
    assert frame.loc[frame["f_raw_return_1d"].notna(), "f_cs_return_1d"].is_monotonic_increasing
    assert len(RAW_FEATURES) == 43
    assert len(CS_FEATURES) == 10
    assert len(MISS_FEATURES) == 43
    assert len(FEATURE_LIST) == 96
    assert len(LABELS) == 32
    assert list(FEATURE_LIST) == [*RAW_FEATURES, *CS_FEATURES, *MISS_FEATURES]
