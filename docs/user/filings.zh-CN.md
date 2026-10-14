[English](filings.md) | **简体中文**

# SEC 报告样本

`filings` 命令从本地 SEC financials cache 中的申报身份构建并验证清洗后的报告样本。不创建 archive，不运行模型抽取，也不修改源 cache。

## 构建

```bash
quant-dataset filings build-report-samples \
  --cache-root data/raw/sec/financials \
  --out data/financial_reports \
  --work-dir data/.financial_reports.work \
  --limit 10 --workers 4
```

`config/sources.toml` 中配置的 `filings.forms` 决定可选 forms。Cache 仅用于识别 filing；选定报告文档通过配置的 SEC 联系信息获取。工作流包含主文档选择、安全 URL 校验、有界 worker、checkpoint 与逐 filing 诊断。当 primary 文档不可用时，可使用 accession 纯文本 fallback；非 HTML/PDF payload 会跳过，不当作已解析报告。

必须使用全新的空 output/work 路径。路径可位于 `data/` 下，但不得与 `data/raw/`、`data/baselines/`、`baselines/`、`archive/`、cache 或保留用户数据重叠。输出保护会拒绝符号链接路径和非空目标。

`--limit N` 限制选中 filing 数量；重复使用 `--filing-id` 可选择本地 cache 中精确身份。`--workers` 是正数有界并发。`--resume` 复用已验证的清洗 checkpoint 与诊断，并按本次选择筛选。来源字节仅存在于每份 filing 的私有临时目录，在解析和 checkpoint 提交后删除；resume 不复用已下载来源字节。

## 输出与验证

输出为按年份分区的 Parquet shards，精确 schema：

- `sample_id: string`
- `entity_id: string?`
- `filed_date: date32?`
- `blocks: list<struct<order_index: int32, block_type: string, text: string>>`

Block type 为 `financial_statement`、`note`、`other_context` 或 `unknown`。诊断以 JSONL 保存。Work checkpoint 属于私有 `--work-dir`，不属于已发布 sample 输出。

```bash
quant-dataset filings validate-report-samples \
  --out /path/to/report-samples --cache-root data/raw/sec/financials
```

验证检查精确 Parquet schema、分区/shard 路径、唯一身份、有效 block 内容、行数、诊断与缺失 shard。验证成功说明数据集结构完整，不代表 SEC 覆盖完整或财务正确。SEC HTTP 错误和不支持的文档类型会记录在诊断中；不得将 partial coverage 描述为完整。

`--resume` 用于相同选择及相同 output/work 路径的续跑。不要把已有 output 或 cache 目录当作临时空间。
