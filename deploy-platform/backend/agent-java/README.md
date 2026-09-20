# 构建机 Agent（Java 版）

纯 JDK 8 实现的构建机 Agent，单 jar 无第三方依赖，跨平台（Linux / Windows / macOS）。

## 特性

- **自注册**：启动即注册到平台，返回 agent_id
- **心跳**：每 10s 上报，平台据此判断在线/离线
- **标签调度**：按 `--tags` 标签匹配任务（后端按 Job 的 agent 标签分发）
- **命令执行**：shell-exec / bat-exec 用 ProcessBuilder 真执行，其余插件输出参数日志
- **日志上报**：逐条上报，后端写入 ES / MySQL

## 编译打包

```bash
# Windows
build.bat

# Linux / macOS
./build.sh
```

产出 `deploy-agent.jar`。

## Linux 节点自动安装 JDK

Linux 节点安装脚本在目标机没有可用 Java 8+ 时，会用接入凭证从平台下载 JDK 8，
校验 `java -version` 通过后才继续装 Agent。

把 `jdk-8u271-linux-x64.tar.gz` 在平台「节点管理」上传一次（写入数据卷
`data/shared-packs/jdk-linux/`，重建镜像不会丢）。本地开发也可以放到 `jdk-linux/`，
首次查找会拷进数据卷。`GET /api/v1/agents/jdk-linux` 提供下载。没有这份文件时
平台仍能启动，只是装节点时下载会 404。Windows 节点仍需本机 JRE 8+。

## 接入凭证

构建任务里带着仓库凭证，所以注册不是匿名的：**新构建机首次注册必须带接入凭证**，
在平台「构建机 → 新增构建机」页面复制（生成的启动命令里已经带上）。

注册成功后凭据存在 `~/.release-agent/enrolled/`，之后重启、升级 jar 都不用再带凭证。
凭证外泄时在页面上点「轮换接入凭证」，已登记的构建机不受影响。

jar 包下载（`GET /api/v1/agents/download`）需要管理员身份，脚本里用
`curl` 直取会 401，得先登录换 token，见下方脚本示例。

## 运行

```bash
# 首次接入：带上接入凭证
java -jar deploy-agent.jar --server http://localhost:8080 --name linux-build-01 \
  --tags linux,maven,docker --enroll-token <平台复制的凭证>

# 之后重启/升级：不用再带凭证
java -jar deploy-agent.jar --server http://localhost:8080 --name linux-build-01 --tags linux,maven,docker

# 凭证也可以走环境变量，避免写进命令历史
export RELEASE_ENROLL_TOKEN=<凭证>
java -jar deploy-agent.jar --server http://localhost:8080 --name my-pc --tags windows,dotnet

# 指定轮询间隔（秒）+ 工作空间根目录
java -jar deploy-agent.jar --server http://localhost:8080 --name agent-01 --poll 1 --workspace /data/workspace
```

## 参数

| 参数 | 说明 | 默认 |
|------|------|------|
| `--server` | 平台地址 | http://localhost:8080 |
| `--name` | Agent 名称（唯一） | agent-随机 |
| `--tags` | 标签（逗号分隔） | 当前系统名 |
| `--poll` | 拉任务间隔（秒） | 2 |
| `--concurrency` | 同时执行的任务数（1~32） | 8 |
| `--workspace` | 工作空间根目录 | ./workspace |
| `--enroll-token` | 接入凭证，首次注册必填（或用 `RELEASE_ENROLL_TOKEN`） | 空 |

## 部署脚本示例

```bash
#!/bin/bash
set -e
SERVER="http://platform.example.com:8080"
NAME="linux-test"
ENROLL_TOKEN="${RELEASE_ENROLL_TOKEN:?请先在平台构建机页面复制接入凭证}"
ADMIN_USER="${RELEASE_ADMIN_USER:-admin}"
ADMIN_PASS="${RELEASE_ADMIN_PASS:?请设置管理员密码，用于下载 jar}"

WORK_DIR="${HOME}/release-agent"
mkdir -p "$WORK_DIR" && cd "$WORK_DIR"

pkill -f 'java.*deploy-agent' 2>/dev/null || true
sleep 2

# jar 下载要管理员身份，先登录换 token
TOKEN=$(curl -fsS -X POST "$SERVER/api/v1/auth/login" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$ADMIN_USER\",\"password\":\"$ADMIN_PASS\"}" \
  | sed -n 's/.*"token":"\([^"]*\)".*/\1/p')
[ -n "$TOKEN" ] || { echo "登录失败"; exit 1; }
curl -fsSL -H "Authorization: Bearer $TOKEN" -o deploy-agent.jar "$SERVER/api/v1/agents/download"

nohup java -jar deploy-agent.jar --server "$SERVER" --name "$NAME" \
  --enroll-token "$ENROLL_TOKEN" > agent.log 2>&1 &
sleep 3
ps -ef | grep '[d]eploy-agent' || { tail -n 50 agent.log; exit 1; }
```

## Workspace 目录结构（按流水线隔离）

```
workspace/                        ← 工作空间根目录（--workspace 指定）
└── p-{pipeline_id}/              ← 每条流水线一个目录
    └── src/                      ← ★ 源码目录（git-checkout 拉到这里）
        ├── .git/                 ← git 仓库（clone/pull）
        ├── package.json / pom.xml / ...
        └── target/ dist/         ← 构建产物
```

- **git-checkout**：把代码 clone（首次）或 pull（已存在）到 `p-{pipeline_id}/src/`
- **其它命令**（shell/maven 等）：默认在 `p-{pipeline_id}/src/` 目录下执行
- 同一条流水线多次构建复用同一目录，依赖和产物不重复拉取

## 工作流程

```
启动 → 注册 → 心跳循环 ─┐
                      │
      ┌───────────────▼──────────────┐
      │ 拉取任务 GET /agents/{id}/tasks │ ← 轮询
      └───────────────┬──────────────┘
                      │ 有任务？
              ┌───────┴───────┐
              无              有
              │               │
            sleep          执行 Job 步骤
                           （逐步上报日志）
                              │
                          上报完成/失败
```

## 与后端接口

| Agent 动作 | 后端接口 |
|-----------|---------|
| 注册 | `POST /api/v1/agents/register`（首次需 `X-Enroll-Token`，之后凭 `X-Agent-Token` 续期） |
| 心跳 | `POST /api/v1/agents/{id}/heartbeat` |
| 拉任务 | `GET /api/v1/agents/{id}/tasks` |
| 上报日志 | `POST /api/v1/agents/{id}/tasks/{task_id}/log` |
| 完成 | `POST /api/v1/agents/{id}/tasks/{task_id}/complete` |
