[English](cli.md) | **简体中文**

# CLI 使用手册

CLI 定义在 `src/cli/main.py`，有两个子命令：`download` 与 `build-samples`。安装（`pip install -e .` / `uv sync`）后入口为 `quant-dataset`；开发态等价命令：

```bash
PYTHONPATH=src .venv/bin/python -m cli.main --help
```

## 1. 前置准备

### 1.1 环境

```bash
uv sync --frozen                 # 按 uv.lock 安装依赖；或 pip install -e .
PYTHONPATH=src .venv/bin/python -m cli.main --help
```

### 1.2 密钥（config/secrets.toml）

```bash
cp config/secrets.example.toml config/secrets.toml
```

| `[secrets]` key | 用途 | 示例 |
|---|---|---|
| `sec_user_agent` | SEC 全部请求的 User-Agent，格式 `Name email` | `Jane Doe jane@example.com` |
| `fred_api_key` | FRED observations API key | `abcdef0123456789` |

- `config/secrets.toml` 已被 `.gitignore` 排除，不要提交；泄漏后到 FRED/SEC 侧轮换。
- 凡是要加载 secrets 的阶段（SEC 或 FRED 请求），两个 key 都必须存在，否则会报 `Missing required secret '<key>'`。
- 纯 `--stage market --tickers A,B`（不跑 financials/macros）不加载 secrets。

## 2. download

```text
quant-dataset download [--stage {market,financials,macros,organize,all}]... \
  [--start YYYY-MM-DD] [--end YYYY-MM-DD] [--tickers A,B,C] [--force] \
  [--force-rebuild] [--dry-run] [--data-dir PATH] [--workers N]
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--stage` | `all` | 可重复给多个；`all` 展开为 market + financials + macros + organize（去重保序） |
| `--start` / `--end` | 无 | market 阶段的起止（闭区间）；选择 market 时二者必填 |
| `--tickers` | 无（全 universe） | 逗号分隔子集，内部转大写；不在 SEC universe 中的 ticker 会被忽略并告警 |
| `--force` | off | 忽略 `data/.download_progress`，强制重下已完成的条目 |
| `--force-rebuild` | off | organize 阶段即使产物已完整也重建（代码改动后重算用） |
| `--dry-run` | off | 只打印将要执行的阶段与范围后返回，不联网、不写盘 |
| `--data-dir` | `data` | raw / organized / progress 的基目录（相对仓库根解析） |
| `--workers` | 16 | organize 的 per-ticker 多进程数；<1 报错 |

各阶段落盘位置：

| stage | 数据源 | 输出 |
|---|---|---|
| market | Yahoo 日线（yfinance） | `data/raw/yahoo/<TICKER>/<start>_<end>.csv` |
| financials | SEC Company Facts + Submissions + 历史申报分页 | `data/raw/sec/financials/<sha256>.json` + `manifest.json`（内容寻址） |
| macros | FRED 11 个序列 | `data/raw/fred/<SERIES>/*.json` |
| organize | 上述 raw | `data/organized/stocks/<T>/{market.csv,financials.csv}`、`data/organized/shared/macro.csv` |

进度与续跑（`data/.download_progress`）：

- 按 `(stage, item)` 记录状态；中断后重跑同一命令会跳过 `done`，重试 `pending/failed`。
- 完成状态与日期范围无关：ticker 标记 `done` 后再改 `--start/--end` 仍会被跳过，需加 `--force` 才会重下。
- `--force` 会用当前命令重建整份进度清单（不是逐项重试）；成功结束后进度文件仍保留，兼作重下清单。

## 3. build-samples

```text
quant-dataset build-samples [--data-dir data/organized] [--out data/output] \
  [--exclusions-file config/universes/exclusions_v1.json]
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--data-dir` | `data/organized` | 读取 `stocks/` 与 `shared/macro.csv` |
| `--out` | `data/output` | 样本包输出目录；会先清空并重建 `samples/`、`meta.parquet`、`splits.json`、`manifest.json`、`qc_report.*` |
| `--exclusions-file` | `config/universes/exclusions_v1.json` | 剔除清单；当前默认剔除 `AYA`、`FUND` |

行为要点：无断点续跑，每次都整体重建；任一 ticker 级失败非空时日志输出失败明细并返回退出码 1。

## 4. 典型工作流

首次全量（按阶段顺序下载并组织，然后构建样本）：

```bash
PYTHONPATH=src .venv/bin/python -m cli.main download \
  --stage all --start 1990-01-01 --end 2025-12-31 --workers 16
PYTHONPATH=src .venv/bin/python -m cli.main build-samples
```

增量 / 中断续跑（直接重跑原命令；只处理未完成项；失败项自动重试）：

```bash
PYTHONPATH=src .venv/bin/python -m cli.main download \
  --stage all --start 1990-01-01 --end 2025-12-31 --workers 16
```

单 ticker 补数（比如补某只股票的市场数据与财报，并只重组织它）：

```bash
PYTHONPATH=src .venv/bin/python -m cli.main download \
  --stage market,financials,organize --tickers AAPL \
  --start 1990-01-01 --end 2025-12-31 --workers 1
```

代码改动后重建产物（organize/financials 逻辑更新时，先 `--force-rebuild`，再整体重建样本包）：

```bash
PYTHONPATH=src .venv/bin/python -m cli.main download \
  --stage organize --force-rebuild --workers 16
PYTHONPATH=src .venv/bin/python -m cli.main build-samples
```

只想看计划不落盘：

```bash
PYTHONPATH=src .venv/bin/python -m cli.main download \
  --stage all --start 1990-01-01 --end 2025-12-31 --dry-run
```

## 5. 耗时参考

以下为参考值（16 workers，网络正常）；其中只有 organize/build-samples 是项目运行记录中的本机实测，网络阶段耗时受数据源限速影响而波动，未固化。

| 步骤 | 参考耗时 |
|---|---|
| market 全 universe（6,500+ ticker，1990–2025） | 视 Yahoo 限速与重试而定（数小时量级） |
| financials 全 universe（SEC 分页） | 视 SEC 限速而定（数小时量级） |
| organize 全量 | ≈12 分钟（本机实测参考） |
| build-samples 全量 | ≈2 小时 50 分（本机实测参考） |

## 6. 退出码与常见错误

| 退出码 | 触发 |
|---|---|
| 0 | 成功（含 `--dry-run`，以及 `build-samples` 无 ticker 失败） |
| 1 | `download` 有 per-item 失败或配置错误；`build-samples` 有 ticker 级失败 |
| 2 | argparse 参数错误：缺 `--start/--end`（选了 market）、`--end < --start`、`--workers < 1` |

| 现象 | 原因 / 处理 |
|---|---|
| `Missing required secret 'sec_user_agent'`（或 `fred_api_key`） | 未建 `config/secrets.toml` 或 `[secrets]` 表缺 key；按 1.2 补齐 |
| `--start and --end are required when market stage is selected` | market 阶段必填；或改用不含 market 的 `--stage financials,macros,organize` |
| 下载后 ticker 数量少于 universe | SEC universe 与 Yahoo 覆盖差异；部分目录无 market.csv，属已知情况（见 `manifest.json` 的 `input_inventory`） |
| build-samples 报 `organized stocks directory does not exist` | `--data-dir` 指向了未组织的目录；先跑 `--stage organize` |
| 重建后想确认产物未变 | 对比 `data/output/manifest.json` 的 `outputs[*].sha256` |
