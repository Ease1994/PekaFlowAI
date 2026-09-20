#!/usr/bin/env bash
# 发布部署平台 · 前端启动脚本（Git Bash / Linux）
# 用法: bash scripts/start-frontend.sh

set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT/frontend"

if [ ! -d "node_modules" ]; then
  echo ">> 安装前端依赖..."
  npm install
fi

echo ">> 启动前端: http://localhost:5173"
echo ">> 局域网访问: http://<本机IP>:5173"
# --host 0.0.0.0 监听所有网卡（vite.config.ts 的 host:true 偶发不生效，命令行参数最稳）
npm run dev -- --host 0.0.0.0
