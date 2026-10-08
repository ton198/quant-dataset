# 非 XBRL 财报双模型转换与审核架构

- 状态（2026-10-04）：原设计提案正在分阶段实施；M1 尚未完成，准确实现边界见 [非 XBRL 财务抽取实施说明](financial-extraction.md)。
- 已实现 SEC envelope helper 的 parser-neutral 移动；processing contracts、planning/validation 已窄化拆分。当前仍由 `processing.py` 拥有 active state、IXDS resolution 与主要提交事务，不是完整物理迁移。
- `financial_extraction` 已有来源无关 DTO/校验、有界 evidence、显式数值政策及 SDK-free fake/replay runtime；controller/SEC adapter 仍在进行中。当前不调用模型、不处理真实 filing、不发布财务抽取数据。
- 核心目标仍为：冻结来源和 coverage，由模型 B 先盲审原始来源，模型 A 再转换，代码作确定性校验，B 独立审核 A 输出，并在有界且受合同约束的修订后决定人工审核状态。实现状态不能由该设计目标代替。
- 模块现状和审计证据见 [src 目录职责与结构审计](source-layout-audit.md)。全仓重组不是财务抽取 M1 的前置条件。
- Provider/model、文档外发权限、预算及指标定义仍需明确审批；本设计不是授权。

## 1. 目标与边界

将非 XBRL 财报转成可追溯、可审核、可与 XBRL 数据共同查询的结构化财务数据。AI 负责跨布局的语义理解，不维护逐公司适配器；代码负责来源安全、数据合同、数值计算、预算和发布。

首期支持非 XBRL HTML 的主财务报表及相关正文，重点是利润表、资产负债表、现金流量表。采用少量明确的标准指标定义；其他已识别数值可保留为未映射观察值。不承诺任意财务披露的完整抽取。

不替换原 XBRL/Arelle/IXDS；不伪造 XBRL QName、context 或 validity。不改当前样本、旧财务 organizer 或已发布财务产物。图片/OCR、多模态、legacy SGML 纯文本的列重建另期评估。两个模型同意不等于事实正确，也不等于整份财报完整覆盖。

## 2. 现有系统约束

当前存在三条分离路径：

1. `src/filings/` 文档归档：保存 XBRL 原始 occurrence facts 和 text sections。
2. SEC Company Facts/submissions organizer：产生 `financial_events_v1`，不是从 filing archive 的 facts 派生。
3. 当前无财务样本行为：目标独立为单一 `samples` 包（`__init__`、`builder`、`query`、`contracts`、`validation`），不设旧样本代际选择或兼容入口。AI 产物不会自动进入训练样本；结构与清理见 [Samples 架构](samples-architecture.md)。样本 code lane 尚需独立验证。

`src/download/financial_events.py` 的标准概念来自 organizer，现有九项为：`revenue`、`gross_profit`、`operating_income`、`net_income`、`operating_cash_flow`、`capital_expenditure`、`assets`、`liabilities`、`equity`。建议首期 registry 对齐这些名称，但逐项定义期间、口径、单位及符号政策，不能仅复用名字。例如 capex 的原始现金流出符号与派生正值政策必须区分。

现有 event/fact 合同有 event/version、申报日期、可见交易日和期间字段；其 `value` 为 float64。新增产物保留 Decimal 字符串和来源类型，不直接冒充 Company Facts 行。向旧合同的导出及精度转换是后续显式适配，不在原表中填造假的 taxonomy/tag。

仓库目前没有模型 SDK、provider client、模型响应缓存或 token/费用预算系统，需要新增小型基础设施。

## 3. 总体拓扑

```text
filings：已验证 archive snapshot + 不可变 CAS
                   |
           来源冻结 / 财务范围检查
                   |
        +----------+----------------------+
        |                                 |
   XBRL / iXBRL                       非 XBRL
   原离线解析器                    filings adapter
        |                      SEC 解包、身份与来源绑定
   原 facts/dependencies                  |
   保持原样                        VerifiedDocument
                                          |
                        financial_extraction（独立兄弟包）
                        通用 blocks -> 分段 discovery
                                          |
                                  模型 A -> 校验 V
                                          |
                              模型 B：独立语义与完整性审核
                                          |
                        +-----------------+----------------+
                        |                 |                |
                       pass             需修订           证据不足
                        |                 |                |
                        |           A 受限 patch       abstain/review
                        |           V -> B 再审核
                        |           最多两次修订
                        |
                  ExtractionResult
                  观察值、映射、审计、lineage
                        |
                 返回 filings adapter
                 最终校验、head 重核、原子发布
                        |
                 显式统一查询 / 下游适配
```

依赖方向是 `filings adapter -> financial_extraction.workflow -> domain/evidence/runtime`。通用提取包不得导入 `filings`、`download`、samples 或 `ArchiveWriter`，也不理解 CIK/accession、SEC 封装或 archive 表路径。

通用接口接收 `VerifiedDocument/EvidenceBundle`：已验证 payload、opaque source identity、原始来源 hash/定位映射及 coverage plan；返回 `ExtractionResult`，不写归档。CIK、accession、公开时间及 SEC/native metadata 由 adapter 绑定。

模型执行、纯验证、archive 发布是三个不同边界。推理时不持有 archive writer lock；模型没有 writer、shell、SQL、下载或外部工具权限。SDK 只在 runtime 显式使用时加载，不在 CLI 注册或 package __init__ 中初始化。

## 4. 来源与预处理

### 4.1 EvidenceSnapshot

由 filings adapter 固定 input snapshot、RunSpec、scope/selection 证据、approved document IDs、SEC URL、CAS hash/size、预处理版本及 coverage plan。通用提取包仅接收已批准的来源投影及 opaque IDs；不读取 archive、不自行找附件或扩大处理范围。

当前范围规则仍独立存在：included 可以进入默认财务转换；candidate 可以显式进行分析但不自动转为 included；excluded 默认跳过。HTML/AI 抽取不修改 filing scope 或 raw completeness。

### 4.2 Source blocks

SEC 来源处理归 `filings/source/sec_envelope.py`：先由 host 校验路径、raw hash、封装和编码，产生 verified payload 与原始文档定位映射。初期从 `parse_text.py` 抽取既有行为，保留兼容入口，不同时改变封装政策。

`financial_extraction/evidence/blocks.py` 只处理通用 HTML/文本 payload，复用 lxml 输出标题、段落、列表、表格、物理单元格、原始 spans、脚注和相邻上下文。source envelope 应同时记录 raw hash、payload/规范表示的版本化 hash 及映射，不能把解包后的 bytes 冒充原始 SEC bytes。SEC 身份和附件规则不进入通用 blocks 模块。

每个 block/cell 有稳定 ID，绑定 document/hash、预处理版本和定位。保留最近祖先 table 归属与嵌套关系；span 展开引用不复制数值。限制文件大小、DOM 深度、cell 数和展开网格总量，禁止外部资源加载。

block 字符范围是规范化证据表示中的范围，不冒充原始字节位置。保留原始 CAS 及 payload 边界，供审计还原。DOM recovery、不可解码片段和静态隐藏处理的限制必须显式记录。

不在此层实现完整财务表头、期间、币种和指标规则。CSS-only 布局或复杂文档的结构恢复是否足够，须由 pilot 验证；不足时报告限制，不承诺文本预处理能恢复全部视觉布局。

### 4.3 分段与完整性

先对冻结 blocks 分段 discovery，再组装候选正文、表头、单位声明和必要脚注供转换。模型 B 的窗口依据 coverage plan，由控制程序构造，不能只看 A 选中的引文。

保存 planned/scanned/unscanned/omitted 文档和区域，明确原因。分段上下文不足可在 approved sources 内扩展，并计入预算。预算耗尽、截断或 discovery 未完成意味着 coverage partial/unknown，不能以一个成功片段代表全文完成。

## 5. 数据合同

通用 `financial_extraction.domain` 拥有观察值、指标映射和审核意见合同，不依赖 SEC/native schemas。域合同使用 opaque source/document IDs；filings adapter 负责映射和验证 filing_id、document_id、CIK/accession、申报时间及 Arrow 外键。下表包含通用字段及 host 持久化投影，不能把所有 SEC 字段变成其他来源也必须填写的合同。

Company Facts 的 tag 优先顺序是来源专属映射，不等于通用指标定义。旧 core、parser 与 financial_events_v1 schemas 保持独立，不把新合同混进原 XBRL FACT_SCHEMA。

### 5.1 financial_observations

| 字段组 | 内容 |
| --- | --- |
| 通用身份 | observation_id、opaque source/document IDs、source_type、run_id、revision |
| SEC host 投影 | filing_id、document_id 及来源绑定；其他来源通过自己的 adapter 提供对应关系 |
| 来源 | source_hash、block/cell IDs、引文及字符范围、原始行列标签 |
| 数值 | raw lexical value、displayed numeric、quantity kind、sign/scale、normalized numeric、转换链 |
| 期间 | instant/duration、period_start/end、期间证据及 resolution status |
| 单位 | currency/unit、单位与倍率证据及 resolution status |
| 口径 | statement type、consolidated/segment、GAAP/non-GAAP、restatement qualifiers |
| 质量 | source_validation、semantic_status、审核范围、未解决问题 |

数值采用 Decimal 计算、字符串保存。模型返回原始数值和解释；代码计算最终换算值。金额、股数、EPS、百分比分开处理；`$` 不自动等于 USD；空白、dash、N/A 不自动等于零。

reported 与 derived 分开；首期以 reported values 为主。单位换算不是创造新的财务披露，转换步骤仍须记录。AI 执行时间只属于审计，不是财务期间或公开可用时间。

### 5.2 financial_mappings

绑定 observation、metric_id、registry version、期间/单位/口径兼容性、映射证据、接受或拒绝理由。Registry 定义不仅是指标名，还包括意义、必要限定条件和允许单位。未知指标可以保留 observation，不强制归类。

统一查询适配引用真实 XBRL occurrence/context/unit，或 AI observation/mapping；保留 `source_type` 与验证类别。XBRL 已转换的 scale/sign 不重复应用。重复披露保留 occurrence；派生来源选择层处理主表、MD&A、新闻稿间重复，不盲目求和或按 label/date 合并。

### 5.3 审核意见

```text
issue_id、typed code、severity
目标 observation/mapping、field path
证据 IDs/spans、问题说明
可选修改建议、受影响上下文、解决要求
```

典型 code：WRONG_COLUMN、WRONG_PERIOD、SCALE_ERROR、CURRENCY_UNSUPPORTED、SCOPE_MISMATCH、MISSING_OBSERVATION、RESTATEMENT_CONFLICT、METRIC_DEFINITION_MISMATCH。

## 6. 双模型流程

### 6.1 模型 A：转换

输入来源证据、coverage plan、metric registry；输出 observations、mapping proposals、已处理范围及 abstentions。不修改来源、scope、registry、发布状态；不凭公司背景补币种，不心算最终规范金额。

### 6.2 确定性校验 V

封闭且版本化的 JSON 合同，严格校验 keys/types/enums、长度和数量；白名单校验文档与证据 ID；原文逐段匹配；Decimal、符号、倍率和百分比换算；检查期间合法性、外键、来源、重复身份及口径冲突。

这些是必要条件，但不是语义正确性的证明：真实的数值引用也可能绑错列或期间。

### 6.3 模型 B：审核

B 使用冻结原始来源和独立 coverage plan。对高风险核心值先不看 A 的答案，独立识别期间、单位和数值，冻结为 blind baseline；再核验 A 的结果及漏项。

B 输出 `pass`、`revision_required` 或 `insufficient_evidence`，并附有证据的问题清单。B 的引文和建议同样必须经过 V。B 不能直接写结果；A 可依据原文修订、提出有证据的异议或放弃。

不同模型家族可以减少部分相关错误，但不能保证统计独立。两个模型一致的漏项、错口径仍需要留出集评测识别。

### 6.4 有界修订

默认初次转换加最多两次语义修订，可配置更低。A 提交绑定 candidate hash 的 patch，只修改 issue 涉及字段，或按 missing-item issue 增补；不得修改来源、scope、schemas、registry 或审核结果。需要更大改动时明确申请/隔离，不能静默整份重写。

同表头、期间、单位、口径共享关系组成 impacted set，patch 后重新验证这一集合及 mappings。最终审核绑定最终完整输出 hash；之后任何改动都必须重新验证和审核。

候选 canonical JSON/stable ordering 用于 hash。重复旧输出、振荡、同问题持续、轮次/预算耗尽，停止并隔离，不无限采样直到审核通过。所有版本、patch 和 issue 保留 lineage。

## 7. 状态与发布政策

```text
prepared -> converted -> validated -> audited
                               -> revision_requested -> revised -> validated -> audited
```

终态按处理单元及 observation/mapping 分开：

- accepted：来源、数值、语义审核和配置的人审要求满足。
- quarantined：候选存在，但冲突、审核不完整或预算/轮次上限未解决。
- abstained：证据不足，明确不作解释或映射。
- failed：来源安全、执行或输出合同无法成立。

run 另报执行状态与 coverage；部分 accepted 不等于整份 filing accepted。默认消费者只读取满足接受政策的映射，候选和隔离数据通过审计入口查看。未知币种可保留原文观察值，但不能晋升需要币种的标准金额事实。

会计勾稽只在期间、单位、范围一致时运行，一般作为 warning/review；不能为平衡而改源数字。模型 confidence 或双模型 agreement 不单独决定发布。pilot 人工验证后才能批准明确的自动接受范围。

## 8. 时间、版本与下游边界

保留 issuer、accession、filed_date、来源 acceptance datetime、report periods、amendment/restatement lineage。可见交易日由版本化确定性政策推导，不由 AI 选择。AI produced_at/reviewed_at 不得替代公开可用时间。

后续财报披露的比较历史数字，仍属于后续申报事件；不能回填到当年的可见日期。不能仅按 report_period_end 覆盖旧版本。新 AI 事实对现有金融事件的投影需要显式来源类型、版本和精度政策，不覆盖 `financial_events_v1` 或旧 CSV。

单一 `samples` 保持当前不接财务的行为。独立 samples lane 负责迁包及删除旧样本逻辑，不增加财务特征；财务接线、特征定义和 PIT join 属于未来独立变更与验收。

## 9. 运行、安全、缓存与预算

采用可注入的最小 ModelClient，A/B 分别配置，不预选 provider。现有 parse 默认离线；新增 AI 阶段必须显式授权发送文档和计费。

文档是 untrusted data；模型无工具执行权限。指令和证据分离；输出仅允许合同字段及白名单 ID，不能用作路径、SQL、命令或 scope 更新。请求/审计不保存 API key、认证头或环境变量。

区分 transient transport retries 与语义修订；二者有界，所有真实调用均计入预算。设置输入/输出 tokens、总调用数、并发、费用上限。费用预估需要 provider 价格配置，实际 usage 要记录；不承诺未经测量的成本。

缓存 key 包含来源内容/segmentation、模型及可用 revision、推理参数、prompt/schema/registry/config hashes。审核缓存另绑定 candidate hash、blind baseline 和审核政策。错误响应不是通过缓存；任何选择性重试也必须审计。

保存精确请求、原始响应、usage、各轮 hashes、issues 和最终选用响应。固定 temperature 不保证新生成可复现；精确复现依赖 response replay。模型 revision 无法固定时显式记录限制。

## 10. 存储与原子发布

新增 optional manifest-listed tables：`source_blocks`、`financial_observations`、`financial_mappings`、`ai_runs`、`ai_issues`。具体 schema 在 M1 固化。请求、响应、审阅及修订对象写入内容寻址对象存储，audit rows 标明对象角色；不得与 SEC 原始响应混称。

推理工作目录与 active archive 分离。暂存不代表已发布。最终发布时重新检查 head/source/scope bindings 和完整合同；并发冲突不覆盖别人的更新，必要时使用已有响应重新验证。

filings adapter/host 是该 AI 流程的唯一发布拥有者；通用提取包只返回结果。利用 `ArchiveWriter.commit()` 的 optional table 和原子替换能力，仅提交真正变化的表。旧无 AI 表的归档继续可读；各产物带 schema/policy version。不改全局 `_PROCESSING_CONTRACT`、现有 `_parse_id`、IXDS 身份、core/XBRL schemas 或历史 snapshots。

分包不代表迁移 archive/CAS/table 数据路径，不建立第二套归档读写合同。未来其他来源通过各自 host adapter 接入，不能让通用工作流偷偷获得 SEC writer 权限。

## 11. 独立提取包与 host adapter：当前进度

以下是 2026-10-04 的源码状态，不是已完成的产品流程。`financial_extraction` 已开始形成来源无关的独立兄弟包；当前各区域职责如下：

```text
src/
├── financial_extraction/                 # 来源无关的财务抽取合同与运行时
│   ├── domain/                            # DTO、数值政策、合同/evidence/patch 校验
│   ├── evidence/blocks.py                 # 有界 HTML blocks、物理 cells、coverage plan
│   ├── runtime/client.py                  # typed requests、fake client、response protocol
│   ├── runtime/execution.py               # 显式预算、调用 lineage、内存 replay
│   └── workflow/                          # controller 集成进行中；API 尚未稳定
└── filings/
    ├── source/sec_envelope.py             # 已共享的 SEC envelope/encoding helper
    └── adapters/financial_extraction.py   # SEC/archive 绑定进行中；未开放发布流程
```

可用的核心是领域 DTO/校验、确定性 evidence 预处理与 fake/replay 骨架；它不是完整 A/V/B workflow。请勿依赖未完成 controller/adapter 的内部 API。当前不支持将真实 filing 交给该链路，即使使用 fake/replay，也没有 supported extraction command 或 publication route。详情、合适的唯一离线 fixture 和已知限制见 [实施说明](financial-extraction.md)。

目标合同仍是 `VerifiedDocument/EvidenceBundle` 输入与 `ExtractionResult` 输出。`VerifiedDocument` 不自行证明来源已验证；SEC/native identity、原始 CAS/hash 验证、可用性/来源 scope 与输入冻结属于 host adapter。通用领域代码不应导入 `filings`、`download`、samples 或 archive writer，也不持有 CIK/accession 或 archive 路径。候选状态不能隐式晋级；模型流程不改变 SEC scope、raw coverage 或公开时间。

目标 M1 workflow 必须先冻结 evidence/完整 coverage，让 B 在未看到 A 输出时盲审原始来源，再由 A 转换；代码确定性校验后，B 再独立审核 A。有限 patch 必须严格受授权 issue/path 和多个内容 hash 约束，并在最后执行校验、来源审计与输出 hash/振荡预算检查。当前实现与目标 gate 的差距及必须人工审核的 fixture-only 状态，见实施说明；目标不表示 gate 已通过。

原子发布、可选 archive tables、查询/核验集成以及真实模型 provider 都仍待后续阶段。Provider SDK/模型、数据外发权限和预算未选定或授权；不应注册 CLI 选项、依赖或可运行示例来暗示这些能力现已存在。M1 的 fake tests 不构成模型质量或真实财务数据正确性证据。

AI lane 首期不改原 XBRL parser、text parser、Company Facts organizer、`financial_events_v1` 或当前样本业务语义。样本迁移与 filing/parser 整理是独立 lane；AI pilot 不要求先完成全仓重组。处理模块现状见 §14–15。

## 12. Shopify 个案

已检查 accession `0001594805-24-000072` 的完整目录、primary 和 EX-99.1：内容为 CEO automatic securities disposition plan，不属于本项目主财务报告范围。建议财务 scope excluded，但实际 archive 仍为 candidate/partial。

正式修改需独立显式 scope-review：将临时 EX-99.1 校验 hash 后入 CAS，保存正文、目录审阅及原子排除证据。保留原始文件和历史解析，不提升 raw coverage。AI 可建议，不得自行提交 scope 排除；不能用新闻稿/售股关键词黑名单代替审阅。

## 13. 验收与里程碑

### M1：合同和离线控制器

固化 schemas、metric definitions、证据 ID、状态、patch 协议和预算。用 fake A/B client 测试：正确通过、修订后通过、B 无依据意见、漏项、跨来源证据、错倍率/期间、越权 patch、附带修改、振荡、预算耗尽、transport retry、缓存/replay、并发及发布故障。纯验证测试无需真实 API。

### M2：双模型接入

显式配置 converter/reviewer，验证无秘密泄漏、请求限额、usage/audit 和 replay。真实调用仅在模型/provider/外发权限与预算获批准后执行。

### M3：真实模型 pilot

冻结 CNR 2024 Q3、MSFT 2008 样本，加入跨公司/年份留出集和 Shopify 负例。人工标注数值、出处、期间、倍率、币种、口径和目标指标；冻结 prompts/registry 后评估留出集。

测量转换 precision/recall、审核 error detection/false-pass、漏项、source/period/scale correctness、abstention、coverage、实际 tokens/成本。accepted pilot core 要求没有已知金额/期间/倍率错误；这是验收要求，不是已达到的准确率保证。其他上线门槛根据 pilot 设定，不以成功示例代替评测。

### M4：发布与回归

原子发布、审核查询、verify、重复发布 no-op、响应重放和历史可读全部通过。固定 v15 基线：25,656 XBRL facts、95 sections、159 dependencies 的完整投影不变；RBC 仍一个 primary-anchored IXDS parse、7,543 occurrences，无 solo 恢复或重复。原 scope/raw coverage 和公开时间字段不因 AI 执行改变。

### 实施前待确认

1. converter/reviewer provider、模型及可用固定 revision。
2. 允许外发的公开来源范围、单次/批次预算与并发。
3. 首期九概念 registry 的精确定义和符号/期间政策。
4. 哪些结果必须人工批准，以及 pilot 后自动接受范围。

本设计不构成上述权限或预算的默认授权。

## 14. 现有 parsing 的职责整理

采取有明确行为边界的窄迁移，不按文件行数或目录外观一次性搬迁。以下最小重组已进入源码，同时保持 parser versions、输出、身份、fingerprint 输入和既有兼容导入；它不是全部 processing 的物理拆分。

### 14.1 已实施的最小结构

1. **共享 SEC 来源合同**：将 `parse_text.py` 原有 envelope、编码和 PDF wrapper helpers 移入 `filings/source/sec_envelope.py`，公开 `extract_sec_envelope`、`sec_envelope_encoding` 与 `is_complete_sec_pdf_envelope`。`parse_text` 保留旧私有名称作为兼容入口；`parse_xbrl` 使用来源中立模块。封装安全、legacy/unsupported 区分不变。
2. **processing 合同所有权**：`filings/processing_contract.py` 拥有 `PARSES_SCHEMA`、`DEPENDENCIES_SCHEMA` 及相应 parse identity helper。序列化、指纹算法、`_PROCESSING_CONTRACT` 内容和版本不变；这是职责所有者调整，不是格式升级。
3. **纯规划**：`filings/pipeline/planning.py` 提供 `select_filings`；它通过注入的 pending-work predicate 选择显式或待处理 filing，不修改 archive state。IXDS resolution、active state 与结果协调未整体迁入该模块。
4. **来源校验**：`filings/pipeline/validation.py` 提供解析 facts 的物理 source/hash 校验和 group physical-owner provenance 标记。processing 仍拥有 active-state validation、IXDS 恢复与原子提交。
5. **RunSpec 只读恢复**：authenticated RunSpec loader 归 archive 边界；acquisition 的旧入口保留兼容委托，并继续校验 canonical/hash。

`parse_archive()` 仍是确定性解析唯一事务/提交拥有者。IXDS identity、group recovery、active-state 更新、Arelle load/serialize/close 生命周期及主 commit 仍位于 `processing.py`，其余大范围物理迁移按需延后。不能让多个模块各自决定或发布 processing state。

### 14.2 完整目标分层，延后按需物理迁移

| 目标区域 | 当前模块与处理原则 |
| --- | --- |
| core / contracts / storage | models、parsing_models、archive 保持合同独立；parser models 不并入 archive models |
| source | SEC envelope；以后按需加入明确的格式判别 |
| parsers | 未来 text.py、xbrl.py；最初不搬实现 |
| runtime | 未来 packages、dependencies；可信 workspace 与 taxonomy preparation 仍分工 |
| pipeline | `planning.select_filings` 与 source validation 已拆；active state、IXDS resolution/恢复、`parse_archive` 唯一事务和提交仍由 `processing.py` 拥有 |
| transport | config、sec_client、单文档 download，后续按需归类，不混同 taxonomy preparation |
| SEC metadata / domain | catalog、document_selection 暂留，保守 metadata triage 不变成 AI scope 决策 |
| entrypoints / consumers | __init__、cli、query、verify 保持稳定入口 |
| trusted vendor | sec_transforms.py 与 _vendor/sec_transforms 原位置保留 |
| AI adapter | adapters/financial_extraction.py，与独立提取包单向连接 |

acquisition 可以提取 scope/evidence 的纯政策函数，但来源连续性、验证和 scope 更新仍由下载事务协调。archive 可按需抽纯 validator，reader/writer 必须共用同一合同，不能形成两套规则。

## 15. src 其他区域与迁移证据

### 15.1 不强制全仓改名

- `download/financials.py` 获取 Company Facts/submissions，`filings/acquisition.py` 获取 filing inventory/文档：对象与合同不同，不合并。
- `financial_events.py` 借用 organizer 私有 `_CONCEPTS` 应逐步改为公开版本化合同；通用指标定义与 Company Facts tag 选择优先顺序分开，后者保持原顺序。
- `filings/query.py` 与 `samples/query.py` 已共享公开窄接口 `query_support/readonly_sql.py`；领域输入验证仍分别保留。
- Samples 已从旧顶层模块迁入 `samples.builder/query`，并有单一 contracts/validation 模块；`filings/query` 使用中立共享 SQL guard。CLI、tests、Ruff 与 setuptools 包发现已同步。当前产品合同统一为 finance-free `samples`，不留代际选择或旧路径 re-export。
- `freeze_v1_baseline.py`、`verify_projections.py`、`calibrate_coverage.py` 及旧财务样本专属 helpers/tests/docs 已退出当前源码/测试路径。当前契约使用独立小 fixtures、手算预期值和安全/时间/内存不变量，不以旧跨代 projection 为 gate；混合测试按用例处理。实现边界与当前 gate 状态见 [Samples 架构](samples-architecture.md)。
- 不在本次强制建立全仓 `quant_dataset.*` namespace。缓存、egg-info 是生成物，不参与业务重构。

### 15.2 优先级

| 优先级 | 批次 |
| --- | --- |
| M1 / P0（进行中） | `financial_extraction` domain/evidence/runtime 脚手架已有；workflow/SEC host adapter 和验收 gate 尚未完成。共享 SEC source helper 已实施。 |
| P1（窄迁移已实施） | processing contracts、planning/source validation、archive 公开 RunSpec loader；`processing.py` 仍保留 active state、IXDS 与唯一 commit owner。 |
| Samples P0 独立 lane | 已完成：唯一样本合同、fixtures、删除范围、共享 SQL guard 与两个 consumer 迁移 |
| Samples P1 独立 lane | 实现已完成：samples 五文件包、旧逻辑与工具清理、caller/CLI/packaging/tests/docs 同步；当前 correctness/source-consistency gate 由 task owner 单独验收 |
| P1 独立批次 | Company Facts 指标合同的私有跨模块依赖，保留来源专属 tag 优先顺序 |
| P2 | parser/runtime/transport/catalog workflow 的物理迁移，仅在职责稳定后 |
| 延后 | 全仓根 namespace；不为本轮 samples 分包强制改名 |

保留 AI M1–M4 的顺序；samples 和 parsing 是独立工作线，各有 evidence gate，不要求先完成 samples 或 P2 才开展 AI pilot。旧样本工具不再设计为需要长期维护的历史产品。

### 15.3 每批迁移 gate

本节旧导入/patch 兼容要求针对 filings。samples 不保留旧模块入口或旧财务 helpers，测试同步到新路径，以实际 I/O 禁止断言替代旧 helper 空壳。Company Facts、archive/parser 和 AI 的技术身份不因取消样本产品代际而删除；当前样本 schema/hash/输入配置验证仍保留。数据删除单独盘点确认，本轮不处置 baseline/backup。

1. filings 先 characterization，再只提取一种职责；核对旧 public/private imports、CLI 及 monkeypatch seams。
2. 简单 `from new_module import function` 不会改函数 __globals__，也不会让旧路径 patch 自动影响新模块 lookup。保留需要的动态入口或显式注入依赖，测试不能仅验证 import 成功。
3. 保持 filing/document/parse IDs、schema 序列化、parser versions、group fingerprints、cache/no-op、来源 owner 和旧 snapshot 可读性。v15 完整表投影不变，不迁移数据路径。
4. wheel 安装、CLI --help、缺少 optional extras 的基础导入均应通过；注册 AI 参数不能初始化 SDK。
5. sec_transforms 根据自己的 __file__ 寻找 _vendor，并校验 pinned manifest/files。保持 loader、vendor、license、package-data 路径和 hash pins，不因整理改变 transform 身份。
6. 分别验证 source/processing/acquisition/query 对应 tests，最终执行完整项目 gate；模型 pilot 评测不能替代这些确定性回归。

AI M1 与进一步 filings 重组仍是独立实施工作线；financial-extraction 的现有脚手架不等于完整 workflow 或财务产品。Samples P0/P1 已在源码树实现，但其最终测试/source-consistency gate 由各自负责人核验。本文区分已落地模块、进行中的实现和未来设计，不以文档或 fake tests 宣称金融抽取正确性。详见 [financial-extraction 实施说明](financial-extraction.md)。
