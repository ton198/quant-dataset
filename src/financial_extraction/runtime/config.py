"""Provider credentials and numeric parsing configuration for extraction."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 uses the declared TOML backport.
    import tomli as tomllib

from ..domain import ExtractionLimits, NumericPolicy


class ExtractionConfigError(ValueError):
    """The extraction TOML file is missing or invalid."""


@dataclass(frozen=True, slots=True)
class ExtractionConfig:
    base_url: str
    model: str
    api_key: str = field(repr=False)
    timeout_seconds: float
    limits: ExtractionLimits
    numeric_policies: tuple[NumericPolicy, ...]
    structured_mode: str = "json_schema"
    workers: int = 4
    model_workers: int = 1
    prefetch_windows: int = 4


def load_extraction_config(
    path: str | Path, *, secrets_path: str | Path | None = None
) -> ExtractionConfig:
    source = Path(path).expanduser()
    try:
        with source.open("rb") as stream:
            data: Any = tomllib.load(stream)
    except OSError as exc:
        raise ExtractionConfigError(f"cannot read extraction config: {source}") from exc
    except (UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ExtractionConfigError("extraction config must be valid UTF-8 TOML") from exc
    provider = data.get("provider") if isinstance(data, dict) else None
    extraction = data.get("extraction") if isinstance(data, dict) else None
    if not isinstance(provider, dict) or not isinstance(extraction, dict):
        raise ExtractionConfigError("config requires [provider] and [extraction] tables")
    base_url = provider.get("base_url")
    model = provider.get("model")
    if "api_key_env" in provider or "api_key" in provider:
        raise ExtractionConfigError("provider credentials belong only in secrets.toml [secrets].api_key")
    for name, value in (("base_url", base_url), ("model", model)):
        if isinstance(value, str) and value.casefold().startswith(("replace_", "your_")):
            raise ExtractionConfigError(f"provider.{name} must be configured")
    structured_mode = provider.get("structured_mode", "json_schema")
    if not isinstance(structured_mode, str) or structured_mode not in {"json_schema", "json_object"}:
        raise ExtractionConfigError("provider.structured_mode must be json_schema or json_object")
    timeout = provider.get("timeout_seconds", 120)
    if any(
        not isinstance(value, str) or not value.strip()
        for value in (base_url, model)
    ):
        raise ExtractionConfigError(
            "provider.base_url and model must be non-empty strings"
        )
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        raise ExtractionConfigError("provider.timeout_seconds must be positive")
    try:
        limits = ExtractionLimits(
            max_request_bytes=extraction.get("max_request_bytes", 100_000),
            max_output_tokens=extraction.get("max_output_tokens", 12_000),
        )
    except TypeError as exc:
        raise ExtractionConfigError("extraction limits must be integers") from exc
    if any(
        not isinstance(value, int) or isinstance(value, bool) or value <= 0
        for value in (limits.max_request_bytes, limits.max_output_tokens)
    ):
        raise ExtractionConfigError("extraction limits must be positive integers")
    workers = extraction.get("workers", 4)
    model_workers = extraction.get("model_workers", 1)
    prefetch_windows = extraction.get("prefetch_windows", 4)
    for name, value in (("workers", workers), ("model_workers", model_workers), ("prefetch_windows", prefetch_windows)):
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ExtractionConfigError(f"extraction.{name} must be a positive integer")
    if model_workers > workers:
        raise ExtractionConfigError("extraction.model_workers must not exceed extraction.workers")
    if model_workers > prefetch_windows:
        raise ExtractionConfigError("extraction.model_workers must not exceed extraction.prefetch_windows")
    policies_table = data.get("numeric_policies", {})
    if not isinstance(policies_table, dict):
        raise ExtractionConfigError("[numeric_policies] must be a table of named policies")
    policies: list[NumericPolicy] = []
    try:
        for policy_id, values in policies_table.items():
            if not isinstance(values, dict):
                raise ExtractionConfigError(f"numeric policy {policy_id!r} must be a table")
            policies.append(
                NumericPolicy(
                    policy_id=policy_id,
                    decimal_separator=values["decimal_separator"],
                    group_separator=values.get("group_separator"),
                    allow_parentheses_negative=values.get("allow_parentheses_negative", False),
                    allow_leading_sign=values.get("allow_leading_sign", True),
                    allow_trailing_sign=values.get("allow_trailing_sign", False),
                    allowed_prefixes=tuple(values.get("allowed_prefixes", ())),
                    allowed_suffixes=tuple(values.get("allowed_suffixes", ())),
                )
            )
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, ExtractionConfigError):
            raise
        raise ExtractionConfigError("invalid numeric policy definition") from exc
    secrets_source = Path(secrets_path).expanduser() if secrets_path is not None else source.parent / "secrets.toml"
    try:
        with secrets_source.open("rb") as stream:
            secrets_data = tomllib.load(stream)
    except OSError:
        raise ExtractionConfigError("cannot read secrets configuration") from None
    except (UnicodeError, tomllib.TOMLDecodeError):
        raise ExtractionConfigError("secrets configuration must be valid UTF-8 TOML") from None
    secrets = secrets_data.get("secrets")
    api_key = secrets.get("api_key") if isinstance(secrets, dict) else None
    if not isinstance(api_key, str) or not api_key.strip():
        raise ExtractionConfigError("secrets configuration requires a non-empty [secrets].api_key")
    if api_key.casefold().startswith(("your_", "replace_")):
        raise ExtractionConfigError("[secrets].api_key must be configured")
    return ExtractionConfig(
        base_url=base_url,
        model=model,
        timeout_seconds=float(timeout),
        limits=limits,
        numeric_policies=tuple(policies),
        structured_mode=structured_mode,
        api_key=api_key,
        workers=workers,
        model_workers=model_workers,
        prefetch_windows=prefetch_windows,
    )
