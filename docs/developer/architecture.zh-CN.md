# 架构

本仓库保留三个 raw provider 下载阶段及独立的 SEC 报告样本工作流。不再将 provider 数据组织为派生面板。

```text
Yahoo ─┐
SEC ───┼→ download → data/raw/ + progress
FRED ──┘

本地 SEC submissions cache → filing identity scan → 有界 SEC 报告获取
                                               → 清洗 blocks → Parquet + diagnostics
```

Raw 下载实现在 `src/download/`，CLI 分发位于 `src/cli/main.py`。SEC identity/cache 校验位于 `src/filings/catalog.py`；文档选择、SEC transport、解析与报告样本工作流在相邻 `src/filings/` 模块中。`data/raw/sec/financials` 提供 filing identity metadata；报告正文另行获取，不保留在 raw cache 中。

报告样本 builder 使用有界 worker、逐 filing 私有临时目录、清洗输出 checkpoint、年份分区及分片 Parquet。诊断保留 partial/failure 状态。验证检查 schema、路径、shard 覆盖与行 identity。SEC 原始字节在解析/checkpoint 提交后删除；resume 只使用清洗 checkpoint。

既有 `data/`、baselines、archive snapshots 与私有凭据均为用户数据，不属于源码清理范围。测试只使用本地 fixtures 与 mock HTTP。见[下载实现](download.zh-CN.md)、[SEC 用户指南](../user/filings.zh-CN.md)与[测试](testing.zh-CN.md)。
