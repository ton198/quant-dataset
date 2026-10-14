# quant-dataset

离线原始数据摄取工具，支持 Yahoo 行情、SEC Company Facts/submissions、FRED 观测数据，并提供有界 SEC 报告样本构建与验证。本项目不训练模型，也不做回测。

[English](README.md) | **简体中文**

## 安装

```bash
uv sync --extra dev
quant-dataset --help
```

将 `config/secrets.example.toml` 复制为 `config/secrets.toml`；按需填写 FRED API key 与真实 SEC 联系邮箱 User-Agent。不得提交凭据。测试网络行为时必须 mock，不访问真实 provider。

## 原始 provider 下载

```bash
quant-dataset download --stage macros
quant-dataset download --stage financials
quant-dataset download --stage market --start 2024-01-01 --end 2024-12-31 --tickers AAPL
quant-dataset download --stage all --start 2024-01-01 --end 2024-12-31
```

阶段为 `market`、`financials`、`macros`、`all`。`all` 包含需要日期范围的 market。输出保留在 `data/raw/`，进度可恢复。`financials` 保存 Company Facts 与 submissions 资源，不下载完整申报文件。下载器不将 raw 数据组织为派生面板。

## SEC 报告样本

`filings build-report-samples` 扫描本地 SEC financials cache 中配置的申报表单，从 SEC 获取选定报告文档，清理为带类型的文本 blocks，并写出分片 Parquet 与诊断信息。该工作流只通过 SEC 获取网络资源，测试必须 mock。真实构建要求 `config/secrets.toml` 中有有效 SEC 联系方式。

```bash
quant-dataset filings build-report-samples \
  --cache-root data/raw/sec/financials \
  --out /path/to/new/report-samples \
  --work-dir /path/to/new/work \
  --limit 10 --workers 4
quant-dataset filings validate-report-samples \
  --out /path/to/report-samples --cache-root data/raw/sec/financials
```

必须使用全新空的 output/work 目录。不得将命令目标设为保留数据、baseline 或 cache 内容。构建支持 checkpoint 与 `--resume`；来源原始字节仅临时保留，处理后删除。验证器检查精确 schema、分区、shard、行身份及输出完整性。`--limit` 可限制离线/mock smoke 的工作量，但真实运行仍会联网。

## 文档

- [CLI 指南](docs/user/cli.zh-CN.md)
- [SEC 报告样本](docs/user/filings.zh-CN.md)
- [下载实现](docs/developer/download.zh-CN.md)
- [架构](docs/developer/architecture.zh-CN.md)
- [测试](docs/developer/testing.zh-CN.md)
- [AGENT 导航](AGENT.zh-CN.md)

## 数据保护

现有 `data/`、`baselines/`、archive snapshots 与 `config/secrets.toml` 均为用户数据。代码清理不得删除或改写这些内容。

## 许可证

MIT — 见 [LICENSE](LICENSE)。
