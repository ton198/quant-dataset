# 数据契约

本文是各层数据（`data/raw/` → `data/organized/` → `data/output/`）的 schema 与语义契约，也是重建后验货的依据。所有数值与当前仓库实物一致（生成于 2026-09-29）；重建后以新生成的 `manifest.json` / `qc_report.*` / `splits.json` 为准。

消费者侧的使用说明见 [../user/data-format.md](../user/data-format.md)；样本构建实现细节见 [samples.md](samples.md)；下载与 organize 实现细节见 [download.md](download.md)。

## 0. 契约层级

| 层 | 路径 | 契约强度 | 变更影响 |
|---|---|---|---|
| raw | `data/raw/` | payload 内容寻址、只追加；`manifest.json` 记录 key → 版本 | 只追加不影响下游；但 organized 不自动跟进 |
| organized | `data/organized/` | 本节契约；每个输出在 `_meta.json` 记录输入/输出 sha256 | 改列/语义 → 必须重建 `data/output/` |
| output | `data/output/` | `samples_v1`；`manifest.json.outputs` 记录全部输出 sha256/bytes/rows | 契约变更须 bump `schema_version` 并更新本文 |

## 1. data/raw/

### 1.1 Yahoo 行情

```
data/raw/yahoo/<TICKER>/<start>_<end>.csv        # start/end 为 download --start/--end
```

- 列（yfinance 原样写出，`auto_adjust=false, actions=true`）：`date`(index), `Adj Close`, `Close`, `Dividends`, `High`, `Low`, `Open`, `Stock Splits`, `Volume`。
- **非内容寻址**：同一 ticker 同区间重下会覆盖同名文件；下载失败/空结果不落盘——因此“没有目录”= 该 ticker 无行情（当前 1,128 只此类，见 §4）。
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

- 行数因 IPO/退市/数据缺口而异（当前 6,534 个文件、3,377 种行数）；全量基线中单文件最多 9,067 行（= 样本 canonical session 数），若 organize 用更晚的结束日期重跑可更长。
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
- 行数 = organize 运行时 session calendar 的长度，当前实物为混合值（9,067 行 × 1,118 文件；9,252 行 × 6,539；个别 9,250）。build-samples 只消费 `available_as_of ≤ 信号日` 的快照，与文件行数无关。

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
| `row_counts` | `market_input`/`market_output`/`market_dropped_non_session` 或 `financials_input`（申报数）/`financials_output`（session 行数）/`macro_output` |
| `known_issues` | 人工/程序记录的口径说明 |

- `manager._ticker_is_organized` 依据该文件 + 文件存在性/哈希判断能否跳过。

## 3. data/output/（样本包，schema_version = samples_v1）

### 3.1 文件清单

| 文件 | 规模（当前实物） | 说明 |
|---|---|---|
| `samples/year=YYYY/part-00000.parquet` | 36 个文件，23,938,669 行 | 按 signal session 年份 Hive 分区，snappy |
| `meta.parquet` | 6,532 行 | 每 ticker 汇总（见 §3.4） |
| `manifest.json` | — | 契约、参数、偏差、全部输出 sha256 |
| `splits.json` | — | 四窗边界、purge 语义与行数（见 §3.5） |
| `qc_report.json` / `qc_report.md` | — | QC（见 §3.7） |

### 3.2 samples 列契约（147 列）

列序固定：4 键/标志 + 48 `f_raw` + 15 `f_cs` + 48 `miss` + 30 `target_return_*` + 2 `excess_*`。Parquet dtype：`date`=date32，`asset_id`=large_string，`is_common`=bool，`flag_extreme_label` 与全部 `miss_*`=uint8，其余数值=float32。

**键与标志（4）**

| 列 | dtype | 语义 |
|---|---|---|
| `date` | date32 | signal session（canonical calendar） |
| `asset_id` | large_string | ticker（大写） |
| `is_common` | bool | 非单位/权证/优先股/权利的启发式标记（后缀规则）；false 行保留 |
| `flag_extreme_label` | uint8 | 见 §3.3 |

**f_raw：个股（15）**，`float32`

| 列 | 语义 |
|---|---|
| `f_raw_return_1d` / `_5d` / `_20d` | 直接取自 market.csv 同名列（raw close 收益） |
| `f_raw_volatility_20` | market.csv `volatility_20` |
| `f_raw_volume_ratio_20` | market.csv `volume_ratio_20` |
| `f_raw_intraday_range` | market.csv `intraday_range` |
| `f_raw_momentum_60` / `_120` | canonical 轴上 `adj_close[t]/adj_close[t-n] - 1`，要求两端正有限 |
| `f_raw_volatility_60` | canonical 轴上 adj_close 日收益的 60 期滚动标准差（ddof=1） |
| `f_raw_volume_zscore_60` | `(volume - mean60) / std60`（ddof=1；std≤0 或不足 60 期为空） |
| `f_raw_revenue_yoy` / `f_raw_net_income_yoy` / `f_raw_operating_income_yoy` / `f_raw_assets_yoy` | as-of 快照上的同比：`current/prior - 1`；prior 优先同 `(fiscal_year-1, fiscal_period)`，否则 report_period_end −1 年 ±15 天内最近期末；prior 缺失或为 0 则空 |
| `f_raw_days_since_filing` | `signal date - available_as_of`（天） |

**f_raw：宏观（33）**，`float32`，按 canonical 轴逐位对齐：

| 序列（11） | 派生列 |
|---|---|
| `BAMLH0A0HYM2`, `CPIAUCSL`, `CPILFESL`, `DCOILWTICO`, `DEXUSEU`, `DGS10`, `DGS2`, `FEDFUNDS`, `PAYEMS`, `UNRATE`, `VIXCLS` | 每条 3 列：`f_raw_m_<S>`（水平值）、`f_raw_m_<S>_d1`、`f_raw_m_<S>_d5`（canonical 轴上的 1/5 位差分，不做前视） |

**f_cs：截面标准化（15）**，`float32`，逐列对应 15 个个股 `f_raw`：

| 列 | 语义 |
|---|---|
| `f_cs_return_1d` … `f_cs_days_since_filing` | 同日期截面上对该日非空 raw 值做 average rank → `p = (rank - 0.5) / n` → 逆正态 `Φ⁻¹(p)`（Acklam 近似）；raw 为空则结果为空 |

- 截面范围：当日所有纳入的股票（含 `is_common=false` 行）；宏观列不参与（同日期恒定）。
- 方法字符串以 `manifest.json.feature_contract.cross_sectional_method` 为准。

**miss：缺失指示（48）**，`uint8`（1 = 缺失）：

- 与 48 个 `f_raw` 一一对应，命名 = `miss_` + raw 名去掉 `f_raw_` 前缀（如 `f_raw_return_1d` → `miss_return_1d`，`f_raw_m_DGS10_d1` → `miss_m_DGS10_d1`）。
- 规则：非有限值（±inf/NaN）在特征生成时先转缺失，再按 `isna()` 置位（见 [AGENT.md](../../AGENT.md) 不变式 7）。

**标签（32）**，`float32`：

| 列 | 语义 |
|---|---|
| `target_return_{1..30}d` | `adj_open(t+1+h) / adj_open(t+1) - 1`；`adj_open = open × adj_close / close`；h 为 canonical 轴上的位序偏移 |
| `excess_5d` / `excess_21d` | `target_return_hd - 同日等权均值`；均值只统计 `is_common=true` 且 `flag_extreme_label=0` 的行（无 SPY 基准） |

- 标签要求 entry/exit 两个 bar 都存在且 `open/close/adj_close/adj_open` 正有限；不要求中间 bar，也不做任何填充。
- 数据集尾部（entry/exit 越界）与停牌缺口自然为空。

### 3.3 flag_extreme_label

- `1` = 任一 1..30 日标签窗口（t+1 到 t+1+h）跨越源数据价格跳变：相邻 session 的 `adj_open` 比值落在 `[0.5, 2.0]` 之外（如未复权的反向拆股）。
- 行保留；`excess_*` 基准均值剔除；训练侧应自行 drop/down-weight。当前计数 120,510（`manifest.json.data_quality_flags`）。

### 3.4 meta.parquet

| 列 | dtype | 语义 |
|---|---|---|
| `asset_id` | large_string | ticker |
| `is_common` | bool | 同 samples |
| `first_date` / `last_date` | date32 | 该 ticker 在样本中的首/末 signal session |
| `n_rows` | int64 | purge 之后保留的行数 |
| `missing_frac` | double | 该 ticker 的 raw 特征缺失比例 |

- 6,532 行 = 6,534（有 market.csv 的 ticker）− 2（`exclusions_v1.json` 排除的 AYA、FUND）。

### 3.5 splits.json

| 字段 | 语义 |
|---|---|
| `fit` / `select` / `screen` / `reserve` | `[start, end]` 闭区间（signal session 日期） |
| `purge_sessions` | `30`（最大标签 horizon） |
| `purge_semantics` | purge 口径全文（见下） |
| `purged_windows` | `{select, screen, reserve}` → 边界窗口详情（见下表） |
| `rows_by_split` | `{split: {before_purge, purged, retained}}` |
| `rows_removed_by_split` | `{split: purged}` 冗余便于校验 |

当前基线：

| split | 窗口 | before_purge | purged | retained |
|---|---|---:|---:|---:|
| fit | 1990-01-02 – 2018-12-31 | 15,374,917 | 120,987 | 15,253,930 |
| select | 2019-01-01 – 2020-12-31 | 2,105,325 | 139,158 | 1,966,167 |
| screen | 2021-01-01 – 2024-12-31 | 5,351,300 | 182,398 | 5,168,902 |
| reserve | 2025-01-01 – 2025-12-31 | 1,549,670 | 0 | 1,549,670 |

| purged_windows key | 被 purge 的 split | boundary_date | 窗口 | sessions | rows_removed |
|---|---|---|---|---:|---:|
| `select` | fit | 2019-01-02 | 2018-11-14 – 2018-12-31 | 31 | 120,987 |
| `screen` | select | 2021-01-04 | 2020-11-17 – 2020-12-31 | 31 | 139,158 |
| `reserve` | screen | 2025-01-02 | 2024-11-15 – 2024-12-31 | 31 | 182,398 |

- 口径：标签在 t+1 进入、t+1+30 退出，因此每个边界前 **31 个 signal session** 在构建期剔除；只有后续 split 窗口内有 canonical session 的边界才 purge，数据集尾部自然缺失的行保留。**消费者不再 purge。**

### 3.6 manifest.json

顶层字段：

| 字段 | 语义 |
|---|---|
| `schema_version` | `"samples_v1"`；列/语义变更必须 bump 并同步本文 |
| `row_counts` | `{samples, meta, by_year:{...}}` |
| `date_range` | `{start,end}` 样本 signal session 范围 |
| `feature_list` | 111 列特征顺序表（48 raw + 15 cs + 48 miss） |
| `feature_contract` | `raw_features` / `cross_sectional_features` / `missing_indicators`（raw→miss 映射）/ `cross_sectional_method` / `cross_sectional_scope` / `macro_differences` |
| `label_semantics` | 标签公式、horizon 口径、excess 基准全文 |
| `build_params` | 构建参数与 canonical 统计（见下表） |
| `data_quality_flags` | `flag_extreme_label: {dtype, rule, handling, count}` |
| `known_biases` | 4 条已知偏差（survivorship / FRED latest revised / adj_open 不可成交 / `is_common` 是启发式） |
| `exclusions_applied` | `{file, asset_ids, dropped_asset_ids_present}` |
| `input_inventory` | 输入盘点：`stock_directories`=7,662、`market_ticker_files`=6,534、`directories_without_market_csv`=1,128 + 名单、`financial_ticker_files`=6,534、`calendar_denominator_scope` |
| `is_common_column` | `is_common=false` 行保留、仍参与截面 rank 的说明 |
| `benchmark` | SPY 缺席、excess 为同日等权均值的说明 |
| `outputs` | `{relative_path: {sha256, rows, bytes}}`，含全部产物；`rows` 仅 parquet 有值 |
| `manifest_hash_note` | 自引用排除说明：`manifest.json` 不在自己的 `outputs` 中 |

`build_params` 当前值：

| key | 当前值 |
|---|---|
| `canonical_axis_rule` | dates with ≥ `canonical_min_tickers` `is_common` tickers present |
| `canonical_min_tickers` / `canonical_minimum_common_tickers` | 500 / 6,168 |
| `common_tickers_in_denominator` | 6,168 |
| `canonical_session_count` | 9,067 |
| `rank_batch_sessions` / `staging_tickers` | 40 / 50 |
| `parquet_compression` / `sample_partitioning` | snappy / `samples/year=YYYY/part-00000.parquet` |
| `derived_windows` | 60、120 canonical sessions（仅过去/当前数据） |
| `financial_asof` | `available_as_of ≤ signal date` 最新快照；YoY 匹配规则全文 |
| `purge_sessions` / `purge_semantics` | 30 / 同 §3.5 |
| `rows_by_split` | 同 §3.5 |

验货示例（consumer 按哈希校验；本机 6.3G 全量约 15s）：

```python
import hashlib, json
from pathlib import Path

root = Path("data/output")
manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
for relative, record in manifest["outputs"].items():
    actual = hashlib.sha256((root / relative).read_bytes()).hexdigest()
    assert actual == record["sha256"], relative
```

### 3.7 qc_report.{json,md}

| 字段 | 语义 |
|---|---|
| `rows_total` / `rows_per_year` | 行数 |
| `label_stats` | 每个 label 的 `all` / `clean`（剔除 flag 行）两套 `n/mean/std/p50/p99/exact_zero_fraction` |
| `missingness_per_feature` | 每个 raw/cs 特征的 `missing` 数与比例 |
| `cross_section_size_per_year` | 每年截面 `dates/min/median/max` |
| `purge` | `purge_sessions` / `semantics` / `rows_by_split` |
| `extreme_labels` | `flag_extreme_label` 计数、规则、最多 100 条样例 |
| `ticker_failures` | 构建期失败明细（当前为空） |

## 4. 基线数值与验货

| 指标 | 当前基线 |
|---|---|
| samples 行数 / 日期范围 / canonical sessions | 23,938,669 / 1990-01-02 – 2025-12-31 / 9,067 |
| 股票目录 / 有 market.csv / 无行情 | 7,662 / 6,534 / 1,128（权证/单位/空壳等源缺失） |
| SEC：有 companyfacts 的 ticker / 无数据（404） | 7,479 / 183 个 ticker（165 个唯一 CIK；基金、ETF、外国发行人常无 companyfacts；这些 ticker 仍会生成全 `missing` 的 financials.csv） |
| meta 行数 | 6,532 |
| flag_extreme_label | 120,510 |
| purge（fit/select/screen） | 120,987 / 139,158 / 182,398 |
| output 文件数 | 41（36 年分区 + meta + splits + manifest + qc×2） |

重建后若这些数字变化：先按 [AGENT.md](../../AGENT.md) 的不变式 5 判断是否由 purge/标签逻辑变更导致；只改财务特征的重建必须逐位不变。
