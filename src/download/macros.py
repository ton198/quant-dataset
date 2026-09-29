"""Fetch FRED observation series and keep content-addressed histories."""

from __future__ import annotations

import hashlib
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Secrets, SourcesConfig
from .progress import Progress, save_atomic

logger = logging.getLogger(__name__)


def _manifest(path: Path) -> dict[str, Any]:
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


def fetch_macros(
    cfg: SourcesConfig, secrets: Secrets, raw_dir: Path, progress: Progress
) -> list[Path]:
    """Download configured series, updating per-series progress as each succeeds."""
    root = raw_dir / "fred"
    root.mkdir(parents=True, exist_ok=True)
    manifest_path = root / "manifest.json"
    manifest = _manifest(manifest_path)
    resources = manifest["resources"]
    outputs: list[Path] = []
    for series_id in cfg.macros.series:
        query = {
            "series_id": series_id,
            "file_type": cfg.macros.file_type,
            "observation_start": cfg.macros.observation_start,
        }
        logical_url = cfg.macros.base_url + "?" + urllib.parse.urlencode(query)
        url = logical_url + "&" + urllib.parse.urlencode({"api_key": secrets.fred_api_key})
        cached = None
        for item in reversed(resources):
            if (
                isinstance(item, dict)
                and item.get("logical_key") == f"observations:{series_id}"
                and item.get("url") == logical_url
            ):
                candidate = root / str(item.get("path", ""))
                if candidate.is_file() and hashlib.sha256(
                    candidate.read_bytes()
                ).hexdigest() == item.get("sha256"):
                    cached = candidate
                    break
        if cached is not None:
            outputs.append(cached)
            progress.stages.setdefault("macros", {})[series_id] = "done"
            save_atomic(progress, cfg.progress_file, cfg.progress_tmp_file)
            continue
        last_error: Exception | None = None
        for attempt in range(max(1, cfg.macros.max_retries)):
            if cfg.macros.rate_limit_seconds:
                time.sleep(cfg.macros.rate_limit_seconds)
            response = None
            try:
                request = urllib.request.Request(url, headers={"Accept": "application/json"})
                response = urllib.request.urlopen(request, timeout=cfg.macros.timeout_seconds)
                content = response.read()
                json.loads(content.decode("utf-8"))
                digest = hashlib.sha256(content).hexdigest()
                directory = root / series_id
                directory.mkdir(parents=True, exist_ok=True)
                destination = directory / f"{digest}.json"
                if not destination.exists():
                    destination.write_bytes(content)
                resources.append(
                    {
                        "logical_key": f"observations:{series_id}",
                        "path": str(destination.relative_to(root)),
                        "sha256": digest,
                        "url": logical_url,
                        "fetched_at_utc": datetime.now(timezone.utc)
                        .isoformat()
                        .replace("+00:00", "Z"),
                        "attempts": attempt + 1,
                        "byte_size": len(content),
                        "status": "done",
                    }
                )
                outputs.append(destination)
                progress.stages.setdefault("macros", {})[series_id] = "done"
                save_atomic(progress, cfg.progress_file, cfg.progress_tmp_file)
                last_error = None
                break
            except (
                urllib.error.URLError,
                TimeoutError,
                OSError,
                ValueError,
                UnicodeDecodeError,
            ) as exc:
                last_error = exc
                if attempt + 1 < max(1, cfg.macros.max_retries):
                    time.sleep(min(2**attempt, 8))
            finally:
                if response is not None:
                    response.close()
        if last_error is not None:
            progress.stages.setdefault("macros", {})[series_id] = f"failed:{last_error}"
            save_atomic(progress, cfg.progress_file, cfg.progress_tmp_file)
            logger.error("FRED request failed for %s: %s", series_id, last_error)
    manifest["resources"] = _manifest_mapping(resources)
    temporary = manifest_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(manifest_path)
    return outputs
