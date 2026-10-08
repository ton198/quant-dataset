[English](data-contracts.md) | **简体中文**

# 数据契约

本文记录数据各层（`data/raw/` → `data/organized/` → 样本输出）的契约。唯一维护的样本契约见 §3，是不含财务数据的 `samples` schema。现有样本包按原样保留为历史产物：2026-10-03 发布于 `data/output/` 的 manifest 原值为 `schema_version="samples_v3"`，原 147 列 output 保留在 `data/output-v1-backup-20261003T172214933236Z`；冻结 baseline 另行保留。这些是既有数据记录，不代表新发布或当前核验。已废弃且未实施的财务特征提案不属于当前契约。

消费者侧的使用说明见 [../user/data-format.zh-CN.md](../user/data-format.zh-CN.md)；样本构建实现细节见 [samples.zh-CN.md](samples.zh-CN.md)；下载与 organize 实现细节见 [download.zh-CN.md](download.zh-CN.md)。

## 0. 契约层级

| 层 | 路径 | 契约强度 | 变更影响 |
|---|---|---|---|
| raw | `data/raw/` | payload 内容寻址、只追加；`manifest.json` 记录 key → 版本 | 只追加不影响下游；但 organized 不自动跟进 |
| organized | `data/organized/` | 本节契约；每个输出在 `_meta.json` 记录输入/输出 sha256 | 样本候选只消费行情/宏观面板；既有 bundle 与 baseline 保持原样 |
| output | 保留的历史 `data/output/`；CLI 新候选默认 `data/samples-output` | 新构建使用单一 `samples` 契约；manifest 登记输出 hash/bytes/rows | 不得指向保留的 bundle、backup 或 baseline；必须另选全新未占用路径 |

## 1. data/raw/

### 1.1 Yahoo 行情

```
data/raw/yahoo/<TICKER>/<start>_<end>.csv        # start/end 为 download --start/--end
```

- 列（yfinance 原样写出，`auto_adjust=false, actions=true`）：`date`(index), `Adj Close`, `Close`, `Dividends`, `High`, `Low`, `Open`, `Stock Splits`, `Volume`。
- **非内容寻址**：同一 ticker 同区间重下会覆盖同名文件；下载失败/空结果不落盘——因此“没有目录”表示该 ticker 无行情。本文不声称当前样本 ticker 数量实测。
- 文件名区间是 CLI 传入值，不是数据实际覆盖区间；organize 时才按 session 裁剪。

### 1.2 SEC universe 快照

```
data/raw/sec/universe/<sha256(content)>.json
```

- 原始 `company_tickers_exchange` JSON：`{"fields": ["cik","name","ticker","exchange"], "data": [[...], ...]}`。
- 读取端要求文件名 stem == 内容 sha256，否则跳过；多个快照取排序后第一个有效者。exchange 过滤（Nasdaq/NYSE）在读取时进行。

### 1.3 SEC financials（内容寻址 + manifest）

```
data/raw/sec/financials/
  <sha256>.json          # companyfacts / submissions / submissions-page payload
  manifest.json          # {"resources": {"<logical_key>": [entry, ...]}}
```

| `logical_key` | 对应 url / 内容 |
|---|---|
| `companyfacts:<cik10>` | `https://data.sec.gov/api/xbrl/companyfacts/CIK<cik10>.json` |
| `submissions:<cik10>` | `https://data.sec.gov/submissions/CIK<cik10>.json`（`filings.recent`） |
| `submissions-page:<cik10>:<name>` | 历史分页 `CIK<cik10>-submissions-<...>.json` |

`manifest.json` 的每个 entry（版本）字段：

| 字段 | 类型 | 语义 |
|---|---|---|
| `path` | string | 相对 `data/raw/sec/financials/` 的文件名（= `<sha256>.json`） |
| `sha256` | string | 文件内容 sha256，读取时校验 |
| `url` | string | 请求地址（缓存匹配键之一） |
| `fetched_at_utc` | string | 抓取时刻（UTC ISO） |
| `attempts` | int | 网络尝试次数 |
| `byte_size` | int | 字节数 |
| `status` | string | 当前均为 `"done"` |

- 同 key 可累积多版本；读取时对 `(logical_key, url)` 取**逆序第一个 sha256 校验通过**的版本，命中缓存不发网络请求。
- `manifest.json` 本身不是内容寻址文件（tmp + rename 原地更新），其余 payload 永不改写。

### 1.4 FRED

```
data/raw/fred/<SERIES_ID>/<sha256>.json          # FRED observations JSON
data/raw/fred/manifest.json                      # 同 1.3 的 entry 结构
```

- `logical_key = observations:<SERIES_ID>`；`path` 形如 `<SERIES_ID>/<sha256>.json`。
- 当前 11 条序列：`BAMLH0A0HYM2`、`CPIAUCSL`、`CPILFESL`、`DCOILWTICO`、`DEXUSEU`、`DGS10`、`DGS2`、`FEDFUNDS`、`PAYEMS`、`UNRATE`、`VIXCLS`（`config/sources.toml [macros].series`）。

## 2. data/organized/

### 2.1 stocks/<TICKER>/market.csv

| 列 | 类型 | 语义 |
|---|---|---|
| `date` | string `YYYY-MM-DD` | XNYS session；行集 = Yahoo 区间 ∩ organize calendar，按 date 排序 |
| `open` / `high` / `low` / `close` | float64 | 原始（未复权）OHLC |
| `adj_close` | float64 | Yahoo 复权收盘 |
| `volume` | int64 | 原始成交量 |
| `adjustment_factor` | float64 | `adj_close / close`（标签用的 adj_open 在 build-samples 内用同样比值重算） |
| `return_1d` / `return_5d` / `return_20d` | float64 | `close.pct_change(n)`，前 n 行为空 |
| `volatility_20` | float64 | `return_1d` 的 20 日滚动标准差（ddof=1） |
| `volume_ratio_20` | float64 | `volume / volume.rolling(20).mean()` |
| `intraday_range` | float64 | `(high - low) / close` |
| `quality_flag` | string | `ok` / `invalid_ohlc` / `negative_price`，逐行由 OHLC+volume 合法性判定 |

- 行数因 IPO/退市/数据缺口而异；不要从旧样本 bundle 推断当前 universe 大小。若 organize 使用更晚结束日期重跑，单文件可能更长。
- 重复 date 保留最后一条；非 session 行在 organize 时剔除（计入 `_meta.row_counts.market_dropped_non_session`）。

### 2.2 stocks/<TICKER>/financials.csv

每个 session 一行，宽表；内容 = 该 session 可见的最新一份申报快照。

| 列 | 类型（读入约定） | 语义 |
|---|---|---|
| `date` | string `YYYY-MM-DD` | organize calendar session |
| `available_as_of` | string / null | 该快照的 SEC `filingDate`；可见规则 **filingDate < session**（下一 session 才可见） |
| `accession_number` | string / null | SEC accession |
| `form` | string / null | 原样 form 类型（含 `/A`） |
| `is_amendment` | bool / null | `form.endswith("/A")` |
| `fiscal_year` | Int64 可空 | 申报覆盖的财年；无法判定为 null |
| `fiscal_period` | string / null | `Q1`..`Q4` / `FY` |
| `report_period_end` | string / null | 申报覆盖期末 |
| `days_since_filing` | int / null | `session - filingDate` |
| `revenue` `gross_profit` `operating_income` `net_income` `operating_cash_flow` `capital_expenditure` `assets` `liabilities` `equity` | float / null | 白名单 concept 抽取结果（见下表），null = 该申报没有匹配事实 |
| `quality_status` | string | `ok`（至少一个 concept 非空）/ `missing`（无任何 concept）/ `amendment_only`（修订稿且同期末无原始申报） |

Concept 白名单（按顺序取第一个有值的 tag，来自 `organize_financials._CONCEPTS`）：

| 输出列 | tag 优先级（高 → 低） |
|---|---|
| `revenue` | `Revenues`, `RevenueFromContractWithCustomerExcludingAssessedTax`, `RevenueFromContractWithCustomerIncludingAssessedTax`, `SalesRevenueNet`, `SalesRevenueGoodsNet`, `SalesRevenueServicesNet`, `SalesRevenueGoodsGross`, `SalesRevenueServicesGross`, `RevenuesNetOfInterestExpense`, `FinancialServicesRevenue`, `InsuranceServicesRevenue`, `RevenueNotFromContractWithCustomer`, `SalesRevenueOilGas` |
| `net_income` | `NetIncomeLoss`, `ProfitLoss`, `NetIncomeLossAvailableToCommonStockholdersBasic`, `NetIncomeLossAvailableToCommonStockholdersDiluted` |
| `operating_income` | `OperatingIncomeLoss` |
| `gross_profit` | `GrossProfit` |
| `operating_cash_flow` | `NetCashProvidedByUsedInOperatingActivities` |
| `capital_expenditure` | `PaymentsToAcquirePropertyPlantAndEquipment` |
| `assets` | `Assets`, `AssetsNet` |
| `liabilities` | `Liabilities` |
| `equity` | `StockholdersEquity` |

Fact 选择与 fiscal 标识：

- 只匹配同一 filing（`filed` + `end` + 可选 `fy`/`fp`/`form`/`accn`）的事实；10-Q 优先 70–125 天的单季区间、10-K 优先 300–400 天的年度区间（instant 事实按 `end` 匹配）；平局取时长最接近 91/365 天者。
- `fiscal_year`/`fiscal_period` 依次取：选中事实的 `fy`/`fp` → submissions 元数据 → filing 自带 `fy`/`fp` → 10-K 按报告年 + `FY` → 10-Q 由上一份 10-K 起数季度（Q1..Q3）→ 否则 null。
- 行数等于 organize 时的 session calendar 长度。该文件保留旧每日**整份快照覆盖**语义：`available_as_of` 存 `filed_date`，非财务申报可替换行内所有概念值，且不按概念 carry-forward。`samples` 不读取该 CSV，也不将其作为样本输入。

### 2.2.1 既有 organized financial event/fact 抽取（与 samples 分离）

两张 Parquet 表及其 `_meta.json` 登记共同构成现有 `financial_events_v1` artifact。它们从 SEC submissions 与 Company Facts 生成，早于旧 daily snapshot 选择；与 `financials.csv` 共存，不替换或重新定义它。该产物是选定九概念的结构化抽取，不是完整 XBRL archive 或 filing 全文 archive。`samples` builder 不读取这些文件或其中的财务 metadata。

| 产物 | 粒度与关键字段 |
|---|---|
| `financial_events.parquet` | 每行一个有效申报事件，包含没有财务 facts 的事件。字段包括稳定 `event_id`、`asset_id`、补零的 `cik10`、`accession_number`、`filed_date`、`effective_visible_session`、`form`、`is_amendment`、报告/财政期间元数据及 key 来源、`quality_status`、submission 路径/hash/locator。 |
| `financial_facts.parquet` | 每行一个规范化概念/实际期间/版本。字段包括 `fact_version_id`、`event_id`、`concept`、有限 float64 `value`、`unit`、`taxonomy`、`tag`、`period_kind`、`period_start`、`report_period_end`、`duration_days`、解析出的 fiscal key/source、披露/生效日期、fact accession、`match_method`、fact 路径/hash/locator。 |

两表使用固定 Arrow schema，即使为空表也如此。事件 ID 有 accession 时由 CIK + accession 组成；无 accession 时由来源 hash + 记录 locator 生成，并计数诊断。事实必须引用有效事件。候选先做有限值筛选再按 tag 优先级选择；本 artifact 消费货币仅接受 USD。流量期间只有经验证的 10-Q 且时长 70–125 天时标为 `quarter`，经验证的 10-K 且时长 300–400 天时标为 `annual`；YTD/其他时段不能作为单季。缺 start 的事实可保留为 `unknown` 披露，但不具备增长或流量比率公式资格。存量概念为 `instant`。无法匹配/匹配有歧义、来源冲突、日期无效、币种不支持、非有限值和区间无效均拒绝并计数，绝不根据下载时间静默构造披露顺序。

`effective_visible_session` 是严格晚于 `filed_date` 的第一个 XNYS session。来源完成身份同时包含 CIK 和 `raw_input_inventory_sha256`；`_meta.json.financial_events.complete` fail-closed，要求资源状态、日历映射、文件、schema、输出 hash 与事件/事实关系均有效。确认无输入时写 schema 正确的空表并注明原因；缺失、损坏或不完整输入不属于有效空 artifact。例如，某 CIK 没有可用资源记录时以 `empty_reason="no_usable_input_for_cik"` 和 `submissions/companyfacts=no_input` 表示。有效的 Company Facts 占位/空 facts payload 则可记为非致死状态 `companyfacts=no_usable_facts`：submissions 仍可提供申报事件，而事实表可为空。`input_resource_status` 对象记录 `manifest`、`submissions`、`companyfacts`；`rejection_counts` 提供 raw manifest/payload 有效性、申报日期与 CIK 身份、来源/accession 匹配、taxonomy/unit/数值/有限值检查、期间有效性、fact/fiscal-key 冲突及生效 session 映射等诊断。单个计数器 key 名称和聚合细节属于实现诊断，不是稳定契约；消费者不得依赖具体 key。

Raw manifest 和单资源诊断也会按需纳入；具体计数器名称与聚合方式属于实现诊断，不是稳定接口。

### 2.3 shared/macro.csv

| 列 | 类型 | 语义 |
|---|---|---|
| `date` | string `YYYY-MM-DD` | organize calendar session |
| 11 条序列（同 §1.4） | float64 | session 对齐、向前填充（ffill）的观测值 |

- 可见性规则（保守代理）：观测参考期 + 1 个自然月为该月近似发布日期，取**严格晚于**该日期的第一个 session 起可见；缺失在 session 轴上 ffill。
- FRED 响应不含发布日期，这是已知近似（写入 `_meta.json.known_issues`），不是 bug。

### 2.4 _meta.json（每 ticker 与 shared/ 各一份）

| 字段 | 语义 |
|---|---|
| `ticker` | 目录名大写；shared 为 `"shared"` |
| `generated_at_utc` | 最近一次 organize 时刻 |
| `cleaning_rules_version` | 当前 `"v1"` |
| `inputs` | `[{path, sha256}]`，raw 层实际消费的文件 |
| `outputs` | `[{path, sha256, rows}]`，organized 输出 |
| `row_counts` | `market_input`/`market_output`/`market_dropped_non_session`、旧 `financials_input`/`financials_output`、`financial_events`、`financial_facts` 或 `macro_output` |
| `financial_events` | 嵌套 artifact 记录：`contract_version`、`complete`、`status`、`empty_reason`、`cik10`、`input_hashes`、`raw_input_inventory`、`raw_input_inventory_sha256`、`input_resource_status`、`input_resource_diagnostics`、`calendar`、`output_calendar`、`quality_counts`、`rejection_counts` 及 `events`/`facts` 的 path/hash/rows |
| `cik10` / `raw_input_inventory_sha256` | 顶层 completion identity 与申报事件记录一致；绑定发行人和 raw SEC 输入盘点 |
| `known_issues` | 人工/程序记录的口径说明 |

- `_ticker_is_organized` 只有在当前 `financial_events_v1` 契约、`complete=true`、CIK 与 raw-input fingerprint 匹配、状态字段有效、事件/事实表 schema 正确且输出 hash/行数登记一致时才会跳过。仅有旧 CSV 不能视为完成。

## 3. 单一样本契约（`samples`）

新样本构建使用不含财务数据的单一 `samples` 契约。现有 2026-10-03 `data/output/` bundle 是原样保留的历史产物，其 manifest 仍为 `schema_version="samples_v3"`；本节不会重新标记或发布该 bundle。原 147 列 output 与冻结 baseline 分别保留。旧 artifact 中的 `financial_status=not_applicable` 不代表财务覆盖 PASS。

### 3.1 输入与财务隔离

builder 读取 organized `stocks/<TICKER>/market.csv`、`shared/macro.csv` 和配置的 exclusions。它不读取 `financials.csv`、`financial_events.parquet`、`financial_facts.parquet` 或财务 `_meta.json` 记录；不运行财务预检、财务覆盖分析或财务输入盘点。财务状态为 `not_applicable`。既有 SEC organized 文件保持原样并可独立使用。

### 3.2 封闭 schema 与 canonical 列序

| 组 | 数量 | 内容 |
|---|---:|---|
| 键 / 标志 | 4 | `date`、`asset_id`、`is_common`、`flag_extreme_label` |
| Raw | 43 | 10 个股票行情/派生特征 + 33 个宏观特征 |
| 截面 | 10 | 10 个股票级 raw 的同日变换；不生成宏观 CS |
| 缺失指示 | 43 | 每个 raw 列一个 `miss_*` |
| `manifest.feature_list` | **96** | 43 raw + 10 CS + 43 MISS |
| 标签 | 32 | 30 个 forward return 标签 + `excess_5d` 与 `excess_21d` |
| 样本物理列 | **132** | 4 键/标志 + 96 特征 + 32 标签 |

列序：4 键/标志 → 43 raw → 10 CS → 43 MISS → 32 标签。物理 Arrow dtype 为 `date32`、`asset_id` 使用 `large_string`、`is_common` 为 bool、flags/MISS 为 uint8、数值特征/标签为 float32。此前 51 个财务列全部移除：17 个财务 raw + 17 个财务 CS + 17 个财务 MISS。活跃 schema 不含申报年龄、财务年龄、level、增长率、比率或 QoQ 字段。

10 个股票 raw 列为既有 6 个行情直通列（`return_1d/5d/20d`、`volatility_20`、`volume_ratio_20`、`intraday_range`）及 4 个派生值（`momentum_60/120`、`volatility_60`、`volume_zscore_60`）。33 个宏观列仍为 11 个序列 × 水平值 / canonical 位序 `_d1` / `_d5`。

### 3.3 保持的行为与 metadata

当前行为契约定义 `(date, asset_id)` 键、flags、32 个 label 值及 null mask、splits/purge、extreme-label 行为、ticker failure 语义及非财务 raw/CS/MISS 值。使用独立 fixtures 和明确不变式验证公式及边界；跨代历史 projection 不是正确性 oracle。Canonical calendar、宏观对齐、截面排名和标签公式见下文。

`meta.parquet.missing_frac` 在 purge 后只统计 43 个非财务 raw 特征：`raw 空值单元格数 / (retained_rows × 43)`。排除 CS/MISS、标签、键/标志及财务数据。

每个新 manifest 用 `schema_version="samples"` 标识唯一契约，并记录精确有序的 96 列 `feature_list`、`semantic_contract`、schema/semantic fingerprints、实际消费的行情/宏观/exclusion 输入、`label_semantics` 和构建参数。不列出或检查财务 artifact。QC 保留行数、label、raw/CS/MISS、截面、purge、extreme-label、ticker-failure 摘要；契约没有财务特征 coverage 章节。`input_provenance.files` 记录每个实际消费输入的路径、流式 SHA-256 与字节数，并在 manifest 发布前复核；`code_identity` 记录 samples 五个 package 文件的 hash/bytes 与 NumPy、pandas、PyArrow 版本。`input_inventory` 仅为计数，不是内容盘点。每个登记输出均记录 SHA-256、字节数和行数。

### 3.4 安全候选与发布核验

CLI 默认 `--out data/samples-output`。`workspace_root` 默认是当前工作目录，可用 `--workspace-root` 指定已存在目录，绝不从已安装 source package 位置推导。默认 exclusions 文件是 `<workspace_root>/config/universes/exclusions_v1.json`；显式 `--exclusions-file` 路径相对于 CWD。exclusions 文件缺失时报错，不使用空清单。保护 `<workspace_root>/data/` 及相关 raw/output/baselines 路径；若 organized 输入位于常规 `<data>/organized`，还会独立于 `workspace_root` 保护其 `<data>` 旁识别出的 raw/output/baseline/archive 路径。拒绝符号链接路径/祖先、非空目标，以及与受保护路径或实际输入重叠的目标。使用全新未占用候选路径，不要指向保留 output 或 baseline。对候选使用独立当前契约 fixtures 与不变式验证：准确列清单/顺序/dtype、不含财务输入/列、标签和 split/purge 边界、43 raw 的 `missing_frac` 分母、安全输出路径、实际输入 provenance 与 output hashes。

已归档的 2026-10-03 发布报告仅记录当时的产物；复用的测试结果及历史 projection 不是当前源码验证或正确性 oracle。不得据此推断性能、财务正确性或新候选发布 PASS；新候选须有自己的核验记录。


## 4. 既有归档样本产物（只读）

| 路径 | 已记录的产物事实 | 处理方式 |
|---|---|---|
| `data/output/` | 2026-10-03 发布时 manifest 为 `schema_version="samples_v3"`；23,938,669 行、6,532 个 ticker、132 个物理列、96 个特征。 | 文件与 manifest 原样保留；此记录不代表重新发布。 |
| `data/output-v1-backup-20261003T172214933236Z/` | 原 147 列样本产物。 | 作为历史数据保留；不是可选产品版本或兼容目标。 |
| `data/baselines/samples_v1_financial_upgrade/` | 冻结的历史 baseline。 | 作为只读归档参考原样保留。 |

这些产物中的 schema 标签只描述其原始内容。不得重命名、改写或重标已有输出，使其看起来像由新的 `samples` 契约生成。当前契约正确性测试使用独立 fixtures，不把这些归档数据当成长期 oracle。
