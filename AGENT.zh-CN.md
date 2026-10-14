# AGENT 导航

本仓库保留三个 raw 下载阶段（`market`、`financials`、`macros`），以及独立的 SEC 报告样本构建/验证工作流。保护用户数据：已有 `data/`、`baselines/`、archive snapshots 与 `config/secrets.toml` 不得因代码清理而删除或改写。所有网络测试必须 mock SEC、Yahoo 和 FRED。

[English](AGENT.md) | **简体中文**

## 任务路由

| 任务 | 代码入口 | 验证 |
|---|---|---|
| Raw 下载 | `src/cli/main.py`、`src/download/{manager,market,financials,macros,universe,progress,config}.py`、`config/sources.toml` | `tests/test_download.py`；CLI smoke 使用隔离的 `--data-dir`。 |
| SEC 报告样本 | `src/filings/{catalog,document_selection,sec_client,download,content_parse,report_samples,cli}.py` | `tests/test_report_samples_workflow.py`、`tests/test_content_parse.py`；所有 HTTP 必须 mock。 |
| Filing identity/cache reader | `src/filings/{catalog,models}.py` | `tests/test_filing_catalog.py`；仅用本地 fixtures。 |
| CLI 或配置 | `src/cli/main.py`、`src/filings/cli.py`、`src/download/config.py` | 检查 `--help`、参数拒绝，以及隔离的离线/dry-run 路径。 |

## 不变式

1. 代码清理不得手工编辑 raw provider 数据，也不得删除保留的用户产物。
2. SEC 文档获取受 selection、worker 数、响应限制和私有 work 目录约束。来源字节仅临时保存；checkpoint 输出只包含清洗样本和诊断。
3. Output/work 路径必须全新，且不得与 cache 或受保护用户数据重叠。拒绝符号链接组件和非空 output。
4. Resume 只复用按当前 filing ID 请求范围校验过的清洗 checkpoint；不依赖先前下载的来源字节。
5. 报告样本 block 使用 `docs/user/filings.zh-CN.md` 所述精确 schema；保持 `sample_id` identity、申报年份分区、唯一 shard 路径与诊断可见性。
6. 测试不得访问真实 SEC、Yahoo 或 FRED；通过 mock transport 和本地 fixtures 验证行为。
7. CLI 行为变化时同步更新中英文用户指南。

## 文档地图

- [CLI 手册](docs/user/cli.zh-CN.md) / [English](docs/user/cli.md)
- [SEC 报告样本](docs/user/filings.zh-CN.md) / [English](docs/user/filings.md)
- [下载实现](docs/developer/download.zh-CN.md) / [English](docs/developer/download.md)
- [架构](docs/developer/architecture.zh-CN.md) / [English](docs/developer/architecture.md)
- [测试](docs/developer/testing.zh-CN.md) / [English](docs/developer/testing.md)
