# Raw 下载实现

保留的下载流水线只获取 provider 原始 payload，不再组织数据或创建派生面板。

## 阶段与数据流

```text
CLI → manager → provider modules → data/raw + 可恢复进度
```

`src/cli/main.py` 接受 `market`、`financials`、`macros`、`all`。`src/download/manager.py` 负责配置、universe、锁和进度；provider modules 负责网络访问。`all` 展开为三个 raw 阶段。Market 必须指定含首尾日期的 `--start` 和 `--end`。

| 模块 | 职责 |
|---|---|
| `config.py` | 解析 `config/sources.toml` 与私有 provider 凭据。 |
| `universe.py` | 获取/缓存 SEC exchange universe，并解析 ticker/CIK identity。 |
| `market.py` | 获取 Yahoo 日线。 |
| `financials.py` | 获取 SEC Company Facts、submissions 与引用的历史 submissions 分页。 |
| `macros.py` | 获取配置的 FRED observations。 |
| `progress.py` | 原子化可恢复进度与进程锁。 |

SEC raw financial metadata 用于识别申报，不是报告正文语料。完整报告样本获取独立实现于 `src/filings/report_samples.py`，见[用户指南](../user/filings.zh-CN.md)。

## 配置与路径

`config/sources.toml` 存放非敏感 provider URL、forms、series、限速及 raw/progress 路径。`config/secrets.toml` 存放 provider 凭据，必须保密。`--data-dir` 重映射 raw 和 progress 路径；CLI smoke 测试必须使用临时隔离目录。`--dry-run` 不联网、不写入。

进度按 stage/item 保存；恢复时跳过已完成项。`--force` 重置选定进度。不得添加真实网络集成测试，应使用 mock transport。代码维护不得覆盖或清理现有 raw 数据。
