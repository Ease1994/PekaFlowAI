# PekaFlowAI · 技术概览

> 架构、模块、数据库、启动方式与踩坑见仓库根目录 [`../docs/开发者手册.md`](../docs/开发者手册.md)。本文件只列本目录怎么放、技术栈和快速启动。
>
> 功能按模块说明（每个模块：做什么 / 功能 / 亮点；文末总结 AI）—— Word [`docs/发布部署平台-功能说明书.docx`](docs/发布部署平台-功能说明书.docx)，Markdown [`docs/功能说明书.md`](docs/功能说明书.md)。

PekaFlowAI。**产品形态是 AI Agent**；运行时按 **DeepSeek AI-Harness**（session 事件日志、agent-loop、tools 守卫管线、system-prompt 分段、compaction、Skills、MCP→tools.register、sandbox）在发布域落地。编排仍是 Stage→Job→Step。

## 仓库结构

```
deploy-platform/
├── backend/                 # Python 3 + FastAPI
│   ├── app/modules/         # 20 个业务模块（ai / llm / harness 是助手核心）
│   ├── agent-java/          # 生产版 Agent（JDK 8 单 jar，builder + node）
│   └── tests/
├── frontend/                # React 18 + TS + Vite + Ant Design 5
│   └── src/modules/assistant/   # AI Agent 对话框
├── harness-runner/          # 第三方 Agent Tool 隔离容器
├── docs/                    # 功能说明书（.md + .docx）/ 部署文档 / 插件规范
├── docker-compose.yml       # MySQL + Redis + backend + frontend + harness-runner
└── README.md                # 本文件
```

## 技术栈

- **前端**：React 18 + TypeScript + Vite + Ant Design 5 + React Flow + TanStack Query + Zustand
- **后端**：FastAPI + SQLAlchemy 2 + Pydantic v2；SQLite（本地）/ MySQL 8（生产）
- **AI**：DeepSeek AI-Harness — Session Event Sourcing、Agent Loop、Tools Pipeline、System Prompt sections、Compaction、Skills、MCP、Sandbox Runner
- **数据**：Redis 7（定时触发 ZSET、日志 Stream）+ ES 可选
- **执行**：Java Agent 拉模式；harness-runner 隔离跑第三方工具

后端模块完整表、助手一轮对话怎么跑、四层扩展（内置 Skill / Agent Skill 包 / Agent Tool 包 / 流水线插件）见 [`../docs/开发者手册.md`](../docs/开发者手册.md) §2、§4。

## 核心特性

| 特性 | 说明 |
|------|------|
| AI Agent + AI-Harness | 产品是 Agent；运行时对齐 dsh：session 为真源、loop 可替换、能力都是 seam |
| session / surface | append-only `SessionEvent`；模型历史由日志投影，可 fork/replay |
| agent-loop + tools 管线 | turn/step；`pre-execute → execute → post-execute → result`；高风险 ask 确认 |
| compaction + system-prompt | tool-result-pruner + fit_messages；identity/persona/tool-guidance 分段 |
| Skills / MCP / sandbox | `SKILL.md` inject；MCP 注册进同一 Tool 目录；第三方进 harness-runner |
| 流水线编排 | Stage→Job→Step，插件化 |
| git 秒级拉取 | 浅克隆检出 + bare mirror 缓存 |
| 失败即终止 | 后端级联取消 + Agent 长轮询杀进程 |
| 执行权仅 AI 申请 | 网页直调申请接口会 400；有权之后页面/API 都能执行 |
| 操作审计 | `username` 永远是人；`source` 为 web/ai/api/cron |
| 拉模式 Agent | 构建机与部署节点同一 jar、不同 `--role` |

## 快速开始

```bash
# 后端（Windows 用 py，勿用 python3）
cd backend && py -m venv .venv && .venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8080

# 前端
cd frontend && npm install && npm run dev -- --host 0.0.0.0
```

完整栈（同级 `.env` 必填 `MYSQL_ROOT_PASSWORD`、`REDIS_PASSWORD`、`DATABASE_URL`）。
Linux 节点要自动装 JDK 时，在「节点管理」上传 `jdk-8u271-linux-x64.tar.gz`（写入 `data/shared-packs/jdk-linux/`，重建镜像不会丢）。也可以先放到 `backend/agent-java/jdk-linux/` 再构建，首次启动会拷进数据卷：

```bash
docker compose up -d --build
```

访问：前端 http://localhost:5173（compose 下为 http://localhost:8000）。接口清单在登录后的「调用手册」菜单，不要把 `/docs` 反代到公网。

演示账号：`admin / admin123`。先在「模型管理」配一个可用模型，再到 AI Agent 里发一条测试流水线，核对确认卡片上的流水线 id。

> 架构、数据库、权限/LDAP、踩坑记录见 [`../docs/开发者手册.md`](../docs/开发者手册.md)。
