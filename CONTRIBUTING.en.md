# Contributing

[中文](CONTRIBUTING.md) · [English](CONTRIBUTING.en.md)

Thanks for wanting to change this project. Read the pitfalls table in [`docs/开发者手册.md`](docs/开发者手册.md) first (Chinese).

## Run locally

On Windows do not use `python3` (it is often the Store redirector). Use `py` or the interpreter inside the venv.

```bash
# Backend
cd deploy-platform/backend
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8080

# Frontend
cd deploy-platform/frontend
npm install
npm run dev -- --host 0.0.0.0
```

Demo account: `admin` / `admin123`. In production change the password or set `BOOTSTRAP_ADMIN_PASSWORD`.

Full stack: copy `deploy-platform/.env.example` to `.env`, then `docker compose up -d --build`. Trial passwords are already in the example. Step-by-step: [README.en.md](README.en.md).

## Tests

```bash
cd deploy-platform/backend
pytest tests/ -q

cd deploy-platform/frontend
npm test
```

Live tests (`tests/test_live_*.py`) skip unless you set `LIVE_BASE`.

When changing pipeline name matching, run `tests/test_pipeline_matching.py` first.

## Commit rules

- Write the message in Chinese and say why you changed it.
- Do not commit `.env`, secrets, `data/.secrets.json`, caches, or build artifacts.
- Deploy/release behavior with an unclear scope must fail closed. Do not silently widen the blast radius.
