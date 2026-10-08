# quant-dataset

面向美国股票行情/宏观数据与样本包的离线流水线。唯一维护的样本契约是不含财务数据的 `samples` 产品；现有输出产物保持原样，不会因文档或代码迁移而改写。本仓库不训练模型，也不做回测。

[![CI](https://github.com/ton198/quant-dataset/actions/workflows/ci.yml/badge.svg)](https://github.com/ton198/quant-dataset/actions/workflows/ci.yml)

[English](README.md) | **简体中文**

## 当前契约与历史产物

唯一的 `samples` 契约为不含财务数据的 schema，共 **132 个物理列**：4 键/标志 + 43 个 raw 特征 + 10 个截面特征 + 43 个缺失指示 + 32 个标签。`manifest.feature_list` 有 96 个特征列。builder 只读取 organized 行情/宏观输入与 exclusions，不读取财务文件或财务 metadata。产物保留的财务状态若为 `not_applicable`，也不代表财务覆盖率。

**历史发布记录（2026-10-03）：** 现有 `data/output/` bundle 发布时的旧 manifest 值为 `schema_version="samples_v3"`，共 **23,938,669 行**、**6,532 个 ticker**、**132 个物理列**及 **96 个特征**。将它作为原样保留的只读历史产物；本文不重新发布该 bundle，也不改写 manifest。原 147 列 output 保留在 `data/output-v1-backup-20261003T172214933236Z`；冻结的 `data/baselines/samples_v1_financial_upgrade/` 另行保留。发布记录报告不变式投影差异为 0；735 passed/2 skipped 测试证据因代码未变而复用，发布时未重跑。上述仅是历史记录，不是当前源码或新候选的验证结果，也不声称性能实测或财务正确性。记录：`/tmp/opencode/v3_release_publication/report.json` 和 `data/.output-publication-20261003T172214933236Z.json`（`COMMITTED_V3_V1_BACKUP_RETAINED`）。

| 保留的历史 `samples_v1` output/baseline | 记录值 |
|---|---|
| 行 × 物理列 | 23,938,669 × 147 |
| `meta.parquet` 股票数 | 6,532 |
| Canonical session 数 | 9,067 |
| 覆盖区间 | 1990-01-02 – 2025-12-31 |

canonical session = 统一交易日历中的日期，要求至少有 500 只 common 股票（`is_common`）出现。

## 一行样本是什么

一行表示一个 ticker（`asset_id`）在一个信号日（`date`）的观测。`samples` 契约有 43 个行情/宏观 raw 特征、10 个同日截面变换、43 个对应缺失指示和 32 个未来标签。财务数据不是样本输入。

```text
信号日 t                     t+1 开盘入场              t+1+h 开盘出场
    │                            │                          │
    │ 特征止于 t                 │ 标签窗口：h 个 session
    └────────────────────────────┴──────────────────────────┘
      h = 1..30 个 canonical session；excess 主目标 h = 5 和 21
```

## 在独立目录构建候选

样本 workspace root 默认是当前工作目录；使用 `--workspace-root PATH` 可显式指定。默认 exclusions 文件为 `<workspace_root>/config/universes/exclusions_v1.json`；文件缺失时报错，显式指定的 `--exclusions-file` 则相对于当前工作目录解析。保留文件名 `exclusions_v1.json`，它不是样本产品版本。

候选只能构建到全新的输出路径。guard 保护 `<workspace_root>/data/` 及根据实际输入识别的相关 raw/output/baselines 路径；拒绝符号链接目标、非空目标，以及与受保护路径或实际输入重叠的目标。不要指向保留的 `data/output/`、backup 或 baseline；若示例路径已存在，请另选全新路径，不要删除并复用。

```bash
WORKSPACE_ROOT=$PWD
CANDIDATE_DIR=/tmp/opencode/candidate-samples-finance-free
test ! -e "$CANDIDATE_DIR" && test ! -L "$CANDIDATE_DIR" || { echo "Choose a new, unused, non-symlink candidate directory" >&2; exit 1; }
PYTHONPATH=src .venv/bin/python -m cli.main build-samples \
  --workspace-root "$WORKSPACE_ROOT" \
  --data-dir data/organized --out "$CANDIDATE_DIR"
```

候选构建需要已有 organized 行情与宏观面板。它不读 SEC 财务文件或财务 metadata，不做财务预检/覆盖率/输入盘点，也不下载数据。用小型、独立的当前契约 fixtures 与记录的不变式验证新行为；历史 projection 比较不是正确性 oracle。每次发布都需自己的核验证据。上方历史发布记录不是当前源码的测试结果。

## 查询已有样本包

样本包仍以 Parquet 文件保存。需要可选 SQL 命令时，再安装对应依赖，并明确指定要读取的样本包：

```bash
uv sync --frozen --extra query
BUNDLE_DIR=/tmp/opencode/candidate-samples-finance-free
quant-dataset query-samples --bundle "$BUNDLE_DIR" \
  --sql 'SELECT COUNT(*) AS row_count FROM samples'
```

`query-samples` 只接受当前 `samples` bundle，并会先严格核验 manifest/schema 及登记的输出 hashes；保留历史标签为 `samples_v3` 的 bundle 和原 147 列布局不受此命令支持。历史 artifact 请用 Python/PyArrow 检查，或先构建全新候选再查询。查询直接读取，不会创建持久化数据库或迁移数据。普通流水线安装方式不变。日期筛选、返回行数限制与只读规则见[查询指南](docs/user/query.zh-CN.md)。

## 既有 download/organize 流程

既有 `download` 与 `organize` 命令保持不变。SEC `financials` stage 仍下载 Company Facts、submissions 和历史 submissions-page JSON，不抓取完整 filing HTML/iXBRL。另有有界 `filings catalog/download/parse/verify/query` 命令，使用显式 archive 路径和本地缓存。两个私有 parser pilot 分开保留：首批五份 v7 与第二批十份 v15；它们不是 `samples` 输入，也不代表广泛发行人覆盖。v15 活动 snapshot 有 25,656 条 facts，其中 RBC 有一个 primary 锚定的 IXDS group（7,543 个原始 occurrence），但 RBC raw coverage 仍为 partial。organized `financials.csv` 与选定范围的 `financial_events_v1` 抽取仍是独立数据产品；后者只包含选定九概念 facts，不是全部 XBRL 或完整申报。详见[申报指南](docs/user/filings.zh-CN.md)、[CLI 手册](docs/user/cli.zh-CN.md)、[download 机制](docs/developer/download.zh-CN.md)与[archive 状态](docs/developer/financial-filing-archive.zh-CN.md)。

## 文档

| 用户文档 | 开发文档 |
|---|---|
| [CLI 使用手册](docs/user/cli.zh-CN.md)：既有下载、候选构建与 CLI 命令 | [AGENT.md](AGENT.md)：任务路由与不变式 |
| [申报档案指南](docs/user/filings.zh-CN.md)：有界 catalog、获取、解析、核验、查询、pilot 证据与限制 | |
| [查询指南](docs/user/query.zh-CN.md)：用可选 SQL 查询现有样本 Parquet 包 | |
| [数据格式](docs/user/data-format.zh-CN.md)：唯一的 samples 契约与保留产物 | [architecture.zh-CN.md](docs/developer/architecture.zh-CN.md)：数据流与输入分离 |
| [推荐用法](docs/user/recommended-usage.zh-CN.md)：候选消费注意事项 | [data-contracts.zh-CN.md](docs/developer/data-contracts.zh-CN.md)：各层、当前契约与归档 schema 事实 |
| | [samples-architecture.md](docs/developer/samples-architecture.md)：包与迁移契约 |
| | [samples.zh-CN.md](docs/developer/samples.zh-CN.md)：特征、标签与当前核验提示 |
| | [download.zh-CN.md](docs/developer/download.zh-CN.md)：既有数据源行为 |
| | [financial-filing-archive.zh-CN.md](docs/developer/financial-filing-archive.zh-CN.md)：已实现边界与待处理 parsing 状态 |
| | [known-quirks.zh-CN.md](docs/developer/known-quirks.zh-CN.md)：数据源限制 |
| | [testing.zh-CN.md](docs/developer/testing.zh-CN.md)：离线测试与核验 |

## 已知局限

- **幸存者偏差：** 股票池来自当前 SEC ticker 名单，不含退市历史。
- **FRED 非 vintage：** 宏观输入使用 latest-revised 值与近似可见日期。
- **Adjusted open 不是成交价：** 标签使用复权价格，不等于成交模拟，也未包含交易成本。
- **财务数据独立：** `samples` 契约不含财务特征和财务缺失指示。已有 SEC 结构化抽取仍独立保留，但不等于完整申报档案。
- **旧 bundle 不是产品模式：** 现存 `data/output/` bundle 保留原始 `samples_v3` manifest 值，旧 147 列产物保留在 `data/output-v1-backup-20261003T172214933236Z`。这些都是只读历史数据，不是可切换的 samples 产品版本；冻结 baseline 仍单独保留。

更多说明见 [known-quirks.zh-CN.md](docs/developer/known-quirks.zh-CN.md)。

## 许可证

MIT — 见 [LICENSE](LICENSE)。
