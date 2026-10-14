# 已知行为与限制

- Yahoo universe 基于当前 SEC exchange list；不提供退市历史或 point-in-time ticker 映射。
- FRED 数据为 latest-revised observations，不是 vintage releases。不同序列起始日期不同。
- SEC Company Facts/submissions cache 是 metadata 与结构化事实，不是完整 filing HTML/iXBRL。
- 报告样本覆盖取决于 selection 与来源。文档不可用、不支持的 payload、解析失败和 partial coverage 均保留在 diagnostics 中；验证成功不代表发行人覆盖完整或财务正确。
- 真实 SEC 获取要求有效、非占位的联系信息。网络测试始终 mock transport。
- Yahoo 复权价格不是可执行成交价；标签不构成回测或交易模拟。

保留现有用户数据；只报告具名运行实际观察到的测量结果。见[下载实现](download.zh-CN.md)、[数据契约](data-contracts.zh-CN.md)与[SEC 报告样本](../user/filings.zh-CN.md)。
