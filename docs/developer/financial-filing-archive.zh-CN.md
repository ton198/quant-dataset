[English](financial-filing-archive.md) | **简体中文**

# SEC 申报档案：已实现边界与待处理流程

**状态（2026-10-04）：有界 archive/processing 已实际验证，但不代表财务覆盖或财务正确性已完成。** 首批五份独立私有 archive `/tmp/opencode/filings-pilot-v1` 已到 manifest v7，含 6,017 条 facts、165 条 sections、6 条 full text、4 条 full XBRL 与 1 条 legacy cover numeric unsupported；精确重复运行保持同一 head。第二批独立 archive `/tmp/opencode/filings-pilot-diverse-v1` 已到 v15，10 份选定申报（8 个 CIK）共 25,656 条 facts、95 条 sections 和 25 条 active parser 行（15 full text、6 full XBRL、4 unsupported XBRL）。状态按 parser/document 行统计，不是财务完整性计数。第二批 RBC 40-F 使用一个 primary 锚定 IXDS group parse，保留 7,543 个原始 occurrence（主文档 40、EX2 7,503），并退休两条旧 solo parse；RBC raw coverage 仍为 `partial`。CNR 2002 40-F/A 的 136,629 字节 SGML TEXT payload 由 TEXT 1.0.3 完整抽取，numeric XBRL unsupported，raw coverage 仍 partial。Shopify 6-K 保持 candidate/partial；正文检查显示 EX-99.1 是 CEO 自动证券处置计划公告，但没有 scope-review API 或自动排除规则。全套测试为 787 passed、2 个 skip；之后仅格式化一个 pure parser 测试，其 5 个定向测试通过。独立 daily release 已提交至 `data/output`，当前为 v3；旧 v1 output 保留在 `data/output-v1-backup-20261003T172214933236Z`，冻结 baseline 仍单独保留。本文区分已实现行为与待完成范围；现有 raw/organized 输入与冻结 baseline 均继续保留；完整旧 v1 output 保存在记录的同级 backup 路径中，不得删除。当前 `samples_v3` builder 仍不含财务数据，见 [samples.zh-CN.md](samples.zh-CN.md)。

## 1. 当前状态与范围边界

现有 `data/raw/sec/financials/` 缓存包含 SEC Company Facts、submissions 与历史 submissions-page JSON。现有 organize 路径还会生成 `financials.csv` 和选定范围的 `financial_events_v1` 结构化抽取。后者含申报事件行及来自九概念白名单的 facts；它不是全部 XBRL facts，也不是完整 filing 文档档案。

现在另有独立 archive 边界。`filings catalog` 将本地缓存的 submission 资源复制到显式指定的档案；`filings download` 有界获取选定 inventory/文档；`filings parse` 处理已存在的选中 package；`filings verify` 核验已发布档案/core rows；`filings query` 查询 manifest 实际列出的表。它与既有 Company Facts cache 分开，也不是 `samples_v3` 输入。不要把 archive 复制到每日样本行；未来连接仍是可选下游操作。

Parser 模块/schema 已通过有界 `filings parse` CLI 接入。当前真实 evidence 是两个独立 archive：首批五份 v7 有 6,017 facts/165 sections；第二批十份 v15 有 25,656 facts/95 sections，含一个持久化的 RBC IXDS group parse。v15 共 25 条 active parser 行（15 full text、6 full XBRL、4 unsupported XBRL）；状态按 parser/document 行计，不是经济完整性计数。Active fact owner 按精确 source URI/hash 与 `provenance.source_document_id` 核对；group facts 仍以 primary parse ID 为锚点。详细证据和限制见第 7 节。

**独立的非 XBRL 抽取 lane：** 来源无关的 domain/evidence/runtime 脚手架正在实施，但目前没有受支持的真实申报抽取、`filings extract` CLI、财务 optional tables 或 archive 发布路径。该工作不改变 archive snapshots、既有 parser schemas、`financial_events_v1` 或 `samples`。见[财务抽取：当前实现与离线边界](financial-extraction.zh-CN.md)；Fake/fixture 测试不是真实来源 evidence。

## 2. 身份、raw 保留与候选布局

### Filing 身份

- 使用 `(cik10, accession_number)` 作为 filing 主键。CIK 保留为十位字符串。
- Ticker 是随时间变化的市场/证券标签，不是 filing identity。保留带生效/观察时间与来源 provenance 的 ticker↔CIK 映射表，因为一个发行人可能有多个 ticker/类别股，映射也可能变化。
- Amendment 有自己的 accession 和 filing 记录。不得用 amendment 覆盖原申报，或按 report period 将两者合并。
- 保留 `filed_date`；若来源提供则记录 `acceptance_datetime`。抓取时间只作 provenance，不作为公开可用时间戳。

### 已实现 archive 存储边界

每个 run 使用显式 archive root，与现有 `data/raw/sec/financials/` 分开。当前布局包括：

```text
<archive-root>/
  run.json
  manifest.json                     # 当前发布 head；当前 snapshot 的依据
  ledger.jsonl                      # attempt/audit 事件，不是发布事实来源
  raw/sha256/<prefix>/<sha256>      # 不可变来源/资源字节
  snapshots/<snapshot-id>/manifest.json
  tables/<table>/<snapshot-id>/part-00000.parquet
```

`manifest.json` 列出不可变 snapshot 和精确的 Parquet/raw 路径；reader 不会 glob 搜索文件。Catalog/download 写入 core `filings` 和 `documents` 表。Parse 可发布由 `parsing_models` 所有的 `facts`、`sections`，以及由 `processing.py` 所有的 `parses`、`dependencies`；这四张表均为可选 manifest output，缺失时不会创建空占位表。实际实现/schema 为准；本文后续的 occurrence 字段列表是契约说明，不保证普适抽取。

### 待实现的派生产物

解析/索引产物与原始 archive 字节分开。`filings parse` 可对有界且已就绪的 package 发布 parser 结果；DuckDB 只是可选本地查询工具，不是服务或必需的数据迁移。当前实现为小型 pilot 做整表 replacement/materialization，不声称适合数十亿行生产规模。

## 3. 文档盘点与抓取范围

当前有界 acquisition 使用 SEC index/detail metadata 盘点 accession 资源，并记录文档名/角色、URL、selection/fetch 状态及已获取字节的 hash。它保留 inventory/index 证据，并获取已选择的 required primary 文档及被选中的 XBRL 资源；不会获取每个 exhibit，也不会镜像整个申报目录。Processing package 仅复制已提交、已选择且 present 的 parser 文档，保留 SEC 原文件名并重验 hash。Text parser 只处理选中的 primary/财务附件。若同一 filing 的 present required 原始 Inline 文档间存在类型明确、来源绑定、owner 无歧义的 context/unit/continuation/tuple/relationship 跨文件引用，Python API 可把它们作为一个 default-target IXDS work unit；不依赖文件名、namespace/ID 重合或生成 XML。`parse_archive(..., inline_document_sets={filing_id: (primary_document_id, other_member_id, ...)})` 是 prototype API，没有对应 CLI flag。Group 持久化并在省略参数时恢复，不回退到 solo；一个 primary 锚定 numeric parse 记录 physical owner 来源，named/multiple target 与歧义 ownership 不支持。Schema/linkbase 是依赖，不是 fact/text entrypoint。PDF 与 plain-text numeric unsupported 等状态明确记录，不伪造零值。Metadata selector 仍是保守分流，不是完整内容分类。

- `filings download` 默认最多处理 5 份申报，上限 50；显式 ID 必须已在 catalog archive 中。结果区分 acquired、unavailable、error、blocked、candidate、out-of-scope 与 `needs_review` 状态。
- 6-K 只有在 metadata scope gate 有足够 inventory 证据时才可能把财务相关子集纳入；它不会自动将所有 6-K 纳入，也不证明外国发行人覆盖完整。首批 v7 中的 TSMC 6-K 是在核验 detail index 与 EX-99.1 财务报表 exhibit 后纳入，选定 raw scope 为 `scoped_complete`；第二批 Shopify 6-K 仍为 `candidate`/`partial`，且没有自动排除规则。
- 无法识别的附件可保持 candidate。仅凭 press-release 标签不能证明申报为非财务内容。不要把 out-of-scope 选择解释为完整内容分类。

Primary 文档与全部附件范围必须明确区分。除非已测量并报告范围和完整性，不得声称建立了“完整全文档案”或完整申报覆盖。

## 4. 已接入的 XBRL fact-occurrence 处理边界

`filings parse` 可发布由 `src/filings/parsing_models.py` 所有的 `FACT_SCHEMA` occurrence rows；字段由 parser version/schema 管理，不应在下游假设固定列数。结果保留原始词面字符串、可用时的变换/规范化值、context/unit XML 和维度来源、source hash/locator、parser version/status 与 Arelle diagnostics。无效 fact 保留 occurrence 和 validation error，数值保持 null/invalid，不会丢弃或补 0。这是结构与来源核验，不是 SEC/EFM 认证、经济解释正确性，也不是每个 concept 只选一个值。

真实 AAPL 2024 10-Q parser attempt 得到 781 条 full XBRL fact rows，parser 无 error codes。运行时固定为 Arelle 2.46.0，并使用本地打包的 SEC inline-transform 支持：来自 Arelle/EDGAR commit `47a372d099168f8669d20a8a6bbe5cb16bbf71ac` 的两个官方实现文件，source 与 license hash 均已核验。Parse 时不会联网获取 transform 源码。这只是 inline transformation 支持，不是完整 SEC EFM 验证 plugin/整份申报合规核验；不支持通过 HTTP 加载 plugin 代码，也不开放任意用户 plugin path。

保留事实的**出现记录（occurrences）**，不只保留每个 concept 的一个规范化值。每个派生 occurrence 应保留足够字段以还原来源内容与解释方式，例如：

- filing key（`cik10`、accession）及来源文档 payload hash；
- taxonomy namespace URI 和 local tag name（必要时保留来源 prefix 作为展示/provenance）；
- 来源 locator：文档名/hash，以及可用时的 byte/element/DOM/XBRL locator、line/fragment；
- 原始 lexical value 与可选 parsed numeric value；不得丢弃 lexical form；
- unit 引用及解析后的 unit 表示；
- context 引用、entity 标识；
- instant date 或 duration start/end，并保留明确 period kind；
- explicit 与 typed dimensions，保留 member QName/namespace 及 typed-member 内容，不要压平为单一标签；
- 可用时的 `decimals`、`precision` 与 `xsi:nil`/nil 状态；
- inline XBRL transformation/format、sign、scale、continuation 链及适用时转换后的数值；
- parser/tool 名称与版本、仅作 provenance 的 extraction timestamp、parse status。

不要强制映射到当前九概念 `_CONCEPTS` 白名单，不要合并 custom taxonomy tags，不要聚合 dimension 变体，也不要静默挑选一个重复项。重复/重叠 occurrence 是可审计的来源事实。未来任何 normalization/concept mapping 都应做成单独版本化的 derived view，并可回指 occurrence 记录及原始 payload hash。

本提案不声称 SEC Company Facts 覆盖所有 custom taxonomy 或 dimensional facts。当前 raw cache 只有 Company Facts/submissions/历史分页 JSON；这些 JSON 资源不能替代归档 filing HTML/iXBRL 或 filing 专属 XBRL 文档集。

## 5. 已实现的申报文本抽取边界

原始 filing bytes 保持不可变。Text parser 可通过 `SECTION_SCHEMA` 发布归一化 HTML/text sections，并关联 filing/document/parse identity、source hash、parser version、section kind、heading/content（可用时）、结构 XPath/ordinal、提取 scope/status。Heading 是 heuristic anchor，不是 byte offset。PDF text parsing 未实现；PDF 保留原始字节并标记 unsupported。Text `full` 表示 text/source checks 完成，不代表财务或语义完整。当前 parse 状态区分 `full`、`partial`、`unsupported`、`failed`；unsupported 不等同 parser failure，也不表示财务完整。

将**字节/文档盘点完整性**与**文本/事实抽取覆盖**分开跟踪。一份 filing 可能所有预期字节均在，但文本抽取不完整；也可能附件因范围决策未取，并非 parser error。不要用一个 aggregate completeness bool 隐藏这些状态。

## 6. 可见时间、用户编写的 as-of 筛选与连接

- Catalog 将 `effective_visible_session` 设为严格晚于 `filed_date` 的第一个 XNYS session；周末/假日披露映射到之后首个 session。这是 metadata，不是 `samples_v3` 特征。
- Acceptance datetime 保留为来源 metadata，不替代可见时间规则，也不是 query 默认值。
- `filings query` 不自动按可见日期筛选、不选“latest”、也不合并 amendments。As-of 查询必须由用户显式写入 `effective_visible_session <= CAST('<AS_OF_DATE>' AS DATE)` 之类的日期谓词。该筛选作用于 catalog metadata，不能保证截止日前所有来源文档或处理均已完成。
- Filing identity 使用 `(cik10, accession_number)`；amendment 保持独立。当前流程不提供完整的历史 ticker/CIK PIT 映射；不可将当前 ticker 字符串当作 filing 主键。
- 未来如要下游连接日频样本，仍是可选操作，必须有明确 as-of 规则；不要把全文重复写入每日行。

## 7. 当前证据与待完成核验

- Offline fixtures 覆盖 miniature catalog → mocked acquisition → package → taxonomy preparation → offline parse → snapshot。它们证明边界行为，不是实际 SEC 覆盖率或财务正确性证据。
- **首批五份 archive v7：** Apple 2010 10-K/A、Apple 2024 10-Q/10-K、TSMC 2024 20-F/6-K。Catalog 有 823 条 filing metadata，**不是** 823 份下载报告；获取 26 个原始文档 payload 和 10 个 inventory/index metadata 来源；423 条 document registry 是 metadata 行，不是 payload 数。v7 中五份选定 raw scope 都达到 `scoped_complete`。TSMC 6-K 是在核验 detail index 与 EX-99.1 实际财务报表 exhibit 后纳入；它只有选定文件范围完整，不是全部附件覆盖。
- 首批 v7 活动解析结果为 6 条 full text、4 条 full XBRL、1 条 legacy cover numeric XBRL unsupported；共 6,017 facts、165 sections，精确重复运行保持同一 head。Unsupported 表示没有接受 numeric facts，不表示经济零值。报告：`/tmp/opencode/filings_first_five_rerun_report.json`。
- **第二批十份 archive v15：** 10 个显式 filing IDs、8 个 CIK，25 条活动 parser 行（15 full text、6 full XBRL、4 unsupported XBRL）、25,656 facts、95 text sections、159 dependency 行。3,051 条 filing metadata 与 1,253 条 document registry 是目录记录，不是全体 filing/body 获取。总 case 矩阵见 `/tmp/opencode/filings_second_batch_final_rerun/report.json`。
- RBC 40-F 的 primary `d519293d40f.htm` 与 EX2 `d519293dex2.htm` 通过来源绑定的跨文件引用形成一个 default-target group。活动 parse 只有一条 primary 锚定记录，full/7,543 occurrences（primary 40、EX2 7,503），两条旧 solo parse ID 已退休；事实按 exact `source_uri`/`document_hash` 与 `provenance.source_document_id` 审计。Group full 仍不改变 RBC `raw_coverage_status=partial`。省略 group 参数可恢复持久化成员计划；省略参数与相同显式成员映射的重复均未产生新 head 或新行。
- CNR 2002 40-F/A 的 plain SGML TEXT 1.0.3 完整保留原始 payload span `[70, 136699)`（136,629 字节），仅按规则裁掉外层边界空白；numeric XBRL unsupported/0，raw coverage 仍 partial。Microsoft 2008 10-K 与两个选定 6-K 的 unsupported numeric 状态也不是经济零值。Shopify 6-K 仍为 `candidate`/`partial`；临时 EX-99.1 正文显示 CEO 自动证券处置计划公告，而不是公司业绩，但 scope-review API 尚未实现，因此证据没有被写成自动排除规则。见 `/tmp/opencode/filings_second_batch_body_evidence/report.json`。
- 有效依赖准备失败的 group 会保存 retryable failed/partial 组状态与 source-bound 错误图、退休旧 solo 输出；重复失败不会被跳过为 terminal，后续成功依赖准备可恢复 group 并清理 orphan error graph。定向 E2E 报告：`/tmp/opencode/filings_ixds_failed_preparation_fix.json`；失败测试覆盖未扩大到真实 archive。
- 全源码 gate 为 787 passed、2 skipped；之后只格式化 `tests/test_filing_inline_document_set.py`，其 5 个定向测试通过。第二批真实 v15 执行器在解析及两次 repeat 后遇到本地 report 序列化变量名错误；报告已根据提交后的活动 snapshot 和独立 guarded repeat 检查重建。执行日志与报告在 `/tmp/opencode/filings_second_batch_final_rerun/`。Final v15 的 processing 状态经活动表核验，protected first-five/v3/output/baseline/raw-cache manifests 未变。运行依赖仍固定于 `pyproject.toml`/`uv.lock`；此处文档同步未改代码、未安装依赖、不提交 commit。
- Daily release 与 archive pilot 分开：`data/output` 已提交为 v3；旧 v1 tree 保留在 `data/output-v1-backup-20261003T172214933236Z`，冻结 baseline 独立保留。发布证据见 `/tmp/opencode/v3_release_publication/report.json` 与 `data/.output-publication-20261003T172214933236Z.json`。本 archive pilot 未重建 v3、添加财务模型列、训练 encoder，也未合并进 daily bundle；不声称 SEC 规模覆盖、经济正确性、未来申报覆盖或历史 ticker/CIK PIT 保证。
- 不向 samples_v3 添加财务特征；本流程不包含训练、历史 ticker/CIK 重建或 encoder 任务。当前 archive 的 CIK/accession 身份不是历史 ticker 映射。
- 当前不存储或生成 embeddings。未来若任务提出 embeddings，应放在独立 derived store/table，并至少按 filing identity、encoder/model 标识与版本、输入文档/文本 hash、preprocessing/chunking 版本建立键。它们必须是可选下游产物，不能写入 raw source 记录或每日样本列。
- 全部附件策略、抽取完整性指标、保留期与财务验收标准仍待决定。已下载源字节或 parser `full` 状态都不证明财务报表正确。

## 8. 相关契约

- 用户命令、安全路径与状态边界：[filings.zh-CN.md](../user/filings.zh-CN.md)。
- 当前不含财务数据的样本输出：[samples.zh-CN.md](samples.zh-CN.md)、[data-contracts.zh-CN.md](data-contracts.zh-CN.md)。
- 既有 SEC Company Facts/submissions 与选定结构化 organizer 行为：[download.zh-CN.md](download.zh-CN.md)。
- 独立非 XBRL 抽取工作及当前离线边界：[financial-extraction.zh-CN.md](financial-extraction.zh-CN.md)。
- Consumer schema 与安全候选构建：[data-format.zh-CN.md](../user/data-format.zh-CN.md)、[cli.zh-CN.md](../user/cli.zh-CN.md)。
