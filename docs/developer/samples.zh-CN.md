[English](samples.md) | **简体中文**

# 样本构建内部机制（`src/samples/`）

> 唯一维护的契约是不含财务数据的 `samples` 产品。2026-10-03 发布在 `data/output/` 的 bundle 保留旧 manifest 值 `schema_version="samples_v3"`，是原样只读的历史产物，不是新发布或当前发布。旧 147 列产物和冻结 baseline 也分别保留。该发布报告及复用的 735 passed/2 skipped 测试证据仅属历史记录，不验证当前源码。若保留财务状态 `not_applicable`，也不代表财务覆盖率。
> `samples` 包迁移已在当前源码树实现；当前契约测试/发布 gate 单独跟踪，本文不暗示完整 gate 已通过。见 [samples-architecture.md](samples-architecture.md)、[data-contracts.zh-CN.md](data-contracts.zh-CN.md)、[architecture.zh-CN.md](architecture.zh-CN.md)、[AGENT.zh-CN.md](../../AGENT.zh-CN.md) 与 [known-quirks.zh-CN.md](known-quirks.zh-CN.md)。

## 1. 输入、输出与安全构建

样本 builder 只消费 organized 行情、宏观面板和配置的 exclusions。它**不读取** `financials.csv`、`financial_events.parquet`、`financial_facts.parquet` 或财务 `_meta.json` 记录；不运行财务预检、覆盖率计算或财务输入盘点。当前 manifest 不含财务状态字段，也不声称财务覆盖率。

现有 SEC download/organize 流程独立且保持不变。现有路径中的结构化 `financials.csv` 与 `financial_events_v1` artifact 仍保留，但都不是 `samples` 输入。`financial_events_v1` 是选定九个概念的结构化抽取，不是完整 XBRL 数据集或完整申报档案。

`samples.builder.build_samples` 的 `workspace_root=None` 默认是当前工作目录；CLI 通过 `--workspace-root PATH` 暴露同一设置。未指定 `--exclusions-file` 时，builder 从 `<workspace_root>/config/universes/exclusions_v1.json` 读取 exclusions。显式传入的 exclusions 路径相对于当前工作目录解析。配置文件缺失时必须报错，不得隐式使用空 exclusions。保留既有 `exclusions_v1.json` 文件名；它是配置文件名，不是样本产品版本。

CLI 默认 `--out data/samples-output`。构建到全新、独立的输出目录。guard 保护 `<workspace_root>/data/` 与相关 raw/output/baselines 路径；若输入为常规 `<data>/organized`，还会独立于 `workspace_root` 保护其 `<data>` 旁识别出的 raw/output/baseline/archive 路径。显式 workspace root 必须存在。拒绝符号链接路径/祖先、非空目标，或与受保护路径/实际输入重叠的目标。不要覆盖历史 bundle 或 baseline。

```bash
WORKSPACE_ROOT=$PWD
CANDIDATE_DIR=/tmp/opencode/candidate-samples-finance-free
# 若路径已存在，请换成另一个未使用路径；不得删旧数据后复用。
test ! -e "$CANDIDATE_DIR" && test ! -L "$CANDIDATE_DIR" || { echo "Choose a new, unused, non-symlink candidate directory" >&2; exit 1; }
PYTHONPATH=src .venv/bin/python -m cli.main build-samples \
  --workspace-root "$WORKSPACE_ROOT" \
  --data-dir data/organized --out "$CANDIDATE_DIR"
```

Workspace、默认配置和保护路径不得由已安装 source package 的位置推导。

构建 `samples` 不要求新增财务下载或 filing-archive 操作。

## 2. 唯一 samples 特征清单

| 组 | 定义 | 数量 |
|---|---|---:|
| 非财务 raw 特征 | 10 个行情/派生股票特征 + 33 个宏观特征 | 43 |
| 截面特征 | 10 个股票 raw 特征各有一个 `f_cs_*` 镜像；宏观排除 | 10 |
| 缺失指示 | 43 个 raw 特征各有一个 `miss_*` | 43 |
| `manifest.feature_list` | Raw + CS + MISS | **96** |
| 标签 | `target_return_1d..30d`、`excess_5d`、`excess_21d` | 32 |
| 样本物理列 | 4 键/标志 + 96 特征 + 32 标签 | **132** |

物理列 canonical 顺序：

```text
date, asset_id, is_common, flag_extreme_label,
43 个非财务 f_raw 列,
10 个 f_cs 列,
43 个 miss 列,
32 个标签
```

物理 dtype 为：`date` 使用 Arrow `date32`，`asset_id` 使用 Arrow `large_string`，`is_common` 为 bool，`flag_extreme_label`/`miss_*` 为 uint8，raw/CS/label 数值列为 float32。

10 个股票 raw 特征保留既有 6 个行情直通值（`return_1d/5d/20d`、`volatility_20`、`volume_ratio_20`、`intraday_range`）和 4 个派生值（`momentum_60/120`、`volatility_60`、`volume_zscore_60`）。33 个宏观 raw 列仍为 11 个序列 × 水平值 / canonical 轴位序 `_d1` / `_d5`。

样本中移除此前全部 51 个财务列：17 个财务 raw、17 个截面镜像和 17 个缺失指示列。移除范围包括申报年龄、财务年龄、水平值、同比、比率及季度环比字段。不会把财务值替换或 carry 到其他样本列。

## 3. 保持不变的样本语义

当前行为契约保留规定的行情/宏观 raw 与 CS/MISS 计算、标签语义、`(date, asset_id)` 键、flags、split 分配、purge、extreme-label 逻辑与 ticker failure 处理。用独立当前契约 fixtures 核验公式和边界；历史 v1 projection 不是正确性 oracle。

- Canonical calendar：行情面板中至少有 `canonical_min_tickers` 只 common ticker 的日期（默认 500）。
- 截面变换：逐日、逐股票 raw 特征，对非空值做 average rank → `(rank - 0.5) / n` → 逆正态变换。`is_common=false` 行仍参与；宏观列不参与排名。
- 缺失指示：`miss_* = 1` 当且仅当对应 raw 特征为空；非有限 raw 值先归一为空。
- 标签：`adjusted_open = open × adj_close / close`；信号位 `t` 的入场为 `t+1`，出场为 `t+1+h`，`h=1..30`。`excess_5d/21d` 减去同日 `is_common=true` 且 `flag_extreme_label=0` 行的等权目标均值。
- 按 signal date 切分：fit ≤ 2018、select 2019–2020、screen 2021–2024、reserve 从 2025 起。
- Purge：若后续窗口有 session，则在构建期剔除每个适用 split 边界前 31 个 canonical-calendar signal session。消费者不再 purge。

### `meta.parquet`

`missing_frac` 在 purge 后只使用 43 个非财务 raw 列重新计算：

```text
raw 空值单元格数 / (保留行数 × 43)
```

不含 CS、缺失指示、键/标志、标签或任何财务字段。

## 4. Manifest、输入盘点与 QC

新构建 manifest 以 `schema_version="samples"` 标识唯一契约，并记录精确有序的 96 列 feature list、semantic contract、schema/semantic fingerprints、label semantics 和构建参数。`input_provenance.files` 为每个实际消费的行情/宏观/exclusions 文件记录路径、流式 SHA-256 与字节数；manifest 发布前会复核输入 hash。`code_identity` 记录 samples 五个 package 文件的 hash/bytes 及 NumPy/pandas/PyArrow 版本。`input_inventory` 只包含计数，不是内容盘点。每个登记输出记录 SHA-256、字节数和行数。样本不读取、预检、核算覆盖率或登记财务文件和财务 metadata。

QC 仍记录行数、标签、raw/CS/MISS 缺失、截面规模、purge、extreme-label 行和 ticker failures。当前契约没有财务覆盖率报告或财务特征盘点。

## 5. 核验与发布状态

用当前契约 fixtures 与不变式核验全新候选，至少检查：

1. 96 个有序特征、132 个物理列、dtype 与 null/missing-indicator 语义。
2. 对标签、entry/exit 缺失、horizon 边界、split 边界及 extreme-value 行为使用独立预期值案例。
3. 无财务列，也不读取财务文件/metadata，不运行预检、coverage 或输入盘点。
4. `meta.missing_frac` 分母为保留行数 × 43 个 raw 特征。
5. 确定性、安全输出路径、实际输入 provenance 与输出 hashes；是否已有实现须按源码核实。

使用 `tests/fixtures/samples/current/` 中当前的小型 fixtures 和当前样本测试（相关时包括 `tests/test_samples_current_contract.py`、`tests/test_samples_semantic_regression.py`、`tests/test_samples_integrity_regression.py`、`tests/test_samples_safety_regression.py`、`tests/test_query_samples.py`、`tests/test_verification_tools.py`、`tests/test_build_samples.py`）；预期值不得由 builder 生成。已删除旧跨代 projection/baseline 工具，不存在兼容壳，也不能用作正确性 oracle。2026-10-03 归档发布报告及复用的测试证据仅为历史记录；不得据此推断当前测试状态、性能或财务正确性。现有 output、backup 与 baseline 目录保持不变。

## 6. 验证命令

```bash
# 仅运行任务指定的当前契约 samples 测试，并使用当前 tests/ 中的路径。

CANDIDATE_DIR=/tmp/opencode/candidate-samples-finance-free
# 构建前确认该路径未使用；不要覆盖既有产物。
test ! -e "$CANDIDATE_DIR" && test ! -L "$CANDIDATE_DIR" || { echo "Choose a new, unused, non-symlink candidate directory" >&2; exit 1; }
PYTHONPATH=src .venv/bin/python -m cli.main build-samples \
  --workspace-root "$PWD" --data-dir data/organized --out "$CANDIDATE_DIR"
```

只运行任务负责人指定的测试和核验。不得把归档产物或历史核验报告当作独立当前契约 fixtures 的替代。
