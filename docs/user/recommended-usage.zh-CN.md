[English](recommended-usage.md) | **简体中文**

# `samples` 推荐用法

本指南适用于唯一不含财务数据的 `samples` 契约与保留的历史 bundle。现有 `data/output/` 发布保留原始 `schema_version="samples_v3"` manifest；旧 147 列 output 保留在 `data/output-v1-backup-20261003T172214933236Z`，冻结 baseline 另行保留。消费前检查每个 bundle 的实际 manifest 与 schema。历史 schema 标签仅描述 artifact，不是产品选择器或兼容承诺。完整契约见 [data-format.zh-CN.md](data-format.zh-CN.md)，安全构建见 [cli.zh-CN.md](cli.zh-CN.md)。

## 1. 读取并校验候选

- 读取所选 bundle 的 manifest 和 schema，再核验登记的 output hashes。新构建使用唯一的 `samples` 契约；不要通过产品版本选择器推断契约。
- 样本以 `(date, asset_id)` 为键。按 `splits.json` 切分；适用边界窗已在构建期 purge，下游不要再次 purge。
- 当前 schema 有 132 个物理列：4 键/标志、43 非财务 raw、10 同日截面特征、43 缺失指示和 32 标签。`manifest.feature_list` 有 96 个特征列。
- `samples` 契约不含财务特征或财务缺失指示。`data/organized/` 中已有 SEC 文件是独立 artifact，不是样本输入。若旧 manifest 中有财务状态 `not_applicable`，也不代表财务覆盖核验结果。
- `null` 不等于 0。选取或 impute 对应 raw 值时保留 `miss_*`；不要静默把缺失填为 0。
- `f_cs_*` 是同日截面 rank，应在生成它的截面内解释，不要视为跨时间的全局量纲。宏观列仅保留 raw，因为同日所有 ticker 共享。

## 2. 标签与行筛选

- 既有标签不变：`target_return_1d..30d`、`excess_5d`、`excess_21d`。`excess_*` 减去同日 `is_common=true` 且 `flag_extreme_label=0` 行的等权均值；不含 SPY 基准。
- 使用 `splits.json` 做时间切分。`reserve` 留作最后一次评估；相邻日期共享未来价格，不要随机拆行。
- `flag_extreme_label=1` 表示标签窗口跨越相邻 session adjusted-open 比值超出 `[0.5, 2.0]`。这些行仍留在 bundle；下游应记录统一规则并在评估中保持一致。
- 未来入场/出场 bar 不可用时，标签自然为空。不要填补标签。

## 3. 读取少量结果

想在命令行快速查询时，可安装可选的 `query` 依赖，只取需要的行和列。`query-samples` 严格核验并只接受当前 `samples` 契约；它拒绝保留的历史 bundle，包括 `data/output/`（`samples_v3`）。历史 bundle 请用 Python/PyArrow 检查，或查询新构建的当前契约候选。

```bash
uv sync --frozen --extra query
BUNDLE=/tmp/opencode/candidate-samples-finance-free
quant-dataset query-samples --bundle "$BUNDLE" \
  --sql "SELECT date, asset_id, is_common FROM samples WHERE date >= DATE '2019-01-01' AND date < DATE '2020-01-01' ORDER BY date, asset_id" \
  --limit 5
```

样本包仍是 Parquet 文件；Python 用户可用 PyArrow 直接读取历史 artifact。CLI 查询只支持当前 `samples` 契约，并使用 bundle 的物理列；不会把分区目录名 `year` 自动加成查询列。安装、输出格式与行数限制见[查询指南](query.zh-CN.md)。

## 4. 数据源注意事项

| 注意项 | 影响 |
|---|---|
| 幸存者偏差 | 股票池基于当前 SEC ticker 名单，不含退市历史。这是数据限制，不代表未来收益。 |
| FRED 非 vintage | 宏观值沿用既有 latest-revised 输入和发布日期近似；历史值可能不同于当时已知值。 |
| adjusted open 非可成交价 | 标签按 `open × adj_close / close` 计算，不是成交模拟，也未计成本/滑点。 |
| `is_common` 是启发式 | 按 ticker 后缀判断，不是 security master；非 common 行仍保留。 |
| 宏观覆盖各异 | 序列起始日和缺失率不同。使用对应 `miss_m_*` 并检查候选 QC；本文不声称任何 v3 覆盖率。 |
| 无 SPY 基准 | Excess 标签按上文所述的 common 股票等权目标均值定义。 |

旧 `financials.csv` 仍保留历史的整份快照替换语义，`financial_events_v1` 仍是选定九概念的 organized 抽取。二者均不由 `samples` 读取，也都不是完整 filing archive。独立 filing pilot 与样本契约保持分离；范围与限制见[申报指南](filings.zh-CN.md)。

## 5. 常见坑

1. 读取候选后不要再次 purge。
2. 不要把 `miss_*` 当标签，也不要默认把 raw null 填为 0。
3. 不要把 `f_cs_*` 当成跨日全局标准化指标。
4. 不要把 `is_common=false` 当普通股；这些行仍可能参与截面 rank。
5. 全新候选须有自己的当前契约核验记录后才能称为已发布版本。现有 `data/output/` 与旧 147 列产物是保留的历史 artifact，不是此次文档变更的输出。
6. 使用独立编写的当前契约 fixtures 和任务指定不变式；跨代 projection 不是正确性 oracle。历史发布证据不等于新核验结果。
