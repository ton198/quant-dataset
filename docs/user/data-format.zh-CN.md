[English](data-format.md) | **简体中文**

# 样本数据格式（data/output/）

冻结样本包位于 `data/output/`，schema 版本 `samples_v1`（见 `manifest.json`）。本文所有数字与仓库当前实物一致；重建后以新生成的 `manifest.json` / `qc_report.*` 为准。

## 1. 文件清单

| 文件 | 规模（当前实物） | 说明 |
|---|---|---|
| `samples/year=YYYY/part-00000.parquet` | 36 个分区，23,938,669 行 | 训练样本长表，Hive 分区 |
| `meta.parquet` | 6,532 行 | 每股票汇总：`asset_id`、`is_common`、`first_date`、`last_date`、`n_rows`、`missing_frac` |
| `splits.json` | — | 四段切分边界、purge 语义与 `purged_windows`、每段行数 |
| `manifest.json` | — | schema、列清单、契约、偏差声明、输出 sha256 |
| `qc_report.json` / `qc_report.md` | — | QC：行数、标签统计、缺失率、截面规模、极端标签、失败明细 |

## 2. dtype 与分区约定

| 项 | 约定 |
|---|---|
| 分区路径 | `samples/year=YYYY/part-00000.parquet`，每年一个文件，可直接 glob |
| 压缩 | snappy |
| `date` | `date32[day]`（信号日，canonical session） |
| `asset_id` | string（ticker，大写；当前为 `large_string` 物理类型） |
| `is_common` | bool |
| `flag_extreme_label` | uint8 |
| `f_raw_*` / `f_cs_*` / `target_return_*` / `excess_*` | float32；非有限值已归一为 null（NaN） |
| `miss_*` | uint8（1 = 对应 `f_raw_*` 为 null） |

## 3. 147 列总表

| 组 | 列 | 数量 | 说明 |
|---|---|--:|---|
| 键 / 标志 | `date`、`asset_id`、`is_common`、`flag_extreme_label` | 4 | `flag_extreme_label=1` 共 120,510 行（≈0.5%） |
| 原始特征 | `f_raw_*` | 48 | 量价 10 + 财务 5 + 宏观 33 |
| 截面特征 | `f_cs_*` | 15 | 股票维度特征按日 rank→逆正态 CDF（≈N(0,1)），宏观不参与 |
| 缺失指示 | `miss_*` | 48 | 与 `f_raw_*` 一一对应 |
| 标签 | `target_return_1d..30d` | 30 | 见第 5 节公式 |
| 标签 | `excess_5d`、`excess_21d` | 2 | 相对同日 is_common 等权均值基准的超额 |

`f_cs_*` 对应关系：`f_cs_<name>` 是 `f_raw_<name>` 的当日截面变换；截面范围为当日该列非空的全部股票（含 `is_common=false` 的行），宏观列因同日恒定被排除。方法：`(rank - 0.5) / 当日非空数` 经逆正态 CDF。

## 4. f_raw_* 明细

量价（10）：

| 列 | 含义 |
|---|---|
| `f_raw_return_1d` / `_5d` / `_20d` | 收盘价 1/5/20 session 收益率（organize 阶段计算，past-only） |
| `f_raw_momentum_60` / `_120` | `adj_close(t)/adj_close(t-60/120) - 1`（canonical 轴） |
| `f_raw_volatility_20` | close 日收益 20 session 滚动标准差（organize 阶段） |
| `f_raw_volatility_60` | canonical 轴 adjusted_close 日收益 60 session 滚动标准差 |
| `f_raw_volume_ratio_20` | volume / 20 session 均值（organize 阶段） |
| `f_raw_volume_zscore_60` | volume 相对 60 session 均值/标准差的 z-score（canonical 轴） |
| `f_raw_intraday_range` | `(high - low) / close` |

财务（5，point-in-time 快照，`available_as_of <= 信号日` 的最新申报）：

| 列 | 含义 |
|---|---|
| `f_raw_revenue_yoy` / `f_raw_net_income_yoy` / `f_raw_operating_income_yoy` / `f_raw_assets_yoy` | 同比增速（优先同 fiscal_period 的上一 fiscal_year；无标识时取一年前 ±15 天内的最近报告期） |
| `f_raw_days_since_filing` | 信号日距最近可用申报的天数 |

宏观（33 = 11 序列 × 水平 / `_d1` / `_d5`），序列：

| 序列 | 别名 |
|---|---|
| `BAMLH0A0HYM2` | 美国高收益债 OAS |
| `CPIAUCSL` / `CPILFESL` | CPI / 核心 CPI |
| `PAYEMS` / `UNRATE` | 非农就业 / 失业率 |
| `FEDFUNDS` / `DGS2` / `DGS10` | 联邦基金利率 / 2Y / 10Y 国债收益率 |
| `DCOILWTICO` / `DEXUSEU` / `VIXCLS` | WTI 原油 / 美元兑欧元 / VIX |

水平列名为 `f_raw_m_<SERIES>`；`_d1`、`_d5` 为 canonical 日期轴上的位置差（不做 forward-fill）。序列可见性规则：参考期 + 1 个月后的第一个 session（保守近似，FRED 响应无 release timestamp）。

## 5. 标签语义

公式（`manifest.json.label_semantics`，已实测重算逐位吻合）：

```text
adjusted_open = open × adj_close / close
target_return_hd = adjusted_open(t+1+h) / adjusted_open(t+1) − 1
excess_5d/21d    = target_return_5d/21d − 同日 is_common 且 flag_extreme_label=0 的等权均值
```

```text
信号 t        t+1 入场          t+1+h 出场
  │            │                  │
  │  特征止于 t │  标签窗口 h 个 session │
  └────────────┴──────────────────┘
     h 为 canonical session 位置偏移；入场/出场 bar 必须存在且为正有限值，不做替代或 ffill
```

- 主训练目标为 `excess_5d` / `excess_21d`；其余 `target_return_*` 为辅助任务（manifest 声明）。
- 数据集尾部（最后约 31 个信号日）无足够未来价格，长 horizon 标签自然为 null，属预期。
- `flag_extreme_label=1`：1..30 session 标签窗口穿过价格毛刺（相邻 `adjusted_open` 比值落在 [0.5, 2.0] 之外）。该行保留在表中，但已被排除出 excess 基准均值；训练侧应剔除或降权。

## 6. splits.json 与 purge

四段切分（构建期已按边界 purge，**下游不要重复 purge**）：

| split | 区间 | retained | before_purge | purged |
|---|---|--:|--:|--:|
| fit | 1990-01-02 – 2018-12-31 | 15,253,930 | 15,374,917 | 120,987 |
| select | 2019-01-01 – 2020-12-31 | 1,966,167 | 2,105,325 | 139,158 |
| screen | 2021-01-01 – 2024-12-31 | 5,168,902 | 5,351,300 | 182,398 |
| reserve | 2025-01-01 – 2025-12-31 | 1,549,670 | 1,549,670 | 0 |

purge 语义：`purge_sessions=30`。边界前 31 个信号日被整行剔除（标签在 t+1 入场、t+31 出场，最长 horizon 会伸进下一段）；数据集尾端无后续段，不 purge。`splits.json` 顶层的 `purge_semantics` 是文字版规则；`purged_windows` 记录三个边界的字段：

| 字段 | 含义 |
|---|---|
| `split_start` / `boundary_date` | 新 split 起始日 / 该段第一个 canonical session |
| `purged_split` | 被剔除行所属的 split |
| `first_purged_session` / `last_purged_session` | 被剔除信号日区间 |
| `sessions` / `rows_removed` | 剔除 31 个 session；剔除行数 |

## 7. manifest.json 关键字段

| 字段 | 说明 |
|---|---|
| `schema_version` | 当前 `samples_v1` |
| `outputs` | 40 项文件 → `{sha256, rows, bytes}`；`manifest.json` 自身不参与哈希 |
| `row_counts` | `samples` 总数、`meta` 行数、`by_year` |
| `feature_list` | 111 个特征列（48+15+48），顺序与文件中特征列一致 |
| `feature_contract` | `raw_features`、`cross_sectional_features`、`missing_indicators`、截面/宏观定义 |
| `label_semantics` | 标签公式与主目标声明 |
| `build_params` | canonical 规则（≥500 is_common）、9,067 session、purge、切分与行数 |
| `data_quality_flags` | `flag_extreme_label` 规则、数量、处理建议 |
| `known_biases` | 幸存者偏差、FRED 非 vintage、adjusted open 非成交价、is_common 后缀启发式 |
| `exclusions_applied` | 剔除清单文件与生效的 `asset_ids` |
| `input_inventory` | 输入盘点：7,662 个股票目录、6,534 个 market.csv、1,128 个无 market.csv 目录 |

## 8. meta.parquet 与 qc_report

- `meta.parquet`：仅含产出过保留样本的股票（6,532）。`missing_frac` 是该股票全部 `f_raw_*` 单元格的缺失比例；`first_date`/`last_date` 为其样本覆盖。
- `qc_report.json`：`rows_total`、`rows_per_year`、`label_stats`（每标签 all/clean 两组统计）、`missingness_per_feature`、`cross_section_size_per_year`、`purge`、`extreme_labels`（含最多 100 条示例）、`ticker_failures`。
- `qc_report.md`：上述内容的可读版；缺失率最高的是财务类（≈96%）与 `BAMLH0A0HYM2`（87%），解释见 [recommended-usage.zh-CN.md](recommended-usage.zh-CN.md) 第 3 节。

## 9. 校验建议

重建后按 `manifest.json.outputs` 逐项校验 sha256；行数不一致时优先看 `qc_report` 的 `ticker_failures` 与 `input_inventory`。列级契约与开发者约定见 [AGENT.zh-CN.md](../../AGENT.zh-CN.md)。
