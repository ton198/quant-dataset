[English](testing.md) | **简体中文**

# testing.md — 测试布局、跑法与验证门槛

> 适用：加/改测试、判断改动能否合并、复现 smoke gate。
> 关联：[AGENT.zh-CN.md](../../AGENT.zh-CN.md) §1 路由表、[download.zh-CN.md](download.zh-CN.md)、[samples.zh-CN.md](samples.zh-CN.md)、[known-quirks.zh-CN.md](known-quirks.zh-CN.md)。

## 1. 跑法与基线

```bash
# 标准命令（从仓库根目录执行）
PYTHONPATH=src .venv/bin/python -m pytest tests/ -q
# 等价写法：测试文件自身会把 src/ 插入 sys.path，因此下面也能跑
.venv/bin/python -m pytest -q

# 单文件
PYTHONPATH=src .venv/bin/python -m pytest tests/test_build_samples.py -q
PYTHONPATH=src .venv/bin/python -m pytest tests/test_download.py -q -k cli
```

| 指标 | 基线（实测） |
|---|---|
| 收集 | 63 tests |
| 结果 | **61 passed, 2 skipped**（本机约 40 秒，视硬件而定） |
| 警告 | `test_organize_parallel.py`/`test_ticker_overrides.py` 在 ProcessPool 下产生 `DeprecationWarning: fork() ... multi-threaded`，属预期 |
| 依赖 | 全部离线：不发真实 HTTP；无 `secrets.toml` 也能跑（config 测试用 tmp 文件） |

注意：测试进程不依赖真实 `data/`；只有 `test_load_sources_parses_provider_settings` 与 CLI dry-run 子进程会读仓库的 `config/sources.toml`（无网络）。

## 2. 测试文件地图

| 文件 | 数量 | 覆盖 | 代表测试 |
|---|---|---|---|
| `tests/test_download.py` | 26 | `config`（4）、`progress`（6）、`acquire_lock`（1）、`organize_market` 质量/派生（6，含 2 skip）、`organize_financials` as-of 可见性（4）、CLI（5） | `test_progress_atomic_save_and_load_round_trip`、`test_market_adjustment_factor_and_one_day_return`、`test_financial_filing_becomes_visible_on_next_calendar_session`、`test_cli_market_stage_requires_dates` |
| `tests/test_build_samples.py` | 7 | 端到端样本构建、标签公式、截面秩、`miss_*`、purge、inf→NaN、dtype/schema、YoY 主路径/兜底/优先级 | `test_training_sample_build_labels_features_exclusions_and_splits`、`test_build_purges_label_windows_at_split_boundaries`、`test_fiscal_identifiers_take_precedence_over_closer_report_end` |
| `tests/test_organize_financials.py` | 11 | `_owned_paths` 不读无关 JSON/回退、无输入仍写 missing 行、ticker 缓存、`_CONCEPTS` 白名单、新旧 tag 优先级、Apple 式 fiscal 兜底、三种申报形状、ragged 拒绝、page-only 可见、accession 去重 | `test_submission_rows_parses_recent_table_and_column_page_shapes`、`test_column_oriented_payload_rejects_ragged_arrays`、`test_page_only_old_filing_fact_becomes_available_at_its_filing_date` |
| `tests/test_organize_parallel.py` | 8 | 并行=串行一致、完成即跳过、partial/corrupt 产物重组、共享 CIK 独立输出、空 CIK、无行情有 CIK、失败隔离 | `test_process_results_match_serial_organization`、`test_corrupt_meta_or_missing_recorded_output_is_reorganized`、`test_one_ticker_failure_does_not_block_other_tickers` |
| `tests/test_progress_lock.py` | 6 | stale PID 回收、存活 PID 阻止、正常释放、空/非法 PID（3 个参数化） | `test_stale_pid_lock_is_reclaimed`、`test_live_pid_lock_is_not_reclaimed` |
| `tests/test_ticker_overrides.py` | 5 | XOM override 前向/反向、缺文件回退、CLI flag、`--force-rebuild` 语义 | `test_xom_forward_lookup_uses_vetted_cik_override`、`test_missing_override_file_preserves_snapshot_mappings`、`test_force_rebuild_reorganizes_completed_tickers` |

## 3. 两个 skip 到底是什么

两个都来自 `tests/test_download.py`，是“刻意记录未实现行为”而非环境导致：

| 测试 | skip 原因（实义） |
|---|---|
| `test_market_zero_close_flag_case_is_not_supported` | `close == 0` 当前被 `_market_quality` 归为 `invalid_ohlc`：只有 `close < 0` 才是 `negative_price`，`close == 0` 命中后面的 `close <= 0` invalid 分支。若产品要求 close=0 单独成类，需要改代码+改测试 |
| `test_market_non_session_quality_flag_is_not_supported` | `organize_market` 在赋 `quality_flag` 之前已把非 calendar 日期过滤掉，因此不存在“非 session”行可打 flag；非 session 只体现为 `market_dropped_non_session` 计数 |

改 `_market_quality` 或 organize 过滤顺序时，这两个 skip 会变成真实的行为变更点：要么实现并转正，要么确认继续 skip 并同步本文。

## 4. fixture 约定

| 约定 | 说明 |
|---|---|
| 临时目录 | 一律 `tmp_path`；测试不写仓库 `data/`（唯一例外是只读 `load_sources`） |
| 构造 fake raw | `test_download._write_market_csv/_bar` 造 Yahoo 形状 CSV；`test_organize_financials._write_company_facts_fixture/_fact` 造 facts+submissions+manifest；`test_build_samples._fixture` 造完整 `organized/`（5 个 ticker：AAA/BBB/CCC/UNIT-WT/DROP + macro + exclusions） |
| 直接调用内部函数 | 测试有意直接调 `_organize_tickers`、`_submission_rows`、`_financial_features`、`organize_financials` 等，绕过 CLI，便于单点断言 |
| monkeypatch | 只用于路径与 IO 追踪：`universe.TICKER_CIK_OVERRIDES_PATH`（ticker_overrides）、`Path.read_text` 包装（organize_financials 的“不读无关文件”断言） |
| 缓存隔离 | 涉及 `_TICKER_CACHE` 的测试用 `_reset_ticker_cache(raw_dir)`（或全清） |
| 网络 | 零真实请求；`market.fetch_market`/`financials.fetch_financials`/`macros.fetch_macros`/`universe.fetch_universe` 的网络分支**当前无测试覆盖**（见 §6） |
| 子进程 CLI | `_run_cli` 用 `sys.executable -m cli.main` + 注入 `PYTHONPATH=src`，cwd 固定仓库根，timeout=10s |

## 5. smoke gate（先单点、再全量）

### 5.1 改财务抽取（`organize_financials.py`）

```bash
# 前置：raw/sec/financials 与 raw/sec/universe 已有缓存；market 不参与
PYTHONPATH=src .venv/bin/python -m cli.main download --stage organize --force-rebuild --tickers AAPL,XOM
```

判定标准（本仓库当前基线）：

| 检查 | 期望 |
|---|---|
| AAPL `financials.csv` | `revenue` 首个非空 `report_period_end ≈ 2009-06-27`（回溯到 2009 年附近）；`operating_income/net_income/assets` 均有值 |
| XOM `financials.csv` | `revenue` 首个可见行 `date=2012-02-27`、fiscal FY2011；`operating_income` 全空（XOM 无该 tag，属预期） |
| 行数 | `financials_output` = organize 日历 session 数（当前 9,252） |
| provenance | `_meta.json.inputs` 非空且 sha256 与 raw 文件一致；无 owned 输入时 inputs 为空但产物仍写出 |

通过后再跑全量 `download --stage all`（或至少对所有 ticker 的 organize）。

### 5.2 改样本逻辑（`build_samples.py`）

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_build_samples.py -q
# 真实验证（可先在小副本上做，见 samples.md §9）
PYTHONPATH=src .venv/bin/python -m cli.main build-samples
```

判定标准：`manifest.json` 的 `row_counts.samples`、`splits.json` 的 purge 行数、`qc_report.json` 的 `flag_extreme_label_count` 与 [AGENT.zh-CN.md](../../AGENT.zh-CN.md) §2 不变式 5 基线一致（23,938,669 / 120,987+139,158+182,398 / 120,510）——**只动财务特征时必须逐位不变**；schema 仍为 date32 + float32 + uint8（`test_outputs_use_date32_and_float32`）。

### 5.3 改下载/进度/override

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_download.py tests/test_progress_lock.py tests/test_ticker_overrides.py -q
PYTHONPATH=src .venv/bin/python -m cli.main download --stage all --start 1990-01-01 --end 2025-12-31 --dry-run
```

判定：dry-run 只打印计划且不落盘；override 改动后 XOM 的新旧 CIK 双向映射与 force-rebuild 行为符合 `test_ticker_overrides.py` 的断言。

## 6. 新改动补测试的最小要求

| 改动 | 最小测试 | 现状 |
|---|---|---|
| 新增/调整概念 tag | 更新 `_CONCEPTS` 元组断言 + 加一条 fixture fact 验证优先级 | 已有模板 |
| 改 `_submission_rows` 形状支持 | 每种形状一个用例；ragged/空数组拒绝用例；accession 去重用例 | 已有模板 |
| 改 fiscal 兜底 | Apple 式非日历财年 + 无 fy/fp 的 FY 判定 | 已有模板 |
| 改 as-of/可见性 | filed 当日不可见、次日可见；修正申报 quality_status | 已有模板 |
| 改 override/force-rebuild | 前向、反向、缺文件回退、CLI flag、完成项强制重建 | 已有模板 |
| 改进度/锁 | round-trip、force/resume、stale/live/malformed 锁 | 已有模板 |
| 改行情质量/派生列 | 一个合法 bar + 一个非法 high + negative price + adjustment_factor/return | 已有模板 |
| 改标签/特征 | `tests/test_build_samples.py` 的 fixture 断言期望值；新增列必须断言 dtype 与 miss 指示 | 已有模板 |
| 改 purge/splits | 31-session 窗、`rows_by_split` 恒等式、末端 NaN 保留 | 已有模板 |
| 新增网络下载分支 | **当前空白**：至少补“缓存命中不重下”“manifest 损坏回退”“ragged/非 JSON 报错”的离线测试（monkeypatch urlopen 或注入 fixture 文件） | 待补 |
| 确定性 | 建议补“同输入重建两次 manifest.outputs 哈希一致”的测试 | 未自动化 |

原则：新测试必须离线、只用 `tmp_path`、直接断言内部状态；不要放宽既有断言来适配改动，先判断是行为回归还是契约变更（契约变更同步 [data-contracts.zh-CN.md](data-contracts.zh-CN.md) 与 [../user/cli.zh-CN.md](../user/cli.zh-CN.md)）。

## 7. 排错

| 症状 | 原因/处理 |
|---|---|
| `ModuleNotFoundError: cli`/`download` | 未从仓库根运行或未加 `PYTHONPATH=src`（测试文件自身会兜底插入 src，但手动跑 CLI 需要） |
| CLI 子进程测试超时 | `_run_cli` 固定 timeout=10s；本机负载高时可能偶发，重跑确认 |
| `fork() may lead to deadlocks` 警告 | ProcessPool + 多线程解释器的已知告警，不影响结果；无需修改 |
| `test_load_sources_parses_provider_settings` 失败 | 有人改了 `config/sources.toml` 的关键值（provider / rate_limit / series 顺序），同步测试或接受为契约变更 |
| 锁测试在 NFS/tmpfs 上行为异常 | 锁依赖 inode 身份与 `os.kill`，`tmp_path` 本地盘是前提 |
