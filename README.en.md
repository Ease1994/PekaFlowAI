# PekaFlowAI

[中文](README.md) · [English](README.en.md)

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**PekaFlowAI** is an AI-agent-first release platform for mixed targets. People query, request access, ship, diagnose failures, and write plugins in natural language; pipelines and builder / node agents put the change onto the environment.

The web app has a language switcher on the sign-in page and in the header: Simplified Chinese, Traditional Chinese, English, Japanese, Hindi, Brazilian Portuguese, and German.

The assistant runtime follows [DeepSeek AI-Harness](https://github.com/deepseek-ai/deepseek-harness) (session event sourcing, agent-loop, tool guards, compaction, Skills, MCP, sandbox).

## What it does

- **Three doors**: AI assistant, web UI, and OpenAPI can all do what the signed-in identity is allowed to do
- **Approve before release**: every production ship goes through approval, and every decision is kept on record
- **Heterogeneous targets**: Kubernetes, Docker, jars, IIS incremental, artifacts, rollback, and more
- **Agents**: builders and deploy nodes update themselves; permissions are tight so unauthorized actions do not reach production
- **Roles and permissions**: access can be scoped to menus and buttons
- **Production safety first**: commands are reviewed before they run; unnamed or unconfirmed changes stay off the environment
- **Source control**: most common forges work out of the box; for a niche one, ask the assistant to write a plugin
- **More**: see the [feature spec](deploy-platform/docs/功能说明书.md) (Chinese)

## Architecture diagrams

- [System](deploy-platform/docs/系统架构图.png)
- [Business modules](deploy-platform/docs/全模块业务架构图.png)
- [Technical](deploy-platform/docs/技术架构图.png)

---

# Deploy the system

Full stack with Docker Compose: MySQL, Redis, Elasticsearch, backend, frontend, harness-runner. Compose files live under **`deploy-platform/`**.

One command (Linux / macOS, Docker Compose v2):

```bash
git clone https://github.com/Ease1994/PekaFlowAI.git
cd PekaFlowAI
bash deploy/pekaflow.sh
```

If `.env` is missing it copies the trial config and starts the stack. If `.env` already exists it upgrades: fast-forward `master`, rebuild images, keep volumes. Subcommands: `install` / `upgrade` / `status`. For production, edit `deploy-platform/.env` before the first start.

Builders, nodes, HTTPS, and backups: [deployment guide](deploy-platform/docs/部署文档.en.md). Manual steps below.

### Step 1 · Prepare the host

- [Docker](https://docs.docker.com/get-docker/) with Compose v2
- Network access to pull images. The first build compiles frontend and backend and can take several minutes

```bash
git clone https://github.com/Ease1994/PekaFlowAI.git
cd PekaFlowAI/deploy-platform
```

### Step 2 · Create the env file

```bash
cp .env.example .env
```

On Windows, copy `.env.example` to `.env` by hand. `.env` must sit next to `docker-compose.yml`. Never commit a filled `.env`.

| Use | What to do with `.env` |
|------|----------------|
| Local trial | Keep the example passwords |
| Production | Change every secret before the first start. The password in `DATABASE_URL` must match `MYSQL_ROOT_PASSWORD` exactly |

Changing MySQL / Redis passwords on an existing volume has no effect. To change them you must `docker compose down -v` and start over (**this wipes the database**). **Do not rotate JWT / AES on a running install**, or Git credentials already encrypted in the database will not decrypt. Take `.env` with you when migrating hosts.

### Step 3 · Start

```bash
docker compose up -d --build
```

### Step 4 · Wait until it is ready

```bash
docker compose ps
docker compose logs -f backend
```

Open the browser only after backend and frontend are `healthy` or `running`, and the backend log shows tables and the admin user.

| Entry | URL |
|------|------|
| UI | http://localhost:8000 |
| API | http://localhost:8080 (`/docs` is off by default) |
| Health | http://localhost:8080/api/v1/health |

Do not publish MySQL 3306, Redis 6379, or ES 9200 on the host in production. Compose already runs ES; build logs go to `rp-exec-logs-YYYY-MM-DD`.

### Step 5 · First login checklist

1. Sign in as `admin` / `admin123` (or `BOOTSTRAP_ADMIN_PASSWORD` if you set it)
2. Open Users and change the admin password immediately
3. Open Settings and set the public site URL: `http://localhost:8000` locally, or the host IP / domain on a LAN
4. Open Models and add an OpenAI-compatible model; the AI Agent will not talk without one
5. To build or deploy, install builders and nodes using the [deployment guide](deploy-platform/docs/部署文档.en.md)

### Step 6 · Stop without wiping data

```bash
docker compose down
```

**Do not** add `-v`. That deletes MySQL, secrets, artifacts, and log volumes and destroys the install.

---

# Upgrade

An upgrade replaces code and images. **Volumes must stay.** Do not change JWT / AES that this install already uses. Do not run `docker compose down -v`.

```bash
cd PekaFlowAI
bash deploy/pekaflow.sh upgrade
```

Add `--skip-git` if you already pulled. The script fast-forwards `master`, rebuilds the Agent jar when the fingerprint drifts, then runs `docker compose up -d --build`.

### Step 1 · Back up first (recommended)

Keep at least: MySQL, the `backend-data` volume (secrets / artifacts / plugins), and the JWT and AES in force at that time. See [Backup](deploy-platform/docs/部署文档.en.md).

### Step 2 · Pull the new code

From the directory you cloned:

```bash
cd PekaFlowAI
git pull
cd deploy-platform
```

### Step 3 · If you changed Java Agent sources

If `deploy-platform/backend/agent-java/src` changed, rebuild the jar **before** rebuilding the backend image. Otherwise the platform keeps shipping the old jar.

```bash
cd backend/agent-java
# Windows (needs JDK 8 javac)
build.bat
# Linux / macOS
./build.sh
cd ../..
```

This produces `deploy-agent.jar` and an updated `.srcsha`.

### Step 4 · Rebuild and start

Still under `deploy-platform/`:

```bash
docker compose up -d --build
```

Do not add `-v`. After the image updates, the database, secrets, and artifacts remain. The backend applies schema patches on start; you do not migrate the database by hand.

### Step 5 · Confirm the upgrade

1. `docker compose ps`: backend and frontend are `healthy` or `running`
2. Health check: http://localhost:8080/api/v1/health
3. You can sign in; pipelines and history are still there

### Step 6 · Refresh agents on builders and nodes

| Role | How the jar updates |
|------|----------------|
| Builder | Pulls the new jar from the platform when idle; usually no manual step |
| Deploy node | Click Upgrade on Nodes; wait until the current release finishes |

Do not start a second process on the machine to “swap the jar” by hand.

---

## Local development (no Compose)

You need Python 3.10+ and Node.js 18+. On Windows use `py`, not `python3`. This is for changing code, not a production install.

```bash
git clone https://github.com/Ease1994/PekaFlowAI.git
cd PekaFlowAI

# Backend
cd deploy-platform/backend
py -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8080

# Another terminal: frontend
cd deploy-platform/frontend
npm install
npm run dev -- --host 0.0.0.0
```

Open http://localhost:5173 . Demo login `admin` / `admin123` (local only).

## Docs

| Doc | Contents |
|------|------|
| [Feature spec](deploy-platform/docs/功能说明书.md) | What each module does (Chinese); [Word](deploy-platform/docs/发布部署平台-功能说明书.docx) |
| [Deployment](deploy-platform/docs/部署文档.en.md) · [中文](deploy-platform/docs/部署文档.md) | Install, Compose, agents, HTTPS, backup |
| [Developer handbook](docs/开发者手册.md) | Architecture, modules, database, pitfalls (Chinese) |
| [Contributing](CONTRIBUTING.en.md) | Tests and commit rules |
| [Security](SECURITY.en.md) | Report vulnerabilities via GitHub Advisory, not public issues |

## Stack

React 18 + TypeScript + Vite + Ant Design 5; FastAPI + SQLAlchemy 2; MySQL 8 / SQLite + Redis 7 + Elasticsearch 8; Java agent (JDK 8 single jar).

## License

[MIT](LICENSE)
