# 贡献指南

本仓库是单人维护的离线数据流水线，欢迎通过 issue / PR 提交修复与改进。开始前请先读 [README.zh-CN.md](README.zh-CN.md) 与 [AGENT.zh-CN.md](AGENT.zh-CN.md)：后者是任务路由表（按任务类型指出必读文档与代码入口）与不变式清单。

[English](CONTRIBUTING.md) | **简体中文**

## 开发环境搭建

- Python ≥ 3.10（以 `pyproject.toml` 为准）。
- 新建虚拟环境并安装（含开发依赖）：

  ```bash
  python -m venv .venv
  .venv/bin/pip install -e '.[dev]'
  ```

- 工作区已有 `.venv/` 时无需重装，直接跑测试或命令即可。在源码树内运行 CLI 需带 `PYTHONPATH=src`，例如：

  ```bash
  PYTHONPATH=src .venv/bin/python -m cli.main --help
  ```

## 跑测试

测试全部离线：不发真实 HTTP，也不依赖 `data/` 实物。基线：**61 passed / 2 skipped，约 40 秒**。

```bash
# 全量
PYTHONPATH=src .venv/bin/python -m pytest -q

# 锁定单个测试文件
.venv/bin/python -m pytest tests/test_build_samples.py -q
```

新增或修复行为时请补离线测试（用 `tmp_path` 夹具）；提交 PR 前确保全量测试通过。

## 代码风格

```bash
ruff check .
ruff format .
```

- 用 ruff 统一检查与格式化；可选配置 pre-commit，在提交前自动执行。
- 文档与实物不一致时，以 `src/`、`config/`、`data/output/manifest.json` 为准，并在同一 PR 内修补文档。

## 分支命名

- `feat/<简短描述>`：新功能
- `fix/<简短描述>`：缺陷修复
- `docs/<简短描述>`：仅文档
- `chore/<简短描述>`：构建、依赖、杂务

## Commit 标题

格式为 `<type>: <简短描述>`，type 取 `feat:` / `fix:` / `docs:` / `chore:` / `test:`。一个 commit 做一件事，描述说明做了什么。

## PR 约定

- 小 PR：控制在约 400 行以内；大改动拆成可独立审阅的多个 PR。
- PR 描述写清动机、改动内容与验证方式（跑了哪些命令、结果如何）。
- 数据/产物相关改动请说明对 `data/output/manifest.json` 登记的行数/哈希有何影响；重建后行数、标签、flag 的变化必须明确写出。
- 合并方式为 squash merge；CI 全绿才能合并。

## Issue 约定

- 小改动（拼写、小修复、文档）不必先开 issue，直接提 PR。
- 大特性、契约/基线变更、有争议的设计先开 issue 讨论，达成一致后再动代码。

## 数据与秘密

- `data/` 与 `config/secrets.toml` **永不入库**（`.gitignore` 已覆盖）；不要手工编辑 `data/raw/` 文件。
- 数据版本以 `manifest.json` 中的 sha256 为真源，消费者按哈希验货。
- 改动 `download` / `build-samples` 的参数、默认值或退出码，必须在同一 PR 同步更新 [docs/user/cli.zh-CN.md](docs/user/cli.zh-CN.md)（AGENT.md 不变式 8）。
- 文档双语同步规则同 [AGENT.zh-CN.md](AGENT.zh-CN.md)：任何文档改动必须同 PR 更新两种语言版本；不一致以英文版为准。

## 新贡献者入口

1. [README.zh-CN.md](README.zh-CN.md)：仓库定位、当前数据规模、快速开始。
2. [AGENT.zh-CN.md](AGENT.zh-CN.md)：任务路由表、不变式、常用命令速查。
3. [docs/developer/testing.zh-CN.md](docs/developer/testing.zh-CN.md)：测试布局、离线约定与 tmp 夹具用法。
