"""RunSpec canonicalization and immutability tests."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from filings import RunSpec  # noqa: E402


def test_runspec_recursively_freezes_nested_json_and_hash() -> None:
    supplied: dict[str, Any] = {
        "nested": ({"values": [1, {"label": "before"}]},),
    }
    spec = RunSpec(policy=supplied)
    original_hash = spec.sha256

    supplied["nested"][0]["values"][1]["label"] = "external mutation"
    supplied["nested"][0]["values"].append(2)

    assert spec.sha256 == original_hash
    assert spec.canonical_dict()["policy"] == {
        "nested": [{"values": [1, {"label": "before"}]}],
    }
    with pytest.raises(TypeError):
        spec.policy["nested"][0]["values"][1]["label"] = "internal mutation"
    with pytest.raises(AttributeError):
        spec.policy["nested"][0]["values"].append(2)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_runspec_rejects_nonfinite_json_numbers_at_construction(value: float) -> None:
    with pytest.raises(ValueError, match="canonical JSON"):
        RunSpec(policy={"nested": (value,)})
