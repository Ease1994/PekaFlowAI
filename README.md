# PekaFlowAI

[中文](README.md) · [English](README.en.md)

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**PekaFlowAI** 是以 AI Agent 为交互核心的多形态发布部署平台。人用自然语言查询、申请权限、发起发布、诊断失败、编写插件；流水线编排与构建机 / 节点执行，把变更落到环境上。

界面语言可在登录页和顶栏切换：简体中文、繁体中文、English、日本語、हिन्दी、Português（巴西）、Deutsch。

助手运行时按 [DeepSeek AI-Harness](https://github.com/deepseek-ai/deepseek-harness) 的思路落地（session 事件溯源、agent-loop、tools 守卫管线、compaction、Skills、MCP、sandbox）。

## 能做什么

- **三种入口**：AI 助手、Web 页面、OpenAPI 都能做权限范围内的操作
- **变更先审批**：每次上线走审批流程，审批记录留档可查
- **异构落地**：K8s、Docker、jar、IIS 增量、制品与回滚等
- **Agent**：构建机与部署节点自动更新，不用手工维护；权限收得很紧，不会把未授权动作放到生产上
- **权限与角色**：权限可以细分到菜单和按钮
- **生产安全优先**：严格审查要执行的命令，未点名、未确认的变更不会上环境
- **代码库**：覆盖市面上大部分代码托管；偶尔遇到冷门的，可以让助手开发插件接上
- **更多功能**：见 [功能说明书](deploy-platform/docs/功能说明书.md)

## 架构图

- [系统架构图](deploy-platform/docs/系统架构图.png)
- [全模块业务架构图](deploy-platform/docs/全模块业务架构图.png)
- [技术架构图](deploy-platform/docs/技术架构图.png)

---

# 系统部署

完整栈用 Docker Compose：MySQL、Redis、Elasticsearch、后端、前端、harness-runner。编排文件在 **`deploy-platform/`**。

一键（Linux / macOS，需 Docker Compose v2）：

```bash
git clone https://github.com/Ease1994/PekaFlowAI.git
cd PekaFlowAI
bash deploy/pekaflow.sh
```

没有 `.env` 时复制试用配置并拉起完整栈；已有 `.env` 则按升级处理（拉 `master`、重建镜像、不删数据卷）。子命令：`install` / `upgrade` / `status`。生产请先改 `deploy-platform/.env` 再启动。

构建机、节点、HTTPS、备份的细节见 [部署文档](deploy-platform/docs/部署文档.md)。下面是等价的手工步骤。

### 步骤 1 · 准备机器

- 已安装 [Docker](https://docs.docker.com/get-docker/)（含 Compose v2）
- 能访问镜像仓库，第一次构建会拉镜像并编译前后端，可能要几分钟

```bash
git clone https://github.com/Ease1994/PekaFlowAI.git
cd PekaFlowAI/deploy-platform
```

### 步骤 2 · 写环境文件

```bash
cp .env.example .env
```

Windows 没有 `cp` 时，把 `.env.example` 复制一份改名为 `.env`。`.env` 必须和 `docker-compose.yml` 同级。填了值的 `.env` **不要提交进 Git**。

| 用途 | 怎么处理 `.env` |
|------|----------------|
| 本机试用 | 直接用 example 里的口令即可 |
| 生产 / 对外 | 改掉每一项口令和密钥再启动。`DATABASE_URL` 里的密码必须和 `MYSQL_ROOT_PASSWORD` 逐字相同 |

已经在跑的数据卷上改 MySQL / Redis 口令不会生效。要换口令只能 `docker compose down -v` 后重来（**库会清空**）。**已经在跑的环境不要换 JWT / AES**，否则库里加密的 Git 凭证解不开。迁机把 `.env` 一起带走。

### 步骤 3 · 启动

```bash
docker compose up -d --build
```

### 步骤 4 · 确认就绪

```bash
docker compose ps
docker compose logs -f backend
```

等到 backend、frontend 为 `healthy` 或 `running`，且后端日志里出现建表、管理员就绪，再打开浏览器。

| 入口 | 地址 |
|------|------|
| 前端 | http://localhost:8000 |
| API | http://localhost:8080（默认不开 `/docs`） |
| 健康检查 | http://localhost:8080/api/v1/health |

生产不要把 MySQL 3306、Redis 6379、ES 9200 映射到宿主机。Compose 已带本栈 ES，构建日志写入 `rp-exec-logs-YYYY-MM-DD`。

### 步骤 5 · 第一次登录后必做

1. 用 `admin` / `admin123` 登录（若设了 `BOOTSTRAP_ADMIN_PASSWORD` 则用新口令）
2. 打开「用户管理」，立刻改掉管理员密码
3. 打开「平台设置」，填写站点根地址：本机用 `http://localhost:8000`，局域网改成平台机 IP 或域名
4. 打开「模型管理」，配一个 OpenAI 兼容模型，否则 AI Agent 不能用
5. 需要构建或往环境上发时，按 [部署文档](deploy-platform/docs/部署文档.md) 安装构建机和部署节点

### 步骤 6 · 停机（不删数据）

```bash
docker compose down
```

**不要**加 `-v`。加了会删掉 MySQL、密钥文件、制品和日志卷，等于拆掉这套环境。

---

# 版本升级

升级只换代码和镜像，**数据卷必须留下**。不要改正在用的 JWT / AES。不要 `docker compose down -v`。

```bash
cd PekaFlowAI
bash deploy/pekaflow.sh upgrade
```

代码已经拉过时加 `--skip-git`。脚本会快进 `master`，再 `docker compose up -d --build`。宿主机不需要 Python 或 JDK。

### 步骤 1 · 建议先备份

至少留这三样：MySQL、`backend-data` 卷（密钥 / 制品 / 插件）、当时的 JWT 与 AES。做法见 [部署文档 · 备份](deploy-platform/docs/部署文档.md)。

### 步骤 2 · 拉新代码

在当初 clone 的目录里：

```bash
cd PekaFlowAI
git pull
cd deploy-platform
```

### 步骤 3 · 若改过 Java Agent 源码

改过 `deploy-platform/backend/agent-java/src` 时，必须先重编 jar，再构建后端镜像。否则平台继续下发旧包，和源码对不上。

```bash
cd backend/agent-java
# Windows（需要 JDK 8 的 javac）
build.bat
# Linux / macOS
./build.sh
cd ../..
```

产出 `deploy-agent.jar` 和更新后的 `.srcsha`。

### 步骤 4 · 重建并启动

仍在 `deploy-platform/` 下：

```bash
docker compose up -d --build
```

不要加 `-v`。镜像更新后，库、密钥、制品都还在。后端启动会跑库表补丁，一般不必手工迁库。

### 步骤 5 · 确认升级成功

1. `docker compose ps`：backend、frontend 为 `healthy` 或 `running`
2. 打开健康检查：http://localhost:8080/api/v1/health
3. 浏览器能登录，流水线和历史还在

### 步骤 6 · 更新构建机和节点上的 Agent

| 角色 | 怎么升级 jar |
|------|----------------|
| 构建机 | 空闲时自己向平台拉新包，一般不用上手 |
| 部署节点 | 在「节点管理」里点升级，等当前发布做完再点 |

不要在机器上另起一套进程「手工换 jar」。

---

## 本机开发（不用 Compose）

需要 Python 3.10+、Node.js 18+。Windows 用 `py`，不要用 `python3`。适合改代码，不是生产装法。

```bash
git clone https://github.com/Ease1994/PekaFlowAI.git
cd PekaFlowAI

# 后端
cd deploy-platform/backend
py -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8080

# 另开终端：前端
cd deploy-platform/frontend
npm install
npm run dev -- --host 0.0.0.0
```

浏览器打开 http://localhost:5173 。演示账号 `admin` / `admin123`（只适合本机）。

## 文档

| 文档 | 内容 |
|------|------|
| [功能说明书](deploy-platform/docs/功能说明书.md) | 每个模块做什么、亮点；[Word 版](deploy-platform/docs/发布部署平台-功能说明书.docx) |
| [部署文档](deploy-platform/docs/部署文档.md) · [English](deploy-platform/docs/部署文档.en.md) | 从零安装、Compose、Agent、HTTPS、备份升级 |
| [开发者手册](docs/开发者手册.md) | 架构对照、模块表、数据库、踩坑 |
| [贡献指南](CONTRIBUTING.md) · [English](CONTRIBUTING.en.md) | 怎么跑测试、提交约定 |
| [安全披露](SECURITY.md) · [English](SECURITY.en.md) | 漏洞请走 GitHub Advisory，不要开公开 Issue |

## 技术栈

前端 React 18 + TypeScript + Vite + Ant Design 5；后端 FastAPI + SQLAlchemy 2；数据 MySQL 8 / SQLite + Redis 7 + Elasticsearch 8；执行层 Java Agent（JDK 8 单 jar）。

## 许可证

[MIT](LICENSE)
