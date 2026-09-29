[English](samples.md) | **简体中文**

# samples.md — 样本构建内部机制（`src/build_samples.py`）

> 适用：改标签（`target_*`/`excess_*`）、特征（`f_raw_*`/`f_cs_*`/`miss_*`）、canonical calendar、purge/splits、输出包结构。
> 关联：[data-contracts.zh-CN.md](data-contracts.zh-CN.md)（字段级契约与基线数值）、[AGENT.zh-CN.md](../../AGENT.zh-CN.md) §2 不变式 5、[architecture.zh-CN.md](architecture.zh-CN.md)、[known-quirks.zh-CN.md](known-quirks.zh-CN.md)。
> 本文对应 1,176 行实物 `src/build_samples.py`；所有函数名均可在该文件中找到。

## 1. 入口与顶层流程

```text
CLI: quant-dataset build-samples [--data-dir data/organized] [--out data/output] [--exclusions-file ...]
      └─ src/cli/main.py: build_samples(args.data_dir, args.out, args.exclusions_file)
          └─ build_samples(data_dir, output_dir, exclusions_file=None, *,
                           canonical_min_tickers=500, rank_batch_sessions=40, staging_tickers=50)
```

默认排除文件是 `config/universes/exclusions_v1.json`（`build_samples` 内按 `__file__` 推导；CLI 不传时也走这里）。顶层流程：

1. 校验 `stocks/` 与 `shared/macro.csv` 存在；`_load_exclusions` 读取并校验排除清单（大写、去重，重复即 `ValueError`）。
2. 扫描 `stocks/*/market.csv`：排除 hit；`_is_common` 分类；用**有 market.csv 的 is_common ticker**逐日计数。
3. canonical calendar = 出现 ticker 数 `>= canonical_min_tickers`（默认 500）的日期集合；为空则 `ValueError`。`_macro_matrix` 把 macro.csv 对齐到该轴。
4. `_purge_windows(calendar)` 预计算三个边界禁运窗 → `purge_split_by_date`。
5. `_clean_outputs(output)` 清理旧样本/旧 staging/旧 manifest 等。
6. 逐批（`staging_tickers`，默认 50）调用 `_ticker_samples` → 立即按 split 剔除 purge 行 → 按 `(date, asset_id)` 排序，按 `rank_batch_sessions`（默认 40 个 session）切 chunk 写入 `output/samples-stage-*/chunk=*/stage-*.parquet` 临时目录。
7. 逐 chunk 读回：`_add_cross_sectional_features` → 生成 `miss_*` → 计算 `excess_5d/21d` → 按年分组攒行，用 `pq.ParquetWriter` 追加到 `output/samples/year=YYYY/part-00000.parquet`。
8. 写 `meta.parquet`、`splits.json`、`qc_report.json`/`.md`，最后 `_hash_output_files` + `manifest.json`。任何 ticker 级异常进入 `failures`/QC，不中断全局；全空则 `ValueError`。

## 2. 常量与特征/标签清单

| 常量 | 内容 | 数量 |
|---|---|---|
| `MACRO_SERIES` | `BAMLH0A0HYM2, CPIAUCSL, CPILFESL, DCOILWTICO, DEXUSEU, DGS10, DGS2, FEDFUNDS, PAYEMS, UNRATE, VIXCLS` | 11 |
| `MARKET_RAW` | `f_raw_return_1d/5d/20d`、`f_raw_volatility_20`、`f_raw_volume_ratio_20`、`f_raw_intraday_range`（market.csv 直通） | 6 |
| `DERIVED_RAW` | `f_raw_momentum_60/120`、`f_raw_volatility_60`、`f_raw_volume_zscore_60`（canonical session 位置计算） | 4 |
| `FINANCIAL_RAW` | `f_raw_revenue_yoy`、`f_raw_net_income_yoy`、`f_raw_operating_income_yoy`、`f_raw_assets_yoy`、`f_raw_days_since_filing` | 5 |
| `MACRO_RAW` | `f_raw_m_<series>`、`f_raw_m_<series>_d1`、`f_raw_m_<series>_d5` | 33 |
| `RAW_FEATURES` | `STOCK_RAW + MACRO_RAW`（`STOCK_RAW = MARKET_RAW+DERIVED_RAW+FINANCIAL_RAW`） | 48 |
| `CS_FEATURES` | 仅对 `STOCK_RAW` 生成 `f_cs_*`；宏观列是日期常量，**不参与**截面秩 | 15 |
| `MISS_FEATURES` | 所有 `RAW_FEATURES` 的 `miss_*` | 48 |
| `LABELS` | `target_return_1d..30d` + `excess_5d` + `excess_21d` | 32 |
| `_PURGE_SESSIONS` | 30（最大标签窗口） | — |
| `_SPLIT_TRANSITIONS` | `select` 2019-01-01 / `screen` 2021-01-01 / `reserve` 2025-01-01（fit 无起点，隐含 data_start） | 3 个边界 |

## 3. canonical calendar、分批与确定性的关系

- calendar 是全局轴：`market["date"]` 用 `calendar.get_indexer` 映射到轴位；不在轴上的行情行直接不产生样本（`organize_market` 已按 organize 时日历过滤过一层，这里再按 canonical calendar 过滤）。
- 只遍历 `stocks/*/market.csv` 的 ticker；没有行情（1,128 只）不进入样本。`ticker_map` 用 upper 后的 asset_id，重复目录名会 `ValueError`。
- `date_counts` 只统计 `_is_common=True` 的 ticker，所以“>=500”的分母是 common ticker，不是全 universe（manifest `build_params.common_tickers_in_denominator` 记录该值）。
- 截面秩在**同一 date chunk（40 个 session）内**计算；chunk 内包含该日期所有已构建 ticker 的行，因此结果与全量秩等价。`rank_batch_sessions` 只影响内存/IO，不影响数值。
- ticker 处理顺序 = `glob("*/market.csv")` 的排序（目录名字典序）；输出统一 `sort_values(["date","asset_id"], kind="mergesort")`；JSON `sort_keys=True`。

## 4. 核心函数地图

| 函数 | 职责 | 关键行为/坑 |
|---|---|---|
| `build_samples` | 顶层编排 | 所有 kwargs 只影响性能/日历阈值，不改变值的定义；`output_dir` 下所有旧产物由 `_clean_outputs` 清理 |
| `_clean_outputs` | 删除 `samples/`、`samples-stage-*`、旧 `meta.parquet/manifest.json/splits.json/qc_report.*` 后新建 `samples/` | 临时 staging 目录建在 `output/` 内（同文件系统），异常时 `TemporaryDirectory` 回收 |
| `_ticker_samples` | 单 ticker → 长表行（特征+标签+flag） | 要求 `_MARKET_REQUIRED` 列齐全、date 无重复；`valid_bar` 要求 open/close 为正有限、factor 有限、adjusted_open 有限；标签缺失=NaN 而非 0 |
| `_financial_features` | 从 `financials.csv` 生成 5 个 `FINANCIAL_RAW` 特征 | 见 §5.3；路径缺失返回全 NaN，不报错 |
| `_macro_matrix` | macro.csv → canonical 轴上的 33 列 float32 | 缺列/重复日期 `ValueError`；`d1/d5` 用 canonical 轴位序 `diff`，无前向填充 |
| `_add_cross_sectional_features` | 按日期对 15 个 `STOCK_RAW` 做平均秩 → `_inverse_normal` → `f_cs_*` | 分母是该 date+feature 的非空计数；NaN 不参与秩，保持 NaN；就地写入 frame |
| `_inverse_normal` | Acklam 逆正态近似（仅 (0,1) 有效） | 概率 `(rank-0.5)/count`，中位数恰好 0；越界/NaN 输出 NaN |
| `_positive_finite` | `isfinite & >0` 掩码 | 标签窗口与动量共用的有效性判据 |
| `_aligned_numeric` | 把 ticker 局部数组放到全 calendar 长度的位置数组 | 未覆盖位置为 NaN |
| `_rolling_std` | `rolling(window, min_periods=window).std(ddof=1)` | 窗口不足 → NaN，无部分窗口 |
| `_purge_windows` | 计算每个 split 边界的 31-session 禁运窗 | 边界取 `calendar.searchsorted(split_start, side="left")`；边界在轴首/轴外则跳过该窗 |
| `_public_purge_windows` | 去掉内部 `_positions` 后供 splits.json 输出 | `rows_removed` 由 `purge_rows_by_split` 回填 |
| `_label_qc` | 逐标签统计 all / clean（排除 `flag_extreme_label=1`） | clean 组若整列为 NaN 则取空数组，`_stats_block` 返回 n=0 |
| `_stats_block` | n/mean/std/p50/p99/exact_zero_fraction | std 用 ddof=1；n=1 时 std=0 |
| `_hash_output_files` | 对 output 下除 `manifest.json` 外所有文件记 sha256/bytes/rows | samples 分区按 `year=` 目录取 rows，`meta.parquet` 取 meta 行数 |
| `_arrow_table` | DataFrame → Arrow，并把 `date` 转 `date32` | 输出 schema 的日期类型由此保证 |
| `_write_json` | `json.dumps(indent=2, sort_keys=True, allow_nan=False)` | `allow_nan=False`：任何 NaN 进入 splits/manifest/qc 都会立刻报错 |
| `_is_common` / `_load_exclusions` / `_normal_date_column` / `_sha256` | 工具函数 | `_is_common` 只看后缀；`_normal_date_column` 出现无效日期即 `ValueError` |

## 5. 特征实现细节

### 5.1 行情特征（`_ticker_samples` 前半）

- 直通列：`return_1d/5d/20d`、`volatility_20`、`volume_ratio_20`、`intraday_range`，`pd.to_numeric(errors="coerce")` → float32。
- `factor = adj_close / close`（close 为 0/非有限 → NaN）；`adjusted_open = open * factor`；`valid_bar` 要求 open/close/factor/adjusted_open 全为正有限。
- `momentum_60/120 = adjusted_close[t] / adjusted_close[t-60/120] - 1`，两个端点都必须 `_positive_finite`；不足 60/120 个轴位 → NaN。
- `volatility_60`：先在 canonical 全轴上用该 ticker 对齐后的 adjusted_close 定义相邻轴位 return（缺 bar 的轴位为 NaN），再 `rolling(60).std`，最后取 ticker 自身轴位；窗口内任一轴位缺失都会让结果保持 NaN。
- `volume_zscore_60 = (volume - rolling_mean_60) / rolling_std_60`（std 为 0 或非有限 → NaN）。
- 宏观列直接 `macro.iloc[positions][feature]` 取值，不做逐 ticker ffill。
- 最后统一清洗：所有 raw 特征中的 ±inf/NaN → NaN；`miss_*` 在 §5.5 由 `isna()` 生成。

### 5.2 标签（同一函数后半）

- 定义：`adjusted_open = open × adj_close / close`；信号 session `t` 的 **entry = canonical 轴位 t+1**、**exit = t+1+h**（h=1..30）。
- `target_return_hd = adjusted_open[t+1+h] / adjusted_open[t+1] - 1`，要求 entry/exit 都存在（`entry_positions < size`、`exit_positions < size`）且 `valid_bar` 为真；否则 NaN。**没有中间 bar 要求、没有替换/填充**。
- 非正/非有限 open/close/factor、adj_open 无效、entry/exit 越界 → 标签 NaN。数据集末端的天然 NaN 行保留（purge 只删边界窗）。

### 5.3 财务 as-of 与 YoY（`_financial_features`）

1. 读 `financials.csv`，要求 `available_as_of`、`report_period_end` 与 `_FINANCIAL_VALUES = ("revenue","net_income","operating_income","assets")`；`available_as_of` 无效的行剔除。
2. **快照语义**：organize 输出是逐日快照；按 `(_available, _source_order)` 稳定排序后 `drop_duplicates("_available", keep="last")`，每个可用日只留最后一行——即“当日实际可见的申报信息”。`fiscal_year/fiscal_period` 列缺失时按 NaN 处理（兼容旧文件）。
3. **YoY 主路径**：`fiscal_history[(fiscal_year, fiscal_period)]` 记录最近一次出现该键的快照；当前行的 prior = `fiscal_year-1`、同 `fiscal_period` 的快照。四个值都满足 `current/prior 非缺 & prior != 0` 才计算比率。
4. **兜底路径**（无 fiscal 标识或找不到同键）：在**上个报告年**的 report_period_end 中找 `|candidate - (end - 1年)| <= 15 天` 的候选，取最近；平手取日期更早的 period-end（排序键 `(abs(diff), candidate_date)`）。
5. 可见性：`searchsorted(_available, signal_date, side="right") - 1`，即**latest available_as_of <= 信号日**；`f_raw_days_since_filing = signal_date - available_as_of`（自然日）。
6. 坑：fiscal 主路径只认“最近一次出现的键”。若最近快照带 fy/fp 但四个值全缺（例如白名单没抽到概念），该键会指向空快照，YoY 不会回退到更早的同键快照（见 [known-quirks.zh-CN.md](known-quirks.zh-CN.md)）。

### 5.4 截面特征（`_add_cross_sectional_features`）

- 对每个 date、每个 `STOCK_RAW`：`rank(method="average")` → `p = (rank - 0.5) / non_null_count` → `_inverse_normal(p)` → float32。
- 宏观列明确排除（同一日期所有 ticker 相同，秩无意义）；`is_common=False` 的行仍参与排名；NaN 保持 NaN。
- 与 `excess_*` 的差别：`excess_5d/21d = target - 同日 is_common & flag==0 的等权均值`（在 chunk 内 groupby date 计算；目标 NaN 自动跳过）。

### 5.5 缺失与 flag

- `miss_* = raw.isna().astype(uint8)`，对全部 48 个 raw 特征生成，不含截面特征。
- `flag_extreme_label`：先在全轴上计算相邻轴位 `adjusted_open[t]/adjusted_open[t-1]`（两端都 `valid_bar` 才算），比率 `>2.0` 或 `<0.5` 记 glitch；再对信号行统计 `glitch` 是否落在其标签窗口轴位 `[t+2, t+31]` 内（对应 entry t+1 到 exit t+1+h 的 30 个窗口）→ `uint8(0/1)`。保留行，不删除；`excess_*` 基准均值排除 flag=1 行。

## 6. purge 与 splits

- 三个边界：select/screen/reserve 的 split 起点（2019-01-01 / 2021-01-01 / 2025-01-01）。每个边界取 canonical 轴上第一个 >= 起点的 session 作为 boundary；窗 = boundary 前 `31` 个 signal session（`_PURGE_SESSIONS + 1`：30 session 标签 + 1，因为 exit 在 t+1+30）。
- 构建期删除：ticker frame 中 `date ∈ purge_split_by_date` 的行全部剔除，`rows_by_split[前一个 split].purged` 计数；`before_purge/retained` 同步记录。
- `splits.json` 字段：`fit/select/screen/reserve` 起止、`purge_sessions=30`、`purge_semantics`、`purged_windows`（key=后一个 split 名：boundary_date、first/last_purged_session、sessions、rows_removed）、`rows_by_split`、`rows_removed_by_split`。
- 边界在轴首/轴外（例如 reserve 之后无 session）→ 该窗不生成；数据集末端天然 NaN 标签行**不 purge**。
- 当前基线（`data/output/splits.json`）：purged fit=120,987、select=139,158、screen=182,398、reserve=0；`flag_extreme_label=120,510`；总行数 23,938,669（与 [AGENT.zh-CN.md](../../AGENT.zh-CN.md) §2 不变式 5 一致）。

## 7. 输出包

| 产物 | 内容 | 关键点 |
|---|---|---|
| `samples/year=YYYY/part-00000.parquet` | 列顺序：`date, asset_id, is_common, flag_extreme_label, RAW_FEATURES(48), CS_FEATURES(15), MISS_FEATURES(48), LABELS(32)` | `date=date32`；float32 特征/标签；`is_common` bool；`flag_extreme_label`/`miss_*` uint8；snappy |
| `meta.parquet` | 每 asset_id：`is_common、first_date、last_date、n_rows、missing_frac` | 按 asset_id 排序；missing_frac 在 purge 后用 RAW_FEATURES 的 NaN 率计算（不含截面） |
| `splits.json` | §6 的切分与 purge 记录 | `_write_json` 排序键 |
| `qc_report.json` / `.md` | rows_total/rows_per_year、32 个标签 all/clean 统计、逐特征缺失率、截面规模/年、purge、极端标签计数+最多 100 例、ticker failures | clean = 排除 `flag_extreme_label=1` |
| `manifest.json` | `schema_version="samples_v1"`、`outputs`（除自身外全部文件 sha256/bytes/rows）、`row_counts`、`date_range`、`feature_list`、`feature_contract`、`label_semantics`、`build_params`、`data_quality_flags`、`known_biases`、`exclusions_applied`、`input_inventory`、`is_common_column`、`benchmark`、`manifest_hash_note` | `outputs` 不含 `manifest.json`（避免自引用哈希）；rows 基线见 [data-contracts.zh-CN.md](data-contracts.zh-CN.md) |

## 8. 确定性与可复现

- 行序：ticker 字典序 + `(date, asset_id)` 稳定排序；chunk/年 partition 的边界固定（40 sessions、日历年）。
- JSON：`sort_keys=True`；parquet：固定 schema、`row_group_size=8192`（staging）。
- manifest 对每个输出做 sha256+bytes+rows；相同输入字节 + 相同依赖版本 + 相同 kwargs → 相同 manifest（`splits.json`、`meta.parquet`、`manifest.json` 均不含生成时间戳；时间只出现在 organize 层的 `_meta.json`）。
- 注意 `_` 前缀内部列 `_date_chunk` 只在 staging 文件里存在，写出样本前已 drop。

## 9. 修改警示（改标签/特征/切分后必做）

1. 跑锁定测试：`PYTHONPATH=src .venv/bin/python -m pytest tests/test_build_samples.py -q`（7 个测试覆盖标签、YoY、purge、inf→miss、dtype、截面）。
2. 重建后对照结构不变式（[AGENT.zh-CN.md](../../AGENT.zh-CN.md) §2 第 5 条）：总量 23,938,669；purge 120,987/139,158/182,398；flag 120,510。**只改财务特征时这些必须逐位不变**；变化只允许来自 purge/标签逻辑变更，且必须同步 [data-contracts.zh-CN.md](data-contracts.zh-CN.md)。
3. 校验 schema/类型（`tests/test_build_samples.py::test_outputs_use_date32_and_float32`）：`date32`、float32 特征与标签、uint8 的 `flag_extreme_label`/`miss_*`、无 `f_cs_m_*`。
4. 用 `data/output/manifest.json.outputs` 逐文件复核 sha256；`qc_report.json` 的 missingness/label stats 无异常跳变。
5. 没有 ticker/年份粒度的 CLI 过滤：小范围真实验证请直接调用 `build_samples(data_dir, out, canonical_min_tickers=<小值>, staging_tickers=<小值>, rank_batch_sessions=<小值>)`，或在临时目录复制少量 `stocks/<T>` 与 `shared/macro.csv`；默认 500 的门槛要求样本集足够大。

## 10. 已知偏差（manifest 自述）

- Survivorship：universe 是当前 SEC 名单，无退市 ticker。
- FRED 为 latest revised 值，非 vintage。
- `adj_open` 是复权价，不是可成交价；标签收益不等于可实现成交。
- `is_common` 是后缀启发式（`-UN/-WT/-P/-R/-U` 等），不能替代 security master；样本中 6,532 个 asset_id 里 364 个 `is_common=False`。
- 无 SPY：`excess_*` 用同日等权 common 均值（且排除 extreme flag）。

## 11. 验证命令速查

```bash
# 样本逻辑单测
PYTHONPATH=src .venv/bin/python -m pytest tests/test_build_samples.py -q

# 全量重建（默认 data/organized → data/output）
PYTHONPATH=src .venv/bin/python -m cli.main build-samples

# 结构基线快速核对
.venv/bin/python - <<'PY'
import json

m = json.load(open("data/output/manifest.json"))
s = json.load(open("data/output/splits.json"))
q = json.load(open("data/output/qc_report.json"))
assert m["row_counts"]["samples"] == sum(m["row_counts"]["by_year"].values())
assert q["rows_total"] == m["row_counts"]["samples"]
print(
    m["row_counts"]["samples"],
    s["rows_removed_by_split"],
    q["extreme_labels"]["flag_extreme_label_count"],
)
PY
```
