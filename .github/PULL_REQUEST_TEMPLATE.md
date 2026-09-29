## 动机（为什么改）

<!-- 背景、关联 issue、影响面（下载 / organize / build-samples / 文档 / CI）。 -->

## 验证方式（怎么证明没坏）

<!--
跑了哪些命令、结果如何。涉及 data/output 产物的改动，请给出 manifest.json
对应条目的 sha256 / rows 对比；仅改财务特征的重建需满足 AGENT.md 不变式 5。
-->

## Checklist

- [ ] 本地测试通过：`PYTHONPATH=src .venv/bin/python -m pytest -q`（基线 61 passed / 2 skipped）
- [ ] 如改动了 CLI 参数或 schema 契约，已同步文档（AGENT.md 不变式 8，如 `docs/user/cli.md`、`docs/user/data-format.md`）
- [ ] 未提交数据或密钥：`data/`、`config/secrets.toml` 从不进入 git
- [ ] 已在上面写明「动机」与「验证方式」
