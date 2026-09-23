#!/bin/bash
# 构建机一键安装 / 升级（Linux / macOS）
#
#   curl -fsSL http://平台地址:8080/api/v1/agents/install-script?role=builder-linux -o install-agent.sh
#   ENROLL_TOKEN=<接入凭证> bash install-agent.sh
#
# 可用环境变量：SERVER / NAME / TAGS / WORK_DIR / WORKSPACE / CONCURRENCY
#
#   ENROLL_TOKEN  接入凭证，平台「构建机 → 新增构建机」页面复制。jar 下载和首次注册都用它，
#                 之后凭据落在 ~/.release-agent/enrolled/，升级重跑不用再带。
#   SERVER        平台地址。从平台下载本脚本时已自动填好，一般不用管。
#   WORKSPACE     工作空间目录，拉代码和编译产物都放这儿，很吃磁盘。
#                 默认在安装目录下，系统盘紧张时指到数据盘，如 WORKSPACE=/data/release-workspace
set -e

SERVER="${SERVER:-__RELEASE_SERVER__}"
NAME="${NAME:-$(hostname 2>/dev/null | cut -d. -f1)}"
TAGS="${TAGS:-}"                 # 留空则由 Agent 按当前系统自动打标签
# 生产 / 测试 / UAT / 预发 / 开发，或自定义小写码。决定这台能构建哪种环境的流水线。
# 只在首次接入时生效，之后以页面为准。空值按 prod。非法码直接退出，不改写成 prod。
ENV="${ENV:-prod}"
ENV="$(printf '%s' "$ENV" | tr 'A-Z' 'a-z')"
if ! printf '%s' "$ENV" | grep -Eq '^[a-z][a-z0-9_-]{0,15}$'; then
    echo "ENV 不合法「$ENV」，只允许小写字母开头、最多 16 位的字母数字-_，内置：prod、test、uat、staging、dev" >&2
    exit 1
fi
CONCURRENCY="${CONCURRENCY:-8}"
WORK_DIR="${WORK_DIR:-${HOME}/release-agent}"

# 平台地址由下载脚本时回填到上面的默认值。这里只判断它像不像个地址，
# 不能出现占位符字面量——回填是全文替换，写在这儿会被一起换掉
case "$SERVER" in
    http://*|https://*) ;;
    *)
        if [ -t 0 ]; then
            read -r -p "平台地址（如 http://172.18.10.233:8080）: " SERVER
        else
            echo "缺少平台地址，请传 SERVER=http://平台地址:8080" >&2
            exit 1
        fi
        ;;
esac
SERVER="${SERVER%/}"
case "$SERVER" in
    http://*|https://*) ;;
    *) echo "平台地址不对：$SERVER" >&2; exit 1 ;;
esac

file_sha256() {
    local f="$1"
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$f" | awk '{print $1}'
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$f" | awk '{print $1}'
    else
        openssl dgst -sha256 "$f" | awk '{print $NF}'
    fi
}

verify_agent_jar() {
    local jar="$1"
    local expect got
    expect="$(curl -fsSL -H "X-Enroll-Token: ${ENROLL_TOKEN}" \
        "${SERVER}/api/v1/agents/jar-sha256" | tr -d '[:space:]')"
    if [ -z "$expect" ]; then
        echo "拿不到平台 jar 指纹，拒绝使用刚下的包" >&2
        exit 1
    fi
    got="$(file_sha256 "$jar")"
    if [ "$got" != "$expect" ]; then
        echo "jar 指纹不一致（本地 ${got}，平台 ${expect}），已删除，请重试安装" >&2
        rm -f "$jar"
        exit 1
    fi
}

# 交互式补一个变量；非交互（CI/无 tty）时要求预先用环境变量传入
ask() {
    local var="$1" prompt="$2" hidden="$3" val=""
    [ -n "${!var}" ] && return 0
    if [ ! -t 0 ]; then
        echo "缺少 $var，当前不是交互终端，请用环境变量传入： $var=xxx $0" >&2
        exit 1
    fi
    if [ "$hidden" = "hidden" ]; then
        read -r -s -p "$prompt" val
        echo
    else
        read -r -p "$prompt" val
    fi
    printf -v "$var" '%s' "$val"
}

# 与 Agent 的 Config.credentialFile() 保持一致：判断这台机器是否已登记过
cred_file() {
    local key
    key=$(printf '%s|%s' "$SERVER" "$NAME" | sed 's/[^A-Za-z0-9._-]/_/g')
    printf '%s/.release-agent/enrolled/%s.token' "$HOME" "$key"
}

echo "==> 构建机 ${NAME} → ${SERVER}"
command -v java >/dev/null 2>&1 || { echo "未找到 java，请先安装 JDK 8 或以上"; exit 1; }
command -v curl >/dev/null 2>&1 || { echo "未找到 curl"; exit 1; }

mkdir -p "$WORK_DIR"
cd "$WORK_DIR"

echo "[1/4] 停掉旧 agent"
pkill -f 'java.*deploy-agent' 2>/dev/null || true
sleep 2

echo "[2/4] 下载 jar"
# 接入凭证：下载 jar 和首次注册都用它。已登记过的机器本地有凭据，
# 但下载 jar 仍需要凭证，所以这里统一问一次
if [ -f "$(cred_file)" ]; then
    ask ENROLL_TOKEN "接入凭证，平台「构建机」页面复制（本机已登记过，凭证仅用于下载 jar）: "
else
    ask ENROLL_TOKEN "接入凭证，平台「构建机」页面复制（首次安装必填）: "
fi
curl -fsSL -H "X-Enroll-Token: ${ENROLL_TOKEN}" -o deploy-agent.jar \
    "${SERVER}/api/v1/agents/download" \
    || { echo "下载 jar 失败：请确认平台地址可达、接入凭证正确（凭证轮换后要用新的）"; exit 1; }
verify_agent_jar deploy-agent.jar
echo "      $(ls -lh deploy-agent.jar | awk '{print $5}')  deploy-agent.jar"
# 清掉上一轮自升级的残留：留着 .new 的话，systemd 下次启动会把它换上去，
# 刚下的这份新 jar 反而被顶掉——重装完版本却退回去了，没人想得通
rm -f deploy-agent.jar.new deploy-agent.jar.bak deploy-agent.upgrade-attempt

echo "[3/4] 启动"
# --home 显式指定：systemd 拉起时的用户可能和现在不同，user.home 一变登记凭据就找不着了
AGENT_HOME="${AGENT_HOME:-${WORK_DIR}/home}"
mkdir -p "$AGENT_HOME"
# --role builder 显式写出来，和 Windows 节点保持一致，便于按角色匹配进程
ARGS=(-jar deploy-agent.jar --server "$SERVER" --name "$NAME" --role builder \
      --env "$ENV" --home "$AGENT_HOME" --concurrency "$CONCURRENCY")
[ -n "$TAGS" ] && ARGS+=(--tags "$TAGS")
# 工作空间放代码和编译产物，是磁盘占用的大头，装在系统盘紧张的机器上迟早撑爆
if [ -n "$WORKSPACE" ]; then
    mkdir -p "$WORKSPACE"
    WORKSPACE="$(cd "$WORKSPACE" && pwd)"
    ARGS+=(--workspace "$WORKSPACE")
else
    WORKSPACE="${WORK_DIR}/workspace"
fi
echo "      工作空间 ${WORKSPACE}（$(df -h "$(dirname "$WORKSPACE")" 2>/dev/null | awk 'NR==2 {print $4}') 可用）"
if [ -f "$(cred_file)" ]; then
    echo "      本机已登记过，Agent 会用本地凭据续期"
fi
# 凭证走环境变量传给 Agent，不出现在进程命令行里
export RELEASE_ENROLL_TOKEN="$ENROLL_TOKEN"

# 有 systemd 就交给 systemd 管：开机自启、崩溃自动重启、升级后自动拉起，
# 都是它现成的能力，比自己写守护脚本靠谱
USE_SYSTEMD=0
if [ "$(id -u)" = "0" ] && command -v systemctl >/dev/null 2>&1 && [ -d /etc/systemd/system ]; then
    USE_SYSTEMD=1
fi

# 先用接入凭证跑一次，把登记凭据落到 --home 下；之后 systemd 拉起就不需要凭证了，
# 凭证也就不必写进 unit 文件长期留在磁盘上
pkill -f 'java.*deploy-agent' >/dev/null 2>&1 || true
sleep 1
nohup java "${ARGS[@]}" > agent.log 2>&1 &
sleep 6

if [ "$USE_SYSTEMD" = "1" ]; then
    # systemd 现成就有开机自启、崩溃重启、升级后自动拉起，不必自己写守护
    quoted=""
    for a in "${ARGS[@]}"; do
        quoted="$quoted \"$a\""
    done
    cat > /etc/systemd/system/release-agent.service <<UNITEOF
[Unit]
Description=RELEASE Build Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
# 服务进程默认 PATH 不含 /opt/maven、/snap/bin。交互登录能用的 mvn/docker，systemd 里找不到。
Environment=PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/opt/maven/bin:/usr/share/maven/bin:/opt/gradle/bin:/snap/bin
# 卸载标记在：停掉自己并让本次启动失败。disable 成功后 unit 不再自启。
# 换上 .new 前先留 .bak：新 jar 起不来时还能退回，避免构建机被坏包卡死离线
ExecStartPre=/bin/sh -c 'if [ -f ${WORK_DIR}/uninstall.requested ]; then systemctl disable --now release-agent; false; fi; if [ -f ${WORK_DIR}/deploy-agent.jar.new ]; then if [ -f ${WORK_DIR}/deploy-agent.jar ]; then cp -f ${WORK_DIR}/deploy-agent.jar ${WORK_DIR}/deploy-agent.jar.bak; fi; mv -f ${WORK_DIR}/deploy-agent.jar.new ${WORK_DIR}/deploy-agent.jar; fi'
ExecStart=$(command -v java)${quoted}
Restart=always
RestartSec=10
# 升级时 Agent 要等手上的任务跑完才退出，别急着 SIGKILL
TimeoutStopSec=1800

[Install]
WantedBy=multi-user.target
UNITEOF
    systemctl daemon-reload
    systemctl enable release-agent >/dev/null 2>&1 || true
    pkill -f 'java.*deploy-agent' >/dev/null 2>&1 || true
    sleep 2
    systemctl restart release-agent
    sleep 3
fi

echo "[4/4] 检查"
if [ "$USE_SYSTEMD" = "1" ]; then
    if systemctl is-active --quiet release-agent; then
        echo "启动成功，已注册为 systemd 服务 release-agent（开机自启、崩溃自动重启）"
        echo "  看日志：journalctl -u release-agent -f"
        echo "  新版本 jar 会在空闲时自动升级，不用再上这台机器"
    else
        echo "启动失败，日志末尾："
        journalctl -u release-agent -n 30 --no-pager 2>/dev/null || true
        exit 1
    fi
elif pgrep -f 'java.*deploy-agent' >/dev/null 2>&1; then
    echo "启动成功，日志：${WORK_DIR}/agent.log"
    echo "注意：当前不是 root 或没有 systemd，Agent 用 nohup 跑，重启机器后不会自动拉起。"
    echo "  想要开机自启和崩溃自愈，请用 root 重跑本脚本。"
    head -n 5 agent.log
else
    echo "启动失败，日志末尾："
    tail -n 30 agent.log
    exit 1
fi
