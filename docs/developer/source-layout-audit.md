# src 目录职责与结构审计

状态：历史快照与当前源码树并列。下方旧树明确记录 2026-10-03 samples 迁移前的布局；当前实现树按 2026-10-04 worktree 核对。本文不代表 samples gate 已通过，也不声称改动了原始数据。Samples 架构见 [Samples 单一产品架构](samples-architecture.md)；非 XBRL 抽取的当前实现边界见 [实施说明](financial-extraction.md) 与 [非 XBRL AI 架构](non-xbrl-ai-architecture.md)。

**Samples 迁移状态：** 五模块 `samples` 包与 `query_support.readonly_sql` 已在当前源码；旧顶层样本模块、freeze/projection/calibration 工具及旧财务样本 helpers 已退出。当前合同为单一 finance-free `samples`，不保留产品代际选择或旧入口/shim。旧 output、backup 与 baseline 数据仍按历史事实原样保留。

**Financial-extraction 状态：** `financial_extraction` 已有 domain/evidence/runtime 脚手架；workflow 与 SEC host adapter 正在实施，没有受支持的真实文件抽取或 archive 发布路径。独立门槛和限制见 financial-extraction 实施说明。

## 1. src 顶层树

### 1.1 迁移前历史快照（2026-10-03）

```text
src/
├── cli/
├── download/
├── filings/
├── build_samples.py
├── query_samples.py
├── freeze_v1_baseline.py
├── verify_projections.py
└── calibrate_coverage.py
```

此树只作迁移前证据，不能当作当前源码结构。

| 迁移前位置 | 当时作用 | 当前状态 |
| --- | --- | --- |
| `build_samples.py` | 顶层 samples builder | 已迁入 `samples.builder`；无旧导入兼容壳 |
| `query_samples.py` | 样本产物只读 SQL 查询 | 已迁入 `samples.query`；SQL 安全 guard 在 `query_support.readonly_sql` |
| `freeze_v1_baseline.py` | 冻结/核验 v1 baseline 的工具 | 已删除；历史数据单独保留 |
| `verify_projections.py` | 跨代 projection 工具 | 已删除；不作为当前正确性 gate |
| `calibrate_coverage.py` | 历史财务样本 coverage 诊断 | 已删除；不属于当前 samples 合同 |

### 1.2 当前源码树（2026-10-04）

```text
src/
├── cli/
├── download/
├── filings/
│   ├── adapters/                 # SEC/archive host adapters；financial extraction 进行中
│   ├── pipeline/                 # 窄规划与来源校验拆分
│   ├── source/                   # parser-neutral SEC envelope
│   └── processing.py             # active state、IXDS resolution 与提交仍在此处
├── financial_extraction/
│   ├── domain/                   # DTO、数值政策与校验
│   ├── evidence/                 # 有界 HTML evidence/coverage
│   ├── runtime/                  # fake client、预算和内存 replay
│   └── workflow/                 # M1 controller 正在实施，尚无稳定 public workflow
├── query_support/
│   └── readonly_sql.py
└── samples/
    ├── __init__.py
    ├── builder.py
    ├── contracts.py
    ├── query.py
    └── validation.py
```

`src/` 当前无 `build_samples.py`、`query_samples.py`、`freeze_v1_baseline.py`、`verify_projections.py` 或 `calibrate_coverage.py`。Setuptools 使用 package discovery；CLI console script 指向 `cli.main:main`。样本实现/调用路径同步完成；当前 fixtures 与 source-consistency/full validation 的结论由任务负责人单独确认，不在此文宣称通过。金融抽取目前仅有离线脚手架，不能据此声称有真实 filing workflow 或发布产物。

| 当前位置 | 当前作用 | 放置判断 |
| --- | --- | --- |
| `cli/main.py` | quant-dataset 主 CLI，连接 download、samples 和 filings 命令 | 入口包合理；只做命令编排，不应承载解析或模型调用实现 |
| `download/` | 市场、宏观、SEC Company Facts/submissions 获取与 organized 数据产物 | acquisition 与组织逻辑都在其中，名称覆盖不完整；与 filings 的文档获取不是重复代码 |
| `filings/` | SEC filing 文档目录、获取、归档、解析、查询、验证 | 领域包合理；内部平铺导致不同层级不够显眼，存在具体交叉职责 |
| `samples/` | 单一 finance-free `samples` builder、contracts、strict query 与 validation | 已实现的职责包；无旧顶层 re-export/兼容入口 |
| `query_support/readonly_sql.py` | samples 与 filings 共用的 SELECT/SQL guard | 已实现的窄共享模块，不承载 query runtime 或领域输入验证 |
| caches / egg-info | 编译缓存、打包元数据（若存在） | 生成物，不纳入业务模块重组 |

当前样本行为不消费财务；filing archive facts 与 Company Facts 的 `financial_events_v1` 也不是同一套产物。目录整理不能隐含地把这些路径接起来，也不能抹掉独立财务 artifact 的既有合同。

## 2. download 模块地图

| 模块 | 职责 |
| --- | --- |
| `manager.py` | 下载/组织阶段编排，按 ticker 调度及并行处理 |
| `config.py` | 数据源 TOML 配置与下载凭据模型，不是 LLM 配置 |
| `market.py` | Yahoo 市场数据获取 |
| `macros.py` | FRED 宏观数据获取 |
| `financials.py` | SEC Company Facts、submissions 及历史 submissions page 获取与缓存 |
| `universe.py` | ticker/CIK universe、映射及 overrides |
| `organize.py` | market/macro 组织及 metadata，同时兼容 re-export organize_financials |
| `organize_financials.py` | Company Facts 标准概念选择、旧 financials.csv 和 event artifact 组织入口 |
| `financial_events.py` | financial_events_v1 schemas、申报事件、期间/版本筛选和 Parquet 产物 |
| `progress.py` | 下载进度、锁、状态支持 |
| `errors.py` | 下载领域错误类型 |
| `__init__.py` | 下载 package facade 与部分错误导出 |

实际边界问题：financial_events 导入 organizer 的私有 `_CONCEPTS`。共享概念定义应有明确合同所有者，但不必为了这个问题迁移整个 download 包。organize 的财务 re-export 在 pyproject 中被明确标注为兼容入口，不能随手删除。

## 3. filings 完整模块地图

### 3.1 纯解析与输出合同

| 模块 | 职责 |
| --- | --- |
| `parse_text.py` | 离线正文/HTML/legacy text -> sections；保留 SEC envelope helpers 的旧私有兼容入口 |
| `parse_xbrl.py` | 离线 XML/XBRL/iXBRL/IXDS -> occurrence facts；preflight、Arelle 运行和来源转换 |
| `source/sec_envelope.py` | parser-neutral SEC envelope、编码、PDF wrapper helpers；`parse_xbrl` 使用此模块，旧 `parse_text` 名称仍兼容 |
| `parsing_models.py` | FACT_SCHEMA、SECTION_SCHEMA、ParseResult，不依赖 archive/catalog |
| `sec_transforms.py` | 校验受信任 vendored SEC transformation plugin 与身份 |
| `_vendor/sec_transforms/` | 上游 transform 实现及 text2num、license/NOTICE/UPSTREAM 资产；按一个 vendor 单元保留 |

原有 `parse_xbrl -> parse_text` 私有 envelope 依赖已改为对 `source/sec_envelope.py` 的共享调用；旧 helper 名保留以满足既有导入/patch seam。没有证据表明两个 parser 已有双向循环导入。纯 parser 不直接写 archive，这个边界应保留。

### 3.2 来源、目录与获取

| 模块 | 职责 |
| --- | --- |
| `sec_client.py` | SEC/taxonomy HTTP transport、URL/user-agent 安全、限流与重试 |
| `config.py` | SEC user-agent 加载与配置错误 |
| `catalog.py` | 从本地 submissions cache 建立 filing 元数据、identity 和可见日期 |
| `document_selection.py` | index/detail inventory 枚举、文档选择与 metadata-based 财务范围 triage |
| `download.py` | 获取单份已选择文档的小型 primitive |
| `acquisition.py` | archive 级下载工作流、inventory/来源证据、scope transitions、coverage 和写入 |
| `dependencies.py` | XBRL taxonomy/linkbase 依赖发现、校验、cache/staging、可选获取和离线运行配置 |

两个 download 名称不代表重复：download/financials.py 获取 Company Facts/submissions；filings/download.py 获取 filing document；filings/acquisition.py 是 archive 级编排；dependencies 是 parser taxonomy 环境准备。

### 3.3 Archive 与运行编排

| 模块 | 职责 |
| --- | --- |
| `models.py` | filing/document schemas 与稳定 ID、RunSpec、RawObjectRef、Snapshot 等 core 合同 |
| `processing_contract.py` | PARSES_SCHEMA、DEPENDENCIES_SCHEMA 与 parse identity 合同；序列化和身份规则保持不变 |
| `archive.py` | CAS、Parquet、manifest/snapshot 读写、reader/writer 完整性合同、原子提交及只读 RunSpec loader |
| `workflow.py` | catalog 运行编排、calendar/protected paths、输入指纹、发布与复用 |
| `packages.py` | hash-verified 来源物化为隔离 parse workspace |
| `pipeline/planning.py` | 通过注入的 pending-work predicate 做纯 filing 选择，不修改 archive state |
| `pipeline/validation.py` | parser fact 对应物理 source/hash 核验及 group owner provenance 标记 |
| `processing.py` | active-state 校验、IXDS resolve/recovery、依赖编排、结果/source owner 协调与唯一主提交事务；其他职责仅部分迁出 |

models 与 parsing_models 服务不同合同，不能因为名称相似合并。archive 的读写和验证共同定义格式，不应拆成各自漂移的两套规则。Processing 的路径拆分是窄迁移，不是完整的物理分层。

### 3.4 命令及消费者

| 模块 | 职责 |
| --- | --- |
| `__init__.py` | core/catalog/archive 公共 facade，避免引入 optional parser imports |
| `cli.py` | filings 命令参数与 dispatch |
| `query.py` | 本地 manifest-listed 表的 DuckDB 只读查询 |
| `verify.py` | archive integrity 审计，不证明财务内容完整或经济含义正确 |

### 3.5 来源无关的财务抽取（M1 进行中）

| 模块 | 职责/状态 |
| --- | --- |
| `financial_extraction/domain/` | 不透明来源 ID 的 DTO、显式 Decimal policy、数值/证据/映射与 patch 校验；不依赖 SEC/archive schemas |
| `financial_extraction/evidence/blocks.py` | 可选 lxml、有界 HTML evidence blocks、物理 cells 和 coverage plan；canonical 字符 span 不等于 raw byte locator |
| `financial_extraction/runtime/` | SDK-free typed fake/replay 请求协议、显式 call/token/cost budgets 和进程内 response replay |
| `financial_extraction/workflow/` | Controller 实施中；当前不作为稳定 API，不声称 A/V/B workflow gate 已通过 |
| `filings/adapters/financial_extraction.py` | Host identity/source/scope binding 实施中；无真实文档抽取入口、CLI 或 archive publish path |

该 lane 目前只使用 checked-in synthetic HTML fixture。Domain、evidence、fake/replay tests 验证确定性合同，不证明金融抽取正确率。没有真实 provider 调用或已选定模型；在 host adapter、blind-review controller、持久化及审批完成前，不处理真实 filing。完整边界见 [financial-extraction 实施说明](financial-extraction.md)。

## 4. 有证据的结构问题

| 优先级 | 问题 | 原因与边界 |
| --- | --- | --- |
| 已解决（最小抽取） | 来源封装 helpers 原属文本 parser 私有实现 | 共用接口已迁至 `filings/source/sec_envelope.py`；`parse_text` 留兼容名称，parser 行为/identity 未升级 |
| 高（部分缓解） | processing 汇集多项职责 | schema/parse ID、纯选择与来源校验已窄化拆出；active-state、IXDS resolve/recovery 与唯一 commit 尚在 processing |
| 进行中 | AI 模块不宜继续加到 filings 平铺目录 | 独立 `financial_extraction` domain/evidence/runtime 已起步；workflow 与 host adapter 尚未形成可用端到端产品 |
| 已解决（窄迁移） | processing 同时拥有持久化 schema | `PARSES_SCHEMA`/`DEPENDENCIES_SCHEMA` 与 parse identity 已移至 `processing_contract.py`；格式与消费路径保持兼容 |
| 中 | acquisition 混合 scope policy 与下载事务 | 可提取证据计算/政策，scope 更新仍必须与验证及原子写入协调 |
| 中 | archive 同时 I/O 与多层格式验证 | 值得按合同拆内部纯 validators，但 reader/writer 必须复用同一组规则 |
| 已解决 | filings query 与 samples query 的共享 SQL guard | 两个 consumer 已使用 `query_support.readonly_sql` 的窄 public contract；领域检查和 query runtime 仍分开 |
| 中 | financial_events 借 organizer 私有概念定义 | 需要稳定的财务概念合同，不把 Company Facts 获取和文档解析混合 |
| 已实施独立 lane | samples package 与旧工具迁移 | 单一 samples 包、共享 SQL guard、旧工具/helper 清理、caller/CLI/package/tests/docs 同步已落入当前树；验证 gate 由负责人单独确认 |

这些结论不等于所有大文件都应拆。文件体量只提示检查，实际职责及状态所有权才决定边界。

## 5. 兼容和验证要求

- 保持 filing/document/parse IDs、parser versions、schema 列序及既有序列化行为。
- 不因路径移动改变 `_PROCESSING_CONTRACT`、IXDS identity、cache指纹或受保护来源的身份。
- 测试不仅导入 facade，也直接导入/monkeypatch parser、processing、acquisition 及私有 helpers；简单 re-export 并不自动保留 patch 行为。
- Samples 已由 setuptools package discovery 安装；wheel/CLI/optional-extra 检查仍应按当前任务指定 gate 核验，但不保留旧顶层 import。
- `_vendor` 的路径、许可证、package data 和 transform identity 需专门保护。
- 基于 frozen archive 进行输出与来源投影比较，覆盖 no-op、dependency失败/重试、IXDS分组、source-owner验证、旧 snapshot 读取和 CLI。

## 6. 证据入口

- `pyproject.toml`：console script、setuptools package discovery、vendor package data 和 intentional download compatibility re-export 注释。
- `src/filings/processing.py`、`processing_contract.py` 与 `pipeline/{planning,validation}.py`：分别核对 active-state/IXDS/commit owner、持久化 schema/parse identity、注入 predicate 的纯选择和物理 source-owner checks。
- `src/filings/source/sec_envelope.py`、`parse_text.py` 与 `parse_xbrl.py`：共享 envelope 实现、兼容旧名称与 parser 使用边界。
- `src/filings/parsing_models.py`：纯 parser 输出合同。
- `src/financial_extraction/{domain,evidence,runtime}/` 与 `tests/test_financial_extraction_*.py`：离线 DTO/evidence/fake-runtime 边界；workflow gate 尚待独立验证。
- `src/download/financial_events.py`：Company Facts event/fact 合同和私有概念导入。
- `src/filings/query.py`、`src/samples/query.py`、`src/query_support/readonly_sql.py`：两个 query consumer 共用只读 SELECT 校验。
- `tests/test_filing_*` 与 samples tests/fixtures：filing 的实际 import/monkeypatch 面按各自合同验证；samples 当前测试包括 `test_samples_current_contract.py`、`test_samples_semantic_regression.py`、`test_samples_integrity_regression.py`、`test_samples_safety_regression.py`、`test_query_samples.py`、`test_verification_tools.py` 与 `test_build_samples.py`，fixtures 位于 `tests/fixtures/samples/current/`。旧 baseline/projection 工具已删除，不是当前正确性 oracle。

本审计只记录源码结构，不声称 samples/full-suite gate 或 financial-extraction M1 gate 已通过。财务抽取脚手架已进入当前源码，端到端 workflow、真实来源接入及发布仍未完成。
