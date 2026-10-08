# Changelog

本文件记录本仓库的重要变更。格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/)，版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

[English](CHANGELOG.md) | **简体中文**

## [Unreleased]

### Changed

- 财务抽取改为异步有界队列（`run_extraction_async`，另有同步 `run_extraction` 封装）：本地处理（`[extraction].workers`，默认 4）、模型（`[extraction].model_workers`，默认 1）、预取（`[extraction].prefetch_windows`，默认 4）三路容量分离，`model_workers` 不得超过 `workers` 或 `prefetch_windows`。来源顺序、单 journal 写线程、无自动重试保持不变。
- 未知结果的传输失败（`UnknownRequestError`：超时、连接错误）停止准入新发送；`RequestStateError`（未知历史状态、identity 冲突）只影响本窗口，其他 journal 故障仍为致命；同一批次完全相同的窗口等待并以单次 provider 调用重放持久化响应。同步/异步单窗口完成回调不重新触发。进度将排队、在途、已持久保存的响应与已完成窗口分别报告。此前 32 路 provider 并发需显式配置 `model_workers=32`（配置文件或 `--model-workers`），且 `workers`/`prefetch_windows` 不小于 32。
- `filings extract-financials` CLI 将阶段和窗口进度实时输出到 stderr，stdout 保留 JSON 摘要，并支持 `--no-progress`；证据分窗完成前窗口总数显示为未知。
- 提示词版本 `open-financial-v2` 明确原始数值词法、数字倍率、已配置政策和当前窗口逐字引文要求，不放宽校验。私有 DeepSeek 三窗口对比返回 164/165 条有效记录、161/162 条有效数值记录；这些是合同校验结果，不是准确率或召回率。双语抽取/CLI 文档及配置示例同步。
- 新增带校验字符的窗口内短引用（`open-financial-v3`），所有引文字段精确按请求映射解码，映射进入 request identity 和 journal。未知或写错的 ref 保持 unresolved；原始来源校验和模型响应不变。
- 新 20 份 filing、598 窗口真实 CLI 批次验证短引用协议：240 个 schema 有效响应没有未知短 ref。批次主要因服务余额耗尽（331 个 HTTP 402）和动态并发限制（23 个 HTTP 429）未完成；保留输出和失败索引，不重试。

## [0.1.0] - 2026-09-29

### Added

- 样本构建流水线：`download` / `organize` / `build-samples` 三个环节，产出 `samples_v1` 冻结样本包（23,938,669 行 × 147 列，覆盖 1990–2025），各输出在 `manifest.json` 中登记哈希。
- SEC 财务摄入：财报概念白名单、`fiscal_year` / `fiscal_period` 标识、历史申报分页解析、`ticker→CIK` override（XOM）。
- purge 禁运与无前视标签：fit/select/screen 三个 split 边界前 31 个 signal session 在构建期禁运；标签以 `t+1` 开盘建仓、`t+1+h` 开盘出场。
- 用户文档（`docs/user/`：CLI、数据格式、推荐用法）与开发者文档（`docs/developer/`：架构、数据契约、测试等），以及 `AGENT.md` 导航。

### Fixed

- 标签跨切分泄漏：split 边界附近的标签窗口不再跨入下一个 split（purge 禁运）。
- YoY 期间匹配：优先按 `fiscal_year` / `fiscal_period` 同键匹配，避免把 Q1 对到 Q2。
- 非有限特征值：raw 特征中的 ±inf/NaN 统一转缺失并生成 `miss_*`。
- 营收概念缺失：扩展 revenue 白名单，纳入 `SalesRevenueNet` 等旧/窄 us-gaap 概念。
- XOM CIK 错配：universe 快照指向错误主体，改用历史申报主体 CIK 并支持 override。
- 申报历史截断：解析 `filings.recent` 之外的列式 submissions 分页，早期 10-K/10-Q 恢复可见。
