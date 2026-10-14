[English](testing.md) | **简体中文**

# 测试

测试必须离线。网络行为使用 mock transport；不得请求真实 SEC、Yahoo 或 FRED。测试使用 `tmp_path`，不得写入保留的 `data/`、baselines、archive snapshots 或凭据。

## 当前保留测试套件

```bash
uv run pytest -q
uv run ruff check
```

定向测试：

```bash
uv run pytest -q tests/test_download.py tests/test_progress_lock.py
uv run pytest -q tests/test_filing_catalog.py tests/test_filing_sec_client.py
uv run pytest -q tests/test_content_parse.py tests/test_report_samples_workflow.py
```

`test_report_samples_workflow.py` 覆盖 mock SEC 请求、filing 选择/fallback、worker 行为、resume、安全路径、streaming shards 与 validator 拒绝场景。`test_content_parse.py` 覆盖清洗 blocks 和内容结构。`test_filing_catalog.py` 仅读取本地 cache fixtures。`test_download.py` 覆盖 raw 配置、进度、锁、CLI 拒绝与 dry-run。

CLI smoke 可运行 help 与使用隔离目录的 dry-run。不得将真实 provider 下载作为测试。

## 测试设计

固定消费者可观察行为：schema/type、identity、selection precedence、partial/error 状态、resume 转换、有界并发、安全路径与清理。避免只断言 wiring 或实现细节。新增永久测试应使用确定性 fixtures 并独立核对预期结果。已退役产品的测试应删除，不保留死接口。
