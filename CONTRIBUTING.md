# Contributing

This repository is a single-maintainer offline data pipeline, and fixes and improvements via issues / PRs are welcome. Before you start, read [README.md](README.md) and [AGENT.md](AGENT.md): the latter is the task routing table (required reading and code entry points per task type) and the invariants list.

**English** | [简体中文](CONTRIBUTING.zh-CN.md)

## Development setup

- Python ≥ 3.10 (see `pyproject.toml`).
- Create a virtual environment and install (with dev dependencies):

  ```bash
  python -m venv .venv
  .venv/bin/pip install -e '.[dev]'
  ```

- If the workspace already has `.venv/`, there is no need to reinstall — just run tests or commands. Running the CLI from the source tree needs `PYTHONPATH=src`, for example:

  ```bash
  PYTHONPATH=src .venv/bin/python -m cli.main --help
  ```

## Running tests

The tests are fully offline: they make no real HTTP calls and do not depend on the `data/` artifacts. Baseline: **61 passed / 2 skipped, about 40 seconds**.

```bash
# everything
PYTHONPATH=src .venv/bin/python -m pytest -q

# pin a single test file
.venv/bin/python -m pytest tests/test_build_samples.py -q
```

When adding or fixing behavior, add offline tests (use `tmp_path` fixtures); make sure the full suite passes before opening a PR.

## Code style

```bash
ruff check .
ruff format .
```

- Use ruff for both linting and formatting; optionally configure pre-commit to run it before each commit.
- When docs and reality disagree, reality (`src/`, `config/`, `data/output/manifest.json`) wins; fix the docs in the same PR.

## Branch naming

- `feat/<short-description>`: new feature
- `fix/<short-description>`: bug fix
- `docs/<short-description>`: documentation only
- `chore/<short-description>`: build, dependencies, chores

## Commit titles

Format: `<type>: <short description>`, with type one of `feat:` / `fix:` / `docs:` / `chore:` / `test:`. One commit does one thing, and the description says what it does.

## PR conventions

- Small PRs: keep them under ~400 lines; split large changes into independently reviewable PRs.
- The PR description states the motivation, the change, and how you verified it (which commands, what results).
- For data/artifact changes, describe the impact on the row counts/hashes registered in `data/output/manifest.json`; any change to row counts, labels, or flags after a rebuild must be called out explicitly.
- Merge by squash; CI must be green before merge.

## Issue policy

- Small changes (typos, small fixes, docs) do not need an issue first — just open a PR.
- Large features, contract/baseline changes, or contested designs: open an issue to discuss first, and agree before writing code.

## Data and secrets

- `data/` and `config/secrets.toml` **never enter git** (already covered by `.gitignore`); do not edit files under `data/raw/` by hand.
- The sha256 in `manifest.json` is the source of truth for data versions; consumers verify by hash.
- Changes to `download` / `build-samples` options, defaults, or exit codes must update [docs/user/cli.md](docs/user/cli.md) in the same PR (AGENT.md invariant 8).
- Documentation bilingual sync rule: same as AGENT.md — any documentation change must update both language versions in the same PR; if they disagree, the English version is authoritative.

## New contributor entry points

1. [README.md](README.md): what the repo is, current data size, quickstart.
2. [AGENT.md](AGENT.md): task routing table, invariants, command cheat sheet.
3. [docs/developer/testing.md](docs/developer/testing.md): test layout, offline conventions, tmp fixture usage.
