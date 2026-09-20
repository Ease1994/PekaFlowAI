#!/usr/bin/env bash
# PekaFlowAI 一键部署 / 升级。宿主机只需要 Git 和 docker-compose，不依赖 Python / JDK。
#
# 第一次：没有 .env 就复制试用配置，构建并拉起完整栈。
# 升级：快进拉取 master，重建镜像。绝不 docker compose down -v，绝不覆盖已有 .env。
# Agent jar 随仓库和后端镜像走，本脚本不在宿主机编译。
#
# 用法：
#   bash deploy/pekaflow.sh              # 没有 .env 就部署，有就升级
#   bash deploy/pekaflow.sh install      # 只部署，不 git pull
#   bash deploy/pekaflow.sh upgrade      # 拉代码并重建
#   bash deploy/pekaflow.sh status       # 看容器状态
#   bash deploy/pekaflow.sh upgrade --skip-git
set -euo pipefail

# 本脚本所在目录，用来反推仓库根。
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# 仓库根目录（deploy/ 的上一级）。
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
# Compose 编排在 deploy-platform/，不在仓库根。
COMPOSE_DIR="${REPO_ROOT}/deploy-platform"
# 运行时口令文件。已存在则永远不覆盖。
ENV_FILE="${COMPOSE_DIR}/.env"
# 试用默认值。生产应改完再启动。
ENV_EXAMPLE="${COMPOSE_DIR}/.env.example"
# 公开仓只留这一条分支。
GIT_BRANCH="master"
# 后端容器名，与 docker-compose.yml 一致。
BACKEND_CONTAINER="deploy-backend"
# 健康检查最多等这么多秒。镜像构建本身不占这段时间。
HEALTH_TIMEOUT_SEC=180

# docker compose 命令。有的机器是插件，有的是独立二进制。
COMPOSE_CMD=()
# 为 1 时升级不跑 git pull。
SKIP_GIT=0


die() {
  echo "错误: $*" >&2
  exit 1
}


log() {
  echo "==> $*"
}


need_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "找不到命令 $1"
}


parse_args() {
  local arg
  SKIP_GIT=0
  for arg in "$@"; do
    if [[ "${arg}" == "--skip-git" ]]; then
      SKIP_GIT=1
    fi
  done
}


compose() {
  # 一律在编排目录执行。禁止调用方再拼 down -v。
  (cd "${COMPOSE_DIR}" && "${COMPOSE_CMD[@]}" "$@")
}


detect_compose() {
  # 不少装机是独立二进制 docker-compose（如 Anolis），docker compose 子命令会报错。
  if command -v docker-compose >/dev/null 2>&1 && docker-compose version >/dev/null 2>&1; then
    COMPOSE_CMD=(docker-compose)
    return
  fi
  if docker compose version >/dev/null 2>&1; then
    COMPOSE_CMD=(docker compose)
    return
  fi
  die "需要 docker-compose（Compose v2）"
}


assert_layout() {
  [[ -f "${COMPOSE_DIR}/docker-compose.yml" ]] || die "找不到 ${COMPOSE_DIR}/docker-compose.yml"
  [[ -f "${ENV_EXAMPLE}" ]] || die "找不到 ${ENV_EXAMPLE}"
}


ensure_docker() {
  need_cmd docker
  docker info >/dev/null 2>&1 || die "Docker 守护进程没起来，先启动 Docker"
  detect_compose
  log "使用 ${COMPOSE_CMD[*]}"
}


ensure_env() {
  if [[ -f "${ENV_FILE}" ]]; then
    log "使用已有 ${ENV_FILE}（不会改口令）"
    return
  fi
  cp "${ENV_EXAMPLE}" "${ENV_FILE}"
  log "已复制试用 .env。生产请先改口令和密钥再启动；已经在跑的环境不要换 JWT / AES"
}


git_remote() {
  # 优先 origin；只有一个远程时用它。多个远程且没有 origin 就停，不猜。
  if git -C "${REPO_ROOT}" remote get-url origin >/dev/null 2>&1; then
    echo origin
    return
  fi
  local names count
  names="$(git -C "${REPO_ROOT}" remote)"
  count="$(printf '%s\n' "${names}" | grep -c . || true)"
  [[ "${count}" -eq 1 ]] || die "请先设置 origin：git remote add origin <仓库地址>"
  echo "${names}"
}


sync_git() {
  if [[ "${SKIP_GIT}" -eq 1 ]]; then
    log "跳过 git pull"
    return
  fi
  [[ -d "${REPO_ROOT}/.git" ]] || die "不是 git 仓库。代码已更新时加 --skip-git"
  need_cmd git
  local dirty
  dirty="$(git -C "${REPO_ROOT}" status --porcelain --untracked-files=no)"
  [[ -z "${dirty}" ]] || die "工作区有未提交改动，拒绝拉取。处理完再升级，或加 --skip-git"

  local remote
  remote="$(git_remote)"
  log "从 ${remote} 拉取 ${GIT_BRANCH}"
  git -C "${REPO_ROOT}" fetch "${remote}" "${GIT_BRANCH}"
  if git -C "${REPO_ROOT}" show-ref --verify --quiet "refs/heads/${GIT_BRANCH}"; then
    git -C "${REPO_ROOT}" checkout "${GIT_BRANCH}"
  else
    git -C "${REPO_ROOT}" checkout -B "${GIT_BRANCH}" "${remote}/${GIT_BRANCH}"
  fi
  git -C "${REPO_ROOT}" pull --ff-only "${remote}" "${GIT_BRANCH}"
  log "代码已更新到 $(git -C "${REPO_ROOT}" log -1 --oneline)"
}


compose_up() {
  log "构建并启动（不删数据卷）"
  compose up -d --build
}


backend_health() {
  # 只问 Docker，宿主机不必装 curl / python。
  docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' \
    "${BACKEND_CONTAINER}" 2>/dev/null || echo missing
}


wait_healthy() {
  local elapsed=0 status
  log "等待 ${BACKEND_CONTAINER} 就绪"
  while (( elapsed < HEALTH_TIMEOUT_SEC )); do
    status="$(backend_health)"
    if [[ "${status}" == "healthy" ]]; then
      log "backend 已就绪"
      return
    fi
    sleep 5
    elapsed=$((elapsed + 5))
  done
  compose ps || true
  die "等待超时。看日志：cd ${COMPOSE_DIR} && ${COMPOSE_CMD[*]} logs --tail 80 backend"
}


print_urls() {
  echo
  echo "前端  http://localhost:8000"
  echo "API   http://localhost:8080"
  echo "健康  http://localhost:8080/api/v1/health"
  echo
  echo "试用登录 admin / admin123（设了 BOOTSTRAP_ADMIN_PASSWORD 则用那个口令）"
  echo "登录后改管理员密码、填站点根地址、配模型。"
}


cmd_install() {
  assert_layout
  ensure_docker
  ensure_env
  compose_up
  wait_healthy
  print_urls
}


cmd_upgrade() {
  assert_layout
  ensure_docker
  [[ -f "${ENV_FILE}" ]] || die "没有 ${ENV_FILE}，请先：bash deploy/pekaflow.sh install"
  sync_git
  compose_up
  wait_healthy
  log "升级完成。浏览器强制刷新（Ctrl+F5）。"
  print_urls
}


cmd_status() {
  assert_layout
  ensure_docker
  compose ps
  echo "backend: $(backend_health)"
}


cmd_help() {
  sed -n '2,16p' "${BASH_SOURCE[0]}"
}


main() {
  parse_args "$@"
  local cmd="auto" arg
  for arg in "$@"; do
    case "${arg}" in
      install|upgrade|status|help|-h|--help) cmd="${arg}" ;;
      --skip-git) ;;
      *) die "未知参数 ${arg}。bash deploy/pekaflow.sh help" ;;
    esac
  done
  case "${cmd}" in
    auto)
      assert_layout
      if [[ -f "${ENV_FILE}" ]]; then
        cmd_upgrade
      else
        cmd_install
      fi
      ;;
    install) cmd_install ;;
    upgrade) cmd_upgrade ;;
    status) cmd_status ;;
    help|-h|--help) cmd_help ;;
  esac
}


main "$@"
