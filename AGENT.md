# AGENT navigation

This repository retains three raw download stages (`market`, `financials`, `macros`) and a separate SEC report-sample build/validation workflow. Preserve user data: existing `data/`, `baselines/`, archive snapshots, and `config/secrets.toml` are out of scope for deletion or rewriting. All network tests must mock SEC, Yahoo, and FRED.

**English** | [简体中文](AGENT.zh-CN.md)

## Task routing

| Task | Entry points | Verification |
|---|---|---|
| Raw downloads | `src/cli/main.py`, `src/download/{manager,market,financials,macros,universe,progress,config}.py`, `config/sources.toml` | `tests/test_download.py`; use isolated `--data-dir` for CLI smoke. |
| SEC report samples | `src/filings/{catalog,document_selection,sec_client,download,content_parse,report_samples,cli}.py` | `tests/test_report_samples_workflow.py`, `tests/test_content_parse.py`; mock all HTTP. |
| Filing identity/cache readers | `src/filings/{catalog,models}.py` | `tests/test_filing_catalog.py`; use local fixtures only. |
| CLI or config | `src/cli/main.py`, `src/filings/cli.py`, `src/download/config.py` | Check `--help`, argument rejection, and isolated offline/dry-run paths. |

## Invariants

1. Raw provider data and retained user artifacts are not hand-edited or deleted during code cleanup.
2. SEC document acquisition is bounded by selection, worker count, response limits and private work directories. Source bytes are temporary; checkpointed outputs contain cleaned samples and diagnostics only.
3. Output/work paths must be new and non-overlapping with cache or protected user data. Reject symlink components and non-empty outputs.
4. Resume reuses validated cleaned checkpoints scoped to the current requested filing IDs; it does not rely on old downloaded source bytes.
5. Report sample blocks use the exact schema documented in `docs/user/filings.md`; preserve `sample_id` identity, filed-year partitions, unique shard paths, and diagnostic visibility.
6. Tests never access live SEC, Yahoo, or FRED. Test behavior through mocked transport and local fixtures.
7. Update English and Chinese user guides together when CLI behavior changes.

## Documentation map

- [CLI manual](docs/user/cli.md) / [中文](docs/user/cli.zh-CN.md)
- [SEC report samples](docs/user/filings.md) / [中文](docs/user/filings.zh-CN.md)
- [Download internals](docs/developer/download.md) / [中文](docs/developer/download.zh-CN.md)
- [Architecture](docs/developer/architecture.md) / [中文](docs/developer/architecture.zh-CN.md)
- [Testing](docs/developer/testing.md) / [中文](docs/developer/testing.zh-CN.md)
