# Semantic data layout

## 四层职责

- `raw/`：provider 原始 payload，内容寻址（filename = sha256）。
- `input/`：语义源表，包括 canonical calendar、identity 和 canonical 日线。
- `artifacts/`：加工输出；`staged/` 是从 input/raw 投影或规范化后的中间产物，`curated/` 保存观测与派生特征，`features/` 与 `samples/` 保存行情派生因子表与样本表，`labels/` 构造未来 1d..30d 收益目标，`prepared/` 保存时序切分后的最终训练集。
- `provenance/`：审计与运行清单。

## 目录树

```text
data/
├── raw/                                    # 原始不可变 provider payload
│   └── sec/financials/                     # SEC Company Facts（sha256-named JSON + manifest）
├── input/                                  # 语义源，下游读取
│   ├── metadata/                           # XNYS session_calendar_*.{csv,json} + sec_ticker_universe.csv
│   ├── sec/
│   │   ├── universe_960/                   # identity.csv, coverage.json, download_manifest.json
│   │   └── universe_958_projected/         # identity.csv（958 rows）, coverage.json, download_manifest.json
│   └── yahoo/aggregate/min5y/aggregate_yahoo_ce58120c8a1fb08e/   # canonical 日线（labels 输入）
├── artifacts/                              # 加工输出
│   ├── staged/yahoo/aggregate/min5y/aggregate_yahoo_projection_35fc8128d49d03b6/   # 958-universe 投影
│   ├── curated/fundamentals/core_v1/       # fundamental_observations.csv, fundamental_feature_changes.csv, skipped_entities.csv, manifest.json
│   ├── features/yahoo/features_e1ef0f729906bdab/   # features.csv + manifest
│   ├── samples/yahoo/samples_54ef1eabc4b791d0/     # samples.csv + manifest
│   ├── labels/multi_horizon_v1/            # multi_horizon_labels.csv（1d..30d）+ labels_manifest.json
│   └── prepared/research_958/              # fit.csv / reserve.csv / screen.csv / select.csv / demand_token_keys.csv / summary.json
├── provenance/
│   └── migrations/                         # relocation receipts, future run manifests
└── _archive/2026-09-23_legacy_sec_capture/ # 冷存储，不属于四层
    ├── artifacts/sec/excluded_958/         # 遗留 958-universe 投影 bundle
    └── provenance/sec/source_960/          # 遗留 960-universe 抓取 + 1995-2008 报文原文（约 7 GB .bin/.htm）
```

## 命名解码

| 后缀 | 含义 |
|---|---|
| `core_v1` / `multi_horizon_v1` | schema 版本 |
| `research_958` | 958 支保留股的研究集 |
| `excluded_958` | 语义陷阱：是“958 支投影 universe”（排除 AYA+FUND），不是“排除 958 支”。仅在 `_archive/` 历史包里保留此字面命名 |
| `source_960` | 源 universe（960 支） |
| `ce58120c…` / `35fc8128…` / `e1ef0f72…` / `54ef1eab…` | 内容 hash（产物 ID，非路径冗余） |
| `<sha256>.json` / `.bin` / `.htm` | 内容寻址（文件名=内容 hash） |

## `_archive/` 说明

`data/_archive/` 是冷存储，不属于四层设计。历史包中保留 `excluded_958` / `source_960` 这两套字面命名，因为它们的 manifest 用相对路径互相引用，改名会破坏校验。

## manifest 与 hash 关系

内容寻址文件的 sha256 同时标识其文件名；产物目录中的短 hash 是产物 ID，不是路径冗余，也不能代替 manifest。Manifest 用于记录产物的来源与校验关系；消费者应按 manifest 中的映射与 hash 核验文件。

历史归档路径通过 `data/provenance/migrations/` 中的迁移 receipt 映射追溯；新构建应在自身 manifest 中记录新路径。
