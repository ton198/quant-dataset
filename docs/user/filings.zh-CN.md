[English](filings.md) | **简体中文**

# SEC 申报档案使用指南

这是一个独立且有界的流程：从本地 SEC 缓存编目申报、将选定的申报文档获取到显式指定的档案、核验档案，并查询实际存在的表。它不会改变既有 `download --stage financials` 缓存、旧 organized 财务产物或当前不含财务数据的 `samples` builder。保留的 daily `data/output/` artifact 仍有历史 manifest 标签 `samples_v3`；它不是当前 builder 契约。

非 XBRL 财务抽取提供显式、禁止发布的 `filings extract-financials` 命令，处理冻结清单中的合格已归档 HTML 文档。使用有界 worker 和带校验字符的窗口内短 ref，不替代 numeric parser，也不改变 archive scope。见 [CLI 合同](cli.zh-CN.md#财务抽取)及[开发者说明](../developer/financial-extraction.zh-CN.md)。严格引用/schema 校验不代表独立财务准确性或数值覆盖。

**状态（2026-10-04）：** 有界 `catalog`、`download`、`parse`、`verify`、`query` prototype 已提供。两个独立私有 archive 均已实际验证；它们彼此独立，也与当前 `samples` 产品分开。

- 首批五份申报 archive 为 manifest v7：6,017 条 facts、165 条文本 sections。Active parse 行包括 6 条 full text、4 条 full XBRL 和 1 条 legacy cover numeric XBRL unsupported；精确重复运行保持同一 head。证据：`/tmp/opencode/filings_first_five_rerun_report.json`。
- 独立第二批 archive 为 manifest v15：10 份选定申报、8 个 CIK、3,051 条 catalog filing 行、1,253 条 document registry 行（两者都不表示下载了全部申报正文）、25 条 active parser 行、25,656 条 facts 和 95 条文本 sections。Active 状态为 15 条 full text、6 条 full XBRL、4 条 unsupported XBRL。RBC 40-F 组使用一个 primary 锚定 parse，保留 7,543 个原始 occurrence（主文档 40、EX2 7,503），并退休了两个旧 solo parse ID。省略 group 参数时恢复了保存的 group；省略参数和显式 group 重复运行均为 no-op。证据：`/tmp/opencode/filings_second_batch_final_rerun/report.json`。

记录的完整测试套件为 787 passed、2 skipped，见 `/tmp/opencode/filings_second_batch_ixds_final_validation/`；之后只有一个 parser test 文件做了格式调整，其 5 个定向测试通过。这些有限 parser 结果不代表广泛发行人覆盖、全部附件齐全或财务正确。特别是：RBC 数值 group parse 虽为 full，raw coverage 仍为 `partial`；CNR 2002 40-F/A 虽有 full TEXT 1.0.3，numeric parse 仍 unsupported 且 raw coverage `partial`；Shopify 6-K 仍为 `candidate`/`partial`。Shopify 正文人工检查仅作证据，没有被写成自动排除规则。Daily `data/output/` v3 独立不变，v1 output 与冻结 baseline 分别保留；发布证据见 `/tmp/opencode/v3_release_publication/report.json`。

## 安装并选择安全的档案路径

Catalog、download、verify 使用普通项目安装。`filings parse` 需要可选的 `filings` extra（Arelle 2.46.0）；档案 SQL query 需要可选的 `query` extra（DuckDB）。普通流水线安装不变。

根据实际需要任选一条安装命令（带 extra 的命令是替代选项，不要依次运行）：

```bash
uv sync --frozen
# 若要解析申报文档：
uv sync --frozen --extra filings
# 若要查询档案表或样本 Parquet：
uv sync --frozen --extra query
# 两种可选工具都要用时：
uv sync --frozen --extra filings --extra query
```

选择一个全新的档案目录，并在命令中明确指定。例如 `data/filings/pilot` 与现有 `data/raw/sec/financials` 缓存分开。不要把档案放在 `data/raw`、`data/organized`、`data/output`、`data/baselines`、v3 candidate 或输入 cache 的里面或上层目录。不得复用 `data/output` 或历史 candidate。

```bash
ARCHIVE=data/filings/pilot
CACHE=data/raw/sec/financials
```

Catalog 命令只读取 cache manifest 已列出的本地文件，不会请求 SEC。cache root 必须包含既有的 `manifest.json` 及其引用的已验证资源。若 cache 本身需要填充，可另行使用既有 `download --stage financials` 获取 Company Facts/submissions metadata；catalog 不会隐式调用它，而且该 stage 不抓取 archive 使用的申报文档。若缺少历史分页，catalog 默认失败；`--allow-partial` 是显式、可见的选择，不会修复或补齐完整性。

## 1. 编目本地缓存的 submissions

```bash
quant-dataset filings catalog \
  --archive "$ARCHIVE" \
  --cache-root "$CACHE" \
  --cik 320193 \
  --start 2024-01-01 \
  --end 2024-12-31 \
  --form 10-K
```

- 必须指定 `--archive`、`--cache-root`、至少一个 `--cik`，以及包含首尾日期的 `--start`/`--end`。可重复指定 `--cik` 或 `--form` 添加选择。
- 表单范围是获批的 SEC 子集。命令只使用本地缓存的 submissions 和历史分页资源，不会抓取缺失的 catalog 输入。
- 可见时间映射为严格晚于 `filed_date` 的第一个 XNYS session；不会用 acceptance time 或下载时间替代。
- 只有运行时保存的配置完全相同时才能用 `--resume`。`--allow-partial` 会明确报告缺失历史页；它不会补齐数据或表示覆盖完整。
- Archive 会把已验证的 source submissions 复制到自己的内容寻址 raw 存储中，不会修改输入 cache。

初始 catalog 记录申报身份和 metadata，不包含附件 inventory，也不表示申报文档字节已到位。

## 2. 有界下载一组申报文档

`filings download` 只能用于已经发布的 catalog 档案，不会新建档案，也不会自动下载某公司的全部历史申报。默认最多处理五份符合范围、尚未完成的申报；`--max-filings` 可设为 1–50。可重复使用 `--filing-id` 指定档案中已有的精确 `cik10:accession_number`。未知、已排除、重复或超过上限的 ID 会在请求 SEC 之前被拒绝。

SEC client 需要在 `config/secrets.toml` 中配置非占位 contact：

```toml
[secrets]
sec_user_agent = "<替换为项目名称和真实联系邮箱>"
```

运行前必须换成经批准的项目名称和真实联系邮箱；示例占位文本会被拒绝。档案命令只读取 `sec_user_agent`，**不**需要 FRED key。可用 `--secrets PATH` 指定其他 TOML 文件。报告不会输出邮箱或 User-Agent 内容。

```bash
quant-dataset filings download --archive "$ARCHIVE" --max-filings 5
# 或从 query 结果中取得精确 filing_id 后指定：
quant-dataset filings download --archive "$ARCHIVE" --filing-id "$FILING_ID"
```

这是一个只访问 SEC、且数量有限的操作；命令没有任意 URL 参数。它会保留 SEC 文件目录等来源记录，并获取已选中的主文档和必要 XBRL 文件；不会下载全部附件或复制整个网站，也不会改写原有缓存。结果会报告处理了哪些申报、选择状态、成功/不可用/错误数量、被阻止的申报和新档案版本。若出现被阻止、不可用或错误的项，命令会返回非零状态，但仍可能输出部分结果。单独的“需要复核”提示不表示完整或成功。

6-K 申报仍标为“待确认”。文件目录信息可能帮助选择部分与财务相关的文件，但这不表示海外发行人申报已完整覆盖，也不会把所有 6-K 改为正式纳入。附件选择只依据有限的目录信息，不能完整判断文件内容；无法识别的附件会保留待复核状态，仅看到“新闻稿”标签也不足以证明申报不含财务信息。命令不会获取所有附件。

### 原件 pilot 结果

原始获取 pilot 选择了 Apple 和 TSMC 的五份申报：Apple 2010 10-K/A、Apple 2024 10-Q 与 10-K、TSMC 2024 20-F 与 6-K。Catalog 有 823 条 filing metadata，**不是** 823 份已下载报告。获取了 26 个选中原始文档 payload 和 10 个 inventory/index metadata 来源；423 条 document registry 是**元数据行，不是** 423 个原始 payload。v7 rerun 中五份选定申报的 raw scope 都达到 `scoped_complete`。TSMC 6-K 是在检查实际 detail index 和 EX-99.1 财务报表附件后纳入；这只覆盖选定文档，不表示附件齐全或财务覆盖完整。

### 真实 parser pilots（2026-10-04）

首批五份申报 archive 为 v7：6,017 条 facts、165 条文本 sections，活动 parser 行包括 6 条 full text、4 条 full XBRL 和 1 条 legacy cover 数值 XBRL unsupported；精确重复运行保持同一 head。Unsupported 表示该来源没有被 numeric parser 接受，不表示申报数值为零。证据：`/tmp/opencode/filings_first_five_rerun_report.json`。

独立第二批 archive `/tmp/opencode/filings-pilot-diverse-v1` 为 v15：10 份选定申报、8 个 CIK、25 条活动 parser 行（15 full text、6 full XBRL、4 unsupported XBRL）、25,656 条 facts 和 95 条文本 sections。3,051 条 catalog filing metadata 和 1,253 条 document registry 是 inventory 元数据，不代表正文全部下载。详细结果见 `/tmp/opencode/filings_second_batch_final_rerun/report.json`。

| 申报 | XBRL 结果 | Text 结果 | 范围限制 |
| --- | --- | --- | --- |
| RBC 2023 40-F | full / 7,543 occurrences | 3 条 full sections | 一个锚定 primary 的 IXDS group：primary 40、EX2 7,503；退休了两个旧 solo parse ID；raw coverage 仍是 `partial`。 |
| CNR 2002 40-F/A | unsupported / 0 | full / 1 section | Plain SGML TEXT 原始 payload span 为字节 `[70, 136699)`，共 136,629 字节；raw coverage 仍为 `partial`。 |
| Bank of America 2023 10-K | full / 8,199 | full / 1 | 仅表示结构/来源核验通过。 |
| Microsoft 2024 10-Q | full / 1,236 | full / 1 | 仅表示结构/来源核验通过。 |
| Wells Fargo 2023 10-Q/A | full / 59 | full / 1 | Amendment 是独立 filing identity。 |
| Microsoft 2008 10-K | unsupported / 0 | full / 80 | 原始 HTML 保留；unsupported 不代表经济零值。 |
| Nokia 2023 20-F | full / 4,205 | full / 1 | 仅表示结构/来源核验通过。 |
| SAP 2023 20-F | full / 4,414 | full / 1 | 仅表示结构/来源核验通过。 |
| CNR 2024 6-K | unsupported / 0 | full / 4 | 选定文档范围为 `scoped_complete`；不表示所有附件齐全。 |
| Shopify 2024 6-K | unsupported / 0 | full / 1 | 仍为 `candidate`/`partial`。临时 EX-99.1 正文检查显示为 CEO 自动证券处置计划公告；scope-review API 尚未实现，因此没有自动排除或写入分类规则。 |

Inline XBRL group 目前只有 prototype Python API，没有新 CLI flag。已声明或已保存的 group 按 default-target source-bound 文档集恢复；省略参数不会退回 solo parse。Fact 的 `document_id` 是 primary 调用锚点；`source_uri`、`document_hash` 和 `provenance.source_document_id` 表示实际物理来源。Group `full` 不会提升 raw coverage，也不代表财务正确。

完整测试套件证据为 787 passed、2 个 skip，见 `/tmp/opencode/filings_second_batch_ixds_final_validation/`；之后只对 `tests/test_filing_inline_document_set.py` 做格式调整，其五个定向测试通过。第二批 v15 已发布到独立 archive，省略 group 参数和再次显式声明两种重复都未产生新 head 或数据行。该执行器在写报告阶段发生局部变量名错误；报告已根据实际 v15 snapshot 和单独的重复验证重建，详见报告与 `/tmp/opencode/filings_second_batch_final_rerun/run.log`。两个 archive 均为有限 source 样本，不代表所有 SEC 申报、附件或财务覆盖。

## 3. 解析选定的档案文档

`filings parse` 要求已有 archive 和单独的 workspace 目录。默认每次最多处理五份已就绪申报；`--max-filings` 为 1–50。可重复指定档案中精确存在的 `--filing-id`。该命令不会新建档案、扩展 catalog 范围或下载申报文档，而是将已提交的包复制并核对 hash 后放入 workspace。Workspace 必须与 archive 分开，并避开受保护的 raw、organized、output、baseline、candidate 与 cache 路径。

默认是**纯离线**：不读 secrets 文件，也不提供依赖 fetch callback。缺少本地 taxonomy 时，parse 会如实记录 failed/partial，不会把事实记成零。确实需要获取 taxonomy 时，必须显式开启有界准备：

```bash
ARCHIVE=/path/to/existing/catalog-run
WORKSPACE=$(mktemp -d /tmp/opencode/filings-workspace.XXXXXX)
FILING_ID=0000320193:0000320193-24-000081  # 仅为示例；请使用自己档案里的 ID。

# 默认离线，不会隐式获取 taxonomy。
quant-dataset filings parse --archive "$ARCHIVE" --workspace-root "$WORKSPACE" \
  --filing-id "$FILING_ID" --max-filings 1

# 显式允许有界获取依赖；Arelle 仍在离线模式下解析。
quant-dataset filings parse --archive "$ARCHIVE" --workspace-root "$WORKSPACE" \
  --filing-id "$FILING_ID" --max-filings 1 --prepare-dependencies
```

只有 `--prepare-dependencies` 模式会读取 `[secrets].sec_user_agent`（默认 `config/secrets.toml`，可用 `--secrets` 指定其他文件）并创建 taxonomy client；不需要 FRED key。内置精确 host allowlist 为 `www.sec.gov`、`data.sec.gov`、`xbrl.sec.gov`、`xbrl.fasb.org`、`www.xbrl.org`、`xbrl.ifrs.org` 和 `www.w3.org`，共七个 host。重复使用 `--taxonomy-host` 只能**缩小**这个名单，不能添加任意 host。未启用准备模式时传入 host 或 secrets 参数会被拒绝。获批的 HTTP-origin taxonomy URL 会使用 HTTPS 传输，并分别记录原始 URL 与 transport URL；重定向相对别名无法核验或依赖 host 不在 allowlist 时安全失败，不会让 Arelle 联网。当前处理 helper 按每份申报限制最多 500 个资源、128 MiB，SEC 请求间隔至少 0.2 秒；Arelle 始终离线。

命令输出 JSON 摘要：snapshot/manifest 版本、选择/处理/跳过的 filing ID、parse 状态数量、facts/sections 行数、dependency 记录数，以及有界诊断 code。实际 failed/partial parse 返回非零。若仅有不支持格式，会报告 `completed_with_unsupported`，不会标成 `completed`；PDF 不会臆造 facts 或文本。Parser 的 `full` 只表示自身结构/来源检查通过，不代表 SEC/EFM 合规、财务数字正确或申报覆盖完整。XBRL occurrence 保留原始词面值、context/unit/taxonomy/dimension 来源及精确字符串数值；缺失或无效数字保持 null/invalid 并带诊断，绝不补零。Readable legacy HTML 可抽取启发式文本，但不一定有 XBRL；PDF 保留原字节并标 unsupported。当前不编码 features、算 ratios、不训练模型，也不连接 daily sample labels。

### Inline XBRL 文档集（prototype Python API）

同一 filing 中 present、required 的原始 Inline 文档，只有在存在类型明确、来源绑定且跨文件的引用时，才会作为一个 default-target 文档集处理。不会仅因 namespace 或 ID 重合就合并彼此独立的报告。Python processing API 接收 archive 中的精确 `document_id`：

```python
parse_archive(
    archive,
    protected_paths=protected_paths,
    workspace_root=workspace,
    filing_ids=(filing_id,),
    inline_document_sets={filing_id: (primary_document_id, exhibit_document_id)},
)
```

Primary 是锚点，其余成员按原始 source URL 排序。没有新增 CLI flag。已持久化且通过验证的 group plan 会在省略 `inline_document_sets` 时恢复，不会悄悄退回逐文档 solo parse。Numeric parse 只有一条记录并锚定 primary。Fact 的 `document_id` 表示本次调用锚点；`source_uri`、`document_hash` 与 `provenance.source_document_id` 表示实际提供 occurrence 的物理成员。应按 filing、精确 source URI 和 raw hash 连接并核对物理来源及 group 成员关系。Named/multiple target 和来源归属不明确的情况不支持。Group parse 为 full 仅代表 occurrence 抽取完成，不会提升 filing 的 raw-coverage 状态。

### Inline XBRL 文档集（prototype Python API）

同一 filing 中 present、required 的原始 Inline 文档，只有在存在类型明确、来源绑定且跨文件的引用时，才会作为一个 default-target 文档集处理。不会仅因 namespace 或 ID 重合就合并独立报告。Python processing API 接收 archive 中的精确 `document_id`：

```python
parse_archive(
    archive,
    protected_paths=protected_paths,
    workspace_root=workspace,
    filing_ids=(filing_id,),
    inline_document_sets={filing_id: (primary_document_id, exhibit_document_id)},
)
```

Primary 是锚点，其余成员按原始 source URL 排序。没有新 CLI flag。持久化且通过验证的 group 会在省略 `inline_document_sets` 时恢复，不会静默退回逐文档 solo parse。一个 numeric parse 行锚定 primary；`facts.document_id` 表示调用锚点，而 `source_uri`、`document_hash`、`provenance.source_document_id` 表示提供 occurrence 的物理成员。物理来源核验应使用 filing、精确 source URI 与 raw hash，并确认 group membership。Named/multiple target 和归属不明确的情况不支持。

## 4. 核验档案

```bash
quant-dataset filings verify --archive "$ARCHIVE"
```

核验会检查已发布清单中列出的原始文件和 Parquet 文件，包括 hash、大小、字段结构，以及 filing/document 记录之间的基本一致性。报告只说明档案文件和记录通过核验；不表示所有申报或财务事实都已收录，不表示每份申报资料齐全，也不判断财报数字是否正确。部分文件目录、不可用文档、待确认范围和待复核状态仍会保留在结果中。

## 5. 查询实际存在的档案表

先安装可选 `query` extra。查询结果以 CSV 写到 stdout，query metadata（含 archive format 和 manifest 版本）写到 stderr。Catalog/download/parse/verify 以 JSON 摘要输出已发布的 manifest 版本。`--limit` 默认 20 行，可设为 1–1,000 行。

设置 `AS_OF_DATE` 为你要检查的日期（将占位符替换为真实的 `YYYY-MM-DD`）：

```bash
AS_OF_DATE='YYYY-MM-DD'
quant-dataset filings query --archive "$ARCHIVE" \
  --sql "SELECT filing_id, form, filed_date, effective_visible_session, scope_status, inventory_status, raw_coverage_status FROM filings WHERE effective_visible_session <= CAST('$AS_OF_DATE' AS DATE) ORDER BY filed_date, filing_id" \
  --limit 20
```

查看已有文档 inventory：

```bash
quant-dataset filings query --archive "$ARCHIVE" \
  --sql "SELECT f.filing_id, d.role, d.original_filename, d.selection_status, d.fetch_status FROM filings AS f LEFT JOIN documents AS d USING (filing_id) WHERE f.effective_visible_session <= CAST('$AS_OF_DATE' AS DATE) ORDER BY f.filed_date, d.original_filename"
```

查询只开放当前 archive manifest 实际列出的 `filings`、`documents`、`facts`、`sections`、`parses`、`dependencies` 表；缺少的表不会自动创建为空占位。Catalog/download 创建或更新 `filings`、`documents`；parse 会发布本次 parser 输出及 parse/dependency 状态记录。可查看每个 parse attempt 的状态和诊断：

```bash
quant-dataset filings query --archive "$ARCHIVE" \
  --sql "SELECT filing_id, document_id, parser_name, status, validation_scope, errors_json FROM parses ORDER BY filing_id, document_id, parser_name" \
  --limit 50
```

依赖尝试会记录原始/final/transport URI 来源和失败 code；可只读查看，不会再次下载：

```bash
quant-dataset filings query --archive "$ARCHIVE" \
  --sql "SELECT filing_id, requested_url, final_url, transport_url, status, diagnostic_code FROM dependencies ORDER BY filing_id, requested_url" \
  --limit 50
```

当 `facts` 表存在时，可查看来源 occurrence 与字符串数值：

```bash
quant-dataset filings query --archive "$ARCHIVE" \
  --sql "SELECT filing_id, fact_qname, raw_value, normalized_numeric, context_id, context_json, unit_id, unit_json FROM facts ORDER BY filing_id, object_index" \
  --limit 50
```

当 `sections` 表存在时，可查看抽取文本；它是启发式归一文本，不是精确字节偏移：

```bash
quant-dataset filings query --archive "$ARCHIVE" \
  --sql "SELECT filing_id, section_kind, heading, content_text, source_xpath, source_ordinal FROM sections ORDER BY filing_id, source_ordinal" \
  --limit 20
```

`normalized_numeric` 是可空字符串；null/无效值不能当作 0。Context/unit 来源保留期间和维度信息，不保证每份申报归并出唯一财务数字。当前 ticker 映射不是完整历史 CIK/ticker PIT 映射，档案表也不会连接到 daily v3 labels/features。

Query 会逐块读取文件来核对 hash，并检查档案关系、Parquet 字段结构和 footer 中的行数；不会把整张表读入内存。这只是文件/metadata 检查，**不等于** `verify` 对所有 core 记录做的完整校验。SQL 限于一条可信的本地 SELECT/CTE；写入语句、多条语句和 PRAGMA 会被拒绝。DuckDB 不能访问任意外部文件，只读取档案已允许的表文件。这是供本地使用的工具，不是公共 SQL 服务。`--limit` 只限制返回行数；统计或排序仍可能读取较多输入。

命令不会自动选择“最新申报”，也不会替用户回答某个历史日期当时能看到什么。Amendment 有独立的 filing ID；查询不会把 amendment 合并覆盖到原申报。若要查看某日之前可见的申报，请设置自己的日期并显式筛选 `effective_visible_session`，例如 `effective_visible_session <= CAST('$AS_OF_DATE' AS DATE)`。该字段按“披露日严格之后的首个 XNYS 交易日”计算，但不保证截止日前所有文档或后续处理都已完成。申报身份使用 CIK/accession；当前 ticker 映射不包含完整历史关系。

## 尚未完成的部分

Parser 不会生成 ratios、encoder features、训练输入或 daily sample join。HTML text sections 是启发式抽取，不是字节偏移。PDF 保留为原始字节并明确标记 unsupported，不臆造文本或数值。XBRL `full` 仅表示 parser 自身的结构/来源检查通过，不代表 SEC/EFM 合规或财务报表正确。格式错误/无效事实可以保留原始词面内容，而数值解释保持 null/invalid，不得视作 0。

这仍是一个有界 parser prototype，不是广泛发行人覆盖。2010 AAPL XBRL 失败若要重试，必须先审查并批准其 allowlist 外依赖；不能隐式扩大 host 范围。可重试的 partial/failed attempt 尚未做 exact rerun/idempotence。此流程不声称覆盖完整公司或全部附件，不提供完整历史 ticker/CIK PIT 映射，也不判断财务/经济正确性。保留的 `data/output/` 是历史 `samples_v3` artifact，与此私有 archive 分开；pilot 未修改或合并到该 bundle。本 archive pilot 没有添加财务模型列、重建样本包或训练 encoder。
