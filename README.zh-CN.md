# quant-dataset

一个冻结的量化训练数据集（美国股票，1990–2025），以及从源头重建它的可复现流水线。

[![CI](https://github.com/ton198/quant-dataset/actions/workflows/ci.yml/badge.svg)](https://github.com/ton198/quant-dataset/actions/workflows/ci.yml)

[English](README.md) | **简体中文**

## 这是什么

`data/output/` 里是 2,394 万行样本：特征只用当天已知的信息，标签是未来收益。
"冻结"指 schema（`samples_v1`）固定、每个文件都用 sha256 锁定；同样的输入重建，应当逐字节一致。
本仓库负责造数据，不训练模型、不做回测——那些属于下游仓库。

| 事实 | 数值 |
|---|---|
| 行 × 列 | 23,938,669 × 147 |
| 股票数 | 6,532 |
| 交易日 | 9,067 个 canonical session |
| 覆盖区间 | 1990-01-02 – 2025-12-31 |
| 文件组织 | 36 个按年 Parquet 分区 + `meta` / `splits` / `manifest` / `qc` |
| 许可证 | MIT |

canonical session = 用来对齐全部股票的统一交易日历：当天普通股（`is_common`）数量 ≥500 才算一天。

## 一行样本是什么

一行 = 一只股票（`asset_id`）× 一个信号日（`date`）。
特征 = 当天就能知道的信息：48 个原始特征（`f_raw_*`，量价、财务、宏观）+ 15 个截面特征（`f_cs_*`）+ 48 个缺失标志（`miss_*`）。
标签 = 未来 1–30 个交易日的收益，外加 `excess_5d` / `excess_21d`（相对市场的超额）。

```text
信号日 t                     t+1 开盘入场              t+1+h 开盘出场
    │                            │                          │
    │ 特征止于 t                 │ 标签窗口：h 个 session
    └────────────────────────────┴──────────────────────────┘
      h = 1..30 个 canonical session；主目标 h = 5 和 21
```

## 30 秒上手

用 DuckDB 直接读 `data/output/`（duckdb 是消费侧工具，不在本仓库依赖里）：

```python
import duckdb

con = duckdb.connect()
df = con.execute("""
    SELECT date, asset_id, excess_5d, excess_21d,
           f_cs_momentum_120, f_cs_volatility_20
    FROM read_parquet('data/output/samples/year=*/part-00000.parquet',
                      hive_partitioning = true)
    WHERE year BETWEEN 2019 AND 2020
      AND is_common
      AND flag_extreme_label = 0
""").df()
```

别一次读全量：float32 特征矩阵约 10 GB，按年读最省内存。
更多加载方式和常见坑见[推荐用法](docs/user/recommended-usage.zh-CN.md)。

## 怎么用

| 步骤 | 做法 |
|---|---|
| 1. 切分 | 按 `splits.json` 分四段：fit（1990–2018）训练、select（2019–2020）调参、screen（2021–2024）样本外、reserve（2025）最后只用一次。构建期已经 purge（剔掉标签会跨段的边界样本），下游不要再 purge。 |
| 2. 目标 | 主目标用 `excess_5d` / `excess_21d`，它们已经减掉同日普通股等权均值；`target_return_*` 只作辅助任务。 |
| 3. 清洗 | 剔除或降权 `flag_extreme_label = 1` 的 120,510 行（约 0.5%）：这些行的标签窗口穿过价格毛刺，收益不可信。 |

两条最容易踩的坑：

- **幸存者偏差**：股票池是当前的 SEC 名单，没有退市股，历史收益偏乐观。真实检验看 screen 段，不要把绝对收益外推。
- **财报覆盖稀疏**：缺失约 96%，而且不是随机缺失——快照语义下只能看到最新一期申报。用 `miss_*` 掩码建模，别把 NaN 填成 0。

完整流程见[推荐用法](docs/user/recommended-usage.zh-CN.md)。

## 怎么从头重建

前置条件：Python ≥3.10（CI 覆盖 3.10–3.13）；能访问 Yahoo、SEC、FRED；在 `config/secrets.toml` 里填好 `sec_user_agent`（SEC 要求留联系方式）和 `fred_api_key`。

```bash
uv sync --frozen
cp config/secrets.example.toml config/secrets.toml   # 然后填两个 key

PYTHONPATH=src .venv/bin/python -m cli.main download \
  --stage all --start 1990-01-01 --end 2025-12-31 --workers 16
PYTHONPATH=src .venv/bin/python -m cli.main build-samples

.venv/bin/python -m pytest
```

`download` 支持断点续跑；`build-samples` 每次都会清空重建 `data/output/`。
完整参数和退出码见 [CLI 使用手册](docs/user/cli.zh-CN.md)。

## 仓库布局

| 路径 | 内容 |
|---|---|
| `data/` | `raw/`（原始快照，只追加）→ `organized/`（逐股票清洗）→ `output/`（冻结数据集） |
| `src/` | CLI 入口、下载与组织、`build_samples.py` 样本构建 |
| `config/` | 股票池、`sources.toml`、本地 `secrets.toml`（已 gitignore） |
| `docs/` | `user/` 使用文档与 `developer/` 开发文档 |
| `tests/` | 离线 pytest（基线：61 passed / 2 skipped） |

## 文档

| 用户文档 | 开发文档 |
|---|---|
| [CLI 使用手册](docs/user/cli.zh-CN.md)：命令、密钥、退出码 | [AGENT.md](AGENT.md)：agent 与贡献者的任务路由 |
| [数据格式](docs/user/data-format.zh-CN.md)：147 列全表、标签公式 | [architecture.zh-CN.md](docs/developer/architecture.zh-CN.md)：五环节数据流 |
| [推荐用法](docs/user/recommended-usage.zh-CN.md)：训练流程与常见坑 | [data-contracts.zh-CN.md](docs/developer/data-contracts.zh-CN.md)：各层契约与基线 |
| | [samples.zh-CN.md](docs/developer/samples.zh-CN.md)：canonical 日历、purge、分区 |
| | [download.zh-CN.md](docs/developer/download.zh-CN.md)：数据源端点、续跑、CIK 纠正 |
| | [known-quirks.zh-CN.md](docs/developer/known-quirks.zh-CN.md)："看着像 bug 其实不是" |
| | [testing.zh-CN.md](docs/developer/testing.zh-CN.md)：测试布局与离线约定 |

## 开发

基线：`pytest` 61 passed / 2 skipped，`ruff check`、`ruff format --check` 干净；CI 在 Python 3.10–3.13 上跑同一套。
贡献见 [CONTRIBUTING.md](CONTRIBUTING.md)；agent 先读 [AGENT.md](AGENT.md)。

## 已知局限

- **幸存者偏差**：股票池是今天的 SEC 名单，退市股不在其中，历史显得比实际更好。
- **FRED 不是 vintage**：宏观特征用的是最新修正值，带有事后信息。
- **adjusted open 不是成交价**：标签按 `open × adj_close / close` 计算，不能当作可成交价格。
- **财报覆盖稀疏且非随机**：缺失约 96%，申报密集期和大公司覆盖更好。

更多条目见 [known-quirks.zh-CN.md](docs/developer/known-quirks.zh-CN.md)。

## 许可证

MIT — 见 [LICENSE](LICENSE)。
