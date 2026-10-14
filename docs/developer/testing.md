**English** | [简体中文](testing.zh-CN.md)

# Testing

Tests are offline. Network behavior must use mocked transport; never contact SEC, Yahoo, or FRED. Tests should use `tmp_path` and must not write to preserved `data/`, baselines, archive snapshots, or credentials.

## Current retained suite

```bash
uv run pytest -q
uv run ruff check
```

Focused suites:

```bash
uv run pytest -q tests/test_download.py tests/test_progress_lock.py
uv run pytest -q tests/test_filing_catalog.py tests/test_filing_sec_client.py
uv run pytest -q tests/test_content_parse.py tests/test_report_samples_workflow.py
```

`test_report_samples_workflow.py` exercises mocked SEC requests, filing selection/fallback, worker behavior, resume, safe paths, streaming shards and validator rejection cases. `test_content_parse.py` covers cleaned blocks and content structure. `test_filing_catalog.py` reads local cache fixtures only. `test_download.py` covers raw configuration, progress, locking, CLI rejection and dry-run behavior.

For CLI smoke, use help and a dry run with an isolated directory. Never run a real provider download as a test.

## Test design

Pin consumer-visible behavior: schema and types, identity, selection precedence, partial/error statuses, resume transitions, bounded concurrency, safe paths and cleanup. Avoid tests that only assert wiring or implementation details. New permanent tests should use deterministic fixtures and independently assert expected outcomes. Remove tests for retired products rather than preserving dead interfaces.
