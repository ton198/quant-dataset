# 架构

定位：**从外部数据源到冻结训练样本的离线流水线**——`data/raw/`（只追加的原始快照）→ `data/organized/`（按 ticker 的日频面板）→ `data/output/`（样本包 + 审计文件）。行为由 `src/` 代码与 `config/` 定义；`data/`、`logs/`、`archive/` 不进版本控制。

改代码前先读 [AGENT.md](../../AGENT.md) 的任务路由表；各层字段级契约见 [data-contracts.md](data-contracts.md)；下载细节见 [download.md](download.md)；样本细节见 [samples.md](samples.md)。

## 1. 数据流总览

```
外部源                                  data/raw/（payload 文件名 = sha256，只追加）            data/organized/                 data/output/
──────                                  ──────────────────────────────────────────────            ────────────────                ────────────
SEC company_tickers_exchange ─┐
config/universes/*.json ──────┴──→  sec/universe/<sha256>.json ──────┐
Yahoo Finance ───────────────────→  yahoo/<TICKER>/<start>_<end>.csv  ├─→ stocks/<T>/market.csv     ─┐
SEC companyfacts / submissions ──→  sec/financials/<sha256>.json      │   stocks/<T>/financials.csv ─┼─→ build-samples ─→ samples/year=YYYY/
（含历史申报分页）                   + manifest.json（key → 版本列表）   │   shared/macro.csv          ─┘      + meta / manifest / splits
FRED observations ───────────────→  fred/<SERIES>/<sha256>.json       │                                          / qc_report
                                    + manifest.json                   ┘
```

生命周期五个环节：

1. **universe**：抓 SEC `company_tickers_exchange` 快照 → 按 `config/sources.toml [universe].exchanges` 过滤（当前 Nasdaq + NYSE）→ 合并 `config/universes/ticker_cik_overrides.json` 人工纠正（当前仅 `XOM → 0000034088`）。
2. **下载**（CLI stage：`market` / `financials` / `macros`）：Yahoo 日线；SEC Company Facts + Submissions + 历史申报分页；FRED 11 条宏观序列。raw 层 payload 只追加、不改写。
3. **organize**（CLI stage：`organize`）：行情清洗 + 派生列；财报 as-of 对齐到 session；宏观 wide 表；按 ticker 多进程（`--workers`，默认 16）。
4. **build-samples**（独立命令）：用全市场行情自举 canonical calendar → 流式逐 ticker 生成特征/标签 → 逐日截面 rank → 按年分区写 parquet + 审计文件。
5. **消费**：训练侧按 `data/output/manifest.json` 哈希验货、按 `splits.json` 选窗，**不再自行 purge**。

## 2. 模块职责

| 模块 | 职责 | 关键入口 |
|---|---|---|
| `src/cli/main.py` | 唯一命令行入口；参数解析、阶段展开、退出码 | `_parser()`、`main()` |
| `src/download/manager.py` | 编排一次 download 运行：ticker 选择、进度锁、逐 stage 执行、organize 进程池 | `run_download()`、`_organize_tickers()` |
| `src/download/universe.py` | SEC 名单快照（内容寻址缓存）+ ticker→CIK override 合并 | `fetch_universe()`、`load_ticker_cik_overrides()` |
| `src/download/market.py` | Yahoo 日线下载（yfinance，auto_adjust=false） | `fetch_market()` |
| `src/download/financials.py` | SEC companyfacts / submissions / 历史分页下载；内容寻址 + manifest 追加 | `fetch_financials()` |
| `src/download/macros.py` | FRED observations 下载；内容寻址 + manifest 追加 | `fetch_macros()` |
| `src/download/organize.py` | 行情清洗/派生列/`quality_flag`；macro wide 表与可见性规则；写 `_meta.json` | `organize_market()`、`organize_macros()` |
| `src/download/organize_financials.py` | 申报事实抽取（concept 白名单）、fiscal 标识回退、按 session as-of 展开 | `organize_financials()` |
| `src/download/progress.py` | 断点续跑工作清单（原子写）+ PID 文件锁（死锁自动回收） | `initialize()`、`save_atomic()`、`acquire_lock()` |
| `src/download/config.py` | 读 `config/sources.toml` / `config/secrets.toml` 并解析为 frozen dataclass | `load_sources()`、`load_secrets()` |
| `src/download/errors.py` | 三类领域异常 | `ConfigError` / `DownloadError` / `OrganizeError` |
| `src/build_samples.py` | 样本长表：canonical calendar、特征/标签、purge、截面 rank、分区写出与审计 | `build_samples()` |
| `config/` | 端点与阶段配置、密钥（gitignored）、universe 覆盖/排除 | `sources.toml`、`secrets.toml`、`universes/*.json` |
| `tests/` | 全部离线测试（6 个文件） | 见 [testing.md](testing.md) |

## 3. 数据落点与生命周期

| 落点 | 生产者 | 内容 | 更新语义 | 消费者 |
|---|---|---|---|---|
| `data/raw/yahoo/<T>/<start>_<end>.csv` | `market.fetch_market` | yfinance 原始 CSV（Adj Close/Close/Dividends/High/Low/Open/Stock Splits/Volume） | 同名重下覆盖（**非**内容寻址） | `organize.organize_market` |
| `data/raw/sec/universe/<sha256>.json` | `universe.fetch_universe` | 全量 SEC 名单快照（`fields`/`data`） | 新内容新文件；文件名 = 内容 sha256，读取时校验 | `manager._cached_universe`、`organize_financials._ticker_mappings` |
| `data/raw/sec/financials/<sha256>.json` | `financials.fetch_financials` | companyfacts / submissions / 历史分页 payload | 只追加，从不改写 | `organize_financials` |
| `data/raw/sec/financials/manifest.json` | 同上 | `logical_key` → 版本列表（path/sha256/url/...） | tmp + rename 原子替换，仅追加版本 | 同上 |
| `data/raw/fred/<SERIES>/<sha256>.json` + `fred/manifest.json` | `macros.fetch_macros` | FRED observations | 同上（append-only manifest） | `organize.organize_macros` |
| `data/organized/stocks/<T>/market.csv` | `organize.organize_market` | 清洗后的行情面板 | 重跑覆盖；`_meta.json` 记录输入/输出哈希 | `build_samples` |
| `data/organized/stocks/<T>/financials.csv` | `organize_financials` | session 级 as-of 财报快照 | 重跑覆盖 | `build_samples` |
| `data/organized/stocks/<T>/_meta.json` | 两个 organize | 完成标记 + 输入/输出 sha256 + row_counts | 重跑合并更新 | `manager._ticker_is_organized` |
| `data/organized/shared/macro.csv` | `organize.organize_macros` | session 对齐宏观 wide 表 | 重跑覆盖 | `build_samples` |
| `data/output/{samples,meta.parquet,manifest.json,splits.json,qc_report.*}` | `build_samples` | 样本包 | 每次全量重建（先清空旧输出） | 训练侧 |
| `data/.download_progress{,.tmp,.lock}` | `progress` | 断点续跑工作清单、写临时文件、PID 锁 | 原子替换；`--force` 丢弃重建 | 仅 `manager` |
| `logs/`、`archive/` | 驱动脚本/人工 | 运行日志、历史冷存 | gitignored，手动管理 | 无 |

配置与运行状态补充：

| 路径 | 内容 |
|---|---|
| `config/sources.toml` | `[universe]` 端点与 exchange 白名单；`[market]`/`[financials]`/`[macros]` 节流、超时、重试；`[macros].series` 序列清单；`[download]` 路径 |
| `config/secrets.toml` | `[secrets] sec_user_agent`、`fred_api_key`；已 gitignore（模板 `secrets.example.toml`） |
| `config/universes/ticker_cik_overrides.json` | ticker→CIK 人工纠正（当前 XOM → 0000034088） |
| `config/universes/exclusions_v1.json` | 样本构建排除名单（当前 AYA、FUND） |
| `data/.download_progress` | JSON 工作清单：`run_id`、`started_at_utc`、`universe_source`、`stages{stage:{item: done / pending / failed:...}}`；配套 `.tmp`（写临时）与 `.lock`（PID 锁） |
| `archive/` | 本地冷存（如 `2026-09-24_pre_download_v2/` 为迁移前整仓备份）；不进 git，人工管理 |

## 4. 调度与并发

- 一次 `download` 运行全程持有 `data/.download_progress.lock`（PID 文件锁）；**同一 data-dir 不可并行跑两轮**。organize 进程池只读 `raw/`，写各自 ticker 目录。
- `--force`：忽略旧 progress，按选中 stage 重建工作清单（会重新下载）。`--force-rebuild`：只强制重刷 organize 产物，不影响下载进度。
- organize 按 ticker 幂等：`_meta.json` 记录 outputs 且文件 sha256 可核验时跳过；`--force-rebuild` 或元数据损坏时重做。
- `--stage organize` 未给 `--start/--end` 时，organize calendar 取 `1990-01-01`..今天（`manager.run_download`）。organize-only 重跑会把 `financials.csv` 延伸到今天；build-samples 只取样本窗口内的 canonical sessions，不受影响。
- build-samples 全程流式，内存不随全市场行数增长：50 tickers/批暂存 → 40 sessions/块算截面 rank → 按年 `ParquetWriter` 追加。

## 5. 确定性机制

| 机制 | 位置 |
|---|---|
| 稳定排序（`kind="mergesort"`；market.csv 按 date、重复日期 keep last） | `build_samples`、`organize_market` |
| JSON 一律 `sort_keys=True` + 临时文件原子替换 | 所有 manifest / splits / progress / `_meta.json` |
| raw payload 内容寻址（文件名 = sha256） | `universe` / `financials` / `macros` |
| 全量输出 sha256 登记（`manifest.json` 自身除外） | `build_samples._hash_output_files` |
| canonical axis 由数据决定而非硬编码日历 | `build_samples`（≥500 common tickers 的 XNYS sessions） |

## 6. 测试布局

| 文件 | 覆盖 |
|---|---|
| `tests/test_download.py` | secrets/sources 加载、progress 原子写、market 清洗、financial as-of、CLI 帮助 |
| `tests/test_organize_parallel.py` | 进程池与串行结果一致、跳过/重做、共享 CIK、失败隔离 |
| `tests/test_organize_financials.py` | concept 优先级、fiscal 标识回退、三种 submissions payload 形态解析 |
| `tests/test_build_samples.py` | 标签/特征/as-of/purge/输出 dtype 与 exclusions |
| `tests/test_progress_lock.py` | 陈旧锁回收、活锁拒绝 |
| `tests/test_ticker_overrides.py` | XOM CIK override 正反向查找、`--force-rebuild` |
