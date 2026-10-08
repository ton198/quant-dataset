# AGENT 导航

**本仓库是离线行情/宏观数据流水线与样本包。** 唯一维护的样本产品是不含财务数据的 `samples` 契约，不设样本产品代际选择器或兼容别名。现有 output 和 baseline 路径只读保留：2026-10-03 发布的 `data/output/` manifest 原值为 `schema_version="samples_v3"`（23,938,669 行、6,532 个 ticker、132 列、96 个特征）；旧 147 列产物仍在 `data/output-v1-backup-20261003T172214933236Z`；冻结 baseline 独立保留。这些是历史产物事实，不是当前源码/测试结果或新发布结果。不得覆盖、迁移或删除。既有 Company Facts 与 `financial_events_v1` artifact 仍独立于 samples。

[English](AGENT.md) | **简体中文**

## 1. 任务路由表

| 任务类型 | 必读文档（≤2） | 代码入口 | 验证方式 |
|---|---|---|---|
| 样本标签、非财务特征、切分或输出 schema | [samples-architecture.md](docs/developer/samples-architecture.md)、[data-contracts.zh-CN.md](docs/developer/data-contracts.zh-CN.md) | 当前 API：`src/samples/{builder,query,contracts,validation}.py` | 仅运行任务指定的当前契约 fixtures/不变式测试；核验 schema/fingerprints、实际消费输入 provenance 和 output hashes；候选只写入全新、非受保护的 `--out` 路径。不要把跨代 projection 当正确性 oracle。 |
| SEC 下载/organize 行为与旧财务 artifact | [download.zh-CN.md](docs/developer/download.zh-CN.md)、[data-contracts.zh-CN.md](docs/developer/data-contracts.zh-CN.md) | `src/download/{financials,organize_financials,financial_events}.py`；`src/download/manager.py` | 仅运行任务指定的离线测试或 organize smoke。这些独立输入不由 `samples` builder 读取；保留 `financial_events_v1` 自身的 artifact 契约。 |
| SEC 申报档案 catalog/获取/解析/核验/query | [filings.zh-CN.md](docs/user/filings.zh-CN.md)、[financial-filing-archive.zh-CN.md](docs/developer/financial-filing-archive.zh-CN.md) | `src/filings/{cli,workflow,archive,acquisition,processing,query}.py` | Archive 与 `samples` builder/旧 Company Facts cache 分开；使用显式 archive/workspace 和有界 ID。Parse 默认离线；taxonomy fetch 必须显式 opt-in/host allowlist。独立五份申报 pilot 已持久化但为 partial；不代表广泛发行人覆盖或经济正确性。 |
| 下载 / 数据源（Yahoo、SEC Company Facts/submissions、FRED、universe、override） | [download.zh-CN.md](docs/developer/download.zh-CN.md)、[architecture.zh-CN.md](docs/developer/architecture.zh-CN.md) | `src/download/{universe,market,financials,macros,organize_financials}.py`；`config/sources.toml`；`config/universes/ticker_cik_overrides.json` | `pytest tests/test_download.py tests/test_ticker_overrides.py -q`；仅在任务指定时 dry-run。现有 SEC 下载范围不变，不抓取完整申报 HTML。 |
| CLI 参数或行为 | [cli.zh-CN.md](docs/user/cli.zh-CN.md)、[filings.zh-CN.md](docs/user/filings.zh-CN.md) | `src/cli/main.py:_parser`、`main`；`src/filings/cli.py`、`processing.py`；当前查询 API `samples.query.query_samples` | `--help` 与任务指定的 CLI/查询检查；download/catalog/verify 不依赖可选 extra，parse taxonomy fetch 必须显式 opt-in。CLI 命令名仍是 `build-samples` 和 `query-samples`。 |
| 查询样本包 | [query.zh-CN.md](docs/user/query.zh-CN.md)、[data-format.zh-CN.md](docs/user/data-format.zh-CN.md) | 当前 API `src/samples/query.py`；`src/cli/main.py` | 查询只接受严格当前 `samples` manifest/schema，并核验所有登记输出 hashes/bytes/rows；拒绝历史 bundle，不改动输入。完整性检查不等于独立发布/语义核验。 |
| 数据质量问题（行情/宏观缺失、标签、排除、ticker→CIK） | [known-quirks.zh-CN.md](docs/developer/known-quirks.zh-CN.md)、[data-contracts.zh-CN.md](docs/developer/data-contracts.zh-CN.md) | `config/universes/exclusions_v1.json`、`ticker_cik_overrides.json`；对应 organize/build 函数 | 运行任务指定的定向测试；检查相关 manifest/QC 投影，不要把历史财务诊断当成当前样本特征。 |
| 测试与离线夹具 | [testing.zh-CN.md](docs/developer/testing.zh-CN.md) | `tests/` | 仅运行任务负责人指定的验证。旧的 735 passed/2 skipped 和 0-difference 发布记录仅为历史证据，不是当前源码或本次文档变更的验证结果。不得将其报告为新测试结果。 |
| 候选 schema / 不变式核验 | [samples-architecture.md](docs/developer/samples-architecture.md)、[data-contracts.zh-CN.md](docs/developer/data-contracts.zh-CN.md) | `src/samples/{contracts,validation,builder}.py` 与 `tests/fixtures/samples/current/` 独立 fixtures | 核验 registry 数量、canonical 列序、无财务输入、语义不变式、实际输入 provenance 和输出保护。不要将已删除的跨代 projection 或财务校准工具用作 gate。 |

## 2. 不变式

1. **不得手工编辑 raw source 文件。** 现有 SEC、FRED、universe payload 使用内容寻址；Yahoo 区间文件可能被现有下载器覆盖。独立 filing archive 已有有界 catalog/download/parse/verify/query 命令；archive/workspace 必须使用显式路径并避开受保护 source/output/cache。Parse 默认离线，外部 taxonomy 准备必须显式 opt-in 并限制 host。Archive 不是 `samples` 输入。见[申报指南](docs/user/filings.zh-CN.md)。
2. **确定性：** 相同输入与构建参数必须产生语义相同的输出；仍须稳定排序并登记 manifest hash。
3. **无前视：** 标签以 `t+1` 开盘入场、`t+1+h` 开盘出场（`adj_open = open × adj_close / close`）；宏观差分按 canonical 轴位序计算。任何申报日期或财务数据都不是 `samples` 输入。
4. **Hash 与 provenance：** `manifest.json.outputs` 为每个生成产物记录 sha256/bytes/rows，manifest 本身不自引用。`input_provenance.files` 为每个实际消费输入记录流式 sha256/bytes，并在发布 manifest 前复核；`input_inventory` 仅为计数。
5. **当前契约正确性：** 用独立编写期望值的小型 fixture 核对样本公式及边界行为，并覆盖稳定键、dtype/schema、split/purge、missingness、determinism、安全和失败不变式。不要把跨代历史 projection 当作正确性 oracle。已归档的 2026-10-03 发布记录不是当前测试结果，也不代表财务覆盖或经济正确性。
6. **构建期 purge：** 每个适用 split 边界前 31 个 signal session 剔除；消费者不再 purge。
7. **非有限值转缺失：** raw 特征中的非有限值归一为 null，并置对应 `miss_*`。
8. **CLI 契约同步：** `download`、`build-samples`、`query-samples` 或任一 `filings` 子命令的参数/行为改变，必须同步更新 [cli.zh-CN.md](docs/user/cli.zh-CN.md)；档案范围/状态还要同步更新[filings 指南](docs/user/filings.zh-CN.md)及其英文版；bundle 查询还要更新[查询指南](docs/user/query.zh-CN.md)。
9. **财务隔离：** `samples` 不得读取财务文件或财务 `_meta.json` 记录，不运行财务预检/覆盖率/输入盘点，也不输出财务特征。若保留财务状态字段，`not_applicable` 不是覆盖声明。现有 `financials.csv` 和选定范围的 `financial_events_v1` 结构化抽取仍作为独立 organized 数据保留；它们不是完整 XBRL/申报档案，也不是样本输入。不得删除或迁移现有 raw、organized、output 或 baseline 数据。
10. **Workspace 与安全构建：** `workspace_root` 默认是 CWD，可用 `--workspace-root` 指定；不得从 package 安装位置推导。默认 exclusions 来自 `<workspace_root>/config/universes/exclusions_v1.json`，缺失时报错；显式 `--exclusions-file` 路径相对于 CWD。保护 `<workspace_root>/data/` 及相关 raw/output/baselines 路径；常规 `<data>/organized` 输入还会独立于 workspace root 保护 `<data>` 旁识别出的 raw/output/baseline/archive 路径。拒绝符号链接路径/祖先、非空目标，以及与受保护路径或实际输入重叠的目标。CLI 默认 `--out data/samples-output`；候选只能写到全新未占用路径，不得覆盖保留 bundle 或 baseline。
11. **发布证据：** 每次新发布均须记录当前契约核验和 provenance。新构建不能沿用历史发布证据；没有明确运行记录时，不要声称性能、正确性或 release PASS。
12. **可选查询层：** 样本仍以源文件和 Parquet bundle 存储。不得为 `query-samples` 新增 SQLite、迁移数据，或默认创建持久化 DuckDB 数据库/catalog（数据库状态）；查询使用可选依赖，只接受严格当前 `samples` 契约，核验 manifest 登记输出的 hashes/bytes/rows，且不改动输入。保留的历史 bundle 不受 `query-samples` 支持，请用 Python/PyArrow 检查。没有 query extra 仍可用 Python 读取，普通流水线安装不变。

## 3. 命令速查

```bash
# 仅运行任务指定的当前契约 samples 测试，并使用当前 tests/ 中的路径。

# 仅在全新、独立目录构建 samples 候选。
# 若路径已存在，请换一个未使用路径；不得删旧产物后复用。
WORKSPACE_ROOT=$PWD
CANDIDATE_DIR=/tmp/opencode/candidate-samples-finance-free
test ! -e "$CANDIDATE_DIR" && test ! -L "$CANDIDATE_DIR" || { echo "Choose a new, unused, non-symlink candidate directory" >&2; exit 1; }
PYTHONPATH=src .venv/bin/python -m cli.main build-samples \
  --workspace-root "$WORKSPACE_ROOT" --data-dir data/organized --out "$CANDIDATE_DIR"

# 现有 download/organize 命令是独立流程，不是构建此候选的前置条件。
# Download 与 filing-archive 获取是独立流程，不是样本构建输入。
```

## 4. 文档地图

| 文档 | 简介 |
|---|---|
| [architecture.md](docs/developer/architecture.md) / [architecture.zh-CN.md](docs/developer/architecture.zh-CN.md) | 流水线阶段与样本输入、SEC 财务 artifact 的分离 |
| [data-contracts.md](docs/developer/data-contracts.md) / [data-contracts.zh-CN.md](docs/developer/data-contracts.zh-CN.md) | raw/organized 契约与单一 samples schema；保留 bundle 标识仅为归档事实 |
| [download.md](docs/developer/download.md) / [download.zh-CN.md](docs/developer/download.zh-CN.md) | 现有 Yahoo/SEC Company Facts/submissions/FRED 下载与 organize 行为 |
| [samples-architecture.md](docs/developer/samples-architecture.md) | 单一 samples 包、契约、清理边界与迁移 gate |
| [source-layout-audit.md](docs/developer/source-layout-audit.md) | 迁移前源码树与 imports 证据 |
| [samples.md](docs/developer/samples.md) / [samples.zh-CN.md](docs/developer/samples.zh-CN.md) | 无财务样本行为与当前契约核验提示 |
| [financial-filing-archive.md](docs/developer/financial-filing-archive.md) / [financial-filing-archive.zh-CN.md](docs/developer/financial-filing-archive.zh-CN.md) | 档案/解析边界、raw pilot 证据与待完成 parser 核验 |
| [testing.md](docs/developer/testing.md) / [testing.zh-CN.md](docs/developer/testing.zh-CN.md) | 离线测试约定与当前契约核验指引 |
| [known-quirks.md](docs/developer/known-quirks.md) / [known-quirks.zh-CN.md](docs/developer/known-quirks.zh-CN.md) | 数据源现实及明确标为历史的财务路径行为 |
| [cli.md](docs/user/cli.md) / [cli.zh-CN.md](docs/user/cli.zh-CN.md) | CLI 命令、安全候选输出与既有下载行为 |
| [filings.md](docs/user/filings.md) / [filings.zh-CN.md](docs/user/filings.zh-CN.md) | 有界 SEC 申报档案命令、配置与限制 |
| [query.md](docs/user/query.md) / [query.zh-CN.md](docs/user/query.zh-CN.md) | 用可选只读 SQL 查询现有 Parquet 样本包 |
| [data-format.md](docs/user/data-format.md) / [data-format.zh-CN.md](docs/user/data-format.zh-CN.md) | 单一 samples 输出格式与保留的旧 bundle 事实 |
| [recommended-usage.md](docs/user/recommended-usage.md) / [recommended-usage.zh-CN.md](docs/user/recommended-usage.zh-CN.md) | samples 候选的下游用法与注意事项 |

## 5. 维护约定

- 文档与实现不一致时，向任务负责人报告并在同次变更修订相关文档；不要静默改写受保护数据或越权修改实现。
- **双语同步：** 英文与中文文档须同步更新；若仍有差异，以英文为准。
- `samples` 只有一个契约；仅在识别真实保留的历史产物时提及 `samples_v1`/`samples_v3`。不要恢复已被取代的财务特征提案或其工具。
- 准确保留既有 SEC organizer 行为：`financials.csv` 仍是每日整份快照；`financial_events_v1` 是仅含选定九概念的结构化抽取，不是完整申报档案。
- 保持测量诚实：不估算性能/覆盖率，不宣称未经验证的发布 PASS。
