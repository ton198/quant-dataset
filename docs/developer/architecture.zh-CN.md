[English](architecture.md) | **简体中文**

# 架构

本仓库将既有行情/SEC/FRED 摄入与唯一的不含财务数据 `samples` builder 分开，并提供可选的 `query-samples` 查询当前契约 bundle。迁移到五模块 `samples` 包及共享只读 SQL guard 已在当前源码树实现，且未改变 download 或 organize 行为。2026-10-03 发布在 `data/output/` 的 bundle 保留历史 manifest 值 `schema_version="samples_v3"`；原 147 列产物与冻结 baseline 分别保留。这些包保持原样只读；本文不表示重新发布。查询不增加新存储格式。

改代码前先读 [AGENT.zh-CN.md](../../AGENT.zh-CN.md)。唯一样本契约与包迁移见 [samples-architecture.md](samples-architecture.md)、[samples.zh-CN.md](samples.zh-CN.md) 与 [data-contracts.zh-CN.md](data-contracts.zh-CN.md)。既有数据源行为见 [download.zh-CN.md](download.zh-CN.md)。独立申报档案有多个有界私有 pilot；它们都不是 `samples` 输入，也不代表广泛覆盖。当前结果与限制见 [financial-filing-archive.zh-CN.md](financial-filing-archive.zh-CN.md) 与[用户指南](../user/filings.zh-CN.md)。

## 1. 数据流总览

```text
既有数据源                    data/raw/                         data/organized/                  samples 候选
──────────                    ─────────                         ────────────────                  ───────────────
Yahoo 行情 ───────────────→ yahoo/<T>/<range>.csv ──────────→ stocks/<T>/market.csv ─────┐
FRED observations ────────→ fred/<series>/<sha>.json ───────→ shared/macro.csv ─────────┼→ build-samples
                                                                                          │  仅全新 --out
SEC CompanyFacts/submissions → sec/financials/<sha>.json ─┐                                │
                         + manifest.json                   └→ financials.csv / selected      ┘
                                                            financial_events_v1 artifact
                                                            （独立存在；builder 不读取）
```

```text
已有样本包（Parquet 文件 + manifest）
        ├── Python + PyArrow 直接读取
        └── 可选 query-samples → 内存中的 DuckDB → CSV 结果
```

SEC raw cache 仍包含 Company Facts、submissions 与历史 submissions-page JSON；它与显式 root filing archive 分开。`filings catalog` 将本地已验证 submissions 复制到 archive；有界 `filings download` 存入选定 inventory/文档；`filings parse` 处理已存在的选中文件并发布 parser attempts 及实际提取的 facts/sections。首批五份私有 pilot 为 v7（6,017 facts/165 sections）；独立第二批十份、8 CIK pilot 为 v15（25,656 facts/95 sections；15 full text、6 full XBRL、4 unsupported XBRL）。RBC 40-F 有一个 primary 锚定、7,543-occurrence group parse（主文档 40、EX2 7,503），但 raw coverage 仍 `partial`。两个 archive 相互独立，不进入 `samples`。organized `financial_events_v1` 仍是选定九概念抽取。Query 使用内存 DuckDB，不增加持久数据库或迁移样本数据。

既有生命周期阶段：

1. **universe：** 获取 SEC `company_tickers_exchange`，应用配置的 exchange filter 和 ticker→CIK override。
2. **download：** 既有 Yahoo 行情、SEC Company Facts/submissions/历史分页及配置的 FRED 序列；行为保持不变。
3. **organize：** 写清洗后的行情、宏观面板以及独立保留的既有财务输出。
4. **build-samples：** 从行情面板建立 canonical calendar，构建非财务特征/标签、截面 rank 与 QC，再写到全新候选目录。它不读取财务文件或财务 `_meta.json` 记录，也不做财务预检、覆盖率或输入盘点；当前 manifest 不包含财务状态声明。
5. **消费：** 校验候选 `manifest.json` 与输出 hashes，按 `splits.json` 选择数据。purge 已在构建期完成，下游不再 purge。
6. **可选样本查询：** `query-samples` 只接受严格的当前 `samples` 契约；它核验 schema/feature registry/fingerprints 及 manifest 登记输出的全部 hash、字节数和行数，再读取样本 Parquet 文件。该完整性核验不替代独立语义或发布验证。
7. **独立申报档案：** `filings catalog` 读取本地 cache；有界 `filings download` 获取选定 inventory/文档；`filings parse` 处理已有文件并发布 parse/fact/text/dependency 表；`verify` 与 `query` 操作显式 archive。此流程不进入 `samples`。当前有首批五份 v7 和独立第二批十份 v15 两个有限 real pilot；详见 [financial-filing-archive.zh-CN.md](financial-filing-archive.zh-CN.md) 的 case 状态与限制。

归档的 `data/output/` bundle 在原始 `samples_v3` manifest 标签下有 96 个特征、132 个物理列。原 147 列 output 保留在 `data/output-v1-backup-20261003T172214933236Z`；冻结 baseline 仍独立保留。这些是只读历史产物事实，不是新产品契约。

## 2. 模块职责

| 模块 | 职责 |
|---|---|
| `src/cli/main.py` | `download`、`build-samples` 与可选 `query-samples` CLI 解析和分发 |
| `src/samples/query.py` | 已实现的只读 SQL 查询，仅支持严格当前契约样本包；不创建持久化数据库 |
| `src/query_support/readonly_sql.py` | `samples` 与 filings query consumer 已共用的中立 SQL guard |
| `src/filings/{cli,workflow,archive,acquisition,processing,query}.py` | 独立 catalog/archive、有界 SEC 获取、offline-first 解析集成、档案核验与 descriptor-backed 查询 |
| `src/filings/config.py` | Acquisition 与显式 taxonomy 准备读取 SEC-only contact；不需要 FRED key |
| `src/filings/{parse_xbrl,parse_text,dependencies,parsing_models}.py` | 保留来源的本地 parser、有界 taxonomy 依赖准备与 parser-owned Arrow occurrence schema |
| `src/download/manager.py` | 既有 download-stage 编排、进度/锁与并行 organize |
| `src/download/universe.py` | SEC ticker snapshot 与 ticker→CIK override |
| `src/download/market.py` | Yahoo 日线下载 |
| `src/download/financials.py` | 既有 Company Facts/submissions/历史分页 JSON 缓存；不含完整 filing HTML |
| `src/download/macros.py` | 既有 FRED observations 下载 |
| `src/download/organize.py` | 行情清理/派生值、宏观对齐与 organize metadata |
| `src/download/organize_financials.py` | 既有选定概念财务抽取与旧 daily snapshot |
| `src/download/financial_events.py` | 既有选定 `financial_events_v1` event/fact Parquet；与样本构建分离 |
| `src/samples/{__init__,builder,query,contracts,validation}.py` | 当前五文件包，实现唯一 finance-free `samples` 产品。旧顶层模块和历史工具已移除；见 [source-layout-audit.md](source-layout-audit.md) 中标明日期的迁移前快照及当前源码树。 |
| `config/` | 数据源、密钥模板、universe override 与 exclusions |
| `tests/` | 离线测试与 fixtures |

## 3. 数据落点与生命周期

| 路径 | 生产者 | 内容/更新语义 | 是否为 `samples` 输入 |
|---|---|---|---|
| `data/raw/yahoo/<T>/<start>_<end>.csv` | 既有行情下载器 | Yahoo CSV；相同区间文件名可能被覆盖 | organize 后间接读取 |
| `data/raw/sec/universe/<sha256>.json` | 既有 universe 下载器 | 内容寻址名单快照 | 否 |
| `data/raw/sec/financials/<sha256>.json` + `manifest.json` | 既有 SEC financials 下载器 | Company Facts、submissions、历史分页 payload；内容寻址且 manifest 追加版本 | 否 |
| 显式 filing archive run root（例如 `data/filings/pilot`） | `filings catalog/download/parse` | 独立不可变 source CAS 与 manifest 列出的 snapshot；只有 processing 发布后才有 parser-owned `facts`/`sections` 与辅助 `parses`/`dependencies` | 否 |
| `data/raw/fred/<SERIES>/<sha256>.json` + manifest | 既有宏观下载器 | FRED observations 与版本 | organize 后间接读取 |
| `data/organized/stocks/<T>/market.csv` | `organize_market` | 清洗后行情面板 | **是** |
| `data/organized/shared/macro.csv` | `organize_macros` | 按 session 对齐的宏观面板 | **是** |
| `data/organized/stocks/<T>/financials.csv` | `organize_financials` | 既有每日整份财报 snapshot，语义不变 | **否** |
| `data/organized/stocks/<T>/financial_events.parquet` 与 `financial_facts.parquet` | 既有财务 organizer | 选定九概念的 `financial_events_v1`；不是完整 archive | **否** |
| `data/organized/stocks/<T>/_meta.json` | 既有 organize 函数 | raw/organized 输入输出来源，包含旧财务 artifact 记录 | builder 不读其中财务记录 |
| 全新候选输出路径（CLI 默认：`data/samples-output`） | `build-samples` | 单一 `samples` 契约、meta、split/manifest/QC；执行安全目标检查 | 输出 |
| 现有 `data/output/` | 归档 daily bundle | 2026-10-03 manifest 原值为 `schema_version="samples_v3"` | 只读历史产物；不得作为候选目标 |
| v1 backup 与冻结 baseline | 保留的历史参考 | `data/output-v1-backup-20261003T172214933236Z` 与 `data/baselines/samples_v1_financial_upgrade/` | 独立保留，不覆盖 |

## 4. 既有摄入与模块化申报档案

当前 `download --stage financials` 仍是 Company Facts/submissions/历史分页流程，不抓取完整 filing 文档。独立的 `filings catalog/download/parse/verify/query` 使用显式 archive root：catalog 仅读 cache；download 有界；parse 只处理已存在的选中文件，默认离线；taxonomy 准备必须显式 opt-in；query 只读 manifest 实际列出的表。filing archive pilot 均与 `samples` 分开。Pilot 结果、归档来源分组、raw-coverage 限制及其单独记录的核验见 [financial-filing-archive.zh-CN.md](financial-filing-archive.zh-CN.md)，不作为 samples gate。

## 5. 确定性与安全输出

- 既有 SEC/FRED raw payload 使用内容寻址；manifest 将逻辑资源映射到 payload 版本。
- 样本行序、canonical session 对齐、registry 列序和 manifest hash 是 `samples` 契约的一部分。
- `workspace_root` 默认是 CWD，可用 `--workspace-root` 设置；默认 exclusions 路径为 `<workspace_root>/config/universes/exclusions_v1.json`。显式 exclusions 路径相对于 CWD；文件缺失时报错。不得从已安装 source package 位置推导 workspace/config/data 路径。
- 保护 `<workspace_root>/data/` 与相关 raw/output/baselines 路径；对常规 `<data>/organized` 输入，还会独立于 `workspace_root` 保护其 data 目录旁识别出的 raw/output/baseline/archive 路径。显式 workspace root 必须存在。拒绝符号链接路径/祖先、非空目标，以及与受保护路径或实际输入重叠的目标。CLI 默认输出是 `data/samples-output`；使用全新、未占用的 `--out`，不得指向保留 bundle 或 baseline。
- 已归档发布报告仅属历史记录，不是当前测试或性能证据。新样本构建须通过独立当前契约验证，不依赖跨代 projection oracle。
