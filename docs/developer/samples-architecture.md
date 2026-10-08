# Samples 单一产品架构

状态：实现已落入当前源码树；`samples` 五模块包、共享只读 SQL guard、单一当前契约与旧工具清理均已实施。实现/文档不声称完整验证 gate 已通过；source-consistency 与测试 gate 由任务负责人单独确认。数据未迁移、删除或重建。

本实现覆盖并替换此前“samples 暂留顶层、旧工具以后归历史目录”的建议。当前树与迁移前快照见 [src 审计](source-layout-audit.md)；AI 与 parsing 的独立工作线见 [非 XBRL 架构](non-xbrl-ai-architecture.md)。

## 1. 职责与目录

samples 负责将 organized 行情、宏观与 exclusions 构建为训练/评估样本，并提供只读查询和产物校验；不下载数据、不解析财报、不调用模型、不训练模型。

```text
src/
├── samples/
│   ├── __init__.py       # 薄包入口
│   ├── builder.py        # 当前特征/标签计算、分批 staging、QC 与输出编排
│   ├── query.py          # 样本只读查询，DuckDB 按需加载
│   ├── contracts.py      # 唯一列注册表、Arrow schema、标签/时间/manifest 合同
│   └── validation.py     # 当前 schema、manifest、路径及完整性验证
└── query_support/
    └── readonly_sql.py   # samples 与 filings 共用的窄 SQL guard
```

规范入口已实现为 `samples.builder.build_samples`、`samples.query.query_samples`。计算 helpers 保留在 builder，不按函数拆成大量文件。contracts 不依赖运行编排；validation 被 builder/query 复用，不形成通用验证框架。

不保留顶层 build_samples.py/query_samples.py re-export，不建 legacy/compat 包，不新增全仓根 namespace。项目内 caller、测试、CLI 和打包配置均已同步迁移；不提供旧模块 re-export 或其他兼容壳。

## 2. 当前唯一行为合同

只保留当前源码已经实现的无财务样本行为；目标产品与包统一称为 `samples`，不再把 `samples_v1`、`samples_v2` 或 `samples_v3` 当作可选产品代际。已有 bundle 中的历史 `schema_version` 只用于解释原样保留的数据，不是当前产品名或兼容入口。

- 输入仅 market、macro、exclusions；不读取 financials.csv、财务 event/fact 表或财务 metadata，不做财务 preflight/coverage。
- 43 raw + 10 CS + 43 MISS = 96 features；32 labels；4 个键/标志；132 总列。列序、dtype、null/NaN 语义保持。
- 日期 date32，特征/标签 float32；具体标志 dtype 以当前合同注册表固定，不因迁移推断或重排。
- canonical 轴、普通股门槛、按 session 的窗口和当前横截面参与条件保持。宏观 d1/d5 使用 positional difference，不 forward-fill、不做宏观 CS。
- 收益标签为 `adjusted_open(t+1+h) / adjusted_open(t+1) - 1`，h=1..30。entry/exit 必须精确有效，不替代、不填充；不新增要求中间 bars 全部存在。
- excess_5d/21d 使用当前同日普通股、排除 extreme-flag 行后的等权基准。未来窗口计算的 extreme flag 不进入 feature_list。
- split 按 signal date 保持 fit ≤ 2018、select 2019–2020、screen 2021–2024、reserve 从 2025 开始。最大 h 为 30、entry 在 t+1、exit 在 t+1+h，因此 applicable split boundary 前 purge 31 个 canonical-calendar signal sessions；仅在后续 split 有对应边界时适用，尾部自然缺失标签行保留。

本次不宣称修复 universe survivorship、FRED latest-revised、调整后开盘价非可执行成交价等现有局限。未来财务特征接入是新的独立设计，不恢复旧财务样本代码。

## 3. 不带产品版本号，保留强验证

新构建 manifest 的唯一合同标识为 `samples`。`schema_version` 的当前唯一值为 `samples`；不接受或分派 `samples_v1`/`samples_v2`/`samples_v3` 作为产品选项，也不提供旧别名。保留旧 bundle 原有 manifest 内容，不通过改名或重写 manifest 伪装成新构建。字段不是多产品路由。

仍要求精确 schema/语义 fingerprint、输出 hashes/行数/QC、实际消费输入的路径/内容 hash、构建配置及代码/依赖身份。schema 校验和内容寻址不是产品代际兼容。

当前 builder 已把完整内容 provenance 与计数盘点分开：`input_provenance.files` 对每个实际消费输入记录 path、流式 SHA-256 与 byte count，并在 manifest 发布前重哈希；manifest 同时记录 build parameters、五个 samples package 文件的 code hashes/bytes 和 NumPy/pandas/PyArrow versions。`input_inventory` 明确仅为 counts，不代表内容 inventory。

此决定只取消 samples 多代产品：Company Facts 的 financial_events_v1、filing archive 的格式/snapshot、parser 身份、AI model/prompt revisions 不在本次删除范围内。

不将旧数据只改 manifest 标识就当作满足新合同。新命令不提供旧版本数据兼容承诺；重新构建或独立迁移须有明确验证。

## 4. 已删除的旧代码与测试

以下实现迁移中已删除：

- src/freeze_v1_baseline.py：旧样本基线工具。
- src/verify_projections.py：跨代基线 projection 工具。
- src/calibrate_coverage.py：旧财务样本覆盖诊断。
- 旧 src/build_samples.py、src/query_samples.py 已移除，不留兼容壳。
- builder 中不被当前构建路径调用、仅服务旧财务样本的 FINANCIAL_RAW、loader/meta/preflight/feature helpers 及独占注册表。已确认 _financial_features、_preflight_financial_inputs 不被当前入口调用；更细的删除按 callsites 验证，不按名字批量删。

删除工具专属测试，如 test_freeze_v1_baseline.py、test_verify_projection_memory.py；混合测试文件只移除旧 projection/eligibility 用例，不连带删除当前契约和安全测试。旧财务公式、TTL、YoY/QoQ 和 preflight helper 测试退出。

保留并更新当前输出合同、查询安全、结果限制、输出路径保护、bounded-memory 和不访问财务输入的测试。无财务 I/O 的测试检查真实访问行为，不为旧 monkeypatch 留空 helper。

活跃文档只描述单一 `samples` 合同与规范包入口；旧样本财务规格、v2 附录和基线工具操作指南退出活跃设计。独立 Company Facts / `financial_events_v1` 数据契约与其来源限制继续保留。旧金融样本规格与入口已从活跃文档中移除；此页是单一 `samples` 契约的架构记录，不是待执行的迁移清单。

## 5. 当前不变量与独立 fixtures

`tests/fixtures/samples/current/` 已包含小型固定输入；当前测试使用独立预期值覆盖：

1. 手算收益、excess、缺失 entry/exit、窗口边界及异常值。
2. 固定 canonical 轴后，未来输入变化不能改变过去特征；核对 t+31 purge、split 边界和尾部 null。
3. 唯一 date/asset_id、重复输入日期、ticker 归一化冲突、缺失及非有限值。
4. 横截面 rank、宏观 positional d1/d5 和 missing flags。
5. 输入顺序、batching 变化下的语义 determinism。
6. schema/列序/dtype、manifest/hashes、只读 SQL 与输出保护。
7. 无财务输入、存在损坏财务文件两种情况下均不得访问财务数据。

expected/golden 不由被测 builder 或同一 helpers 生成。重跑一致证明 determinism，不证明公式正确。迁移前当前输出 characterization 可作为补充证据，但不长期保留旧 baseline 工具作为正确性 oracle。

## 6. 共享 SQL guard 与打包实现状态

### 6.1 共享 SQL guard

`filings.query` 与 `samples.query` 当前均调用 `query_support.readonly_sql.validate_select`，并将中立 SQL 错误映射为各自领域错误；只共享 SQL 规则，不共享领域输入检查或 query runtime。

### 6.2 包迁移、清理与 caller 同步

- builder/query、contracts/validation 已落入 samples 包，并由 `cli.main` 与 filing query consumer 使用。
- 项目内 imports、测试、CLI 和打包已迁移；旧顶层模块及旧 baseline/projection/calibration 工具已删除，无旧路径 shim。
- setuptools package discovery 安装 `samples`/`query_support`；Ruff 与文档路径已同步。
- Wheel 安装、CLI help/build/query 和缺 optional extras 行为仍按当前指定的独立 gate 验收；本文不记录该 gate 的通过结论。

### 6.3 已实现的 Workspace 配置与保护路径政策

`build_samples(..., workspace_root=None)` 的 workspace 默认是当前工作目录（CWD）；CLI 提供 `--workspace-root PATH` 显式指定。默认 exclusions 从 `<workspace_root>/config/universes/exclusions_v1.json` 读取。显式传入的 `--exclusions-file` 路径按 CWD 解析（绝对路径照常使用），不相对 `workspace_root` 解析。配置文件缺失时必须明确报错，不能静默使用空 exclusions。保留既有配置文件名 `exclusions_v1.json`；它是配置名，不是 samples 产品代际。

保护范围包含 `<workspace_root>/data/`，并根据实际输入路径保护相邻/关联的 raw、output、baselines 数据；当 organized 输入位于常规 `<data>/organized` 时，也会独立于 workspace root 保护其 `<data>` 下的 raw/output/organized/baselines/archive sibling paths。显式 workspace root 必须是已存在目录。输出路径或祖先为符号链接、目标非空，或与保护路径/实际输入重叠时必须拒绝。CLI 默认 `--out` 为 `data/samples-output`，但候选必须是 fresh 未占用路径。

所有 workspace/config/protection 路径由明确参数和当前工作目录上下文决定；不得根据包安装位置、`__file__` 层级或 wheel 内代码位置推导 workspace、默认配置或数据根。上述行为需由 builder 与 CLI 测试覆盖；不新增未约定的 flags。

## 7. Implementation and verification status

| Area | Status |
| --- | --- |
| Samples P0/P1 implementation | Single contract, current fixtures, shared SQL guard, five-file package, retired tools/logic, caller/CLI/package/tests/docs migration are implemented in the current tree. |
| Current correctness/security gate | Independently assigned current-contract tests, source consistency, and any wheel/full-suite checks remain under the task owner's validation; no final PASS is claimed here. |
| AI M1–M4 and further filing-parser organization | Separate design/work lanes; not implied to be implemented by the samples migration. |

The Samples lane is distinct from the AI/parser work. The shared SQL guard is already used by both query consumers; it is no longer a migration prerequisite.

## 8. 数据处置边界

Samples code migration retires old implementations but does not authorize deleting data directories. Raw, organized, output, backup, baseline and archive artifacts remain unchanged by this documentation/source-layout update.

Any later data disposal requires a separate inventory, explicit target list, and confirmation. Preserve old baseline/backup artifacts unless separately authorized; never overwrite or batch-delete protected paths. Do not build a historical product compatibility layer merely to retain obsolete implementations.
