# 参与贡献

[中文](CONTRIBUTING.md) · [English](CONTRIBUTING.en.md)

感谢你愿意改这个项目。动手前请先读 [`docs/开发者手册.md`](docs/开发者手册.md) 的踩坑表。

## 本地跑起来

Windows 不要用 `python3`（多半是 Store 重定向器），用 `py` 或虚拟环境里的解释器。

```bash
# 后端
cd deploy-platform/backend
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8080

# 前端
cd deploy-platform/frontend
npm install
npm run dev -- --host 0.0.0.0
```

演示账号：`admin` / `admin123`。生产环境请改密或设置 `BOOTSTRAP_ADMIN_PASSWORD`。

完整栈：复制 `deploy-platform/.env.example` 为 `.env` 后 `docker compose up -d --build`，试用口令已写在 example 里。

## 测试

```bash
cd deploy-platform/backend
pytest tests/ -q

cd deploy-platform/frontend
npm test
```

打已部署环境的用例（`tests/test_live_*.py`）默认跳过，需要时自己设 `LIVE_BASE`。

改流水线名称匹配时，先跑 `tests/test_pipeline_matching.py`。

## 提交约定

- 说明用中文，写清为什么改。
- 不要提交 `.env`、密钥、`data/.secrets.json`、缓存和编译产物。
- 范围不明的发布/部署行为必须失败关闭，不要用静默兜底放大爆炸半径。
