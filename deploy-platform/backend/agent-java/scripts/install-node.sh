#!/bin/bash
# 部署节点一键安装 / 升级（Linux，需要 systemd）
#
# 在节点服务器上以 root 运行：
#
#   curl -fsSL http://平台地址:8080/api/v1/agents/install-script?role=node-linux -o install-node.sh
#   ALLOW_PATHS=/var/www/o2o ALLOW_SERVICES=systemd:nginx ENROLL_TOKEN=<接入凭证> bash install-node.sh
#
# 没有可用 JDK 时，脚本会用接入凭证从平台拉 JDK 8、解压、校验 java -version，
# 通过后才继续装 Agent；校验失败当场退出，不会带着坏的 Java 把服务装上去。
#
# 节点跑在生产服务器上，只执行「发送文件到节点」「还原备份」「服务启停」三类步骤：
# 不编译、不拉代码、不下载插件、不执行任意脚本。编译打包请装构建机（install-agent.sh）。
#
# Agent 以普通用户运行，不是 root。要停的服务通过 sudoers 白名单精确放行，
# 白名单由本脚本按 ALLOW_SERVICES 生成——这条线连 Agent 启动参数被人改了也绕不开。
#
#   ALLOW_PATHS     必填。允许写入的目录，逗号分隔。这是这台机器的安全边界，
#                   任何落在它之外的写操作节点都会拒绝。只填站点根目录，别填 /。
#   ALLOW_SERVICES  允许控制的服务，逗号分隔，形如 systemd:nginx,docker:web。
#                   省略类型按 systemd 解释。只传文件不停服务可以留空。
#   ENV             prod（默认）/ test / uat / staging / dev，或自定义小写码。
#                   决定往这台机器下发文件要不要审批（test/dev 免审，其余要审）。
#                   只在平台首次见到这台机器时生效，之后以页面上的设置为准——
#                   免得重装一次就把人工标好的环境冲掉。非法码直接退出，不改写成 prod。
#   ENROLL_TOKEN    接入凭证，平台「节点管理 → 新增节点」页面复制。
#   RUN_USER        Agent 运行账号，默认 release（不存在会自动建，无登录权限）。
#                   想复用已有的部署账号就指过去，如 RUN_USER=www-data。
#   INSTALL_DIR     你执行安装命令时所在的目录。页面生成的命令会带 INSTALL_DIR=$(pwd)。
#                   备份根取其第一层：/data/soft/release → /data/release-backup。
#   BACKUP_ROOT     可选。不填则按 INSTALL_DIR 第一层自动建 release-backup，并 chown 给运行账号。
#   SERVER          平台地址。从平台下载本脚本时已自动填好，一般不用管。
set -e

SERVER="${SERVER:-__RELEASE_SERVER__}"
NAME="${NAME:-$(hostname 2>/dev/null | cut -d. -f1)}"
# 环境码。默认 prod：它决定往这台机器下发文件要不要审批。
# 猜错方向得是「多审一道」而不是「悄悄免审」。非法码直接退出，不改写成 prod。
ENV="${ENV:-prod}"
ENV="$(printf '%s' "$ENV" | tr 'A-Z' 'a-z')"
if ! printf '%s' "$ENV" | grep -Eq '^[a-z][a-z0-9_-]{0,15}$'; then
    echo "ENV 不合法「$ENV」，只允许小写字母开头、最多 16 位的字母数字-_，内置：prod、test、uat、staging、dev" >&2
    exit 1
fi
RUN_USER="${RUN_USER:-release}"
WORK_DIR="${WORK_DIR:-/opt/release-node}"
# 备份根：有人显式传 BACKUP_ROOT 就用；否则按 INSTALL_DIR 第一层推，见 resolve_backup_root
SERVICE_NAME="rp-node"
SUDOERS_FILE="/etc/sudoers.d/release-node"
# ensure_java 写成绝对路径；systemd ExecStart 和首次登记都用它，不再从 PATH 猜
JAVA_BIN=""

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

die() { echo "错误：$*" >&2; exit 1; }

# 文件内容的 sha256 hex。安装机不一定有 sha256sum，openssl 几乎都有。
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

# 下载后的 jar 必须和平台当前包指纹一致，防止半截文件或被中间人换成别的包。
verify_agent_jar() {
    local jar="$1"
    local expect got
    expect="$(curl -fsSL -H "X-Enroll-Token: ${ENROLL_TOKEN}" \
        "${SERVER}/api/v1/agents/jar-sha256" | tr -d '[:space:]')"
    [ -n "$expect" ] || die "拿不到平台 jar 指纹，拒绝使用刚下的包"
    got="$(file_sha256 "$jar")"
    [ "$got" = "$expect" ] || {
        rm -f "$jar"
        die "jar 指纹不一致（本地 ${got}，平台 ${expect}），已删除，请重试安装"
    }
}

# 从安装目录取第一层：/data/soft/release → /data/release-backup。
# 系统目录那一层推不出来，返回空，调用方退到 /var/release/backup。
derive_backup_root() {
    local dir="$1"
    dir="${dir%/}"
    case "$dir" in
        ""|"/"|.) return 1 ;;
        /*) ;;
        *) return 1 ;;
    esac
    local rest="${dir#/}"
    local first="${rest%%/*}"
    [ -n "$first" ] || return 1
    case "$first" in
        .|..|etc|bin|sbin|usr|boot|sys|proc|dev|lib|lib64|root|tmp|var) return 1 ;;
    esac
    echo "/${first}/release-backup"
}

# 备份根至少两层：/data/release-backup。/ 或 /data 这种会 chown 到根或整块盘，禁止。
# 成功必须 return 0：函数最后一条如果是失败的 `[ ] && die`，bash 在 set -e
# 下会把整个安装脚本杀掉，现象就是打印完备份路径后静默回到提示符。
assert_backup_root_safe() {
    local bak="${1%/}"
    [ -n "$bak" ] || die "备份根为空"
    if [ "$bak" = "/" ]; then
        die "备份根不能是 /"
    fi
    case "$bak" in
        /*) ;;
        *) die "备份根必须是绝对路径：$bak" ;;
    esac
    local parent="${bak%/*}"
    [ -n "$parent" ] || die "备份根不能是 /"
    if [ "$parent" = "/" ]; then
        die "备份不能直接建在 / 下（$bak）。要用 /data/release-backup 这种，避免写到根目录或 chown 整块盘"
    fi
    return 0
}

# 备份根是否包住某个允许目录（含等于）。包住时 chown -R 会改站点属主。
allow_under_backup() {
    local bak="${1%/}"
    local allow="${2%/}"
    [ -n "$bak" ] && [ -n "$allow" ] || return 1
    [ "$allow" = "$bak" ] && return 0
    case "$allow" in
        "$bak"/*) return 0 ;;
    esac
    return 1
}

# 备份根是否落在某个允许目录里面（含等于）。落在里面会被 Agent 拒掉，安装期先拦住。
backup_under_allow() {
    local bak="${1%/}"
    local allow="${2%/}"
    [ -n "$bak" ] && [ -n "$allow" ] || return 1
    [ "$bak" = "$allow" ] && return 0
    case "$bak" in
        "$allow"/*) return 0 ;;
    esac
    return 1
}

# 定备份根：显式 BACKUP_ROOT 优先；否则按 INSTALL_DIR（页面会传 pwd）第一层。
resolve_backup_root() {
    local derived
    INSTALL_DIR="${INSTALL_DIR:-$PWD}"
    if [ -z "${BACKUP_ROOT}" ]; then
        if derived="$(derive_backup_root "$INSTALL_DIR")"; then
            BACKUP_ROOT="$derived"
            echo "      按安装目录 ${INSTALL_DIR} 把备份放在 ${BACKUP_ROOT}"
        else
            BACKUP_ROOT="/var/release/backup"
            echo "      安装目录 ${INSTALL_DIR} 不在数据盘第一层，备份退到 ${BACKUP_ROOT}（系统盘，空间可能紧张）"
        fi
    fi
    assert_backup_root_safe "$BACKUP_ROOT"
}

# 这个 java 能不能跑 Agent：能执行 -version，且大版本 >= 8。
java_usable() {
    local bin="$1" line ver major
    [ -n "$bin" ] && [ -x "$bin" ] || return 1
    line="$("$bin" -version 2>&1 | sed -n '1p')" || return 1
    ver="$(printf '%s' "$line" | sed -n 's/.*version "\([^"]*\)".*/\1/p')"
    [ -n "$ver" ] || return 1
    case "$ver" in
        1.*) major="${ver#1.}"; major="${major%%.*}" ;;
        *) major="${ver%%.*}" ;;
    esac
    case "$major" in
        ''|*[!0-9]*) return 1 ;;
    esac
    [ "$major" -ge 8 ]
}

# 本机没有可用 JDK 时，从平台拉包装到 WORK_DIR/jdk，校验通过才返回。
# JAVA_BIN 写成绝对路径，systemd 不依赖 PATH 里有没有 java。
ensure_java() {
    local sys_java tarball extracted cand
    JAVA_BIN=""
    sys_java="$(command -v java 2>/dev/null || true)"
    if java_usable "$sys_java"; then
        JAVA_BIN="$sys_java"
        echo "      使用本机 JDK：$("$JAVA_BIN" -version 2>&1 | sed -n '1p')"
        return 0
    fi

    echo "      本机没有可用 JDK，从平台安装 JDK 8"
    command -v tar >/dev/null 2>&1 || die "未找到 tar，无法解压 JDK 包"
    case "$(uname -m)" in
        x86_64|amd64) ;;
        *) die "平台只提供 x86_64 的 JDK 包。这台是 $(uname -m)，请先自行安装 JRE 8+" ;;
    esac
    [ -n "$ENROLL_TOKEN" ] || die "安装 JDK 需要接入凭证，请用页面复制的安装命令（里面带 ENROLL_TOKEN）"

    tarball="${WORK_DIR}/jdk-linux.tar.gz"
    curl -fsSL -H "X-Enroll-Token: ${ENROLL_TOKEN}" -o "$tarball" \
        "${SERVER}/api/v1/agents/jdk-linux" \
        || die "下载 JDK 失败：请到平台「节点管理」上传 Linux JDK 包，并确认接入凭证正确"
    [ -s "$tarball" ] || die "下载的 JDK 包是空的"
    if tar -tzf "$tarball" | grep -Eq '(^|/)\.\.(/|$)|^/'; then
        rm -f "$tarball"
        die "JDK 包含非法路径，已拒绝解压"
    fi

    rm -rf "${WORK_DIR}/jdk"
    mkdir -p "${WORK_DIR}/jdk"
    tar -xzf "$tarball" -C "${WORK_DIR}/jdk" || { rm -f "$tarball"; die "解压 JDK 失败，包可能损坏"; }
    rm -f "$tarball"

    # Oracle / Adoptium 常见布局是 jdk1.8.0_xxx/bin/java；跳过 jre/bin/java，那份不够完整
    JAVA_BIN=""
    while IFS= read -r cand; do
        [ -n "$cand" ] || continue
        case "$cand" in */jre/bin/java) continue ;; esac
        if [ -x "$cand" ] || chmod +x "$cand" 2>/dev/null; then
            JAVA_BIN="$cand"
            break
        fi
    done < <(find "${WORK_DIR}/jdk" -type f -name java 2>/dev/null | sort)
    [ -n "$JAVA_BIN" ] || die "JDK 包解压后找不到 bin/java"
    java_usable "$JAVA_BIN" || die "新装的 JDK 校验失败（java -version 不能用或版本低于 8），已停止安装 Agent"
    # Agent 以普通用户跑，root 解压的目录必须交出去，否则 systemd 起不来
    chown -R "$RUN_USER" "${WORK_DIR}/jdk"
    echo "      已安装并校验：$("$JAVA_BIN" -version 2>&1 | sed -n '1p')"
}

# root 不是可选项：要写 systemd unit 和 sudoers 白名单，都得是 root。
# 装完才发现停不了服务、开机不自启，等于白装
[ "$(id -u)" = "0" ] || die "请以 root 运行：sudo bash $0"
command -v systemctl >/dev/null 2>&1 || die "这台机器没有 systemd，无法安装为服务"
command -v curl >/dev/null 2>&1 || die "未找到 curl"

[ -n "$ALLOW_PATHS" ] || die "必须指定 ALLOW_PATHS，例如 ALLOW_PATHS=/var/www/o2o。这是节点的写入边界，不设等于让生产机任人写盘"

# 名称会进 sudo env / systemd。空格、分号会拆成别的赋值（NAME=web RUN_USER=root），
# 中文是允许的——线上节点本来就有中文名。
printf '%s' "$NAME" | grep -Eq '[[:space:]"$`\;|&<>/]' \
    && die "NAME 不能含空格或 ;|&$<>\\\"'\`/ 这类字符（收到「${NAME}」）"
[ ${#NAME} -eq 0 ] && die "NAME 为空"
[ ${#NAME} -gt 64 ] && die "NAME 太长（最多 64 字）"
[ "$RUN_USER" = "root" ] && die "不能用 root 跑 Agent，请改 RUN_USER"
printf '%s' "$RUN_USER" | grep -Eq '^[A-Za-z_][A-Za-z0-9._-]*$' \
    || die "RUN_USER 含非法字符"

# 交互式补一个变量；非交互（CI/无 tty）时要求预先用环境变量传入
ask() {
    local var="$1" prompt="$2" val=""
    [ -n "${!var}" ] && return 0
    if [ ! -t 0 ]; then
        echo "缺少 $var，当前不是交互终端，请用环境变量传入： $var=xxx $0" >&2
        exit 1
    fi
    read -r -p "$prompt" val
    printf -v "$var" '%s' "$val"
}

echo "==> 部署节点 ${NAME} → ${SERVER}"

# ---------- 运行账号 ----------
# 不用 root 跑 Agent：一旦平台侧或 Agent 自身出问题，root 的爆炸半径是整台机器。
# 普通用户 + allow-paths + sudoers 三道线，任何一道都能兜住
if id "$RUN_USER" >/dev/null 2>&1; then
    echo "[1/8] 运行账号 ${RUN_USER}（已存在）"
else
    echo "[1/8] 建运行账号 ${RUN_USER}"
    useradd --system --no-create-home --shell /usr/sbin/nologin "$RUN_USER" 2>/dev/null \
        || useradd --system --no-create-home --shell /sbin/nologin "$RUN_USER" \
        || die "创建用户 ${RUN_USER} 失败"
fi

# ---------- 目录 ----------
echo "[2/8] 准备目录"
AGENT_HOME="${WORK_DIR}/home"

# 先解析允许目录，再定备份根：备份不能落在允许目录里面。
IFS=',' read -r -a _paths <<< "$ALLOW_PATHS"
ALLOW_PATHS_ARG=""
NOT_WRITABLE=()
for p in "${_paths[@]}"; do
    p="$(echo "$p" | xargs)"   # 去首尾空格
    [ -z "$p" ] && continue
    case "$p" in
        /|/etc|/usr|/bin|/sbin|/boot|/var|/lib*|/tmp|/var/tmp|/dev/shm|/root)
            die "ALLOW_PATHS 不能是系统目录 $p，请指到具体的站点目录" ;;
    esac
    rest="${p#/}"
    rest="${rest%/}"
    case "$rest" in
        */*) ;;
        *) die "ALLOW_PATHS 不能是 $p 这种整块盘，请指到具体站点目录（例如 /data/nginx/html）" ;;
    esac
    case "$p" in
        /tmp/*|/var/tmp/*|/dev/shm/*)
            die "ALLOW_PATHS 不能落在临时目录 $p" ;;
    esac
    ALLOW_PATHS_ARG="${ALLOW_PATHS_ARG}${ALLOW_PATHS_ARG:+;}${p}"
done
[ -n "$ALLOW_PATHS_ARG" ] || die "ALLOW_PATHS 解析后为空"

resolve_backup_root
for p in "${_paths[@]}"; do
    p="$(echo "$p" | xargs)"
    [ -z "$p" ] && continue
    if backup_under_allow "$BACKUP_ROOT" "$p"; then
        die "备份根 ${BACKUP_ROOT} 落在允许目录 ${p} 里面。允许目录请指到站点（如 /data/nginx），不要填整块盘；或显式设 BACKUP_ROOT=站点之外的路径"
    fi
    if allow_under_backup "$BACKUP_ROOT" "$p"; then
        die "备份根 ${BACKUP_ROOT} 包住了允许目录 ${p}。安装时 chown 会改站点属主，请把备份放到与站点并列的目录"
    fi
done

mkdir -p "$WORK_DIR" "$AGENT_HOME" "$BACKUP_ROOT"
chown -R "$RUN_USER" "$WORK_DIR" "$BACKUP_ROOT"
chmod 750 "$WORK_DIR"
chmod 700 "$AGENT_HOME" "$BACKUP_ROOT"

# 站点目录的写权限：Agent 不是 root，写不进去的话发布会在最后一步失败。
# 用 setfacl 加权限而不是 chown——站点属主通常是 nginx/tomcat，改了属主应用可能起不来。
# 覆盖走「写临时文件再替换」，需要的是目录写权限；给每个文件递归加 ACL
# 会在大站上扫盘打满 IO，安装瞬间把业务拖死。
grant_site_dir_acl() {
    local p="$1"
    setfacl -m "u:${RUN_USER}:rwx" "$p" 2>/dev/null || true
    setfacl -d -m "u:${RUN_USER}:rwx" "$p" 2>/dev/null || true
    # 只给已有子目录加 ACL，跳过 node_modules/.git 这类深树，ionice 避免抢站点盘
    if command -v ionice >/dev/null 2>&1; then
        ionice -c 2 -n 7 nice -n 19 find "$p" -xdev -maxdepth 6 \
            \( -name node_modules -o -name .git -o -name logs \) -prune -o \
            -type d -print0 2>/dev/null \
            | xargs -0 -r -n 50 setfacl -m "u:${RUN_USER}:rwx" -d -m "u:${RUN_USER}:rwx" 2>/dev/null || true
    else
        nice -n 19 find "$p" -xdev -maxdepth 6 \
            \( -name node_modules -o -name .git -o -name logs \) -prune -o \
            -type d -print0 2>/dev/null \
            | xargs -0 -r -n 50 setfacl -m "u:${RUN_USER}:rwx" -d -m "u:${RUN_USER}:rwx" 2>/dev/null || true
    fi
}

for p in "${_paths[@]}"; do
    p="$(echo "$p" | xargs)"
    [ -z "$p" ] && continue
    mkdir -p "$p"
    if ! sudo -u "$RUN_USER" test -w "$p" 2>/dev/null; then
        if command -v setfacl >/dev/null 2>&1; then
            grant_site_dir_acl "$p"
        fi
        sudo -u "$RUN_USER" test -w "$p" 2>/dev/null || NOT_WRITABLE+=("$p")
    fi
done
echo "      允许写入：${ALLOW_PATHS_ARG}"
echo "      备份目录：${BACKUP_ROOT}"

# ---------- JDK ----------
# 凭证 jar 和 JDK 共用。先问一次，后面下载 jar 不再问。
echo "[3/8] 准备 JDK"
CRED_KEY="$(printf '%s|%s' "$SERVER" "$NAME" | sed 's/[^A-Za-z0-9._-]/_/g')"
if [ -f "${AGENT_HOME}/enrolled/${CRED_KEY}.token" ]; then
    ask ENROLL_TOKEN "接入凭证，平台「节点管理」页面复制（本机已登记过，凭证仅用于下载）: "
else
    ask ENROLL_TOKEN "接入凭证，平台「节点管理」页面复制（首次安装必填）: "
fi
ensure_java
[ -n "$JAVA_BIN" ] || die "JDK 未就绪，已停止安装 Agent"

# ---------- sudoers 白名单 ----------
# 能停哪些服务写死在这里。Agent 侧的 --allow-services 是第一道，这道是第二道：
# 就算有人改了 systemd unit 里的启动参数，也只能操作这几个服务
echo "[4/8] 配置 sudoers 白名单"
SYSTEMCTL="$(command -v systemctl)"
DOCKER_BIN="$(command -v docker || true)"
ALLOW_SERVICES_ARG=""
if [ -n "$ALLOW_SERVICES" ]; then
    TMP_SUDO="$(mktemp)"
    {
        echo "# RELEASE 部署节点：只放行下面这几条服务控制命令，由安装脚本生成，勿手工编辑"
        echo "# 要增减服务请重跑安装脚本并调整 ALLOW_SERVICES"
    } > "$TMP_SUDO"
    IFS=',' read -r -a _svcs <<< "$ALLOW_SERVICES"
    for s in "${_svcs[@]}"; do
        s="$(echo "$s" | xargs)"
        [ -z "$s" ] && continue
        # 裸写类型名是最容易犯的错：ALLOW_SERVICES=docker 看着像「允许控制 docker」，
        # 但按「省略类型即 systemd」的规则会变成 systemd:docker，也就是放行启停
        # Docker 守护进程本身——机器上所有容器会跟着一起下线。这个后果和用户的本意
        # 差太远，不能静默按字面解释，必须停下来问清楚
        case "$s" in
            docker|systemd)
                die "ALLOW_SERVICES=${s} 写法不明确。要控制容器请写 docker:容器名（如 docker:web）；
      若真的是要放行启停 Docker 守护进程本身，请显式写 systemd:docker——
      但那会让 Agent 有能力停掉这台机器上的所有容器，通常不是你想要的"
                ;;
        esac
        stype="systemd"; sname="$s"
        case "$s" in
            *:*) stype="${s%%:*}"; sname="${s#*:}" ;;
        esac
        # 服务名只允许这些字符：它要拼进 sudoers，带空格或逗号会把规则拆断
        echo "$sname" | grep -Eq '^[A-Za-z0-9][A-Za-z0-9._@-]*$' \
            || die "服务名「${sname}」含非法字符，只允许字母数字和 . _ - @"
        case "$stype" in
            systemd)
                # 补 .service：Agent 发出的就是补齐后的名字，两边不一致 sudo 会拒
                case "$sname" in *.*) unit="$sname" ;; *) unit="${sname}.service" ;; esac
                for verb in start stop restart reload; do
                    echo "${RUN_USER} ALL=(root) NOPASSWD: ${SYSTEMCTL} ${verb} ${unit}" >> "$TMP_SUDO"
                done
                ALLOW_SERVICES_ARG="${ALLOW_SERVICES_ARG}${ALLOW_SERVICES_ARG:+;}systemd:${unit}"
                ;;
            docker)
                [ -n "$DOCKER_BIN" ] || die "ALLOW_SERVICES 里配了 docker:${sname}，但这台机器上找不到 docker"
                # docker 组等价于 root：组内成员可以 docker run -v /:/host --privileged，
                # 挂载宿主根目录直接拿到整台机器。所以这里走 sudoers 精确放行到
                # 「哪个容器的哪个动作」，而不是图省事把运行账号丢进 docker 组
                if id -nG "$RUN_USER" 2>/dev/null | tr ' ' '\n' | grep -qx docker; then
                    echo "      警告：${RUN_USER} 在 docker 组里，这等于给了它 root 权限"
                    echo "            （docker run -v /:/host --privileged 可以拿到整台机器）"
                    echo "            建议移出：gpasswd -d ${RUN_USER} docker，容器控制靠下面的 sudoers 白名单就够"
                fi
                for verb in start stop restart; do
                    echo "${RUN_USER} ALL=(root) NOPASSWD: ${DOCKER_BIN} ${verb} ${sname}" >> "$TMP_SUDO"
                done
                # 查状态也要放行，否则 Agent 连容器在不在跑都问不出来
                echo "${RUN_USER} ALL=(root) NOPASSWD: ${DOCKER_BIN} inspect -f {{.State.Running}} ${sname}" >> "$TMP_SUDO"
                ALLOW_SERVICES_ARG="${ALLOW_SERVICES_ARG}${ALLOW_SERVICES_ARG:+;}docker:${sname}"
                ;;
            *) die "不支持的服务类型「${stype}」，只能是 systemd 或 docker" ;;
        esac
    done
    echo "${RUN_USER} ALL=(root) NOPASSWD: ${SYSTEMCTL} stop ${SERVICE_NAME}" >> "$TMP_SUDO"
    echo "${RUN_USER} ALL=(root) NOPASSWD: ${SYSTEMCTL} disable ${SERVICE_NAME}" >> "$TMP_SUDO"
    echo "${RUN_USER} ALL=(root) NOPASSWD: ${SYSTEMCTL} disable --now ${SERVICE_NAME}" >> "$TMP_SUDO"
    # 先校验再落盘：一个语法错误的 sudoers 会让整台机器的 sudo 都不能用，
    # 那是运维事故，不是安装失败
    visudo -cqf "$TMP_SUDO" || { rm -f "$TMP_SUDO"; die "生成的 sudoers 语法校验不通过，已放弃写入"; }
    install -m 0440 -o root -g root "$TMP_SUDO" "$SUDOERS_FILE"
    rm -f "$TMP_SUDO"
    echo "      允许控制：${ALLOW_SERVICES_ARG}"
else
    TMP_SUDO="$(mktemp)"
    {
        echo "# RELEASE 部署节点：页面卸载时允许停掉本机 Agent，不含其它服务"
        echo "${RUN_USER} ALL=(root) NOPASSWD: ${SYSTEMCTL} stop ${SERVICE_NAME}"
        echo "${RUN_USER} ALL=(root) NOPASSWD: ${SYSTEMCTL} disable ${SERVICE_NAME}"
        echo "${RUN_USER} ALL=(root) NOPASSWD: ${SYSTEMCTL} disable --now ${SERVICE_NAME}"
    } > "$TMP_SUDO"
    visudo -cqf "$TMP_SUDO" || { rm -f "$TMP_SUDO"; die "生成的 sudoers 语法校验不通过，已放弃写入"; }
    install -m 0440 -o root -g root "$TMP_SUDO" "$SUDOERS_FILE"
    rm -f "$TMP_SUDO"
    echo "      未配置 ALLOW_SERVICES，服务启停步骤会被拒绝；已放行本机 Agent 卸载"
fi

# ---------- jar ----------
echo "[5/8] 下载 jar"
curl -fsSL -H "X-Enroll-Token: ${ENROLL_TOKEN}" -o "${WORK_DIR}/deploy-agent.jar" \
    "${SERVER}/api/v1/agents/download" \
    || die "下载 jar 失败：请确认平台地址可达、接入凭证正确（凭证轮换后要用新的）"
verify_agent_jar "${WORK_DIR}/deploy-agent.jar"
# 清掉上次留下的换包文件和卸载标记。重装就是再装一次：
# 留着 .new 会把刚下的 jar 顶掉；留着 uninstall.requested 会让守护一启动又把 Agent 卸掉。
rm -f "${WORK_DIR}/deploy-agent.jar.new" "${WORK_DIR}/deploy-agent.jar.bak" \
      "${WORK_DIR}/deploy-agent.upgrade-attempt" "${WORK_DIR}/uninstall.requested"
chown "$RUN_USER" "${WORK_DIR}/deploy-agent.jar"
echo "      $(ls -lh "${WORK_DIR}/deploy-agent.jar" | awk '{print $5}')  deploy-agent.jar"

# ---------- systemd ----------
echo "[6/8] 注册 systemd 服务"
# JAVA_BIN 已在 ensure_java 里校验过，这里不再从 PATH 里猜。
# 启动器负责：换上 .new 并留 .bak；java 起步 20 秒内退了就回退旧包。
# 不能只在 ExecStartPre 里 mv：新 jar 起不来时旧包已经被盖掉，节点会一直离线。
cat > "${WORK_DIR}/start-node.sh" <<'STARTEOF'
#!/bin/sh
JAVA_BIN="$1"
WD="$2"
shift 2
JAR="$WD/deploy-agent.jar"
NEW="$WD/deploy-agent.jar.new"
BAK="$WD/deploy-agent.jar.bak"

if [ -f "$WD/uninstall.requested" ]; then
    sudo -n systemctl disable --now rp-node >/dev/null 2>&1 || true
    # disable 成功会把本服务停掉。没停掉就挂起，避免 Restart=always 空转把机器打满。
    while true; do sleep 3600; done
fi

if [ -f "$NEW" ]; then
    if [ -f "$JAR" ]; then
        cp -f "$JAR" "$BAK" || true
    fi
    mv -f "$NEW" "$JAR"
fi

"$JAVA_BIN" -Xms64m -Xmx256m -jar "$JAR" "$@" &
pid=$!

term() {
    kill -TERM "$pid" 2>/dev/null || true
    wait "$pid"
    exit $?
}
trap term TERM INT

i=0
while [ "$i" -lt 20 ]; do
    sleep 1
    if ! kill -0 "$pid" 2>/dev/null; then
        wait "$pid" || st=$?
        if [ -f "$BAK" ]; then
            echo "[agent] 新 jar 起步失败，回退到升级前的版本" >&2
            mv -f "$BAK" "$JAR" || true
        fi
        exit "${st:-1}"
    fi
    i=$((i + 1))
done
rm -f "$BAK"
wait "$pid"
exit $?
STARTEOF
chmod 0755 "${WORK_DIR}/start-node.sh"
chown "$RUN_USER" "${WORK_DIR}/start-node.sh"

cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<UNITEOF
[Unit]
Description=RELEASE Deploy Node Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${RUN_USER}
WorkingDirectory=${WORK_DIR}
Environment=RELEASE_AGENT_HOME=${AGENT_HOME}
KillMode=mixed
# ---- 内核层加固 ----
# 这几条是在 Agent 代码之外再加一道：就算 Agent 本身被攻破，能碰的东西也有限。
# /usr /boot /etc 变只读，进程再怎么样也改不了系统二进制和配置。
ProtectSystem=full
# 独立的 /tmp，看不到也污染不了别的进程的临时文件
PrivateTmp=yes
#
# 这里刻意没加 NoNewPrivileges=yes。它是加固清单上的常客，但会连 sudo 一起掐死，
# 而服务启停正是靠 sudo 白名单实现的——加上去的结果是所有停服务的步骤都失败。
# 真正限制「能提权成什么」的是 sudoers 白名单本身：只放行了那几条 systemctl/docker
# 命令，加不加这一条都越不过去。
#
# 也没加 ProtectHome：站点目录完全可能在 /home 下，锁上会让发布写不进去。
# 写盘边界由 --allow-paths 管，那条线更准。
#
# Agent 只做发布，不该和站点抢光 CPU / 内存。MemoryLimit 在老 systemd 上也能用。
MemoryLimit=768M
CPUQuota=50%
Nice=10
IOSchedulingClass=2
IOSchedulingPriority=7
ExecStart=/bin/sh "${WORK_DIR}/start-node.sh" "${JAVA_BIN}" "${WORK_DIR}" --server "${SERVER}" --name "${NAME}" --role node --env "${ENV}" --home "${AGENT_HOME}" --allow-paths "${ALLOW_PATHS_ARG}" --backup-root "${BACKUP_ROOT}"${ALLOW_SERVICES_ARG:+ --allow-services "${ALLOW_SERVICES_ARG}"}
Restart=always
RestartSec=10
# 升级时 Agent 要等手上的发布跑完才退出，别急着 SIGKILL
TimeoutStopSec=1800

[Install]
WantedBy=multi-user.target
UNITEOF
systemctl daemon-reload
systemctl enable "$SERVICE_NAME" >/dev/null 2>&1 || true

# ---------- 启动 ----------
echo "[7/8] 启动"
# 首次注册要带接入凭证。凭证只在这一次通过环境变量传进去，之后凭据落在
# AGENT_HOME 里，不必写进 unit 文件长期留在磁盘上
systemctl stop "$SERVICE_NAME" >/dev/null 2>&1 || true
if [ ! -f "${AGENT_HOME}/enrolled/${CRED_KEY}.token" ]; then
    mkdir -p "${AGENT_HOME}/enrolled"
    chown -R "$RUN_USER" "$AGENT_HOME"
    # --env 只在这一次（平台首次见到这台机器）起作用，后面以页面上的设置为准，
    # 所以这条命令上漏了它，环境就只能装完再去页面上改
    sudo -u "$RUN_USER" env RELEASE_ENROLL_TOKEN="$ENROLL_TOKEN" RELEASE_AGENT_HOME="$AGENT_HOME" \
        "$JAVA_BIN" -Xms64m -Xmx256m -jar "${WORK_DIR}/deploy-agent.jar" --server "$SERVER" --name "$NAME" \
        --role node --env "$ENV" --home "$AGENT_HOME" --allow-paths "$ALLOW_PATHS_ARG" \
        --backup-root "$BACKUP_ROOT" \
        ${ALLOW_SERVICES_ARG:+--allow-services "$ALLOW_SERVICES_ARG"} \
        > "${WORK_DIR}/enroll.log" 2>&1 &
    sleep 8
    pkill -u "$RUN_USER" -f 'java.*deploy-agent' >/dev/null 2>&1 || true
    sleep 1
    if [ ! -f "${AGENT_HOME}/enrolled/${CRED_KEY}.token" ]; then
        echo "首次注册失败，日志末尾：" >&2
        tail -n 20 "${WORK_DIR}/enroll.log" >&2
        die "注册未完成，请检查接入凭证和平台连通性"
    fi
fi
systemctl restart "$SERVICE_NAME"
sleep 3

# ---------- 检查 ----------
echo "[8/8] 检查"
if systemctl is-active --quiet "$SERVICE_NAME"; then
    echo "启动成功，已注册为 systemd 服务 ${SERVICE_NAME}（开机自启、崩溃自动重启）"
    echo "  运行账号：${RUN_USER}（不是 root）"
    echo "  看日志：journalctl -u ${SERVICE_NAME} -f"
    echo "  升级 jar 不用再上这台机器，在平台「节点管理」页点「升级」即可"
    if [ ${#NOT_WRITABLE[@]} -gt 0 ]; then
        echo
        echo "注意：${RUN_USER} 对下面的目录没有写权限，发布会在写文件时失败："
        for p in "${NOT_WRITABLE[@]}"; do echo "    $p"; done
        echo "  装上 acl 后重跑本脚本（yum install acl / apt install acl），"
        echo "  或手动授权：setfacl -R -m u:${RUN_USER}:rwx <目录>"
    fi
else
    echo "启动失败，日志末尾：" >&2
    journalctl -u "$SERVICE_NAME" -n 30 --no-pager 2>/dev/null || true
    exit 1
fi
