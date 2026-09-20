# 安全披露

[中文](SECURITY.md) · [English](SECURITY.en.md)

请不要在公开 Issue、讨论区或 Pull Request 里报告安全漏洞。

## 怎么报

1. 用 GitHub 仓库的 **Security → Report a vulnerability**（私密 Advisory）。
2. 写清影响范围、复现步骤、你认为的风险。有 PoC 请一并附上。

我们会尽快确认。在补丁发布前，请不要公开细节。

## 部署时请注意

- 生产必须设置 `BOOTSTRAP_ADMIN_PASSWORD`，不要沿用演示口令 `admin123`。
- 不要把填了值的 `.env`、`data/.secrets.json`、JWT / AES 密钥提交进 Git。
- 接口文档 `/docs` 默认关闭，不要对公网打开。
- 空发布清单不会按全量打包；没写明文件列表的步骤会直接拒绝执行。
