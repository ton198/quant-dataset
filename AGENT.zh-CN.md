# AGENT 导航

**本仓库 = 离线数据流水线 + 冻结训练样本包**（`data/raw/` → `data/organized/` → `data/output/`）。改代码前先读 §1 路由表：按任务类型只读指出的最少文档，再动 `src/`。数据"看起来不对"时，先看 §6。

[English](AGENT.md) | **简体中文**

## 1. 任务路由表（改代码前必读）

| 任务类型 | 必读文档（≤2） | 代码入口 | 验证方式 |
|---|---|---|---|
| 改标签 / 特征 / 切分逻辑（`target_*`、`excess_*`、`f_cs`、`miss_*`、purge） | [samples.zh-CN.md](docs/developer/samples.zh-CN.md)、[data-contracts.zh-CN.md](docs/developer/data-contracts.zh-CN.md) | `src/build_samples.py:build_samples`、`_ticker_samples`、`_add_cross_sectional_features`、`_purge_windows` | `pytest tests/test_build_samples.py -q`；重建后比对 manifest 行数/splits 基线与 [data-contracts.zh-CN.md](docs/developer/data-contracts.zh-CN.md) §3/§4 |
| 改财报抽取 / as-of 规则（concept 白名单、`fiscal_year/period`、`days_since_filing`） | [data-contracts.zh-CN.md](docs/developer/data-contracts.zh-CN.md)、[known-quirks.zh-CN.md](docs/developer/known-quirks.zh-CN.md) | `src/download/organize_financials.py:_CONCEPTS`、`_fact_for_period`、`_fiscal_identifiers`、`organize_financials`；`src/build_samples.py:_financial_features` | `pytest tests/test_organize_financials.py tests/test_build_samples.py -q`；单 ticker smoke（AAPL/XOM）后检查 `financials.csv` |
| 改下载 / 数据源（Yahoo、SEC、FRED、universe、override） | [download.zh-CN.md](docs/developer/download.zh-CN.md)、[architecture.zh-CN.md](docs/developer/architecture.zh-CN.md) | `src/download/{universe,market,financials,macros}.py`；`config/sources.toml`；`config/universes/ticker_cik_overrides.json` | `pytest tests/test_download.py tests/test_ticker_overrides.py -q`；`... download --dry-run` 核对计划 |
| 加 / 改 CLI 参数 | [cli.zh-CN.md](docs/user/cli.zh-CN.md)、[download.zh-CN.md](docs/developer/download.zh-CN.md) | `src/cli/main.py:_parser`、`main`；`src/download/manager.py:run_download` | `pytest tests/test_download.py -q -k cli`、`pytest tests/test_ticker_overrides.py -q -k force_rebuild`；`--help`；**必须同步 [cli.zh-CN.md](docs/user/cli.zh-CN.md)**（不变式 8） |
| 修数据质量问题（缺失、异常值、exclusions、ticker→CIK） | [known-quirks.zh-CN.md](docs/developer/known-quirks.zh-CN.md)、[data-contracts.zh-CN.md](docs/developer/data-contracts.zh-CN.md) | `config/universes/exclusions_v1.json`、`ticker_cik_overrides.json`；`src/build_samples.py:_load_exclusions`、`flag_extreme_label`；organize 的 `quality_flag`/`quality_status` | `pytest tests/test_build_samples.py tests/test_ticker_overrides.py -q`；对照 `data/output/qc_report.json` |
| 性能优化（organize 并行、样本重建） | [architecture.zh-CN.md](docs/developer/architecture.zh-CN.md)、[testing.zh-CN.md](docs/developer/testing.zh-CN.md) | `src/download/manager.py:_organize_tickers`（`--workers`，默认 16）；`src/build_samples.py:build_samples`（`staging_tickers`/`rank_batch_sessions`） | `pytest tests/test_organize_parallel.py -q`；单 ticker smoke 计时；重建后 manifest 哈希/行数不变 |
| 加测试 | [testing.zh-CN.md](docs/developer/testing.zh-CN.md) | `tests/`（6 个离线测试文件） | `.venv/bin/python -m pytest -q`（基线 61 passed / 2 skipped） |
| 重建产物验证（哈希 / 行数 / QC） | [data-contracts.zh-CN.md](docs/developer/data-contracts.zh-CN.md)、[samples.zh-CN.md](docs/developer/samples.zh-CN.md) | `data/output/manifest.json` 的 `outputs`；`data/output/splits.json`；`data/output/qc_report.json`；`src/build_samples.py:_hash_output_files` | 按 [data-contracts.zh-CN.md](docs/developer/data-contracts.zh-CN.md) §3.6 代码片段验哈希；行数/purge/flag 与 §4 基线比对 |

## 2. 不变式清单（改任何代码前先确认不破坏）

1. **raw 只追加**：SEC/FRED/universe payload 内容寻址（文件名 = 内容 sha256），永不改写；重建只新增文件。Yahoo 行情文件按 `<start>_<end>.csv` 命名、重下会覆盖，但**任何 `data/raw/` 文件都不允许手工编辑**。
2. **确定性**：同输入必同输出——稳定排序（`mergesort`）、JSON `sort_keys=True`、`manifest.json` 登记全部输出哈希；organize 可重跑幂等。
3. **无前视**：标签 `entry = t+1` 开盘、`exit = t+1+h` 开盘（`adj_open = open × adj_close / close`）；财务 as-of = `latest available_as_of ≤ 信号日`；宏观用 canonical 轴位序差分。
4. **manifest 哈希**：`data/output/manifest.json.outputs` 对全部输出记 `sha256`/`bytes`/`rows`（仅 `manifest.json` 自引用除外）；消费者按哈希验货。
5. **结构不变式**：只改财务特征的重建**不得**移动行数/标签/flag。当前基线：`23,938,669` 行；purge `120,987 / 139,158 / 182,398`（fit/select/screen）；`flag_extreme_label` `120,510`。行数变化只允许来自 purge 或标签逻辑变更，且必须同步 [data-contracts.zh-CN.md](docs/developer/data-contracts.zh-CN.md)。
6. **purge 构建期完成**：每个 split 边界前 31 个 signal session 的禁运窗在 build-samples 内剔除（fit/select/screen 三个边界，见 [data-contracts.zh-CN.md](docs/developer/data-contracts.zh-CN.md) §3.5）；消费者不再 purge。
7. **非有限值转缺失**：raw 特征中的 ±inf/NaN 必须转缺失，且对应 `miss_*=1`；禁止把 inf 写进样本。
8. **CLI 契约同步**：任何 `download` / `build-samples` 参数、默认值、退出码变更，必须同步更新 [cli.zh-CN.md](docs/user/cli.zh-CN.md)。

## 3. 常用命令速查

```bash
# 全量测试（离线；基线 61 passed / 2 skipped，约 40s）
.venv/bin/python -m pytest -q

# 单 ticker smoke：不下载，只重建 AAPL/XOM 的 organized 产物（读取 raw 缓存）
PYTHONPATH=src .venv/bin/python -m cli.main download --stage organize --force-rebuild --tickers AAPL,XOM

# 全量重建第 1 步：下载 + organize（market stage 必须给 --start/--end；--workers 默认 16）
PYTHONPATH=src .venv/bin/python -m cli.main download --stage all --start 1990-01-01 --end 2025-12-31 --workers 16

# 全量重建第 2 步：样本包（默认 data/organized → data/output）
PYTHONPATH=src .venv/bin/python -m cli.main build-samples

# 只规划不落盘；核对将要跑哪些 stage/ticker
PYTHONPATH=src .venv/bin/python -m cli.main download --stage all --start 1990-01-01 --end 2025-12-31 --dry-run

# 锁定测试文件
.venv/bin/python -m pytest tests/test_build_samples.py -q
```

耗时参考（本机实测）：`pytest` 全量约 40s；单 ticker smoke（AAPL,XOM）约 20s；全量 download 为小时级（受网络与节流限制：Yahoo 0.5s/请求、SEC 0.2s/请求）；`build-samples` 重算 23.9M 行并按年写 parquet，单机分钟级到小时级，强依赖 CPU/磁盘/并发负载——最近一次产出的尾部 QC（label 统计）+ manifest 哈希阶段约 3.6 分钟。当前产物规模：`data/organized` ≈ 9.3G、`data/output` ≈ 6.3G。

## 4. 文档地图

| 文档 | 一句话 |
|---|---|
| [AGENT.md](AGENT.md) / [AGENT.zh-CN.md](AGENT.zh-CN.md) | 本文件：任务路由表 + 不变式 + 命令速查 |
| [architecture.md](docs/developer/architecture.md) / [architecture.zh-CN.md](docs/developer/architecture.zh-CN.md) | 五环节数据流、模块职责、数据落点、并发与确定性机制 |
| [data-contracts.md](docs/developer/data-contracts.md) / [data-contracts.zh-CN.md](docs/developer/data-contracts.zh-CN.md) | raw/organized/output 各层字段契约、manifest/splits/qc 结构、基线数值 |
| [download.md](docs/developer/download.md) / [download.zh-CN.md](docs/developer/download.zh-CN.md) | 下载与 organize 深入：SEC/Yahoo/FRED 端点、进度锁、断点续跑、CIK override |
| [samples.md](docs/developer/samples.md) / [samples.zh-CN.md](docs/developer/samples.zh-CN.md) | 样本构建深入：canonical calendar、特征/标签实现、purge、分区写出 |
| [testing.md](docs/developer/testing.md) / [testing.zh-CN.md](docs/developer/testing.zh-CN.md) | 测试布局、跑法、离线约定、如何用 tmp 数据夹具 |
| [known-quirks.md](docs/developer/known-quirks.md) / [known-quirks.zh-CN.md](docs/developer/known-quirks.zh-CN.md) | 数据源现实导致的"看起来像 bug"清单 |
| [cli.md](docs/user/cli.md) / [cli.zh-CN.md](docs/user/cli.zh-CN.md) | CLI 用户手册：命令、参数、退出码（CLI 契约源） |
| [data-format.md](docs/user/data-format.md) / [data-format.zh-CN.md](docs/user/data-format.zh-CN.md) | 用户侧产物格式说明（样本包怎么读） |
| [recommended-usage.md](docs/user/recommended-usage.md) / [recommended-usage.zh-CN.md](docs/user/recommended-usage.zh-CN.md) | 推荐使用方式与注意事项 |

## 5. 维护约定

- 文档与实物不一致时，以实物（`src/`、`config/`、`data/output/manifest.json`）为准，并在同一 PR 修补文档。
- **双语同步**：任何文档改动必须同 PR 更新两种语言版本；不一致以英文版为准。
- 只改财务特征的重建是最常见的回归验证：行数、标签、flag 必须逐位不变（不变式 5）。
- 重建后用 `data/output/manifest.json` 对全部输出做哈希验货（见 [data-contracts.zh-CN.md](docs/developer/data-contracts.zh-CN.md) §3.6）。

## 6. 数据看着不对？

先读 [known-quirks.zh-CN.md](docs/developer/known-quirks.zh-CN.md)：多数"异常"是数据源现实（缺行情、非日历财年、FRED 修订值、未复权拆股跳变等），不是代码 bug。确认是 bug 后再按 §1 路由到对应文档与代码入口。
