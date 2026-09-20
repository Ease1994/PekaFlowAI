#!/bin/bash
# 构建机一次性安装/更新 Agent（不清 git 缓存；坏缓存由新 Agent 自动重建/降级）
set -e

SERVER="${1:-http://127.0.0.1:8080}"
AGENT_NAME="${2:-linux-test}"
WORK_DIR="${HOME}/release-agent"

mkdir -p "$WORK_DIR"
cd "$WORK_DIR"

echo "==> 停止旧 Agent"
pkill -f 'java.*deploy-agent' 2>/dev/null || true
sleep 2

echo "==> 下载最新 deploy-agent.jar  from $SERVER"
curl -fsSL -o deploy-agent.jar "$SERVER/api/v1/agents/download"
ls -lh deploy-agent.jar

echo "==> 启动 Agent"
nohup java -jar deploy-agent.jar --server "$SERVER" --name "$AGENT_NAME" > agent.log 2>&1 &
sleep 2

ps -ef | grep '[d]eploy-agent' || { echo "启动失败:"; tail -n 50 agent.log; exit 1; }
echo "OK。日志: $WORK_DIR/agent.log"
echo "用法: $0 <平台地址> [agent名称]"
echo "示例: $0 http://172.18.x.x:8080 linux-test"
