[English](known-quirks.md) | **简体中文**

# 已知 quirk 与数据源现实

用本文区分数据源行为和实现缺陷。唯一维护的 `samples` 契约不含财务数据，不读取财务文件、财务 metadata，也不计算财务覆盖率。既有 `financials.csv` 与 `financial_events_v1` 是独立的 organized artifact。本文不作未经测量的覆盖率、性能或发布 PASS 声明。

关联：[download.zh-CN.md](download.zh-CN.md)、[samples.zh-CN.md](samples.zh-CN.md)、[data-contracts.zh-CN.md](data-contracts.zh-CN.md)、[../user/data-format.zh-CN.md](../user/data-format.zh-CN.md)。

## 1. 当前与历史 quirk

| 范围 | 行为 | 适用范围 / 应对 |
|---|---|---|
| 旧 `financials.csv` | 每个 session 选一份最新整份申报快照。后续非财务申报可能用空概念值替换该行；不会按概念 carry-forward。 | 既有 organizer 行为，保持不变。该 CSV 不是 samples 输入。 |
| `financial_events_v1` | 既有 event/fact Parquet 保留选定申报事件及九概念白名单中的 facts。 | 独立的 organized 结构化抽取，不是全部 XBRL、全文 filing 或 `samples` 输入。独立 filing archive 属于另一流程与 artifact 边界。 |
| 已退出的财务特征提案 | 未实施的旧提案曾描述样本财务列及 coverage 规则。 | 不是活跃样本契约或构建路径；不得据此新增财务输入、别名或 gate。既有 Company Facts 与 `financial_events_v1` 文档仍独立保留。 |
| 银行与发行人概念缺口 | 选定九概念白名单不包含所有发行人特有收入/利润概念；例如银行利息/手续费 tag 可能不在其中，XOM 的选定来源中没有 `OperatingIncomeLoss`。后续 comparative facts 不会自动回填到早期 filing event。 | 独立旧抽取的局限。当前没有 samples 财务覆盖指标。保留来源不确定性；不得声称这些抽取是完整 XBRL。 |
| SEC 资源缺失 | 现有 organize 代码会区分确认空/无输入与资源缺失、格式错误或不完整。 | 适用于独立 organized 财务产物。samples 不检查 SEC resource status；财务状态为 `not_applicable`。 |
| 无行情 | 缺少 `market.csv` 的 ticker 无法生成行情样本行。 | 检查行情输入和样本 manifest；本文不声称当前样本 ticker 数量实测。 |
| 多 ticker 共用 CIK | universe 按 ticker 列出，而 CIK 标识发行人；多个类别股可共用 CIK。 | 不要假设一对一映射。现有 organizer 按 ticker 单独输出，并记录 CIK/source provenance。未来 archive 应以 CIK+accession 标识 filing，另存有日期的 ticker mapping。 |
| 历史 v1 中 GAM/MFM/PEO 的 CIK 来源错误 | v1 organizer 曾在 raw payload 的 hash 文件名偶然包含请求 CIK 数字时选入它，但没有检查 payload 内层 CIK。三个 v1 `financials.csv` 字节完全相同（各 333,320 bytes；SHA-256 `a1a8468c7f487ca04eaa31ae9b641b06995039ad2f31845c91f6df5a09be72ba`），内层 CIK 分别为 `0002003977`、`934549`、`0001780731`。 | 历史 v1 数据质量缺陷。不要将这些旧财务历史视为 issuer truth。后续既有 organizer 代码增加了规范化 CIK 精确匹配；samples 完全不含财务输入。 |
| FRED 历史与修订 | FRED 序列起始日期不同；值为 latest-revised 而非 vintage。organizer 用参考期约一个月后的 session 近似发布时间。 | 既有宏观行为。使用 `miss_m_*`，将早期缺口/修订视为数据源限制；本文不声称任何当前样本宏观缺失率。 |
| `is_common` | 普通股分类使用 ticker 后缀启发式，不是 security master。非 common 行保留并参与截面 rank。 | 如需严格普通股池，请明确过滤。 |
| `excess_*` 基准 | 没有 SPY 基准。`excess_5d/21d` 减去同日 `is_common=true` 且 `flag_extreme_label=0` 行的等权目标均值；非 common 行也可能获得 excess 值。 | 按契约。需要其他基准时在下游重构；不要称其为市值加权市场超额。 |
| Adjusted-open 标签 | `adj_open = open × adj_close / close` 是复权价，不是可执行成交价。 | 标签不含成本/滑点，不应被当作成交模拟。 |
| Extreme-label 行 | 标签窗口跨越相邻 session adjusted-open 比值超出 `[0.5, 2.0]` 时打标；行仍保留，但从 excess 基准均值中排除。 | 是标记，不删行。查看候选 QC/manifest 数；本文不声明当前样本数量。 |
| 旧财务年龄列 | `f_raw_days_since_filing`、`f_raw_days_since_financials`、`f_raw_days_since_oldest_financial_input` 属于先前样本 schema。 | 已从 samples 移除。v1 的 `days_since_filing` 12.40% 缺失率只作历史基线。 |
| 既有下载进度 | 配置中的 `preserve_progress_on_success` 已解析但 manager 未使用；进度文件仍会保留。 | 既有行为。按既有 `--force`/`--force-rebuild` 语义操作，不要依赖此选项。 |
| Yahoo 区间文件 | 同一 ticker/区间重下会覆盖同名 CSV；不同区间可共存，organize 时合并并去重。 | 既有行为；不得手工编辑 raw 文件。 |

## 2. 财务来源隔离

样本构建没有活跃的财务特征提案。既有 Company Facts cache、旧 `financials.csv` 与选定的 `financial_events_v1` 抽取各自保留来源和 organizer 契约；独立 filing archive 也有自己的范围。它们都不会被隐式 join 到 `samples`；既有抽取与 filing archive 都不得称为完整 XBRL 覆盖或完整申报全文。

## 3. 新增或复核 quirk

1. 用具体来源/输出 artifact 证明问题，不凭记忆。
2. 判断是数据源限制还是实现缺陷；代码任务按 [AGENT.zh-CN.md](../../AGENT.zh-CN.md) 路由。
3. 所有历史指标都标注 schema/version。不要编造未测样本指标或暗示 gate 已通过。
4. 中英文同步。提议 archive 范围只更新 [financial-filing-archive.zh-CN.md](financial-filing-archive.zh-CN.md)；文档改动不得实现 fetcher/parser。
