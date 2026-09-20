#!/usr/bin/env bash
# 发布部署平台 · 后端启动脚本（Git Bash / Linux）
# 用法: bash scripts/start-backend.sh

set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT/backend"

# 1. 创建虚拟环境（如不存在）
if [ ! -d ".venv" ]; then
  echo ">> 创建虚拟环境..."
  py -m venv .venv
fi

# 2. 安装依赖（如未安装）
if [ ! -f ".venv/.deps_installed" ]; then
  echo ">> 安装依赖..."
  .venv/Scripts/python.exe -m pip install -r requirements.txt
  touch .venv/.deps_installed
fi

# 3. 启动（--host 0.0.0.0 监听所有网卡，虚拟机/局域网设备才能访问）
echo ">> 启动后端: http://localhost:8080"
echo ">> 局域网访问: http://<本机IP>:8080"
echo ">> 接口清单在登录后的「调用手册」，不要把 /docs 反代到公网"
.venv/Scripts/python.exe -m uvicorn app.main:app --host 0.0.0.0 --reload --port 8080
