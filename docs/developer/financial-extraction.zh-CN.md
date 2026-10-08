[English](financial-extraction.md) | **简体中文**

# 开放字段财务抽取

**状态（2026-10-06）：有界并发抽取和带校验字符的短引用已集成。** 历史 v2 全量重跑处理了 3 份 filing（46 个原窗口），provider/schema 完成率 100%，记录校验率 97.79%，数值校验率 97.23%。这些是本地合同校验，不是财务准确率或召回率：163 条跨单元格拆分括号的负数仍为文本。v3 保留严格检查并新增短 ref 精确解码。异步队列默认 4 个本地处理 worker、1 个模型 worker、4 窗口预取，最终结果保持来源顺序。

## 流程与职责

来源无关的 `financial_extraction` 包负责 evidence 构建、请求形成、模型协议、基于来源的校验，以及写入显式 work directory 的持久 journal。`filings` 负责 archive identity、filing 选择、archive 完整性校验和原始来源字节绑定。Host 命令要求显式 filing IDs，最多 50 份；不会提升 candidate，也不会为失败项补充新来源。窗口抽取作为异步有界队列运行（`run_extraction_async`，另有同步 `run_extraction` 封装），保持来源顺序稳定，无自动重试。生产者按 `prefetch_windows` 为界把窗口送入队列；`model_workers` 个消费者准入并发送 provider 调用，`workers` 个本地槽位校验响应并执行完成回调。结果保存在 archive 之外，绝不发布。

开放记录 schema 保留原始 `source_label`、推断出的 `name` 与 `category`、字符串 value、value type、来源引文、可选 unit/currency/scale、period 和任意 dimensions。名称与类别不受预定义 metric registry 限制。Host 的原始 filing identity 和 archive 始终是权威来源。

## 安全与运行时

`build_evidence` 无网络解析已验证的 HTML。`iter_windows` 将每个 evidence block 和 cell 恰好作为一次 core 覆盖；headers 可作为 context 重复。结构边界不得导致相邻数字被拼接。最终序列化请求超过 `max_request_bytes` 时直接拒绝，不截断内容。

OpenAI-compatible client 延迟加载 SDK，只使用从 `secrets.toml` 的 `[secrets].api_key` 加载的显式密钥，不读取环境变量。密钥不进入 config/client repr、descriptor、request identity 或请求 journal；SDK 错误消息会隐藏当前密钥。自动重试保持关闭；拒答、空内容或不完整响应视为错误，校验失败不自动纠正或重试。`provider.structured_mode` 可选 `json_schema`（默认，服务端 strict schema）或 `json_object`（合法 JSON object 模式）。`json_object` 模式把 schema 和示例放入 system prompt，返回内容仍由本地严格解析与校验。DeepSeek 使用 `response_format={"type":"json_object"}`。提示词必须匹配数字倍率和原始数值词法。应用层不设置成本、总 token 或调用次数预算。

持久 journal 保存精确请求和响应。缺少响应的 pending 请求视为状态未知，不会自动重发：`RequestStateError`（未知历史状态或请求 identity 冲突）只使该窗口失败，抽取继续；而其他 journal 故障（journal 损坏、已存响应哈希不匹配、不可写）属于致命 `StoreError`，停止准入新发送。同一批次中完全相同的重复窗口等待首个窗口的 journal 写入，然后以单次 provider 调用重放持久化响应。未知结果的传输失败（`UnknownRequestError`：超时与连接错误，密钥已脱敏）同样停止准入新发送；失败窗口记为已提交，已在途调用等待返回或超时，其他 provider 错误只影响各自窗口。Host CLI 验证已选 archive snapshot 和 source refs，只写入显式指定的 work directory，并报告 `publishable: false`。

提示词版本 `open-financial-v3` 保留 v2 明确的数值合同：`value` 保留原文词法（分隔符、符号及政策允许的前后缀）；`scale` 为 null 或正数十进制倍率字符串，例如 `"1000000"`，不能写 `"millions"`。每个非 text 值必须指定配置中的 numeric policy，并向模型提供全部符号及前后缀规则。引文仍须是当前窗口对应 ref 的连续、精确子串。不支持的词法保留为文本事实，不自动归一化或重试掩盖失败。四个 executor 隔离各阶段：单线程 prepare executor、`model_workers` 线程的 model executor、执行全部 journal 写入的单线程 journal executor、`workers` 线程负责校验与同步完成回调的 processing executor。Provider client 在并发 `complete` 调用之间复用同一个延迟构建的 SDK 句柄（构造加锁保护），运行结束后经 context manager 关闭；自定义 client 须支持最高 `model_workers` 并发的 `complete`，否则使用 `model_workers=1`。`on_window_complete` 接收每个已完成的 `WindowOutcome`，可为普通函数或异步可调用对象；回调失败只记日志，不改变已记录结果，也不会重新触发。缓慢的校验或回调占用本地处理槽位，但不占用模型槽位，后续 provider 调用仍可启动。

模型侧证据使用确定性的窗口内短 ref：`r`、六位十六进制序号、两位模 17 校验字符。校验能检出任意单字符替换，以及序号内不同相邻字符的交换；不证明来源归属正确，也不防范所有多字符错误。core/context 重复项共用 ID。精确 short→original 映射保存在请求 journal 并参与 request identity，不作为 provider 元数据发送。所有 quote 字段通过该请求的映射还原，再对原始证据严格校验。未知 ID、缩写 ID、原始长 ID、错字保持 unresolved；不做前缀匹配、编辑距离修复或引文反查兜底。原始模型响应不修改，最终 ref 和来源位置使用原证据身份。协议变更产生新 request identity，旧长 ref 请求不会复用为新短 ref 请求。

对相同 Microsoft 窗口 5、12、14 的私有定向对比中，记录校验从 54/163 提升到 164/165，数值记录从 52/161 提升到 161/162。三个响应均 stop 并通过 schema；倍率、词法存在性和缺失政策问题消失，剩一条标签引文不匹配。这是选择原失败窗口的样本，不证明准确率或召回率。对比使用三个并发 worker，并将请求上限设为 110KB，以容纳更长提示词且保留旧证据边界（本地默认仍为 100KB）；正常窗口规划将提示词字节计入上限。结果：`/tmp/opencode/deepseek-prompt-v2-5t7geqp5/assessment.json`。

新 v3 批次包含 20 家公司、20 份 10-K，通过真实 CLI 规划并尝试 598 个窗口。只有 243 个返回 stop、240 个通过本地 schema。失败分别为 331 个 HTTP 402 余额不足、23 个 HTTP 429 并发限制、1 个 length、3 个 schema 错误。服务明确随余额下降调低允许并发；未重试。已返回 schema 有效记录共 21,394 条，20,812 条有效，数值记录 16,204/16,750 有效，但这些存活窗口比率不包含 358 个失败窗口。240 个 schema 有效响应的 ref 全部属于各自精确短 ref 映射；不证明重复引文的来源归属正确。另有 1,352 条缺右括号的数值片段保持文本。证据：`/tmp/opencode/new20-shortrefs-20261006/model-shortrefs/assessment.json`；失败窗口索引和原始响应均保留。完成批次需 provider 充值及明确的重跑授权；不得盲目重发 journal 中 pending 请求。

## 限制与 pilot 前置条件

引用和 schema 校验不能证明财务准确性或完整性。数值必须使用显式 `NumericPolicy`；无法解析的数值输出保持 unresolved，但明确返回为 text 的值不做数值解析。括号跨 HTML 单元格拆分的金额因而可能文本有效、数值覆盖缺失。两份历史 pilot archive 在主文档 gate 下只有 3 份可绑定 filing。另行获准的新批次已冻结并获取 20 份此前未处理的 10-K，保存在独立 archive；不是 50 份运行。不放宽资格，也不补选模型失败项。私有 100K 输出配置使用 600 秒超时。`model_workers` 只限制同时 provider 调用数（默认 1），不限制总 token 成本；服务限流可使单窗口失败，不自动重试。中断会取消排队调用，但等待已运行调用返回或超时。

## 命令

当前本地入口：

```bash
quant-dataset filings extract-financials \
  --archive PATH --work-dir PRIVATE_PATH --config config/extraction.toml \
  --filing-id FROZEN_ID
```

对冻结清单重复指定 `--filing-id`，最多 50 个唯一 ID。根据示例配置 `config/extraction.toml`；CLI 将选中来源内容发送给该 provider，把不可发布结果和请求 journal 写到 archive 外，绝不发布。
CLI 将进度输出到 stderr，stdout 保留最终 JSON 摘要。它显示 filing 准备数、已完成/总窗口数（证据生成结束前总数未知）、排队窗口、在途 provider 请求、已持久保存的响应、重放窗口、失败窗口、unresolved 记录以及结果写入阶段。`--no-progress` 关闭实时显示。`saved` 统计已记入 journal 的完整响应，可能多于通过校验的窗口，因为已保存的响应仍可能未通过本地校验；已完成窗口包含失败项。进度事件仅存在于本次运行，不进入 request identity、持久化 journal 或 `result.json`。
## 配置

复制 `config/extraction.example.toml` 为 `config/extraction.toml`，设置 provider 地址、模型名和 `structured_mode`；DeepSeek 使用 `json_object`，支持 strict JSON Schema 的 provider 使用 `json_schema`。密钥只放在 gitignored 的 `config/secrets.toml` 的 `[secrets].api_key`。CLI 默认读取此路径，可用 `--secrets PATH` 指定其他文件；直接调用 `load_extraction_config` 时默认读取抽取配置旁的 `secrets.toml`。密钥缺失、为空或为占位值时直接失败，即使环境变量已设置也不会回退。`provider.api_key` 和 `provider.api_key_env` 不再接受；保留 `[secrets]` 中原有 SEC/FRED 字段。不要分享或提交私有密钥，也不要在示例中填真实密钥。`[extraction].workers`（本地校验/处理槽位，默认 4）、`[extraction].model_workers`（并发 provider 调用，默认 1）、`[extraction].prefetch_windows`（有界计划窗口队列，默认 4）均为正整数，且 `model_workers` 不得超过 `workers` 或 `prefetch_windows`，否则 fail closed。CLI `--workers`、`--model-workers`、`--prefetch-windows` 可覆盖配置值。Provider 并发需服从服务限流。数字字段只有在明确配置的 policy 下才会归一化；不得根据 filing 内容擅自推测地区格式。

CLI 从配置读取 provider 和策略，经有界异步队列执行准备、抽取、来源校验，并将请求 journal 与完整、不可发布的 `result.json` 写入 `--work-dir`。命令不更改或发布 archive。

```bash
quant-dataset filings extract-financials \
  --archive PATH --work-dir PRIVATE_PATH --config config/extraction.toml \
  --filing-id FROZEN_ID
```

重复 `--filing-id` 指定冻结列表，最多 50 个唯一 ID。提交前须确认每个 ID 已 eligible；不补选、不替换失败项。

## 异步队列 API 与迁移

库调用者使用 `financial_extraction.workflow.runner` 中的协程 `run_extraction_async(documents, *, client, task, limits, work_dir, protected_paths=(), workers=4, model_workers=1, prefetch_windows=4, on_progress=None, on_window_complete=None)`；`run_extraction` 参数相同，同步跑完整个队列，也从 `financial_extraction.workflow` re-export。`on_progress` 是原有同步进度回调；`on_window_complete` 接收每个已完成的 `WindowOutcome`，可为同步或异步。filings 侧 `extract_selected_filings` 透传同样的 `workers` / `model_workers` / `prefetch_windows` / `on_progress` / `on_window_complete` 参数。

此前 `workers`（默认 32）限制 provider 并发，该语义现由 `model_workers`（默认 1）承担：恢复 N 路 provider 并发需设 `model_workers=N`，且 `workers>=N`、`prefetch_windows>=N`。除非 provider 配额与 client 支持并发调用，否则保持 `model_workers=1`。只写了 `workers` 的旧 TOML 仍可加载，未设置的字段取新默认值。
