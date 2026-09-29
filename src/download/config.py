"""Load non-secret source settings and required provider credentials."""

from __future__ import annotations

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 uses the standard backport package.
    import tomli as tomllib

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ConfigError


@dataclass(frozen=True)
class Secrets:
    """Credentials required by external data providers."""

    fred_api_key: str
    sec_user_agent: str


@dataclass(frozen=True)
class UniverseConfig:
    source: str
    exchanges: tuple[str, ...]
    url: str


@dataclass(frozen=True)
class MarketConfig:
    provider: str
    interval: str
    auto_adjust: bool
    actions: bool
    threads: bool
    rate_limit_seconds: float
    timeout_seconds: float
    max_retries: int


@dataclass(frozen=True)
class FinancialsConfig:
    provider: str
    company_facts_url_template: str
    submissions_url_template: str
    rate_limit_seconds: float
    timeout_seconds: float
    max_retries: int


@dataclass(frozen=True)
class MacrosConfig:
    provider: str
    base_url: str
    file_type: str
    observation_start: str
    rate_limit_seconds: float
    timeout_seconds: float
    max_retries: int
    series: tuple[str, ...]


@dataclass(frozen=True)
class SourcesConfig:
    raw_dir: Path
    organized_dir: Path
    progress_file: Path
    progress_tmp_file: Path
    progress_lock_file: Path
    preserve_progress_on_success: bool
    universe: UniverseConfig
    market: MarketConfig
    financials: FinancialsConfig
    macros: MacrosConfig


def _read_toml(path: Path, description: str) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            value = tomllib.load(handle)
    except OSError as exc:
        raise ConfigError(f"Unable to read {description} at {path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Invalid TOML in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ConfigError(f"{description} must contain a TOML table")
    return value


def _required_string(values: dict[str, Any], key: str, path: Path) -> str:
    value = values.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"Missing required secret '{key}' in {path}")
    return value.strip()


def load_secrets(repo_root: Path) -> Secrets:
    """Load required API credentials from ``config/secrets.toml``."""
    path = repo_root / "config" / "secrets.toml"
    data = _read_toml(path, "secrets configuration")
    values = data.get("secrets")
    if not isinstance(values, dict):
        raise ConfigError(f"Missing [secrets] table in {path}")
    return Secrets(
        fred_api_key=_required_string(values, "fred_api_key", path),
        sec_user_agent=_required_string(values, "sec_user_agent", path),
    )


def load_sources(repo_root: Path) -> SourcesConfig:
    """Load provider options and resolve data paths against the repository root."""
    path = repo_root / "config" / "sources.toml"
    data = _read_toml(path, "sources configuration")
    try:
        universe = data["universe"]
        market = data["market"]
        financials = data["financials"]
        macros = data["macros"]
        download = data["download"]
        if not all(
            isinstance(section, dict)
            for section in (universe, market, financials, macros, download)
        ):
            raise TypeError("each source section must be a table")
        return SourcesConfig(
            raw_dir=repo_root / str(download["raw_dir"]),
            organized_dir=repo_root / str(download["organized_dir"]),
            progress_file=repo_root / str(download["progress_file"]),
            progress_tmp_file=repo_root / str(download["progress_tmp_file"]),
            progress_lock_file=repo_root / str(download["progress_lock_file"]),
            preserve_progress_on_success=bool(download.get("preserve_progress_on_success", True)),
            universe=UniverseConfig(
                source=str(universe["source"]),
                exchanges=tuple(
                    str(item) for item in universe.get("exchanges", ["Nasdaq", "NYSE"])
                ),
                url=str(universe["url"]),
            ),
            market=MarketConfig(
                provider=str(market["provider"]),
                interval=str(market.get("interval", "1d")),
                auto_adjust=bool(market.get("auto_adjust", False)),
                actions=bool(market.get("actions", True)),
                threads=bool(market.get("threads", False)),
                rate_limit_seconds=float(market.get("rate_limit_seconds", 0.5)),
                timeout_seconds=float(market.get("timeout_seconds", 30)),
                max_retries=int(market.get("max_retries", 3)),
            ),
            financials=FinancialsConfig(
                provider=str(financials["provider"]),
                company_facts_url_template=str(financials["company_facts_url_template"]),
                submissions_url_template=str(financials["submissions_url_template"]),
                rate_limit_seconds=float(financials.get("rate_limit_seconds", 0.2)),
                timeout_seconds=float(financials.get("timeout_seconds", 30)),
                max_retries=int(financials.get("max_retries", 3)),
            ),
            macros=MacrosConfig(
                provider=str(macros["provider"]),
                base_url=str(macros["base_url"]),
                file_type=str(macros.get("file_type", "json")),
                observation_start=str(macros.get("observation_start", "1990-01-01")),
                rate_limit_seconds=float(macros.get("rate_limit_seconds", 0.1)),
                timeout_seconds=float(macros.get("timeout_seconds", 30)),
                max_retries=int(macros.get("max_retries", 3)),
                series=tuple(str(item) for item in macros["series"]),
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ConfigError(f"Invalid or incomplete sources configuration in {path}: {exc}") from exc
