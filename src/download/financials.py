"""Download SEC company facts and submissions as immutable JSON snapshots."""

from __future__ import annotations

import hashlib
import json
import logging
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Secrets, SourcesConfig
from .errors import DownloadError

logger = logging.getLogger(__name__)


def _read_manifest(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(value, dict):
            resources = value.get("resources", {})
            if isinstance(resources, dict):
                flattened: list[dict[str, Any]] = []
                for logical_key, versions in resources.items():
                    for record in versions if isinstance(versions, list) else [versions]:
                        if isinstance(record, dict):
                            flattened.append({"logical_key": logical_key, **record})
                return {"resources": flattened}
            if isinstance(resources, list):
                return {"resources": resources}
    except (OSError, ValueError):
        pass
    return {"resources": []}


def _manifest_mapping(resources: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    mapped: dict[str, list[dict[str, Any]]] = {}
    for record in resources:
        key = str(record.get("logical_key", ""))
        if key:
            mapped.setdefault(key, []).append(
                {name: value for name, value in record.items() if name != "logical_key"}
            )
    return mapped


def _fetch(
    url: str, user_agent: str, timeout: float, retries: int, rate_limit: float
) -> tuple[bytes, int]:
    last_error: Exception | None = None
    for attempt in range(max(1, retries)):
        if rate_limit > 0:
            time.sleep(rate_limit)
        response = None
        try:
            request = urllib.request.Request(
                url, headers={"User-Agent": user_agent, "Accept": "application/json"}
            )
            response = urllib.request.urlopen(request, timeout=timeout)
            return response.read(), attempt + 1
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt + 1 < max(1, retries):
                time.sleep(min(2**attempt, 8))
        finally:
            if response is not None:
                response.close()
    raise DownloadError(f"SEC request failed for {url}: {last_error}") from last_error


def fetch_financials(cik10: str, cfg: SourcesConfig, secrets: Secrets, raw_dir: Path) -> list[Path]:
    """Fetch Company Facts, Submissions, and referenced historical submissions."""
    root = raw_dir / "sec" / "financials"
    root.mkdir(parents=True, exist_ok=True)
    manifest_path = root / "manifest.json"
    manifest = _read_manifest(manifest_path)
    resources = manifest.get("resources", [])
    if not isinstance(resources, list):
        resources = []
    facts_url = cfg.financials.company_facts_url_template.format(cik10=cik10)
    submissions_url = cfg.financials.submissions_url_template.format(cik10=cik10)
    requests: list[tuple[str, str]] = [
        (f"companyfacts:{cik10}", facts_url),
        (f"submissions:{cik10}", submissions_url),
    ]
    written: list[Path] = []
    seen_keys: set[str] = set()

    def obtain(logical_key: str, url: str) -> tuple[Path, Any]:
        matching = [
            item
            for item in resources
            if isinstance(item, dict)
            and item.get("logical_key") == logical_key
            and item.get("url") == url
        ]
        for item in reversed(matching):
            relative = item.get("path")
            expected = item.get("sha256")
            candidate = root / str(relative) if isinstance(relative, str) else None
            if (
                candidate
                and candidate.is_file()
                and hashlib.sha256(candidate.read_bytes()).hexdigest() == expected
            ):
                written.append(candidate)
                seen_keys.add(logical_key)
                return candidate, json.loads(candidate.read_text(encoding="utf-8"))
        content, attempts = _fetch(
            url,
            secrets.sec_user_agent,
            cfg.financials.timeout_seconds,
            cfg.financials.max_retries,
            cfg.financials.rate_limit_seconds,
        )
        try:
            payload = json.loads(content.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise DownloadError(f"SEC returned invalid JSON for {url}: {exc}") from exc
        digest = hashlib.sha256(content).hexdigest()
        destination = root / f"{digest}.json"
        if not destination.exists():
            destination.write_bytes(content)
        record = {
            "logical_key": logical_key,
            "path": destination.name,
            "sha256": digest,
            "url": url,
            "fetched_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "attempts": attempts,
            "byte_size": len(content),
            "status": "done",
        }
        resources.append(record)
        written.append(destination)
        seen_keys.add(logical_key)
        return destination, payload

    obtain(*requests[0])
    _, submissions = obtain(*requests[1])
    files = submissions.get("filings", {}).get("files", []) if isinstance(submissions, dict) else []
    for item in files if isinstance(files, list) else []:
        name = item.get("name") if isinstance(item, dict) else None
        if (
            isinstance(name, str)
            and name.startswith(f"CIK{cik10}-submissions-")
            and name.endswith(".json")
        ):
            url = "https://data.sec.gov/submissions/" + name
            obtain(f"submissions-page:{cik10}:{name}", url)
    # Cache hits are also returned, so callers can organize existing inputs.
    for logical_key, url in requests:
        if logical_key not in seen_keys:
            for item in reversed(resources):
                if (
                    isinstance(item, dict)
                    and item.get("logical_key") == logical_key
                    and item.get("url") == url
                ):
                    path = root / str(item.get("path", ""))
                    if path.is_file():
                        written.append(path)
                        break
    manifest["resources"] = _manifest_mapping(resources)
    temporary = manifest_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(manifest_path)
    return list(dict.fromkeys(written))
