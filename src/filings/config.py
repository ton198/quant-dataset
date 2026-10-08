"""SEC-only configuration loading for bounded filing acquisition commands."""

from __future__ import annotations

from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 uses the declared TOML backport dependency.
    import tomli as tomllib

from .sec_client import validate_sec_user_agent


class FilingConfigError(ValueError):
    """Raised when the SEC contact configuration is missing or invalid."""


def load_sec_user_agent(path: Path) -> str:
    """Load and validate only ``[secrets].sec_user_agent`` from a TOML file.

    This intentionally does not call the broader market secrets loader, which
    also requires an unrelated FRED API key.
    """
    source = Path(path).expanduser()
    try:
        with source.open("rb") as handle:
            document: dict[str, Any] = tomllib.load(handle)
    except OSError as exc:
        raise FilingConfigError(f"Unable to read SEC contact configuration at {source}") from exc
    except UnicodeError:
        raise FilingConfigError("SEC contact configuration must use valid UTF-8 TOML") from None
    except tomllib.TOMLDecodeError:
        raise FilingConfigError("Invalid TOML in SEC contact configuration") from None

    secrets = document.get("secrets")
    if not isinstance(secrets, dict):
        raise FilingConfigError("SEC contact configuration must contain a [secrets] table")
    user_agent = secrets.get("sec_user_agent")
    if not isinstance(user_agent, str) or not user_agent.strip():
        raise FilingConfigError("SEC contact configuration must define sec_user_agent")
    try:
        return validate_sec_user_agent(user_agent)
    except ValueError:
        raise FilingConfigError(
            "sec_user_agent must include a non-placeholder contact email"
        ) from None
