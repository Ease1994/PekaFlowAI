#!/usr/bin/env bash
# PekaFlowAI 一键部署 / 升级。
#
# 第一次：没有 .env 时复制试用配置，构建并拉起完整栈（MySQL、Redis、ES、后端、前端）。
# 升级：只快进拉取 master，再重建镜像。绝不 docker compose down -v，绝不覆盖已有 .env。
#
# 用法：
#   bash deploy/pekaflow.sh              # 没有 .env 就部署，有就升级
#   bash deploy/pekaflow.sh install      # 只部署，不 git pull
#   bash deploy/pekaflow.sh upgrade      # 拉代码并重建
#   bash deploy/pekaflow.sh status       # 看容器和健康检查
#   bash deploy/pekaflow.sh upgrade --skip-git   # 代码已拉好，只重建镜像
set -euo pipefail

# 本脚本所在目录，用来反推仓库根，不依赖从哪启动。
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# 仓库根目录（deploy/ 的上一级）。
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
# Compose 编排在 deploy-platform/，不在仓库根。
COMPOSE_DIR="${REPO_ROOT}/deploy-platform"
# 运行时口令文件。已存在则永远不覆盖。
ENV_FILE="${COMPOSE_DIR}/.env"
# 试用默认值。生产应改完再启动。
ENV_EXAMPLE="${COMPOSE_DIR}/.env.example"
# Agent 源码与已提交的 jar。源码对不上指纹时必须重编。
AGENT_DIR="${COMPOSE_DIR}/backend/agent-java"
# 公开仓只留这一条分支。
GIT_BRANCH="master"
# 健康检查最多等这么多秒。第一次编镜像可能更久，构建阶段本身不占这段时间。
HEALTH_TIMEOUT_SEC=180
# 前端入口（宿主机）。
FRONTEND_URL="http://localhost:8000"
# 后端健康检查。
HEALTH_URL="http://localhost:8080/api/v1/health"

# docker compose 命令拆成数组：有的机器是 docker compose，有的是 docker-compose。
COMPOSE_CMD=()
# 为 1 时升级不跑 git pull（代码已经更新过）。
SKIP_GIT=0


die() {
  # 失败立刻退出，不继续部署，避免半套镜像当成功。
  echo "错误: $*" >&2
  exit 1
}


need_cmd() {
  # 缺命令就停，不静默换另一种实现把范围放大。
  command -v "$1" >/dev/null 2>&1 || die "找不到命令 $1"
}


log() {
  echo "==> $*"
}


parse_args() {
  # 从参数里拆出 --skip-git，剩下的第一个是子命令。
  local arg
  SKIP_GIT=0
  for arg in "$@"; do
    if [[ "${arg}" == "--skip-git" ]]; then
      SKIP_GIT=1
    fi
  done
}


compose() {
  # 一律在编排目录执行，并带上 .env。禁止调用方再拼 down -v。
  (cd "${COMPOSE_DIR}" && "${COMPOSE_CMD[@]}" "$@")
}


detect_compose() {
  # 优先 Compose v2 插件。
  if docker compose version >/dev/null 2>&1; then
    COMPOSE_CMD=(docker compose)
    return
  fi
  if command -v docker-compose >/dev/null 2>&1 && docker-compose version >/dev/null 2>&1; then
    COMPOSE_CMD=(docker-compose)
    return
  fi
  die "需要 Docker Compose v2（docker compose）"
}


python_bin() {
  # 指纹脚本要解释器。优先 python3。
  if command -v python3 >/dev/null 2>&1; then
    echo python3
    return
  fi
  if command -v python >/dev/null 2>&1; then
    echo python
    return
  fi
  return 1
}


assert_layout() {
  # 目录对不上就停，避免在错误位置起一套空栈。
  [[ -f "${COMPOSE_DIR}/docker-compose.yml" ]] || die "找不到 ${COMPOSE_DIR}/docker-compose.yml"
  [[ -f "${ENV_EXAMPLE}" ]] || die "找不到 ${ENV_EXAMPLE}"
}


ensure_docker() {
  need_cmd docker
  docker info >/dev/null 2>&1 || die "Docker 守护进程没起来，先启动 Docker"
  detect_compose
}


ensure_env() {
  # 已有 .env 说明这套环境已经配过口令，覆盖等于换钥匙。
  if [[ -f "${ENV_FILE}" ]]; then
    log "使用已有 ${ENV_FILE}（不会改口令）"
    return
  fi
  cp "${ENV_EXAMPLE}" "${ENV_FILE}"
  log "已复制试用 .env 到 ${ENV_FILE}"
  log "生产请先改口令和密钥再启动；已经在跑的环境不要换 JWT / AES"
}


git_remote_for_branch() {
  # 当前分支跟踪哪个远程。没有跟踪时用 origin，再不行用 github。空远程不猜成全部。
  local remote
  remote="$(git -C "${REPO_ROOT}" rev-parse --abbrev-ref --symbolic-full-name "@{u}" 2>/dev/null || true)"
  if [[ -n "${remote}" ]]; then
    echo "${remote%%/*}"
    return
  fi
  if git -C "${REPO_ROOT}" remote get-url origin >/dev/null 2>&1; then
    echo origin
    return
  fi
  if git -C "${REPO_ROOT}" remote get-url github >/dev/null 2>&1; then
    echo github
    return
  fi
  die "仓库没有 origin / github 远程，无法拉取 ${GIT_BRANCH}"
}


sync_git() {
  # 快进拉取 master。工作区有已跟踪改动就停，避免把机上的手工改动盖掉。
  if [[ "${SKIP_GIT}" -eq 1 ]]; then
    log "跳过 git pull"
    return
  fi
  if [[ ! -d "${REPO_ROOT}/.git" ]]; then
    die "不是 git 仓库，无法升级。用 --skip-git 只重建当前目录里的镜像"
  fi
  need_cmd git
  local dirty
  dirty="$(git -C "${REPO_ROOT}" status --porcelain --untracked-files=no)"
  if [[ -n "${dirty}" ]]; then
    die "工作区有未提交改动，拒绝拉取以免覆盖。提交或 stash 后再升级，或加 --skip-git"
  fi

  local current remote
  current="$(git -C "${REPO_ROOT}" rev-parse --abbrev-ref HEAD)"
  remote="$(git_remote_for_branch)"
  log "从 ${remote} 拉取 ${GIT_BRANCH}（当前在 ${current}）"
  git -C "${REPO_ROOT}" fetch "${remote}" "${GIT_BRANCH}"
  if git -C "${REPO_ROOT}" show-ref --verify --quiet "refs/heads/${GIT_BRANCH}"; then
    git -C "${REPO_ROOT}" checkout "${GIT_BRANCH}"
  else
    git -C "${REPO_ROOT}" checkout -B "${GIT_BRANCH}" "${remote}/${GIT_BRANCH}"
  fi
  git -C "${REPO_ROOT}" pull --ff-only "${remote}" "${GIT_BRANCH}"
  log "代码已更新到 $(git -C "${REPO_ROOT}" log -1 --oneline)"
}


rebuild_agent_if_needed() {
  # jar 是提交进仓库的产物。源码指纹对不上就重编，编不了就停，避免平台继续下发旧包。
  local py current recorded
  py="$(python_bin)" || {
    log "没有 python，跳过 Agent 指纹核对（后端启动仍会警告）"
    return
  }
  current="$("${py}" "${AGENT_DIR}/src_fingerprint.py")"
  recorded="$(tr -d '[:space:]' < "${AGENT_DIR}/deploy-agent.jar.srcsha" 2>/dev/null || true)"
  if [[ -n "${recorded}" && "${current}" == "${recorded}" ]]; then
    log "Agent jar 与源码指纹一致"
    return
  fi
  log "Agent 源码指纹 ${current} 与 jar 记录 ${recorded:-空} 不一致，重编 jar"
  need_cmd javac
  need_cmd jar
  bash "${AGENT_DIR}/build.sh"
}


compose_up() {
  # 只重建镜像并启动。禁止 -v：那会删 MySQL、密钥、制品。
  log "构建并启动 Compose 栈（不删数据卷）"
  compose up -d --build
}


wait_healthy() {
  # 构建结束后再轮询健康检查。超时失败，不当成已经好了。
  local elapsed=0
  log "等待 ${HEALTH_URL}"
  while (( elapsed < HEALTH_TIMEOUT_SEC )); do
    if command -v curl >/dev/null 2>&1; then
      if curl -fsS "${HEALTH_URL}" >/dev/null 2>&1; then
        log "后端健康检查通过"
        return
      fi
    elif command -v wget >/dev/null 2>&1; then
      if wget -q -O /dev/null "${HEALTH_URL}" 2>/dev/null; then
        log "后端健康检查通过"
        return
      fi
    else
      log "没有 curl/wget，请自行打开 ${HEALTH_URL}"
      return
    fi
    sleep 5
    elapsed=$((elapsed + 5))
  done
  compose ps || true
  die "等待健康检查超时。看日志：cd ${COMPOSE_DIR} && ${COMPOSE_CMD[*]} logs --tail 80 backend"
}


print_urls() {
  echo
  echo "前端  ${FRONTEND_URL}"
  echo "API   http://localhost:8080"
  echo "健康  ${HEALTH_URL}"
  echo
  echo "试用登录 admin / admin123（设了 BOOTSTRAP_ADMIN_PASSWORD 则用那个口令）"
  echo "登录后改管理员密码、填站点根地址、配模型。构建机和节点见部署文档。"
}


cmd_install() {
  # 首次部署：准备 .env，不拉 git，避免把正在改的目录突然快进。
  assert_layout
  ensure_docker
  ensure_env
  rebuild_agent_if_needed
  compose_up
  wait_healthy
  print_urls
}


cmd_upgrade() {
  # 升级：快进代码 → 必要时重编 jar → 重建镜像。数据卷留下。
  assert_layout
  ensure_docker
  [[ -f "${ENV_FILE}" ]] || die "没有 ${ENV_FILE}，这是首次部署，请先执行：bash deploy/pekaflow.sh install"
  sync_git
  rebuild_agent_if_needed
  compose_up
  wait_healthy
  log "升级完成。浏览器强制刷新（Ctrl+F5）。构建机空闲会自己拉新 jar，节点要在页面上点升级。"
  print_urls
}


cmd_status() {
  assert_layout
  ensure_docker
  compose ps
  echo
  if command -v curl >/dev/null 2>&1; then
    curl -fsS "${HEALTH_URL}" || echo "健康检查未通过"
    echo
  fi
}


cmd_help() {
  sed -n '2,15p' "${BASH_SOURCE[0]}"
}


main() {
  parse_args "$@"
  local cmd="auto"
  local arg
  for arg in "$@"; do
    case "${arg}" in
      install|upgrade|status|help|-h|--help)
        cmd="${arg}"
        ;;
      --skip-git)
        ;;
      *)
        die "未知参数 ${arg}。bash deploy/pekaflow.sh help"
        ;;
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
