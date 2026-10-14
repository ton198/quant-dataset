# Raw Download Internals

The retained download pipeline fetches provider payloads only. It does not organize data or create derived panels.

## Stages and flow

```text
CLI → manager → provider modules → data/raw + resumable progress
```

`src/cli/main.py` accepts `market`, `financials`, `macros`, and `all`. `src/download/manager.py` resolves config, universe, lock and progress; provider modules own network access. `all` expands to the three raw stages. Market requires inclusive `--start` and `--end` dates.

| Module | Responsibility |
|---|---|
| `config.py` | Parse `config/sources.toml` and private provider secrets. |
| `universe.py` | Fetch/cache SEC exchange universe and resolve ticker/CIK identities. |
| `market.py` | Fetch Yahoo daily bars. |
| `financials.py` | Fetch SEC Company Facts, submissions and referenced historical submission pages. |
| `macros.py` | Fetch configured FRED observations. |
| `progress.py` | Atomic resumable progress and process lock. |

Raw SEC financial metadata identifies filings but is not the report-document corpus. Full report sample retrieval is separately implemented in `src/filings/report_samples.py` and described in [the user guide](../user/filings.md).

## Configuration and paths

`config/sources.toml` contains non-secret provider URLs, forms, series, throttles and raw/progress paths. `config/secrets.toml` contains provider credentials and must remain private. `--data-dir` remaps raw and progress locations; CLI smoke tests must use a temporary isolated directory. `--dry-run` performs no network request or writes.

Progress is maintained per stage/item. Completed items are skipped on resume; `--force` resets selected progress. Do not add network integration tests; use mocked transport. Never overwrite or clean existing raw data as part of code maintenance.
