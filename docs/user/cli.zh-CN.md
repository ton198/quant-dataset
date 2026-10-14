[English](cli.md) | **简体中文**

# CLI 使用手册

CLI 提供 raw `download` 阶段，以及 `filings build-report-samples` / `filings validate-report-samples`。

```bash
quant-dataset --help
quant-dataset download --help
quant-dataset filings --help
```

## 配置

```bash
uv sync --extra dev
cp config/secrets.example.toml config/secrets.toml
```

FRED 下载需要 `[secrets].fred_api_key`；SEC 请求需将 `[secrets].sec_user_agent` 设为真实项目联系信息。妥善保管 `config/secrets.toml`，不得提交。测试禁止访问真实 provider，网络传输必须 mock。

## Raw 下载

```text
quant-dataset download [--stage {market,financials,macros,all}]... \
  [--start YYYY-MM-DD] [--end YYYY-MM-DD] [--tickers A,B,C] \
  [--force] [--dry-run] [--data-dir PATH]
```

| 参数 | 行为 |
|---|---|
| `--stage` | 可重复；默认 `all`，展开为三个 raw 阶段。 |
| `--start` / `--end` | market 的含首尾日期；选择 `market` 时必须同时提供。 |
| `--tickers` | 可选的逗号分隔 ticker 子集。 |
| `--force` | 忽略现有进度并重启选定工作。 |
| `--dry-run` | 显示阶段计划，不联网、不写数据。 |
| `--data-dir` | raw 与进度路径根目录，默认 `data`。 |

`market` 下载 Yahoo 日线，`financials` 下载 SEC Company Facts/submissions 及其引用的历史 submissions 分页，`macros` 下载配置的 FRED 序列。输出保持 provider 原始数据；不再有 organize 阶段。SEC financial metadata 不是 filing 正文档案。

## SEC 报告样本

```text
quant-dataset filings build-report-samples \
  [--out PATH] [--work-dir PATH] [--cache-root PATH] [--limit N] \
  [--filing-id ID]... [--workers N] [--resume]
quant-dataset filings validate-report-samples [--out PATH] [--cache-root PATH]
```

Build 扫描本地 financials cache 中配置的 forms，对 SEC 发出有界请求，解析报告 blocks，并写入 Parquet shards、诊断和 checkpoint。默认 cache root 为 `data/raw/sec/financials`；默认输出为 `data/financial_reports`，默认 work 目录为 `data/.financial_reports.work`。输出和 work 可位于 `data/` 下，但不得与 `data/raw/`、`data/baselines/`、`baselines/`、`archive/`、SEC cache 或保留用户数据重叠。除使用带有本工具诊断与身份元数据的 `--resume` 外，非空输出一律拒绝；不会替换无关文件。`--resume` 复用清洗后的 checkpoint，不复用来源字节。SEC 来源字节在处理后从私有 work area 清除。`--limit` 必须为正数；`--filing-id` 可限定范围，但不会关闭网络。真实构建要求有效 SEC 联系方式；`--workers` 必须为正数。

验证器检查 report-sample schema、shard/partition 一致性、行身份及登记诊断。成功返回 0，运行/配置失败返回 1，参数错误返回 2。测试只使用 mock HTTP；不得请求真实 SEC、Yahoo 或 FRED。
