"""The single current sample registry and its serialized data contract."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pyarrow as pa

SCHEMA_VERSION = "samples"

MACRO_SERIES = (
    "BAMLH0A0HYM2",
    "CPIAUCSL",
    "CPILFESL",
    "DCOILWTICO",
    "DEXUSEU",
    "DGS10",
    "DGS2",
    "FEDFUNDS",
    "PAYEMS",
    "UNRATE",
    "VIXCLS",
)

MARKET_RAW = (
    "f_raw_return_1d",
    "f_raw_return_5d",
    "f_raw_return_20d",
    "f_raw_volatility_20",
    "f_raw_volume_ratio_20",
    "f_raw_intraday_range",
)
DERIVED_RAW = (
    "f_raw_momentum_60",
    "f_raw_momentum_120",
    "f_raw_volatility_60",
    "f_raw_volume_zscore_60",
)
STOCK_RAW = (*MARKET_RAW, *DERIVED_RAW)
MACRO_RAW = tuple(
    feature
    for series in MACRO_SERIES
    for feature in (f"f_raw_m_{series}", f"f_raw_m_{series}_d1", f"f_raw_m_{series}_d5")
)
RAW_FEATURES = (*STOCK_RAW, *MACRO_RAW)
CS_FEATURES = tuple(f"f_cs_{name.removeprefix('f_raw_')}" for name in STOCK_RAW)
MISS_FEATURES = tuple(f"miss_{name.removeprefix('f_raw_')}" for name in RAW_FEATURES)
LABELS = (
    *tuple(f"target_return_{horizon}d" for horizon in range(1, 31)),
    "excess_5d",
    "excess_21d",
)
FEATURE_LIST = (*RAW_FEATURES, *CS_FEATURES, *MISS_FEATURES)
COLUMN_ORDER = (
    "date",
    "asset_id",
    "is_common",
    "flag_extreme_label",
    *FEATURE_LIST,
    *LABELS,
)

PURGE_SESSIONS = 30
PURGE_SIGNAL_SESSIONS = PURGE_SESSIONS + 1
SPLIT_TRANSITIONS = (
    ("select", "2019-01-01", "2020-12-31"),
    ("screen", "2021-01-01", "2024-12-31"),
    ("reserve", "2025-01-01", None),
)
PURGE_SEMANTICS = (
    "At each populated split boundary, purge the 31 signal sessions immediately before the "
    "boundary: a 30-session label enters at t+1 and exits at t+31. Only boundaries with sessions "
    "in the following split are purged; naturally missing labels at the dataset tail are retained."
)
LABEL_SEMANTICS = (
    "For signal session t, entry is adjusted open at canonical session t+1 and exit for horizon h "
    "is adjusted open at canonical session t+1+h. adjusted_open = open * adj_close / close; "
    "target_return_hd = adjusted_open(t+1+h) / adjusted_open(t+1) - 1. Horizons are positional "
    "canonical-session offsets; exact entry and exit bars must exist and be positive/finite, "
    "with no substitution, forward-fill, or intermediate-bar requirement. excess_5d and excess_21d "
    "subtract the same-date equal-weight target mean among common stocks, excluding extreme-label "
    "rows from the benchmark mean."
)

SAMPLE_SCHEMA = pa.schema(
    [
        pa.field("date", pa.date32()),
        pa.field("asset_id", pa.large_string()),
        pa.field("is_common", pa.bool_()),
        pa.field("flag_extreme_label", pa.uint8()),
        *(pa.field(name, pa.float32()) for name in RAW_FEATURES),
        *(pa.field(name, pa.float32()) for name in CS_FEATURES),
        *(pa.field(name, pa.uint8()) for name in MISS_FEATURES),
        *(pa.field(name, pa.float32()) for name in LABELS),
    ]
)

FEATURE_COUNTS = {
    "raw": len(RAW_FEATURES),
    "cs": len(CS_FEATURES),
    "miss": len(MISS_FEATURES),
    "feature_list": len(FEATURE_LIST),
    "sample_columns": len(COLUMN_ORDER),
}


def semantic_contract() -> dict[str, Any]:
    """Return the canonical, domain-level behavior fingerprint payload."""
    return {
        "schema_version": SCHEMA_VERSION,
        "column_order": list(COLUMN_ORDER),
        "arrow_types": {field.name: str(field.type) for field in SAMPLE_SCHEMA},
        "feature_registry": {
            "raw": list(RAW_FEATURES),
            "cross_sectional": list(CS_FEATURES),
            "missing_indicators": list(MISS_FEATURES),
            "labels": list(LABELS),
        },
        "features": {
            "cross_sectional": "per-date average rank transformed by inverse normal CDF at "
            "(rank - 0.5) / non-null count; only the ten stock-varying raw columns are ranked",
            "macro_differences": (
                "d1 and d5 are positional differences on the canonical axis; no forward-fill"
            ),
            "missing_indicators": "uint8; one exactly when its paired raw feature is null",
            "canonical_axis": (
                "dates with at least canonical_min_tickers common-stock market inputs"
            ),
            "derived_windows": "60 and 120 canonical sessions, using only past/current values",
        },
        "labels": LABEL_SEMANTICS,
        "split_boundaries": {
            "fit": [None, "2018-12-31"],
            "select": ["2019-01-01", "2020-12-31"],
            "screen": ["2021-01-01", "2024-12-31"],
            "reserve": ["2025-01-01", None],
        },
        "purge_sessions": PURGE_SESSIONS,
        "purge_signal_sessions": PURGE_SIGNAL_SESSIONS,
        "purge_semantics": PURGE_SEMANTICS,
        "data_quality_flags": {
            "flag_extreme_label": {
                "dtype": "uint8",
                "rule": "1 when any 1..30-session adjusted-open label window crosses "
                "a consecutive-session price ratio outside [0.5, 2.0]",
                "handling": "keep flagged rows; exclude from same-date excess benchmark means",
            }
        },
        "is_common": "suffix heuristic; rows remain eligible for feature ranks",
    }


def semantic_fingerprint() -> str:
    payload = json.dumps(semantic_contract(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def schema_fingerprint() -> str:
    description = [(field.name, str(field.type), field.nullable) for field in SAMPLE_SCHEMA]
    encoded = json.dumps(description, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "COLUMN_ORDER",
    "CS_FEATURES",
    "DERIVED_RAW",
    "FEATURE_COUNTS",
    "FEATURE_LIST",
    "LABELS",
    "LABEL_SEMANTICS",
    "MACRO_RAW",
    "MACRO_SERIES",
    "MARKET_RAW",
    "MISS_FEATURES",
    "PURGE_SEMANTICS",
    "PURGE_SESSIONS",
    "PURGE_SIGNAL_SESSIONS",
    "RAW_FEATURES",
    "SAMPLE_SCHEMA",
    "SCHEMA_VERSION",
    "SPLIT_TRANSITIONS",
    "STOCK_RAW",
    "schema_fingerprint",
    "semantic_contract",
    "semantic_fingerprint",
]
