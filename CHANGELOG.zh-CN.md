# Changelog

本文件记录本仓库的重要变更。格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/)，版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

[English](CHANGELOG.md) | **简体中文**

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
