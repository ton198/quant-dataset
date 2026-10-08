[English](cli.md) | **简体中文**

# CLI 使用手册

`src/cli/main.py` 提供既有 `download`、`build-samples`、可选 `query-samples`，以及本地 `filings catalog`、`download`、`parse`、`verify`、`query` 和 `extract-financials` 命令。样本 CLI 命令名仍为 `build-samples` 和 `query-samples`；实现移入包不会增加产品版本选择。

```bash
PYTHONPATH=src .venv/bin/python -m cli.main --help
```

## 1. 前置准备

```bash
uv sync --frozen                 # 或 pip install -e .
cp config/secrets.example.toml config/secrets.toml
cp config/extraction.example.toml config/extraction.toml
```
`download` 和 `build-samples` 使用普通安装即可。Filing catalog/download/verify 不需要 parser 或 query extra；`filings parse` 需要可选 Arelle 支持（`uv sync --frozen --extra filings`）；`filings query` 与 `query-samples` 需要可选 DuckDB（`uv sync --frozen --extra query`）。同时需要解析和 SQL 查询时，可用 `uv sync --frozen --extra filings --extra query`。普通流水线安装不变。详见[样本查询指南](query.zh-CN.md)与[申报档案指南](filings.zh-CN.md)。

| `[secrets]` key | 用途 |
|---|---|
| `sec_user_agent` | 现有 SEC 请求的 User-Agent，格式为 `Name email` |
| `fred_api_key` | FRED observations API key |

`config/secrets.toml` 已被 gitignore，勿提交。现有 SEC/FRED 下载 stage 需要相应配置 key；纯 market 下载不需加载 secrets。
## 财务抽取

复制 `config/extraction.example.toml` 为 `config/extraction.toml`，配置 provider URL、模型名、结构化输出模式、数字解析政策和单请求限制。密钥只放在 gitignored 的 `config/secrets.toml` 的 `[secrets].api_key` 字段；抽取不读取环境变量。可用 `--secrets PATH` 指定其他私有密钥文件，默认 `config/secrets.toml`。真实密钥不得写入已提交的示例文件。窗口抽取使用有界 worker 并发（`[extraction].workers`，默认 32；`--workers 1` 为串行），保持来源顺序稳定，无自动重试，校验失败不自动纠正。并发需服从服务限流。

模型接收窗口内带校验字符的短 ref，journal 保存到原证据 ref 的精确映射，解码后保留原来源位置。非法或缩写 ID 保持 unresolved，不模糊修复、不自动重试；短 ref 请求与历史长 ref 请求身份不同。引用有效不等于财务准确或数值覆盖：跨 HTML 单元格拆分的金额可能保留为文本。

```bash
quant-dataset filings extract-financials \
  --archive ARCHIVE --work-dir PRIVATE_DIR --config config/extraction.toml \
  --filing-id FROZEN_ELIGIBLE_ID
```

对冻结清单重复指定 `--filing-id`，最多 50 个唯一 ID。命令会将选定来源内容发送给配置的 provider，将 journal 和不可发布的 `result.json` 写在 archive 外，不发布结果。只有 provider 配置和合格 filing 清单均已明确配置后才能运行。

实时进度写入 stderr，最终 JSON 摘要仍写入 stdout。证据分窗结束前窗口总数显示为未知；使用 `--no-progress` 可关闭进度输出。

## 2. 既有 `download` 流程

```text
quant-dataset download [--stage {market,financials,macros,organize,all}]... \
  [--start YYYY-MM-DD] [--end YYYY-MM-DD] [--tickers A,B,C] [--force] \
  [--force-rebuild] [--dry-run] [--data-dir PATH] [--workers N]
```

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--stage` | `all` | 可重复；`all` 展开为 market + financials + macros + organize |
| `--start` / `--end` | 无 | market 闭区间；选择 `market` 时必填 |
| `--tickers` | 全 universe | 逗号分隔 ticker 子集 |
| `--force` | 关闭 | 重置下载进度并重新下载所选条目 |
| `--force-rebuild` | 关闭 | 只强制重建 organize 输出，不重置下载进度 |
| `--dry-run` | 关闭 | 打印计划，不联网、不写盘 |
| `--data-dir` | `data` | raw、organized、progress 的基目录 |
| `--workers` | 16 | organize worker 数；小于 1 无效 |

现有 stage 的输出与范围：

| Stage | 现有行为 |
|---|---|
| `market` | 下载 Yahoo 日线至 `data/raw/yahoo/<TICKER>/<start>_<end>.csv` |
| `financials` | 下载 SEC Company Facts、submissions 与历史 submissions-page JSON 至 `data/raw/sec/financials/`，沿用现有内容寻址缓存/manifest。它**不是**完整 filing HTML/iXBRL 档案。 |
| `macros` | 下载配置的 11 条 FRED 序列至 `data/raw/fred/` |
| `organize` | 写入既有行情/宏观面板及独立财务产物，包括旧 `financials.csv` 和适用时选定的 `financial_events_v1` 事件/事实 Parquet |

旧 `financials.csv` 与 `financial_events_v1` organized artifact 和 `samples` 分离，不是 sample builder 输入。event/fact artifact 是选定九概念抽取，不是完整 XBRL/申报档案。样本包迁移不改变下载或 organize 行为。另有有界的 filing catalog/download/parse/verify/query 命令；档案完整性和 parser 状态都不代表财务覆盖完整。详见[申报使用指南](filings.zh-CN.md)与[开发状态/契约](../developer/financial-filing-archive.zh-CN.md)。

下载进度按 item 续跑。重跑时跳过已完成 item，除非 `--force` 重置工作清单；`--force-rebuild` 只影响 organize。

> **构建 samples：** 使用现有 organized 行情/宏观输入构建候选不需要先下载。不要把 `download --stage all` 作为样本契约变更的一部分；它包含现有 SEC 结构化数据下载。

## 3. `build-samples` 与安全候选输出

```text
quant-dataset build-samples [--workspace-root PATH] [--data-dir data/organized] \
  [--out data/samples-output] [--exclusions-file PATH]
```

| 参数 | CLI 默认值 | 说明 |
|---|---|---|
| `--workspace-root` | 当前工作目录 | 默认 exclusions 路径与 workspace data 保护使用的工作区根目录。不从已安装的 source package 位置推导。 |
| `--data-dir` | `data/organized` | organized 行情与宏观输入。财务文件和财务 `_meta.json` 记录不是样本输入。 |
| `--out` | `data/samples-output` | 样本包输出目录，按 CWD 解析。目标必须是全新/空目录，不能是符号链接路径，也不能与受保护路径或实际输入重叠。保留的 `data/output/` 是历史产物且受保护；若默认路径已存在，请改用全新候选路径。 |
| `--exclusions-file` | `<workspace_root>/config/universes/exclusions_v1.json` | 未指定时读取此 workspace-relative 文件；显式指定时按当前工作目录（CWD）解析。文件缺失时报错，不会回退为空清单。保留文件名 `exclusions_v1.json`，它不是样本产品版本。 |

默认 `workspace_root` 是进程当前工作目录；可用 `--workspace-root PATH` 显式指定。未指定时默认 exclusions 为 `<workspace_root>/config/universes/exclusions_v1.json`；显式指定 `--exclusions-file` 时仍按 CWD 解析。

构建会保护 `<workspace_root>/data/` 及相关 raw/output/baselines 路径；对常规 `<data>/organized` 输入，还会独立于 `workspace_root` 保护其旁边识别出的 raw/output/baseline/archive 路径。显式 workspace root 必须是已存在目录。输出 guard 拒绝符号链接路径/祖先、非空目标，以及与受保护路径或实际输入重叠的目标。候选必须输出到这些位置之外的全新路径。不要指向保留的 `data/output/`、baseline 或输入目录；不得删除历史数据后复用路径。

```bash
WORKSPACE_ROOT=$PWD
CANDIDATE_DIR=/tmp/opencode/candidate-samples-finance-free
test ! -e "$CANDIDATE_DIR" && test ! -L "$CANDIDATE_DIR" || { echo "Choose a new, unused, non-symlink candidate directory" >&2; exit 1; }
PYTHONPATH=src .venv/bin/python -m cli.main build-samples \
  --workspace-root "$WORKSPACE_ROOT" \
  --data-dir data/organized --out "$CANDIDATE_DIR"
```

已实现的 `samples` 契约有 96 个特征（43 raw + 10 CS + 43 MISS）和 132 个物理列（4 键/标志 + 96 特征 + 32 标签）。新构建使用 `schema_version="samples"`；manifest 记录精确列序的 feature list、semantic contract 与 schema/semantic fingerprints、构建参数、实际消费输入的流式内容 hash/字节数、代码/依赖身份，以及所有登记输出的 hash/字节数/行数。发布 manifest 前会复核输入哈希；单独的 `input_inventory` 仍仅为计数，不是内容来源证明。现有 `data/output/` bundle 是原样保留的历史发布，manifest 原值 `schema_version="samples_v3"`（23,938,669 行、6,532 个 ticker）；本指南不会给它改名或重新发布。builder 不读取财务文件或 metadata。候选构建独立进行，不会替换归档 output、backup 或冻结 baseline。

## 4. 核验与发布状态

对全新候选运行任务指定的当前契约检查。使用独立预期值 fixtures 验证标签和边界行为；同时检查 schema/列序/dtype、财务隔离、split/purge、缺失率、安全输出路径、实际输入 provenance 与 output hashes。历史跨代 projection 不是正确性 oracle。

2026-10-03 发布记录（`COMMITTED_V3_V1_BACKUP_RETAINED`）仅描述已保留 artifact 及当时记录的检查，不是当前代码或新候选的核验结果，不得继承其结论。本次不声称性能实测或财务正确性。旧 147 列 output 保留于 `data/output-v1-backup-20261003T172214933236Z`；冻结 baseline 单独保留。

本文不声称任何当前 v3 runtime 或性能实测。实测必须来自标明环境的真实运行；不要把旧 v2 占位符复制到活跃契约。

## 5. 通用的既有流程

下例适用于其他任务中的既有 source-ingestion 流程，不是 sample-candidate 步骤。`--stage all` 会包含上文所述的既有 SEC 结构化数据下载。

```bash
PYTHONPATH=src .venv/bin/python -m cli.main download \
  --stage all --start 1990-01-01 --end 2025-12-31 --workers 16
```

用 `--dry-run` 查看既有下载计划而不写盘。独立的 `filings download` 命令用于向已有 catalog archive 做有界获取；它不是旧 Company Facts stage。`filings parse` 会在独立 workspace 中处理已归档文件。五份真实申报 pilot 为 partial，详见[申报指南](filings.zh-CN.md)。

## 6. 退出码与常见错误

| 退出码 | 含义 |
|---|---|
| 0 | 命令成功完成（含 `--dry-run`） |
| 1 | 既有 download/配置错误、样本 ticker 失败或 filings archive/parse 失败（parse 可能仍输出 partial summary） |
| 2 | 参数解析/校验错误，包括 limit 无效或 `filings parse` 参数组合不兼容 |

| 现象 | 处理方式 |
|---|---|
| 缺 SEC/FRED secret | 从示例创建 `config/secrets.toml`，为所选的既有下载 stage 填入所需 key |
| market stage 缺 `--start`/`--end` | 提供两个闭区间日期，或不选 `market` |
| `build-samples` 找不到 organized 行情输入 | 检查 `--data-dir`；如另有独立需求，再组织既有行情数据 |
| 候选 `--out` 被拒绝 | 另选全新、非符号链接目录，确保不在受保护/输入目录中；不要清理或复用历史产物 |
| 默认 exclusions 文件缺失 | 恢复 `<workspace_root>/config/universes/exclusions_v1.json`，或传入已有的 `--exclusions-file`；缺少 exclusions 不表示使用空清单 |
| `filings parse` 缺 `--workspace-root` 或 `--max-filings` 无效 | 提供已有的独立 workspace，并设置 1–50 的上限 |
| `filings parse` 缺 taxonomy/依赖 | 默认离线不会自动 fetch。仅在显式允许时加 `--prepare-dependencies`；检查真实 SEC contact 与获批 taxonomy host。别名未核实会报 failed/partial，不会补零 |
| Arelle parser support 缺失 | 安装 `uv sync --frozen --extra filings`；query 工具另外需要 `--extra query` |

## 7. 可选的 `query-samples` 命令

```text
quant-dataset query-samples --bundle PATH --sql 'SELECT ...' [--limit N]
```

必须同时指定 `--bundle` 和 `--sql`。结果以 CSV 写到标准输出（stdout），通过校验的 schema 标签（`samples`）写到标准错误（stderr）；`--limit` 默认 20 行，可设为 1 到 1,000 行。命令严格要求当前 `samples` manifest，并核验 132 列样本 schema、完整 feature list、semantic contract 与 fingerprints，以及 manifest 登记输出的全部 hashes/字节数/行数。它拒绝历史 `samples_v1`/`samples_v2`/`samples_v3` bundle，不提供别名或迁移模式。保留的 `data/output/` 与原 147 列 backup 都是历史 artifact；请用 Python/PyArrow 检查，不要用 `query-samples`。示例、只读行为和边界见[查询指南](query.zh-CN.md)。

## 8. 独立的 `filings` 档案命令

```text
quant-dataset filings catalog --archive PATH --cache-root PATH --cik DIGITS... \
  --start YYYY-MM-DD --end YYYY-MM-DD [--form FORM] [--resume] [--allow-partial]
quant-dataset filings download --archive PATH [--max-filings N] [--filing-id ID]... [--secrets PATH]
quant-dataset filings parse --archive PATH --workspace-root PATH \
  [--filing-id ID]... [--max-filings N] [--prepare-dependencies] \
  [--taxonomy-host HOST]... [--secrets PATH]
quant-dataset filings verify --archive PATH
quant-dataset filings query --archive PATH --sql 'SELECT ...' [--limit N]
```

这与 `download --stage financials` 不同：catalog 仅读本地 cache，不会抓取缺失输入；download 有界（默认 5、最多 50），且要求已有 catalog archive。Catalog 范围改变时，请创建新 run root；不要 force-overwrite 到不同 CIK/date/form scope。Archive 必须选在受保护 input/cache/output/candidate 路径之外。下载 contact 只读取 `[secrets].sec_user_agent`，不需要 FRED key。Blocked、unavailable 或 error 会返回非零状态，即使命令仍打印 partial summary；单独的 `needs_review` 仅作提示。

`filings parse` 同样要求已有 archive，且必须指定独立、已存在的 workspace；默认最多解析 5 份已就绪申报，最多 50，可重复用精确 `cik10:accession_number` 指定 ID。只处理 archive 已有的选中文件，不下载申报文档。默认**纯离线**，不读 secrets；本地 taxonomy 缺失时会如实记录 failed/partial，不当作零。只有明确加 `--prepare-dependencies` 才会读 `[secrets].sec_user_agent` 并用有界 client 获取 taxonomy；Arelle 本身仍处于 offline。可重复指定的 `--taxonomy-host` 只能缩小内置批准名单，不能添加任意主机。Failed/partial 返回非零；unsupported-only 会报告 `completed_with_unsupported`，不宣称解析完整。

Query 需要可选 `query` extra。结果 CSV 输出到 stdout，query metadata 写到 stderr。SQL 是一条可信的本地 SELECT/CTE；行数限制只限制返回行数，不限制聚合/排序工作量。只开放 manifest 实际列出的 `filings`、`documents`、`facts`、`sections`、`parses`、`dependencies`，没有空占位 view。Descriptor 检查不等于完整 core row acceptance；`verify` 也不代表财务覆盖完整。Amendment 保持独立，不自动选 latest 或执行 PIT 逻辑。独立五份申报 archive pilot 已持久化，但仍为 partial（10 次 parser attempt：6 full、2 partial、1 unsupported、1 failed）；它不是 daily v3 输入。逐份结果与限制见[filings 指南](filings.zh-CN.md)。Daily v3 发布状态与 filing-archive 状态彼此独立。
