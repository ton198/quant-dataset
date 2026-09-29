# known-quirks.md — “看着像 bug 其实不是”手册

> 用途：数据/产物“看着不对”时先查本表；确认不是已知现实后再按 [AGENT.md](../../AGENT.md) §1 路由到代码。
> 关联：[download.md](download.md)（机制）、[samples.md](samples.md)（特征/标签）、[data-contracts.md](data-contracts.md)（契约与基线）、[../user/data-format.md](../user/data-format.md)（消费者视角）。
> 表内“实测”数字来自当前仓库产物（`data/organized`、`data/output`、`logs/download_full.log`）。

## 1. 条目

| # | 现象 | 根因 | 是否可修 | 应对 |
|---|---|---|---|---|
| Q1 | 某 ticker 的 `financials.csv` 里财务值突然变 NaN 一段时间，之后才恢复 | **快照语义，无跨申报 carry-forward**：每个 session 取“最新 filed 的申报行”，8-K/Form 4/424B*/13G 等非财报申报成为最新行时概念列全空。实测 AAPL 2009-07-28 10-Q 后 2009-07-29 一条 Form 4 使 revenue 立即为 NaN，直到下一次 10-Q/10-K；XOM 末尾被 SCHEDULE 13G/FWP 覆盖 | 设计如此（保守的 as-of 披露口径，避免把非财报申报当成“确认”）；若要“跨非财报申报沿用最近财务值”，需改 `organize_financials` 的选择逻辑并重定义契约 | 用 `miss_*`（样本层）或 `form/quality_status` 判断；不要把它当数据缺失 bug。改逻辑前先看 [data-contracts.md](data-contracts.md) |
| Q2 | XOM 在 2012 年之前 `revenue` 全空；FY2009/10 的营收只出现在 FY2011 10-K 里 | XOM 早年未按 us-gaap 营收 tag 申报；FY2009/10 数值只作为 FY2011 10-K 的**比较期事实**存在（`filed=2012-02-24, fy=2011, end=2009-12-31/2010-12-31`）。as-of 抽取要求 `(filed, end)` 命中申报自身 reportDate，比较期 end 不等于该 10-K 的 reportDate | 可修（例如按 `fy` 归位比较期），但会改变 fiscal 语义与大量历史行，须走契约变更 | 已知边界：XOM `revenue` 首个可见行 = 2012-02-27（FY2011）；首值回溯检查以此为基线 |
| Q3 | 银行 `revenue`/`operating_income` 几乎全空 | 白名单没有银行口径 tag（如 `InterestAndDividendIncomeOperating` 等）。实测 JPM 全历史只有 4 个 session 的 `revenue`（2010–2011 年旧 `Revenues` fact），样本层 `f_raw_revenue_yoy` 仅 1 行非空，`operating_income` 全空 | 可修：向 `_CONCEPTS.revenue` 增补银行 tag（先确认口径一致性），按 [download.md](download.md) §9 走白名单+测试流程 | 训练时按 missing/`miss_*` 处理；JPM 类银行不要按“数据坏了”排查 |
| Q4 | JPM 的 `financials_input` 高达 167,492 条，organize 明显更慢 | JPM 常年发行结构性票据：实测 77% 申报是 424B2（129,203 条），加 FWP/424B3/424B8 等招股文件约 95%。这是 issuer shelf 的真实申报量，不是重复解析 bug | 不修（解析本身按 accession 去重，逻辑正确）；如要提速可在 `organize_financials` 里对无关 form 做预过滤并同步契约 | 把大申报量当容量规划输入；`(filed,end)` 索引 + 双指针就是为这种 CIK 设计的 |
| Q5 | 宏观特征 2000 年前大量缺失；`BAMLH0A0HYM2` 只有 2023 年之后的值 | 序列本身历史长短不同（DEXUSEU 1999 年起）；`BAMLH0A0HYM2` 当前 raw 响应仅 **794 条观测（2023-09-25 起）**，organize 后 732 个非空 session | 数据源现实，非代码；可重跑 macros 并核对 FRED 返回条数（`data/raw/fred/BAMLH0A0HYM2/*.json` 的 `count`） | 用 `miss_m_*`；不要据此判断“下载失败”。若重下仍少，记录到本表 |
| Q6 | 宏观值“提前”出现，且与当期发布值可能不一致 | FRED 是 **latest revised（非 vintage）**；`organize_macros` 用“参考期 + 1 个月 + 1 个 session”近似 release 日（FRED 响应不含 release 时间戳），日期不精确 | 不修（无法从现有 API 拿 release）；如需精确需引入 vintage/ALFRED 流程 | 把宏观当"约一月后可见"；不要做发布日事件研究 |
| Q7 | 1,128 只 ticker 只有 `financials.csv` 没有 `market.csv` | Yahoo 无行情：多为 SPAC 单位/权证/优先股/空壳（155 只带 `-UN/-WT/-P` 等后缀；973 只符号不含连字符，如 `AACI/AACIU/AACIW` 类）；`fetch_market` 空响应返回 `None`，organize 只写 financials | 不修（证券在 Yahoo 无数据/已退市） | 这些 ticker 不进 samples（`build_samples` 只遍历有 market.csv 的 ticker）；用 `manifest.input_inventory.tickers_without_market_csv` 查名单 |
| Q8 | 183 只 ticker 的 `financials.csv` 全部 `quality_status=missing`，且 SEC 请求 404 | Company Facts 端点对这些 CIK 返回 404（实测日志 183 unique、279 次失败重试）。主体是基金/ETF/信托/外国发行人/结构化产品；少数美国运营公司也在列（如 OZK、PBT），可能与端点侧或历史 XBRL 缺失有关。**不是 CIK 错配**（错配是 XOM，已用 override 修复） | 不修代码；可择期重试 download（`--force`）看端点是否恢复 | 不要把 all-missing 当抽取 bug；对比 `logs/download_full.log` 与 `raw/sec/financials/manifest.json` 的 companyfacts 覆盖数（当前 5,883 CIK） |
| Q9 | 同一发行人的多只 ticker（类别股/权证）财务值完全相同；反向 `cik→ticker` 只认一个 | universe 按 ticker 列出、CIK 按发行人；`_ticker_mappings` 对 `cik_to_ticker` 用 `setdefault`。实测 7,662 个 organized ticker 属于 892 个多 ticker CIK；若省略 `output_ticker`，1,625 只会被写进兄弟 ticker 的目录（如 `ABR-PD/ABR-PE`、CIK 0000019617 下的 JPM/JPM-PC…/AMJB/VYLD）。无 companyfacts 的 183 只中有 14 个多 ticker CIK（18 个别名，如 BIOT/BIOTW、ECF/ECF-PA） | 不修，保留每 ticker 独立产物（manager 始终传 `output_ticker`） | 不要依赖 `_ticker_for_cik` 的唯一性；核对产物以 `organized/stocks/<TICKER>/` 目录为准 |
| Q10 | 回测表现偏乐观、早期年份票池偏小 | 幸存者偏差：universe 是**当前** SEC 名单，没有退市 ticker；1990 年代截面仍会随下载年份变化 | 设计限制，需引入历史 universe 快照才能修 | 训练/回测时显式知情；见 `manifest.known_biases` |
| Q11 | 364 只样本 `is_common=False`；判定边界与直觉不一致（如 `HTHIF/HTHIY` 这类无连字符的外国发行人 ADR 会被判 True） | `_is_common` 是**后缀启发式**（`-UN/-WT/-P/-R/-U/WS/RT` 及以这些字母开头/结尾的后缀），不是 security master；无连字符的符号一律 True | 可修：接证券主数据；当前不修 | Rank/`excess_*` 目前只用 `is_common` 过滤基准；如需严格口径自行加白/黑名单 |
| Q12 | `excess_*` 基准收益与直觉不符 | 无 SPY：`excess_5d/21d` 减的是**同日 `is_common=True` 且 `flag_extreme_label=0` 的等权目标均值**，且 `is_common=False` 的行也参与（其 excess 相对 common 均值） | 设计如此（等权宇宙基准） | 需要市值加权/SPY 基准时在训练层自行重构，不要改样本层 |
| Q13 | 标签收益看起来不可实现（复权跳变、无法成交价） | `adj_open = open × adj_close / close` 是复权价；标签是 `t+1` 开盘到 `t+1+h` 开盘的理想化收益 | 不修（契约写明）；可加滑点/可成交性过滤在训练层 | 不要用标签直接模拟成交；解读 `manifest.label_semantics` |
| Q14 | `flag_extreme_label=1` 的行还在样本里 | 规则：标签窗口跨过相邻 session `adjusted_open` 比率越出 `[0.5, 2.0]`（未复权拆股等价格跳变）→ 置 1；**保留不删**，但 `excess_*` 均值排除，训练层应降权/剔除 | 可修但不应删：删除会改变行数/切分基线 | 训练时 `flag_extreme_label==1` 行降权；当前 120,510 行 |
| Q15 | `days_since_filing` 有约 12% 缺失 | 两个来源：首次申报前的 session 无 filing；organize 早期无申报可见的行。实测缺失率 12.4%（覆盖 87.6%），与 `miss_days_since_filing` 对应 | 设计如此 | 用 `miss_days_since_filing` 标识；不要把它当 filing 频率特征 |
| Q16 | `quality_status=missing/amendment_only` 被误读成文件损坏 | `missing`=最新可见申报没有任何白名单概念值；`amendment_only`=只有修正申报、没有更早的同 report_period_end 原始申报 | 设计如此 | 按语义处理：`amendment_only` 谨慎使用该行财务值；两者都可用 `form/accession_number` 追溯 |
| Q17 | `config/sources.toml` 的 `preserve_progress_on_success` 改了没效果 | `config.py` 解析进 `SourcesConfig`，但 `manager.run_download`/`progress.py` 从未使用；进度文件本来就总是保留（TOML 注释亦如此说明） | 可修：实现该开关或删除配置项 | 不要用它控制重跑；用 `--force`（重置进度）/`--force-rebuild`（重建产物） |
| Q18 | 同一区间 `--stage market` 重跑会覆盖 Yahoo CSV；不同区间会叠加 | `fetch_market` 文件名是 `<start>_<end>.csv`；`organize_market` concat 后按 date `keep="last"` | 设计如此 | raw 层不可手改；需要精确重算时用相同区间覆盖或清对应目录后再下 |
| Q19 | 某 ticker 的 `f_raw_*_yoy` 突然整年缺失、之后又恢复 | `_financial_features` 的 fiscal 主路径把 `(fiscal_year, fiscal_period)` 映射到“最近一次出现的快照”。若该快照带 fiscal 标识但四个财务值全缺（白名单未命中/空修正申报），下一年同键 YoY 的 prior 会指向这个空快照，**不会回退到更早的有值快照** | 可修：prior 改为“最近一个有值快照”或在同键内跳过全空快照；会改变历史特征值，须走契约变更并同步基线 | 先用 `miss_*` 与 `qc_report.json` 观察影响；若确需修复，按 [AGENT.md](../../AGENT.md) §1 走特征变更流程 |

## 2. 历史修复记录（防止回退）

以下问题都已修复并被测试守护；若再次出现类似症状，先跑对应测试确认是否回退。

| 问题 | 当时症状 | 修复 | 守护测试 | 提交/时间 |
|---|---|---|---|---|
| purge 泄漏 | 边界前一行的标签窗口跨入下一个 split，评估集被训练集未来信息污染 | 构建期对三个 split 边界前 31 个 signal session 禁运（`_purge_windows`），写 `splits.json` 审计字段 | `tests/test_build_samples.py::test_build_purges_label_windows_at_split_boundaries` | 48651b8 / 2026-09-29 |
| YoY 期间漂移匹配 | 按“上年同期日期”匹配会把 Q1 对到 Q2（非日历财年最明显） | 优先用 `fiscal_year/fiscal_period` 同键匹配；无标识才回退 ±15 天报告期末（取最近、平手取早） | `test_fiscal_identifiers_take_precedence_over_closer_report_end`、`test_financial_yoy_falls_back_to_nearest_prior_report_period` | 48651b8 / 2026-09-29 |
| 非有限 raw 特征（PCG 等 inf） | `pct_change`/异常输入产生的 ±inf 被写进样本，污染截面统计 | raw 特征非有限值统一转 NaN，并生成 `miss_*`（uint8）；qc 单独统计 | `test_infinite_raw_returns_are_missing_with_missing_indicator`、`test_outputs_use_date32_and_float32` | 48651b8 / 2026-09-29 |
| 营收概念白名单缺失 | `SalesRevenueNet` 等旧/窄 tag 未在名单，早年或特定行业 revenue 大面积空 | 扩展 `_CONCEPTS.revenue`（13 个，含 `SalesRevenueNet`/`SalesRevenueGoodsNet`/`SalesRevenueServicesNet` 等）并写明文档顺序 | `test_financial_concept_priority_whitelist_includes_old_and_new_us_gaap_tags`、`test_company_facts_priority_uses_new_and_legacy_concepts_and_emits_periods` | 48651b8 / 2026-09-29 |
| XOM CIK 错配 | universe snapshot 把 XOM 指到 2025 持股公司 CIK `0002115436`，拿不到历史财务 | `config/universes/ticker_cik_overrides.json` 纠正为历史申报主体 `0000034088`；正反双向应用、缺文件零影响 | `tests/test_ticker_overrides.py`（前向/反向/缺文件/force-rebuild） | 48651b8 / 2026-09-29 |
| 历史申报分页未解析 | 申报史被截断在 `filings.recent`（近 1,000 条），老 10-K/10-Q 不可见 | `_submission_rows` 支持列式 submissions-page；`_is_column_oriented` 判形、ragged 拒绝；按 accession 去重且 recent 优先 | `test_submission_rows_parses_recent_table_and_column_page_shapes`、`test_column_oriented_payload_rejects_ragged_arrays`、`test_page_only_old_filing_fact_becomes_available_at_its_filing_date`、`test_overlapping_page_rows_dedupe_prefers_recent_submissions` | 48651b8 / 2026-09-29 |
| 共享 CIK 输出互相覆盖 | 类别股 ticker（如 ABR-PD/ABR-PE）写同一份 financials.csv，后者覆盖前者 | manager 传 `output_ticker`，每只 ticker 独立目录/独立 `_meta.json` | `test_shared_cik_tickers_write_independent_outputs_in_parallel` | 48651b8 / 2026-09-29 |
| 过时文档误导 | 旧 docs 描述已废弃的 `data/input`/`data/artifacts` 四层布局 | 删除 README/docs 旧文件；新文档单独重写（即本套 docs） | 文档评审 | 15548a6 / 2026-09-29 |

## 3. 如何新增/复核一条 quirk

1. 用产物证明现象（organize CSV、`manifest.json`、`qc_report.json`、`logs/download_full.log`），不要凭印象。
2. 判断是“数据源现实”还是代码缺陷：能改且值得改的进 [AGENT.md](../../AGENT.md) §1 路由，修完移入 §2 历史修复记录；不能改的留在 §1。
3. 新增条目必须给：现象、根因（指向函数/文件）、是否可修、应对；涉及数字的注明数据日期（本文数字对应 2026-09 产物）。
4. 若条目变化（例如 183 → 其他、基线行数变化），同一 PR 内更新本表 + [data-contracts.md](data-contracts.md) 基线。
