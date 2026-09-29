[English](recommended-usage.md) | **简体中文**

# 推荐用法（训练侧）

面向消费 `data/output/` 的下游训练代码。列定义与文件细节见 [data-format.zh-CN.md](data-format.zh-CN.md)；CLI 与重建流程见 [cli.zh-CN.md](cli.zh-CN.md)。

## 1. 五步流程

### 第 1 步：用 `splits.json` 切分，不再做 purge

- 取 `fit` 训练、`select` 调参/选型、`screen` 样本外验证；`reserve` 只在最终一次性评估时动。
- 三个边界的 31 个信号日已在构建期整体剔除（`purged_windows`），标签不会跨段；下游重复 purge 会再丢约 44 万行。
- fit 段内如需时序交叉验证，按整段日期切（例如按年滚动），不要随机打散。

### 第 2 步：主目标用 `excess_5d` / `excess_21d`

- 两者已减去同日 is_common 等权基准，直接建模可避免把市场 beta 当 alpha；仓库内没有 SPY，这是唯一基准口径。
- 其余 `target_return_1d..30d` 作辅助任务（多 horizon 共享表示、辅助正则），不要与 excess 目标在同一头混用。
- 标签在数据集尾端自然缺失（未来价格不足），属预期，不要填补。

### 第 3 步：特征用 111 列（48 f_raw + 15 f_cs + 48 miss）

- 股票维度特征优先用 `f_cs_*`：当日截面 rank→逆正态，天然跨日可比，避免量纲漂移。
- 宏观列只有 `f_raw_*`（同日恒定，不做截面）；`_d1`/`_d5` 是 canonical 轴位置差。
- `miss_*` 显式建模（0/1 特征或掩码），不要把 NaN 当 0 填。
- `is_common` 可作过滤条件；若保留非普通股，至少把它作为特征并检查该类行的标签分布。

### 第 4 步：处理 `flag_extreme_label`

- `=1` 共 120,510 行（≈0.5%），标签窗口穿过价格毛刺（相邻 adjusted open 比值超出 [0.5, 2.0]），标签不可信。
- 训练时剔除（或大幅降权）；评估时同样剔除，否则个别毛刺会主导指标。
- 注意 excess 基准已排除这些行，若自己重算基准需保持同一口径。

### 第 5 步：按 year 分块加载

- 全量 float32 特征矩阵在内存中约 10 GB（23.9M × 95 float32 ≈ 9.1 GB + 键列），不适合一次性全量读入。
- 推荐按年（或年区间）流式读取：每个分区约 45–440 MB（snappy），单年一次读入即可。
- 训练循环按年 shuffle 文件顺序即可；跨年随机采样对内存不友好。

## 2. 加载示例

duckdb 直查 parquet glob（适合交互式探索；`year` 为 Hive 分区列）：

```python
import duckdb

con = duckdb.connect()
df = con.execute("""
    SELECT date, asset_id, excess_5d, excess_21d,
           f_cs_momentum_120, f_cs_volatility_20,
           f_raw_days_since_filing, miss_revenue_yoy
    FROM read_parquet('data/output/samples/year=*/part-00000.parquet',
                      hive_partitioning = true)
    WHERE year BETWEEN 2019 AND 2020
      AND is_common
      AND flag_extreme_label = 0
""").df()
```

pyarrow.dataset 惰性读 + 分区下推（`date` 为 `date32`，过滤值要用 `datetime.date`，字符串会报无 kernel）：

```python
from datetime import date
import pyarrow.dataset as ds

dataset = ds.dataset("data/output/samples", format="parquet", partitioning="hive")
table = dataset.to_table(
    columns=[
        "date",
        "asset_id",
        "is_common",
        "flag_extreme_label",
        "excess_21d",
        "f_cs_momentum_120",
    ],
    filter=(ds.field("year") == 2024) & (ds.field("date") >= date(2024, 1, 1)),
)
df = table.to_pandas()
```

## 3. 偏差与数据源现实

| 现象 | 原因 | 应对 |
|---|---|---|
| 幸存者偏差 | universe 是当前 SEC 名单，无退市股 | fit 期收益偏乐观；以 screen 作为真实检验，不要外推绝对收益 |
| FRED 非 vintage | 存的是最新修正值 | 宏观特征含事后修正；对宏观依赖做敏感性检查 |
| adjusted open 非可成交价 | `open × adj_close / close` 的复权价 | 不可当成交价；成本/滑点自行建模 |
| `is_common` 是启发式 | 仅按 ticker 后缀判断 | 需要严格股票池时自建 security master；非普通股行仍在表中 |
| 财务覆盖稀疏（缺失 ≈96%）且非随机 | 快照语义：仅当日最新申报可见，字段缺失不继承旧值；申报密集期/大公司覆盖更好 | 用 `miss_*` + `f_raw_days_since_filing`；按覆盖度分层评估 |
| 宏观前期稀疏 | 部分序列 2000 年代前缺失（如 `BAMLH0A0HYM2` 缺失 87%） | 用 `miss_m_*` 掩码；宏观敏感模型缩短训练窗口 |
| 无 SPY 基准 | 组织数据中不存在指数 | excess 为等权 is_common 均值基准，比较对象要一致 |
| 标签窗口重叠 | 相邻信号日共享未来价格（自相关） | 评估按股票/日期聚类或整段留出；不要用随机 K 折 |
| canonical 日历 ≠ 交易所日历 | 仅保留 ≥500 只 is_common 股票的 session | 对齐外部数据时以样本表内 `date` 轴为准 |

## 4. 常见坑

1. **重复 purge**：purge 已在构建期完成，下游再砍 31 个 session 只会白丢数据（见 `splits.json.purge_semantics`）。
2. **忽略 `miss_*`**：`f_raw_*` 为 null 不等于 0；直接把 NaN 填 0 会制造伪信号，尤其财务列缺失率极高。
3. **截面特征跨日混用**：`f_cs_*` 只在生成它的当日截面内有意义；不要跨日做池化标准化或把不同日期的 rank 混进同一 batch 统计。
4. **标签重叠自相关**：相邻日样本高度相关，随机切分/早停会高估表现；坚持按时间与股票分组评估。
5. **把 `is_common=false` 当普通股**：这些行（权证、优先股等）仍参与截面 rank；只按 `asset_id` 取数会悄悄混入非目标证券。
6. **手改 `data/output/` 产物**：`build-samples` 每次都会清空重建并刷新 sha256；修改应落在代码或 `config/universes/exclusions_v1.json`，再整体重建。
