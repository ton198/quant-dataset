# quant-dataset

准备并发布**冻结量化数据集**的数据仓库与准备库：负责下载（SEC / Yahoo / FRED）、清洗组织、并构建可复现的训练样本长表。本仓库**不做模型训练**，也不提供回测框架；训练、评估代码属于下游仓库。

"冻结"的含义：`data/output/` 下的每个产物都在 `manifest.json` 中登记 sha256，schema 版本为 `samples_v1`，下游按固定 schema 消费，重建后应逐字节校验。

## 数据流水线

```text
Yahoo / SEC / FRED
        │  download
        ▼
data/raw/            原始快照（SEC 内容寻址 + manifest.json）
        │  organize（per-ticker 多进程）
        ▼
data/organized/      stocks/<T>/{market.csv,financials.csv} + shared/macro.csv
        │  build-samples
        ▼
data/output/         samples/year=YYYY/part-00000.parquet + meta/splits/manifest/qc
```

每一步都可独立重跑；`download` 支持断点续跑，`build-samples` 每次整体重建。

## 当前数据规模

以下数值以 `data/output/manifest.json` 与 `data/output/qc_report.md` 为准（本仓库当前实物）。

| 指标 | 值 |
|---|---|
| 样本行数 | 23,938,669 |
| 列数 | 147 |
| 行粒度 | `(asset_id, date)`，一股票一信号日 |
| 覆盖区间 | 1990-01-02 – 2025-12-31 |
| 分区 | `samples/year=YYYY/part-00000.parquet` × 36（snappy） |
| canonical session | 9,067（当日 is_common 股票数 ≥500 的交易日） |
| 股票数 | 6,532（`meta.parquet`，已过 purge 与 exclusions） |
| 特征列 | 48 `f_raw_*` + 15 `f_cs_*` + 48 `miss_*` = 111 |
| 标签列 | 30 `target_return_1d..30d` + 2 `excess_*` |
| 落盘大小 | ≈6.3 GiB（`du -sh data/output`） |

## 快速开始

```bash
# 1. 环境与密钥
uv sync --frozen
cp config/secrets.example.toml config/secrets.toml
# 编辑 config/secrets.toml，填 [secrets] 的 fred_api_key 与 sec_user_agent

# 2. 下载 + 组织（需要联网，可断点续跑；全量耗时以本机与网络为准）
PYTHONPATH=src .venv/bin/python -m cli.main download \
  --stage all --start 1990-01-01 --end 2025-12-31 --workers 16

# 3. 构建样本包
PYTHONPATH=src .venv/bin/python -m cli.main build-samples
```

产物写入 `data/output/`。安装后也可直接用入口命令 `quant-dataset`。参数细节、工作流与排错见 [docs/user/cli.md](docs/user/cli.md)。

常用变体（完整参数见文档）：

| 场景 | 命令要点 |
|---|---|
| 只看计划不落盘 | `download --stage all --start ... --end ... --dry-run` |
| 单 ticker 补数 | `download --stage market,financials,organize --tickers AAPL --start ... --end ...` |
| 代码改动后重建 | `download --stage organize --force-rebuild` 后重跑 `build-samples` |
| 重建前重下 | `download ... --force`（忽略已有进度） |

## 产物校验

```bash
.venv/bin/python -m pytest                     # 单测
.venv/bin/python - <<'PY'
import hashlib, json, pathlib
root = pathlib.Path("data/output")
manifest = json.loads((root / "manifest.json").read_text())
bad = [name for name, rec in manifest["outputs"].items()
       if hashlib.sha256((root / name).read_bytes()).hexdigest() != rec["sha256"]]
print("sha256 mismatches:", bad)
PY
```

## 文档索引

| 文档 | 内容 |
|---|---|
| [docs/user/cli.md](docs/user/cli.md) | 安装、secrets、两个子命令完整参数、典型工作流、耗时与退出码 |
| [docs/user/data-format.md](docs/user/data-format.md) | 输出文件清单、147 列定义、标签公式、splits/purge、manifest 字段 |
| [docs/user/recommended-usage.md](docs/user/recommended-usage.md) | 训练侧五步流程、duckdb/pyarrow 加载示例、偏差与常见坑 |
| [AGENT.md](AGENT.md) | 开发者与 agent 入口：模块结构、内部约定、测试与扩展方式 |

## 仓库布局

| 路径 | 内容 |
|---|---|
| `data/raw/` | 数据源原始落盘：`yahoo/`、`sec/`（`financials/` + `universe/`）、`fred/` |
| `data/organized/` | 逐 ticker 清洗产物 `stocks/<TICKER>/{market.csv,financials.csv}` 与 `shared/macro.csv` |
| `data/output/` | 冻结样本包（发布产物）：`samples/` + 辅助文件 |
| `config/` | `sources.toml`（非密配置）、`secrets.toml`（本地密钥，已 gitignore）、`universes/` |
| `src/cli/` | 命令行入口；`src/download/` 下载与组织；`src/build_samples.py` 样本构建 |
| `tests/` | pytest 单测 |
| `docs/` | `user/` 使用文档与 `developer/` 开发文档 |

## 运行前提

- Python ≥3.10；依赖见 `pyproject.toml`（pyarrow / pandas / numpy / exchange-calendars / yfinance / torch）。
- 联网访问 Yahoo、SEC、FRED；SEC 要求 User-Agent，FRED 要求 API key。
- 单测：`.venv/bin/python -m pytest`。
- 重建产物前先读 [docs/user/recommended-usage.md](docs/user/recommended-usage.md) 的"常见坑"；`build-samples` 会清空并重建 `data/output/`。
