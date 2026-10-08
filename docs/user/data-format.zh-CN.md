[English](data-format.md) | **简体中文**

# 样本数据格式（`samples`）

本文说明唯一维护的、不含财务数据的 `samples` 契约，适用于新构建。现有 bundle 按原样保留为历史产物：2026-10-03 发布在 `data/output/` 的 manifest 仍为 `schema_version="samples_v3"`；旧 147 列 output 保留在 `data/output-v1-backup-20261003T172214933236Z`；冻结 baseline 另行保留。读取前核对各 bundle 实际 manifest 与 schema。旧标签仅用于识别 artifact 内容，不是产品选择器或兼容别名。旧发布报告不能验证当前源码或新构建。

## 1. 候选包

| 文件 | 内容 |
|---|---|
| `samples/year=YYYY/part-00000.parquet` | 按年分区的 signal-date/ticker 行 |
| `meta.parquet` | 每 asset 汇总；`missing_frac` 基于 purge 后 43 个非财务 raw 列 |
| `splits.json` | split 边界与构建期 purge 记录 |
| `manifest.json` | 新构建使用单一契约标签 `schema_version="samples"`、有序 96 列 feature list、provenance/output hashes、构建参数和标签 |
| `qc_report.json` / `qc_report.md` | 行数/标签/缺失/截面/purge/extreme-label/ticker-failure 汇总；没有财务覆盖章节 |

新的 `samples` 构建只读取 organized 行情、宏观输入与 exclusions。它不读取财务文件或财务 `_meta.json` 记录，也不做财务预检、覆盖率或输入盘点。现有 SEC 结构化文件仍是独立 organized 数据，不是样本列或模型输入。

## 2. 类型与物理列序

Parquet 物理类型：`date` 为 Arrow `date32`，ticker `asset_id` 为 Arrow `large_string`，`is_common` 为 bool，`flag_extreme_label` 与 `miss_*` 为 uint8，特征/标签数值为 float32。canonical 列序：

```text
date, asset_id, is_common, flag_extreme_label,
43 个非财务 f_raw 列,
10 个 f_cs 列,
43 个 miss 列,
32 个 label 列
```

| 组 | 数量 | 说明 |
|---|---:|---|
| 键 / 标志 | 4 | `date`、`asset_id`、`is_common`、`flag_extreme_label` |
| Raw 特征 | 43 | 10 个行情/派生股票特征 + 33 个宏观特征 |
| 截面特征 | 10 | 10 个股票 raw 特征的同日 rank 变换；不含宏观 rank |
| 缺失指示 | 43 | 每个 raw 特征一列 |
| 特征列 | **96** | 43 + 10 + 43；`manifest.feature_list` 顺序与物理特征顺序一致 |
| 标签 | 32 | 30 个 forward-return 列 + 2 个 excess-return 列 |
| 样本物理列 | **132** | 4 + 96 + 32 |

当前 schema 不含财务 raw、截面或缺失指示列。此前 51 个财务列（17 raw + 17 CS + 17 MISS）均已从活跃 schema 移除。

## 3. 43 个 raw 特征

### 行情/派生股票特征（10）

| 列 | 定义 |
|---|---|
| `f_raw_return_1d`、`f_raw_return_5d`、`f_raw_return_20d` | organized 行情面板中的既有收盘到收盘收益 |
| `f_raw_volatility_20` | 既有 20-session 收益波动率 |
| `f_raw_volume_ratio_20` | 既有 volume / 20-session 平均 volume |
| `f_raw_intraday_range` | `(high - low) / close` |
| `f_raw_momentum_60`、`f_raw_momentum_120` | canonical 轴上的 `adj_close(t) / adj_close(t-n) - 1` |
| `f_raw_volatility_60` | canonical 轴上 adjusted-close return 的 60 位滚动标准差 |
| `f_raw_volume_zscore_60` | volume 相对于 60 位滚动均值/标准差的 z-score |

### 宏观特征（33）

11 个配置宏观序列各有水平值及 canonical 轴位序 `_d1`、`_d5` 差分：`BAMLH0A0HYM2`、`CPIAUCSL`、`CPILFESL`、`DCOILWTICO`、`DEXUSEU`、`DGS10`、`DGS2`、`FEDFUNDS`、`PAYEMS`、`UNRATE`、`VIXCLS`。现有宏观可见性/ffill 口径不变；FRED 值为 latest-revised，非 vintage。

### 截面与缺失指示

10 个股票 raw 特征各生成一个 `f_cs_<name>`：逐日对非空行做 average rank、`(rank - 0.5) / n`，再做逆正态变换。非 common 行仍参与截面；宏观列因同日恒定而排除。

43 个 raw 列各有一个 `miss_<name>`：对应 raw 值为空时置 1。生成指示前，非有限 raw 值先归一为 null。

## 4. 标签与 flags

既有 32 个标签不变：

```text
adjusted_open = open × adj_close / close
target_return_hd = adjusted_open(t+1+h) / adjusted_open(t+1) - 1，h = 1..30
excess_5d/21d = target_return_5d/21d - 同日普通股等权均值
```

excess 基准只用 `is_common=true` 且 `flag_extreme_label=0` 的行。signal 行在下一 canonical session 开盘入场；不替换或填充中间 bar。所需入场/出场 bar 不可用时，标签自然为空。

`flag_extreme_label` 标记标签窗跨越 adjusted-open 相邻 session 比值超出 `[0.5, 2.0]` 的情况。标记行保留在样本里，并从 excess 基准均值中排除。按 signal date 切分：fit 至 2018、select 为 2019–2020、screen 为 2021–2024、reserve 从 2025 起。适用 split 边界前 31 个 canonical-calendar signal session 在构建时 purge；消费者不再 purge。

## 5. `meta.parquet`、manifest 与核验

`meta.parquet.missing_frac` = purge 后保留样本的 43 个 raw 特征空值数 / `保留行数 × 43`。不含 CS/MISS、标签、键/标志或财务列。

每个新 manifest 记录唯一的 `schema_version="samples"` 身份、精确有序的 96 列 feature list、semantic contract 与 schema/semantic fingerprints、label semantics 和构建参数；所有实际消费的行情、宏观与 exclusions 输入均以流式 SHA-256/字节数记录；还包括已哈希的代码文件/依赖身份。发布 manifest 前会重新核对输入哈希。单独的 `input_inventory` 仅为计数，不是内容 provenance。每个登记输出均记录 SHA-256、字节数和行数；validation 与 `query-samples` 检查全部登记文件。财务文件和 metadata 不会作为样本输入被读取或登记。

使用独立当前契约预期值 fixtures 与不变式核验候选：row keys/flags/labels、split/purge、ticker failures、raw/CS/MISS 值，以及 schema/列序/dtype。不要把跨代 projection 当作正确性 oracle。候选构建本身不等于发布。现有 `data/output/` 是 manifest 标签为 `samples_v3` 的原样历史产物；旧 147 列 output 与冻结 baseline 分别保留。不声称性能实测或财务正确性。

安全构建候选时，CLI 默认 `--out data/samples-output`；`workspace_root` 默认为 CWD，也可用 `--workspace-root` 指定已存在目录。默认 exclusions 文件在 workspace 下的 `config/universes/exclusions_v1.json`；配置缺失时报错，显式传入的 exclusions 路径相对于 CWD。guard 保护 `<workspace_root>/data/` 及相关路径；对于常规 `<data>/organized` 输入，还会独立于 `workspace_root` 保护其旁边识别出的 raw/output/baseline/archive 路径。guard 拒绝符号链接路径/祖先、非空目标，以及与受保护路径或实际输入重叠的目标。使用保护范围外的全新 `--out`；不得指向保留的 `data/output/`。
