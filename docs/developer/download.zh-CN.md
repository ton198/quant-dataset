[English](download.md) | **简体中文**

# download.md — 下载与 organize 内部机制

> 适用：改 `src/download/`、`config/sources.toml`、`config/universes/*`、进度/锁、SEC 财务抽取。
> 关联：[architecture.zh-CN.md](architecture.zh-CN.md)（数据流与模块边界）、[data-contracts.zh-CN.md](data-contracts.zh-CN.md)（字段契约）、[known-quirks.zh-CN.md](known-quirks.zh-CN.md)（数据源现实）、[testing.zh-CN.md](testing.zh-CN.md)（验证）。
> 下文既有 download/organize 行为以 `src/download/*.py`、`src/cli/main.py`、`config/sources.toml` 为准；当前 `samples` 契约不改变这些行为。选定的财务事件/事实输出是独立的九概念结构化抽取，不是完整申报档案，也不是 samples 输入。有界 filing archive 是另一条已实现 workflow；其范围与当前证据见 [financial-filing-archive.zh-CN.md](financial-filing-archive.zh-CN.md)。

## 1. 数据流与入口

```text
CLI: quant-dataset download --stage {market|financials|macros|organize|all} [--start --end --tickers --force --force-rebuild --dry-run --data-dir --workers]
      └─ src/cli/main.py: main → run_download(repo_root=Path.cwd(), ...)
          └─ src/download/manager.py: run_download
               ├─ load_sources/load_secrets（config/sources.toml + config/secrets.toml）
               ├─ acquire_lock（data/.download_progress.lock）
               ├─ market    → raw/yahoo/<TICKER>/<start>_<end>.csv
               ├─ financials→ raw/sec/financials/<sha256>.json（companyfacts + submissions + 历史分页）
               ├─ macros    → raw/fred/<SERIES>/<sha256>.json
                └─ organize  → organized/stocks/<TICKER>/{market.csv,financials.csv,
                                  financial_events.parquet,financial_facts.parquet,_meta.json}
                              organized/shared/{macro.csv,_meta.json}
```

上图所示财务 organizer 输出是独立文件。当前 `samples` builder 只读取 organized 行情/宏观面板及 exclusions；不读取财务文件或财务 `_meta.json` 记录。

- `STAGES = ("market", "financials", "macros", "organize")`；CLI `--stage all` 展开为四者。
- `--data-dir`（默认 `data`）通过 `manager._with_data_dir` 重映射 `raw_dir/organized_dir/progress_file/progress_tmp_file/progress_lock_file`，相对路径按 `repo_root`（即 `Path.cwd()`）解析；`repo_root` 同时决定 `config/` 位置，因此命令必须在仓库根目录运行。
- `market` stage 强制要求 `--start/--end`（`run_download` 抛 `ConfigError`；CLI 层也会先报 parser error）。`--dry-run` 在加载配置后立即返回 0，不写盘、不发网络请求。
- `--force` 重置进度 worklist（重下载）；`--force-rebuild` 只影响 organize（跳过完成检查，重建已有产物）。两者独立。

## 2. 模块职责

| 模块 | 职责 | 关键符号 |
|---|---|---|
| `manager.py` | 编排 4 个 stage、进度持久化、进程锁、并行 organize、失败计数 | `run_download`、`STAGES`、`_organize_tickers`、`_ticker_is_organized`、`_with_data_dir` |
| `universe.py` | 抓取/缓存 SEC 交易所 universe；ticker→CIK override 的正反双向应用 | `fetch_universe`、`_rows`、`load_ticker_cik_overrides`、`apply_ticker_cik_mapping_overrides` |
| `market.py` | Yahoo 日线抓取（单 ticker 一个请求区间） | `fetch_market` |
| `financials.py` | 下载 companyfacts、submissions、历史 submissions 分页；内容寻址 + manifest | `fetch_financials`、`_fetch`、`_read_manifest`、`_manifest_mapping` |
| `macros.py` | 下载 FRED 观测序列；内容寻址 + manifest；逐系列进度 | `fetch_macros` |
| `organize.py` | 行情清洗/派生列、宏观 session 对齐、`_meta.json` 合并写 | `organize_market`、`organize_macros`、`_market_quality`、`write_meta` |
| `organize_financials.py` | SEC facts → 旧 session 快照，并在快照选择前生成申报事件 artifact | `organize_financials`、`_CONCEPTS`、`_submission_rows`、`_fact_for_period`、`_fiscal_identifiers` |
| `financial_events.py` | `financial_events_v1` 规范化申报事件与有限 fact 版本 Parquet 表 | `organize_artifact`、`write_tables`、`artifact_is_valid`、`write_empty_artifact` |
| `progress.py` | 进度原子写 + 按 PID 的排他锁 + stale 锁回收 | `Progress`、`initialize`、`save_atomic`、`acquire_lock` |
| `config.py` | 加载 `secrets.toml` / `sources.toml`，dataclass 化 | `load_secrets`、`load_sources`、`SourcesConfig` |
| `errors.py` | 领域异常 | `ConfigError`、`DownloadError`、`OrganizeError` |

## 3. 落盘布局

| 路径 | 写入者 | 命名/语义 |
|---|---|---|
| `data/raw/yahoo/<TICKER>/<start>_<end>.csv` | `market.fetch_market` | 区间文件名；同区间重下覆盖；不同区间叠加，organize 时按 date 去重（keep last） |
| `data/raw/sec/universe/<sha256>.json` | `universe.fetch_universe` | 文件名必须是内容 sha256，否则缓存失效重下 |
| `data/raw/sec/financials/<sha256>.json` + `manifest.json` | `financials.fetch_financials` | 所有 CIK 共用一个扁平内容寻址目录；manifest `{"resources": {logical_key: [record,...]}}` |
| `data/raw/fred/<SERIES>/<sha256>.json` + `manifest.json` | `macros.fetch_macros` | 同上，`logical_key = observations:<series>` |
| `data/organized/stocks/<TICKER>/market.csv` | `organize_market` | 日历内 session；含 `quality_flag` 与派生列 |
| `data/organized/stocks/<TICKER>/financials.csv` | `organize_financials` | 旧契约：每 session 最新整份申报快照一行；语义不变 |
| `data/organized/stocks/<TICKER>/financial_events.parquet` | `financial_events.organize_artifact` | 每个有效申报事件一行，包括没有财务 facts 的申报 |
| `data/organized/stocks/<TICKER>/financial_facts.parquet` | `financial_events.organize_artifact` | 按来源申报和实际期间/版本组织的有限 USD 概念 facts |
| `data/organized/stocks/<TICKER>/_meta.json` | `organize._write_meta` / `financial_events.commit_artifact_meta` | 登记旧表和事件/事实产物；嵌套 `financial_events` 契约、完整性、CIK/raw-input 身份、资源状态、质量/拒绝计数、日历、hash 与行数 |
| `data/organized/shared/macro.csv` | `organize_macros` | 宽表，session 对齐 + ffill |
| `data/.download_progress`（+ `.tmp`、`.lock`） | `progress.py` | 逐 item 状态；锁文件内容为 PID |

## 4. SEC 财务摄入：旧快照与申报事件 artifact

### 4.1 下载层：`fetch_financials(cik10, cfg, secrets, raw_dir)`

固定请求序列（`financials.py`）：

1. `companyfacts:{cik}` → `cfg.financials.company_facts_url_template`（默认 `https://data.sec.gov/api/xbrl/companyfacts/CIK{cik10}.json`）；
2. `submissions:{cik}` → `submissions_url_template`；
3. `submissions.get("filings", {}).get("files", [])` 中每个 `name` 形如 `CIK{cik10}-submissions-*.json` → `https://data.sec.gov/submissions/<name>`，`logical_key = submissions-page:{cik}:{name}`。

机制要点：

- `_fetch` 负责 UA（`secrets.sec_user_agent`）、`Accept: application/json`、`retries` 与指数退避（≤8s）、每请求节流 `rate_limit_seconds`。失败抛 `DownloadError`。
- `obtain` 先查 manifest：`(logical_key, url)` 命中且文件 sha256 一致 → 直接复用；否则下载、校验 JSON、写 `<sha256>.json`（已存在不重写）、追加 record（`fetched_at_utc`、`attempts`、`byte_size`、`status: done`）。
- manifest 用 `.json.tmp` + `replace` 原子落盘；`resources` 从 list 重排为 `{logical_key: [records]}`。
- 返回值包含缓存命中路径，所以**离线也能 organize**：raw 缓存完整时 financials stage 全 done，organize 只读本地。
- 一个 CIK 的文件可被多个 ticker 复用（共享 CIK 的类别股）；`fetch_financials` 以 CIK 为参数，ticker 只是 manager 的循环单位。

### 4.2 申报解析层：`_submission_rows(payload)` 统一三种形状

| 形状 | 判别 | 解析函数 | 备注 |
|---|---|---|---|
| `filings.recent`（当前 submissions 主文件） | `filings` 与 `filings.recent` 均为 dict（`_submission_rank` 的判定） | `_submission_rows` 内联展开 | 平行数组按 index zip；`_submission_rank=0` |
| `fields`/`data`（通用二维表） | 两键均为 list | 内联 dict 化 | 只取 fields 与行长度的交集（缺列省略、多余值忽略）；`_submission_rank=1` |
| 列式历史分页（`accessionNumber`/`filingDate` 等为平行数组） | `_is_column_oriented(payload)` | `_column_oriented_rows` | 任一列长度不一致（ragged）→ 整体拒绝返回 `[]`；`_submission_rank=2` |

- 解析顺序：`filings.recent` 与 `fields`/`data` 只要存在就都会展开并追加；仅当两者都没有产出任何行时才 fallback 到列式分页。多形状并存时的优先级由调用方按 `_submission_rank` 去重决定：`recent` 永远赢。
- 去重（`organize_financials` 内）：`submission_payloads` 先按 `_submission_rank` 稳定排序，再逐行按 `accessionNumber` 去重——`recent` 永远赢过分页；同 rank 内按 `_owned_paths` 的文件名（内容 hash）顺序，确定性可复现。无 accession 的行不去重、全部保留。
- `_owned_paths(root, cik10)`：优先读 `manifest.json` 中 `logical_key` 以 `cik10` 结尾或包含 `:{cik10}:` 的记录；manifest 缺失/为空才退化为“文件名包含 cik10 的 `*.json`”。另有一道 payload 级校验：`payload["cik"]` 存在时其数值必须等于目标 CIK（去前导零比较），防止错误 payload 混入。

### 4.3 概念抽取层：`_CONCEPTS` 白名单 + `_fact_for_period`

`_CONCEPTS: dict[输出列, tuple[tag,...]]`，按优先级从新到旧；每个输出列取**第一个**能匹配到事实的 tag。全部键都会成为 `financials.csv` 的列。

| 输出列 | 优先级序列 |
|---|---|
| `revenue`（13 个） | `Revenues` → `RevenueFromContractWithCustomerExcludingAssessedTax` → `RevenueFromContractWithCustomerIncludingAssessedTax` → `SalesRevenueNet` → `SalesRevenueGoodsNet` → `SalesRevenueServicesNet` → `SalesRevenueGoodsGross` → `SalesRevenueServicesGross` → `RevenuesNetOfInterestExpense` → `FinancialServicesRevenue` → `InsuranceServicesRevenue` → `RevenueNotFromContractWithCustomer` → `SalesRevenueOilGas` |
| `net_income`（4 个） | `NetIncomeLoss` → `ProfitLoss` → `NetIncomeLossAvailableToCommonStockholdersBasic` → `NetIncomeLossAvailableToCommonStockholdersDiluted` |
| `operating_income` | `OperatingIncomeLoss` |
| `assets` | `Assets` → `AssetsNet` |
| 其余 | `gross_profit`=`GrossProfit`；`operating_cash_flow`=`NetCashProvidedByUsedInOperatingActivities`；`capital_expenditure`=`PaymentsToAcquirePropertyPlantAndEquipment`；`liabilities`=`Liabilities`；`equity`=`StockholdersEquity` |

`_build_fact_index(fact_payloads)` 构建两个索引（一次 O(facts)，供后续 O(1) 查询）：

- `by_tag[tag][(filed, end)] → [fact,...]`：遍历顺序为 payload 顺序 → tag 排序 → unit 排序 → 数组顺序，确定性；
- `by_filing[(filed, end)] → [带 fy/fp 的 fact,...]`：只给 `_fiscal_identifiers` 兜底用。

`_fact_for_period(fact_index, tag, filed, fy, fp, end, form, accession_number)` 的筛选与偏好：

1. 只在 `(filed, end)` 与申报行完全相同的桶里找（`end` 为申报的 `reportDate`）；
2. 申报行 fy/fp 非空时要求 fact 的 fy/fp 相等；fact 的 form 非空时去 `/A` 后需与申报 form 相等；accession 非空时 `accn` 相等；`val` 必须可转 float；
3. **时长过滤防 YTD 当单季**：`10-Q` 且 fact 有 `start` 时仅保留 70–125 天；`10-K` 仅保留 300–400 天；instant fact（无 start，如 Assets）不受限；
4. 偏好取 min：10-Q 选时长最接近 91 天的；10-K 选最接近 365 天的；instant fact 排最前；并列按 `start` 字符串。10-Q 的 YTD fact（如 180+ 天）与过短区间（<70 天）都会在第 3 步被直接排除，不会进入偏好排序。

`_fiscal_identifiers(filing, fiscal_index, reported_facts, all_filings)` 的判定顺序：

1. 本次选中的 facts（`reported_facts`）里任一带 `fy`+`fp` → 直接用；
2. `fiscal_index[(filed, reportDate)]` 中 `accn` 匹配的 fact 的 fy/fp；
3. 申报行自身的 `fy`/`fp`（部分旧 submissions 才带）；
4. `10-K` → `(reportDate.year, "FY")`；
5. `10-Q` → 找**最近一个更早的 10-K** 作锚：fiscal_year 取该 10-K 的 fy（无则报告年），季度号 = 锚之后到本报告期末之间 distinct 10-Q `reportDate` 的排序位次（1–3）→ `(annual_year+1, Qn)`（Apple 类非日历财年因此不会被标成日历年季度）；
6. 其余（含无法定位的 10-Q）→ `(None, None)`，宁可留空不误标。

### 4.4 旧 daily CSV 的 as-of session 对齐（`organize_financials` 主循环）

- `report_rows` 按 `(available_at, accession_number)` 排序，`available_at = filingDate`；`report_filed_dates` 与之平行。
- 对排序后的 calendar 做**双指针前缀扫描**：`while report_filed_dates[visible_count] < session: visible_count += 1`，然后取 `report_rows[visible_count-1]`。因此申报在**申报日的下一个 session** 才可见（严格 `<`）：申报日当天不使用该信息，保守对齐披露时点。
- 输出行 = 最新可见申报的整行值（含 `form`、`accession_number`、`fiscal_year`、`fiscal_period`、`report_period_end`、全部概念列、`days_since_filing`）。**非财报申报（8-K/Form 4/424B* 等）也会成为“最新可见行”**，其概念列全空——即 values 不跨申报向后传递，详见 [known-quirks.zh-CN.md](known-quirks.zh-CN.md)。
- `days_since_filing = (session - 该申报 filingDate).days`（自然日，非 session 数）。
- 修正申报：`is_amendment = form.endswith("/A")`；`quality_status` 判定 = 若存在更早的、同 `report_period_end` 的原始申报 → 有任一概念值取 `"ok"`，否则 `"missing"`；不存在原始申报 → `"amendment_only"`；非修正申报 → 有值 `"ok"` / 无值 `"missing"`。修正申报不会让更早的原始申报“复活”。
- 输出列固定顺序：`date, available_as_of, accession_number, form, is_amendment, fiscal_year(Int64), fiscal_period, report_period_end, days_since_filing, <_CONCEPTS 键顺序>, quality_status`；日期以 `%Y-%m-%d` 写出。
- `_write_meta` 以 path 为键合并 inputs；当本次没有任何 owned 输入时，旧的 `sec/financials/` inputs 会被剔除，避免 provenance 指向已不存在的 raw 文件（test 覆盖）。

### 4.5 既有 organized event/fact 抽取（`financial_events_v1`；非完整 archive）

`organize_financials` 在选择旧 daily snapshot **之前**，从选中的 raw SEC submissions 和 Company Facts 构建既有 `financial_events_v1` artifact。旧 `financials.csv` schema 与整行覆盖行为保持不变；artifact 与其共存，绝不从 CSV 逆向构造。其 facts 仅限既有九概念白名单；它不是完整 XBRL 或申报全文档案。`samples` 不读取该 artifact 或财务 metadata。

| 表 | 粒度与稳定字段 |
|---|---|
| `financial_events.parquet` | 每个有效申报事件一行，包括无财务 facts 的非财务申报。固定字段：`event_id`、`asset_id`、`cik10`、`accession_number`、`filed_date`、`effective_visible_session`、`form`、`is_amendment`、`report_period_end`、`fiscal_year`、`fiscal_period`、`fiscal_key_source`、`quality_status`、`source_submission_path`、`source_submission_sha256`、`source_submission_locator`。 |
| `financial_facts.parquet` | 每个来源申报、概念及实际期间/版本一条规范化候选。固定字段：`fact_version_id`、`event_id`、`concept`、float64 `value`、`unit`、`taxonomy`、`tag`、`period_kind`、`period_start`、`report_period_end`、`duration_days`、`fiscal_year`、`fiscal_period`、`fiscal_key_source`、`filed_date`、`effective_visible_session`、`fact_accession_number`、`match_method`、`source_fact_path`、`source_fact_sha256`、`source_fact_locator`。 |

两表使用固定 Arrow schema，即使为空表也如此。有 accession 时 event ID 用规范化、十位补零的 CIK + accession；无 accession 时用来源 submission hash + 记录 locator 构造稳定 ID，并记录 accession 缺失诊断；其计数器 key 不属于稳定契约。Fact 必须关联唯一事件。事实 accession 精确匹配；fact 无 accession 时，只允许唯一的 `(filed_date, report_period_end, base form)` 事件匹配。匹配歧义/失败会拒绝；同一 event/tag/unit/实际期间的冲突值也拒绝，不能用下载时间排列披露版本。

抽取遍历现有九概念 `_CONCEPTS` 白名单。数值转换与有限值筛选在 **tag 优先级选择之前**，因此高优先级 NaN/inf 不会遮蔽低优先级有限 fact。不推断或换算币种；仅保留 USD。artifact 保留 tag、taxonomy、unit、来源 locator 和期间元数据。`assets`、`liabilities`、`equity` 必须是 instant。期间起止可验证的流量，只有 10-Q 且时长 70–125 天才标 `quarter`，10-K 且时长 300–400 天才标 `annual`（首尾日期均计入）；YTD/其他 duration 留作 `unknown`，不能进入增长/流量比率 cohort。缺 start 的流量可作为有限 `unknown` 披露保留。Fiscal key 冲突、来源匹配歧义、非 USD、非数值/非有限值、期间区间无效、taxonomy 不支持及拒绝申报均计数。

`effective_visible_session` 在 XNYS 日历上映射为严格晚于 `filed_date` 的首个 session；申报日不可见。映射失败会计数并阻止 artifact complete。输出日历范围与扩展的生效日映射日历分开记录。同一 session 生效的全部事件必须在生成该 session 特征前一起更新。原申报/amendment 保持独立事件/版本，从自己的生效 session 起更新；空 amendment 不删除旧 facts。`quality_status` 是元数据，不过滤有效有限 facts。

`_meta.json.financial_events` 记录 `contract_version="financial_events_v1"`、`complete`、`status`、`empty_reason`、`cik10`、`input_hashes`、`raw_input_inventory`、`raw_input_inventory_sha256`、`input_resource_status`、`input_resource_diagnostics`、`calendar`、`output_calendar`、`quality_counts`、`rejection_counts` 与 `events`/`facts` 的 path、sha256、行数。顶层 `_meta.json` 也登记 `cik10`、`raw_input_inventory_sha256`、`row_counts.financial_events`/`financial_facts` 和两张 Parquet 的 outputs。

`input_resource_status` 必须包含 `manifest`、`submissions`、`companyfacts`。`missing`、`unreadable`、`malformed`、`unknown` 等硬失败会使 artifact 不完整；确认的 no-CIK/no-input 状态会明确记录，并可生成 schema 正确的空表。事件/事实文件原子写入，完成 metadata 最后提交。完成身份绑定 CIK 与 raw SEC inventory fingerprint；raw 输入变化需重新 organize。`rejection_counts` 提供 raw resource/payload 有效性、来源/日期/accession 匹配、币种/数值/有限值检查、期间与 fiscal-key 冲突、生效 session 映射等诊断。具体计数器名称和聚合方式属于实现诊断，不是稳定契约；消费者不得依赖单个 key。

## 5. ticker→CIK override 与缓存

- 文件：`config/universes/ticker_cik_overrides.json`，`{"schema": ..., "notes": {...}, "overrides": {"XOM": "0000034088"}}`。ticker 统一 upper，CIK 补零到 10 位；非法条目跳过。
- 前向：`universe._rows` 解析完 snapshot 后按 override 替换 `TickerRow.cik10`（`fetch_universe` 的缓存命中路径同样经过 `_rows`，所以 cache 与网络一致）。
- 反向：`organize_financials._ticker_mappings(raw_dir)` 扫描 `raw/sec/universe/*.json` 建 `ticker→cik` 与 `cik→ticker`（都 `setdefault`：同 CIK 多 ticker 时反向只保留先遇到的一个），再调用 `apply_ticker_cik_mapping_overrides` 同步修正双向映射（替换旧 CIK 时仅当旧 CIK 确实反向指向该 ticker 才删除）。结果按 `raw_dir.resolve()` 缓存在模块级 `_TICKER_CACHE`；测试用 `_reset_ticker_cache(raw_dir)` 定向清除。
- 缺失 override 文件 = 无 overrides，行为与加入该机制前完全一致（test 覆盖）。
- `organize_financials(..., output_ticker=ticker)` 由 manager 显式传入，保证共享 CIK 的每只 ticker 写入自己的目录（`test_shared_cik_tickers_write_independent_outputs_in_parallel`）；`_ticker_for_cik` 只服务不传 `output_ticker` 的旧调用。
- 改 override 后必须 `--force-rebuild --stage organize` 重建相关 ticker；XOM 的 CIK 由 2025 持股公司 `0002115436` 纠正为历史申报主体 `0000034088`，旧 CIK 的 raw 缓存不会被新 CIK 使用（需重新 financials stage 下载）。

## 6. 进度与锁（`progress.py`）

| 结构 | 内容 |
|---|---|
| `Progress` | `run_id`、`started_at_utc`、`universe_source`、`stages: {stage: {item: status}}` |
| status | `pending` / `done` / `failed:<reason>` |
| `initialize(stages, source, force, ...)` | `force=True` 或文件不存在 → 全新 worklist；否则保留已有状态、只为新 item 补 `pending`，并在 `universe_source` 非空时刷新 |
| `pending(state, stage)` | 返回所有非 `done`（失败项会被重试） |
| `save_atomic` | 写 `.tmp` + `flush`/`fsync` + `os.rename` + 目录 `fsync`（失败仅告警） |

- 锁：`acquire_lock` 用 `O_CREAT|O_EXCL` 创建 `.lock` 并立即写入 PID。获取失败时 `_reclaim_stale_lock` 判定：空文件且 mtime < 0.05s 视为“正在写入”，保留；PID 缺失/非法且过了窗口视为 stale；PID 存活（`os.kill(pid,0)`，无权限按存活处理）→ 报 `Another download run holds lock`。删除前用 inode 身份比对（`_unlink_if_same_inode`），避免误删他人的锁；释放时也只在 inode+PID 仍指向自己时 unlink。
- `run_download` 在整个 run（含 universe 抓取与四个 stage）持有该锁；**organize 的 ProcessPool 子进程不做下载**，只读 raw。

## 7. 并行 organize（`manager._organize_tickers`）

- 完成判定 `_ticker_is_organized`：`_meta.json` 可解析、`ticker` 匹配、所有 outputs path 存在；有 CIK 的 ticker 必须同时有旧 `financials.csv` 和有效 `financial_events_v1` artifact。`artifact_is_valid` 校验 `complete`、契约版本、预期 CIK、raw-input inventory fingerprint、fail-closed 资源状态、两张 Parquet 的 hash/行数/schema，以及 event/fact 关系。旧 meta 或仅有 CSV 都不能跳过新 artifact 重建。
- worker（`_organize_ticker_worker`）先 `unlink` 旧 `_meta.json`（防中间态被误判完成），再依次 `organize_market`（无 Yahoo CSV 时仅 log、返回 None）与 `organize_financials`（有 CIK 时）；两个操作的异常分别收集，互不阻塞其他 ticker。
- `ProcessPoolExecutor(max_workers=workers, initializer=_initialize_organize_worker, initargs=(session_calendar,))`：session 日历只在每个进程初始化时复制一次；worker 内不再构建 `exchange_calendars`。
- 返回并记录 `(succeeded, skipped, failed)`；`failed>0` 不中断其他 ticker，run 最终 exit code 为 1。
- organize 的 calendar：`start or 1990-01-01` 至 `end or today`；organize-only 且未给 ticker 时，selected tickers 退化为 `raw/yahoo/` 下的目录（`_raw_tickers`）；只给 tickers 时逐个组织，未出现在 universe 的 ticker 只会尝试 market（CIK 为 None）。

## 8. 行情与宏观 organize

`organize_market(ticker, raw_dir, organized_dir, calendar)`：

- 读取 `raw/yahoo/<TICKER>/*.csv` 全量 concat；列名 casefold + 空格→下划线；支持 `date`/`datetime`、`adj_close`/`adjclose`；缺 `open/high/low/close/volume` 抛 `OrganizeError`（adj_close 缺失时回退 close）。
- `date` 解析失败行丢弃 → 按 date 排序、同日 keep last → 只保留 calendar 内日期（记录 `market_dropped_non_session`）。
- `quality_flag`（`_market_quality`）：任一价格 <0 → `negative_price`；`close<=0`、`volume<0`、`high<max(open,close,low)`、`low>min(open,close,high)`、非有限值 → `invalid_ohlc`；否则 `ok`（close==0 归 `invalid_ohlc`，不是 negative_price）。
- 派生列：`adjustment_factor = adj_close/close`；`return_1d/5d/20d` = close 的 pct_change；`volatility_20` = return_1d 的 rolling(20).std(ddof=1)；`volume_ratio_20 = volume / rolling(20).mean()`；`intraday_range = (high-low)/close`。
- 输出 `market.csv` 列顺序见 `organize_market` 内的 `columns` 列表；`_meta.json` 记 inputs/outputs sha256 与 row_counts。

`organize_macros(raw_dir, organized_dir, calendar)`：

- 遍历 `raw/fred/*/*.json`，以目录名作列名；每条 observation：`approximate_release = 参考月 + 1 个月的同日`（按月末截断）→ 首个**严格晚于**该日的 session 才可见；没有可见 session 的观测丢弃。
- 宽表按 session 排序后逐列 `reindex + ffill`。`_meta.json.known_issues` 固定声明：FRED 响应不含 release 时间戳，可见性用「参考期 + 1 月 + 1 个 session」近似；数值是 latest revised，非 vintage。

## 9. 扩展点

| 想改什么 | 动哪里 | 必须同步 |
|---|---|---|
| 在独立 organized 抽取中新增财务概念 | `organize_financials._CONCEPTS`（输出列 → 有序 tag 元组） | `tests/test_organize_financials.py::test_financial_concept_priority_whitelist_includes_old_and_new_us_gaap_tags` + 一个 fixture fact。`samples` 不消费财务数据；新增财务样本特征须另行决定契约，不是本 organizer 改动的一部分。 |
| 新增数据源 stage | `manager.STAGES` + `run_download` 内新 block + `progress_stages`；CLI `--stage` choices | 新 stage 的失败计数与进度状态；[../user/cli.zh-CN.md](../user/cli.zh-CN.md)（不变式 8） |
| 新增 provider 配置 | `config.py` 的 dataclass + `load_sources` 字段 + `config/sources.toml` | `tests/test_download.py` 的 config 测试 |
| 新增 ticker→CIK 纠正 | `config/universes/ticker_cik_overrides.json` 的 `overrides` | `tests/test_ticker_overrides.py`；重下 financials + `--force-rebuild --stage organize` |
| 排除 ticker | `config/universes/exclusions_v1.json`（`asset_id` 去重、必须大写） | 样本层 `_load_exclusions` 会校验重复；重建 samples 后比对 manifest `exclusions_applied` |

## 10. 既有 organizer 限制（不是 samples 覆盖或发布指标）

- **银行概念仍不在选定白名单内**：`InterestAndDividendIncomeOperating` 等银行利息/手续费概念不属于现有九概念抽取。event/fact artifact 不会生成 `_CONCEPTS` 之外的 facts。由于财务不是样本输入，当前没有 samples 银行覆盖率实测。
- **选定白名单存在发行人/概念缺口**：例如 XOM 的选定来源不含 `OperatingIncomeLoss`；后续申报中的 comparative facts 不会反向归入旧 event 行。这是既有 organizer 限制，不是 samples 字段或覆盖率声明；见 [known-quirks.zh-CN.md](known-quirks.zh-CN.md)。
- **SEC 资源缺失或确认空输入**：既有 organizer 会区分确认无输入与资源缺失/损坏、映射不完整。这些状态只适用于独立 organized 财务 artifact；samples builder 不检查它们。
- **Yahoo 无行情**：没有 `market.csv` 的 ticker 无法生成行情样本行。本文不声明当前样本 ticker 数量实测。
- **申报历史较大**：event 体量因发行人而异。数量和 organize 资源用量应从明确标识的运行中实测；本文不声明当前样本财务输入或运行时测量。
- **宏观序列覆盖**：FRED 历史长度各异。诊断摄入问题前，核对对应 raw resource status 与 manifest；本文不声明当前样本缺失率实测。
- **输入历史边界**：既有 SEC cache 不是完整历史 vintage 档案，且没有归档 filing HTML/iXBRL 集合；raw facts 缺失、仅存在于后续 comparative period 的事实、非 USD 事实、来源匹配歧义和白名单外概念仍不可用。

## 11. 验证方法

```bash
# 1) 离线单测（不触网）
PYTHONPATH=src .venv/bin/python -m pytest tests/test_download.py tests/test_organize_financials.py \
  tests/test_organize_parallel.py tests/test_progress_lock.py tests/test_ticker_overrides.py -q

# 2) 只规划、不落盘
PYTHONPATH=src .venv/bin/python -m cli.main download --stage all --start 1990-01-01 --end 2025-12-31 --dry-run

# 3) 既有 organizer smoke（仅在 organizer 任务指定时；不是样本构建步骤）
# 复用 raw cache；不抓取 market/financials/macros。
PYTHONPATH=src .venv/bin/python -m cli.main download --stage organize --force-rebuild \
  --tickers AMZN,JPM,AAPL,XOM --start 1990-01-01 --end 2025-12-31
# 检查既有 financial_events_v1 metadata/hash 与 event/fact provenance。
# AMZN filed_date 2025-08-01 在 08-01 不可见；旧 organizer 按下一 session 可见。
# financials.csv 保持每日整份快照语义；两类 organizer 输出均非 samples 输入。

# 4) 任选 ticker 核对 provenance / 内容寻址
python -c "import json;m=json.load(open('data/organized/stocks/AAPL/_meta.json'));print([i['path'] for i in m['inputs']])"
# raw 文件名 = sha256；organize 后可用 _meta.json 的 inputs 对照 manifest.json 复核
```

- 改下载/进度/锁后跑 `tests/test_download.py tests/test_progress_lock.py`；改抽取后跑 `tests/test_organize_financials.py` 加 smoke；改 override 后跑 `tests/test_ticker_overrides.py` 再看 XOM 产物。
- 依赖顺序：financials stage 依赖 universe（有 CIK）；organize 依赖 raw 缓存（market 可缺，financials 可缺）。离线重建时不要带 `market`/`financials` stage。
