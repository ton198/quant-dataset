# 数据契约

当前维护的数据流包含两类产物：provider 原始 payload 与清洗后的 SEC 报告样本。既有 organized/output artifact 属于用户数据，继续保留；但现有命令不再重建它们。

## Provider raw 数据

`quant-dataset download` 写入 `data/raw/`，并在独立路径维护可恢复进度。Yahoo 区间文件按 ticker/range 保存；SEC 与 FRED 资源沿用已有内容寻址 manifest。SEC `financials` 阶段保存 Company Facts 与 submissions metadata（含引用的历史 submissions 分页），不是完整申报正文。源码维护不得手工编辑 raw payload 或改写已有 cache。

## 报告样本输出

`filings build-report-samples` 输出按年份分区的 Parquet shards 与 diagnostics。精确 Arrow schema：

| 字段 | 类型 |
|---|---|
| `sample_id` | string |
| `entity_id` | nullable string |
| `filed_date` | nullable date32 |
| `blocks` | `{order_index: int32, block_type: string, text: string}` 列表 |

Block type 为 `financial_statement`、`note`、`other_context`、`unknown`。每个 filing identity 只出现一次；shard 路径唯一，年份分区依据 filed date。诊断保留 unavailable、skipped、failed 等结果。验证器检查精确 schema、分区和 shard 结构、identity、计数与诊断一致性。

申报来源字节先写入逐 filing 私有临时目录，并在解析/checkpoint 提交后删除。Resume 复用清洗后的 checkpoint rows 和 diagnostics，并按请求 ID 限定范围；不会复用来源字节。Output/work 目录必须是新建空目录、不能含符号链接，且与 cache 和保留用户数据分开。

## 保留边界

已有 `data/`、`baselines/`、archive snapshots 与 `config/secrets.toml` 均为用户数据。保留代码不迁移或重新发布这些产物。见[下载实现](download.zh-CN.md)与[报告样本指南](../user/filings.zh-CN.md)。
