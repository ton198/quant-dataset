# quant-dataset

Offline raw-data ingestion for Yahoo market bars, SEC Company Facts/submissions, and FRED observations, plus bounded SEC report-sample construction and validation. The project does not train models or run backtests.

[English] | [简体中文](README.zh-CN.md)

## Install

```bash
uv sync --extra dev
quant-dataset --help
```

Copy `config/secrets.example.toml` to `config/secrets.toml` and set a FRED API key and a real SEC contact User-Agent when those providers are needed. Never commit credentials. No network behavior should be tested against live providers; tests use mocked transport.

## Raw provider downloads

```bash
quant-dataset download --stage macros
quant-dataset download --stage financials
quant-dataset download --stage market --start 2024-01-01 --end 2024-12-31 --tickers AAPL
quant-dataset download --stage all --start 2024-01-01 --end 2024-12-31
```

Stages are `market`, `financials`, `macros`, and `all`. `all` includes the date-bounded market stage. Outputs remain under `data/raw/`; progress is resumable. `financials` stores Company Facts and submissions resources, not full filing documents. The downloader does not organize raw data into derived panels.

## SEC report samples

`filings build-report-samples` scans the local SEC financials cache for the configured filing forms, fetches selected report documents from SEC, cleans report text into typed blocks, and writes sharded Parquet plus diagnostics. Network requests are limited to the SEC workflow and tests must mock them. A valid contact in `config/secrets.toml` is required for a real build.

```bash
quant-dataset filings build-report-samples \
  --cache-root data/raw/sec/financials \
  --out /path/to/new/report-samples \
  --work-dir /path/to/new/work \
  --limit 10 --workers 4
quant-dataset filings validate-report-samples \
  --out /path/to/report-samples --cache-root data/raw/sec/financials
```

Choose new, empty output/work directories. Do not point commands at preserved data, baselines, or cache contents. Build supports checkpoints and `--resume`; source bytes are temporary and are removed after processing. Validation checks exact schema, partitions, shards, row identities and output completeness. `--limit` bounds work for an offline/mock smoke; it does not make a real run offline.

## Documentation

- [CLI guide](docs/user/cli.md)
- [SEC report samples](docs/user/filings.md)
- [Download internals](docs/developer/download.md)
- [Architecture](docs/developer/architecture.md)
- [Testing](docs/developer/testing.md)
- [AGENT navigation](AGENT.md)

## Data protection

Existing `data/`, `baselines/`, archive snapshots, and `config/secrets.toml` are user data. Do not delete or rewrite them as part of code cleanup.

## License

MIT — see [LICENSE](LICENSE).
