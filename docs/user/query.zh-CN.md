[English](query.md) | **简体中文**

# 查询已有样本包

`query-samples` 是一个可选命令，可用 SQL 查询已有样本包（bundle）。数据仍保存在原来的 Parquet 文件中；命令直接读取这些文件，不会复制到持久化数据库，也不会修改样本包。Python 用户仍可用 PyArrow 直接读取 Parquet。

## 安装可选命令

普通安装方式不变。只有需要命令行 SQL 查询时，才安装 `query` 可选依赖：

```bash
uv sync --frozen --extra query
# 或
pip install -e '.[query]'
```

## 执行查询

必须明确指定样本包路径和 SQL 语句。`query-samples` 只接受使用当前 `samples` 契约构建的 bundle，不提供历史 schema 标识别名或兼容模式。查询前会核验精确的 132 列 Arrow schema、全部 96 个有序特征、semantic contract、schema/semantic fingerprints，以及 manifest 登记的每个输出文件 SHA-256、字节数和行数。保留的 `data/output/`（manifest 历史标签 `samples_v3`）与原 147 列 backup 都是历史 artifact，不受此命令支持。请用 Python/PyArrow 检查，或先构建全新的当前契约 bundle。以下示例假设候选目录为 `/tmp/opencode/candidate-samples-finance-free`。

```bash
quant-dataset query-samples --bundle /tmp/opencode/candidate-samples-finance-free \
  --sql 'SELECT COUNT(*) AS row_count FROM samples'
```

`samples` 是经过验证的样本文件查询名称。查询结果是带表头的 CSV，写到标准输出（stdout）；通过核验的当前 schema 标签（`samples`）写到标准错误（stderr）。因此可以将结果重定向到文件：

```bash
quant-dataset query-samples --bundle PATH \
  --sql "SELECT date, asset_id, is_common FROM samples WHERE date >= DATE '2020-01-01' AND date < DATE '2021-01-01' ORDER BY date, asset_id" \
  --limit 5 > sample.csv
```

`--limit` 限制返回行数，默认 20 行，可设为 1 到 1,000 行。按日期筛选时使用 `date`；命令不会把分区目录名里的 `year` 自动变成查询字段。

如果样本包内有 `meta.parquet`，还可以查询 `meta`。下面的例子按共有的 `asset_id` 字段连接，只返回样本数据：

```bash
quant-dataset query-samples --bundle PATH \
  --sql 'SELECT s.date, s.asset_id FROM samples AS s JOIN meta AS m USING (asset_id)' \
  --limit 5
```

## 使用范围与限制

- 每次只接受一条 `SELECT` 查询；也允许以 `WITH` 开头的查询。写入语句和多条语句会在运行前被拒绝。
- 每次运行都在内存中使用 DuckDB 读取样本文件。需要临时文件时，会放在输入目录之外；不会创建 `.duckdb` 数据库文件，也不会保存下次运行还要继续使用的 DuckDB 数据库状态（catalog）；输入文件保持不变。
- 查询会检查 manifest 登记的每个文件，包括流式内容 hash/字节数和 Parquet 行数，并核验当前契约身份/schema；但这不会重新生成样本包，也不能替代独立的语义/发布核验。`COUNT(*)` 等聚合会读取样本输入；`--limit` 只限制返回行数，不限制聚合工作量。
- 这是本机使用的工具，不是专门隔离不可信 SQL 的安全环境，也不可作为公开 SQL 服务开放。只运行自己信任的查询，并指定自己确实要读取的样本包。

DuckDB 只是可选的读取方式，Parquet 仍是实际存储格式。该命令不会抓取完整申报文件，也不会生成财务表。样本包格式见[数据格式说明](data-format.zh-CN.md)；既有下载与构建命令见[CLI 手册](cli.zh-CN.md)。
