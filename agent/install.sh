#!/usr/bin/env bash
# 在 GPU 服务器上安装 lab-agent 并配置 SSH。
#
# 推荐（一条命令，不需要提前下载任何文件；本机需要能访问平台，如 SSH 会话里带着代理）：
#   curl -fsSL http://平台地址/agent/install.sh | sudo -E bash -s -- --platform-url http://平台地址 --token lab_xxx
#   脚本会用 token 从平台取回隧道参数，并下载 Agent 和 chisel；--legacy-user 默认取执行 sudo 的那个账号。
#
# 手动指定所有参数（文件已经放在脚本旁边时）：
#   sudo bash install.sh --server-url https://lab.example.com --token lab_xxx --legacy-user ubuntu
#
#   --platform-url URL        平台的公网地址：从这里下载 Agent / chisel、读取隧道参数。
#                             指定后 --server-url、--chisel-* 都不用写，由平台下发
#   --legacy-user LIST        以前大家共用的账号（如 ubuntu），逗号分隔。保留登录权限（由管理员改密码后自用），
#                             成员可以从它的家目录迁移文件、复制配置和 conda 环境
#   --admin-user LIST         可选：其他本机管理员账号（加入 lab-admin 组，任何时候都能登录）
#   --root-login keep|yes|key|no   root 远程登录：keep=保持系统现状（默认） yes=密码和公钥 key=只允许公钥 no=禁止
#   --migration-roots LIST    旧账号家目录之外、也允许迁移的旧数据目录，逗号分隔（默认 /lab,/data,/workspace）
#   --chisel-port PORT        在本机启动 chisel 服务端监听的端口（公网服务器经 WebVPN 连入，建反向隧道）
#   --chisel-secret SECRET    chisel 认证口令（和公网服务器共用，防止别人冒连）
#   --chisel-key FILE         可选：chisel 服务端私钥文件（固定指纹，防中间人）；不填则每次重启指纹变化、不校验
#   --chisel-allow CIDR       只允许这些来源访问 chisel 端口（默认 59.69.127.0/24，即 WebVPN 出口网段）
#   --no-sshd                 只安装 Agent，不修改 sshd 配置
#   --uninstall               停止 Agent 并移除 sshd 配置（账号和数据保留）
set -euo pipefail

SERVER_URL=""
PLATFORM_URL=""
TOKEN=""
ADMIN_USER=""
LEGACY_USERS=""
ROOT_LOGIN=keep
MIGRATION_ROOTS="/lab,/data,/workspace"
CHISEL_PORT=""
CHISEL_SECRET=""
CHISEL_KEY=""
CHISEL_ALLOW="59.69.127.0/24"
CONFIGURE_SSHD=1
UNINSTALL=0
SSHD_DROPIN=/etc/ssh/sshd_config.d/00-lab.conf
CHISEL_ENV=/etc/lab-agent/chisel.env

die() { echo -e "\033[31m[错误]\033[0m $*" >&2; exit 1; }
info() { echo -e "\033[32m==>\033[0m $*"; }
warn() { echo -e "\033[33m[注意]\033[0m $*"; }

while [ $# -gt 0 ]; do
    case "$1" in
        --server-url) SERVER_URL="${2%/}"; shift 2 ;;
        --platform-url) PLATFORM_URL="${2%/}"; shift 2 ;;
        --token) TOKEN="$2"; shift 2 ;;
        --admin-user) ADMIN_USER="$2"; shift 2 ;;
        --legacy-user) LEGACY_USERS="$2"; shift 2 ;;
        --root-login) ROOT_LOGIN="$2"; shift 2 ;;
        --migration-roots) MIGRATION_ROOTS="$2"; shift 2 ;;
        --chisel-port) CHISEL_PORT="$2"; shift 2 ;;
        --chisel-secret) CHISEL_SECRET="$2"; shift 2 ;;
        --chisel-key) CHISEL_KEY="$2"; shift 2 ;;
        --chisel-allow) CHISEL_ALLOW="$2"; shift 2 ;;
        --no-sshd) CONFIGURE_SSHD=0; shift ;;
        --uninstall) UNINSTALL=1; shift ;;
        *) die "未知参数 $1" ;;
    esac
done

[ "$(id -u)" -eq 0 ] || die "请用 sudo / root 执行"

reload_sshd() {
    sshd -t || return 1
    systemctl try-reload-or-restart ssh 2>/dev/null || systemctl try-reload-or-restart sshd
}

if [ "$UNINSTALL" -eq 1 ]; then
    systemctl disable --now lab-agent 2>/dev/null || true
    systemctl disable --now lab-chisel 2>/dev/null || true
    rm -f /etc/systemd/system/lab-agent.service /etc/systemd/system/lab-chisel.service \
          "$SSHD_DROPIN" /etc/profile.d/lab-proxy.sh /etc/apt/apt.conf.d/95lab-proxy "$CHISEL_ENV"
    systemctl daemon-reload
    reload_sshd
    info "已卸载 lab-agent / lab-chisel 并移除 sshd 配置。账号、家目录、/etc/lab-agent 保留。"
    info "（如需关闭 chisel 端口的防火墙放行，请手动 ufw delete 相应规则）"
    exit 0
fi

[ -n "$TOKEN" ] && { [ -n "$SERVER_URL" ] || [ -n "$PLATFORM_URL" ]; } || die "用法: $0 --platform-url URL --token TOKEN [--legacy-user ubuntu] [--admin-user NAME] [--root-login keep|yes|key|no]"
# 旧共享账号默认取执行 sudo 的账号（在新服务器上用共享账号登录后直接 sudo 执行即可）
if [ -z "$LEGACY_USERS" ] && [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != root ]; then
    LEGACY_USERS="$SUDO_USER"
    info "旧共享账号：$LEGACY_USERS（取自 sudo 的执行者；要换用 --legacy-user 指定）"
fi
case "$ROOT_LOGIN" in
    keep) PERMIT_ROOT="" ;;
    yes) PERMIT_ROOT=yes ;;
    key) PERMIT_ROOT=prohibit-password ;;
    no) PERMIT_ROOT=no ;;
    *) die "--root-login 只能是 keep / yes / key / no" ;;
esac
# 逗号分隔 → 空格分隔
ADMINS="$(echo "$LEGACY_USERS,$ADMIN_USER" | tr ',' ' ')"
command -v python3 >/dev/null || die "需要 python3"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 6))' || die "需要 Python 3.6+"
command -v nvidia-smi >/dev/null || warn "没有找到 nvidia-smi，将无法上报显卡信息"

# ---------- 0. 取参数、下载文件 ----------
# 经 curl | bash 执行、或旁边没有其他文件时，从平台下载到临时目录。
# 下载走当前环境的代理（sudo -E 保留了 SSH 会话里的 http_proxy 等），下载完立刻清掉：
# 之后 Agent 只经隧道访问平台（127.0.0.1），不能带着代理走。
SRC="$(cd "$(dirname "${BASH_SOURCE[0]:-.}")" && pwd)"
if [ -n "$PLATFORM_URL" ]; then
    command -v curl >/dev/null || die "需要 curl"
    DL="$(mktemp -d)"
    trap 'rm -rf "$DL"' EXIT
    info "从平台 $PLATFORM_URL 取回部署参数"
    CFG=$(curl -fsS -m 30 -H "Authorization: Bearer $TOKEN" "$PLATFORM_URL/api/agent/bootstrap-config") \
        || die "取配置失败：确认 Token 正确（可在平台里「重置 Token」），并且当前 SSH 会话里能访问 $PLATFORM_URL（代理是否生效？sudo 要加 -E 才会带上代理）"
    cfg() { printf '%s' "$CFG" | python3 -c "import sys,json;print(json.load(sys.stdin).get('$1',''))"; }
    [ -n "$SERVER_URL" ] || SERVER_URL="$(cfg server_url)"
    [ -n "$CHISEL_PORT" ] || CHISEL_PORT="$(cfg chisel_port)"
    [ -n "$CHISEL_SECRET" ] || CHISEL_SECRET="$(cfg chisel_secret)"
    CHISEL_ALLOW_API="$(cfg chisel_allow)"
    [ "$CHISEL_ALLOW" != "59.69.127.0/24" ] || CHISEL_ALLOW="${CHISEL_ALLOW_API:-$CHISEL_ALLOW}"
    [ -n "$CHISEL_SECRET" ] || CHISEL_PORT=""   # 平台没启用隧道：不装 chisel，Agent 直连平台
    files="lab_agent.py lab-gpus lab-gpu.sh"
    [ -z "$CHISEL_PORT" ] || files="$files chisel"
    info "下载 Agent 文件"
    for f in $files; do
        curl -fsS -m 300 "$PLATFORM_URL/agent/$f" -o "$DL/$f" || die "下载 $f 失败（$PLATFORM_URL/agent/$f）"
    done
    SRC="$DL"
    # 平台没启用隧道时 Agent 要直连平台，不能清掉代理
    if [ -n "$CHISEL_PORT" ]; then
        unset http_proxy https_proxy all_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY no_proxy NO_PROXY
    fi
fi

# ---------- 1. 防止把自己锁在外面 ----------
for u in $ADMINS; do
    id "$u" >/dev/null 2>&1 || die "账号 $u 不存在"
    [ "$(id -u "$u")" -ne 0 ] || die "$u 是 root，root 用 --root-login 控制"
done
if [ "$CONFIGURE_SSHD" -eq 1 ] && [ -z "${ADMINS// /}" ] && [ "$ROOT_LOGIN" != yes ]; then
    die "请用 --legacy-user 指定旧共享账号（或 --admin-user 指定管理员账号），否则配置 SSH 后没有人能以管理员身份远程登录"
fi

# ---------- 2. 用户组 ----------
for g in lab-users lab-active lab-admin; do
    getent group "$g" >/dev/null || { groupadd "$g"; info "创建用户组 $g"; }
done
# root、旧共享账号、管理员账号在 lab-admin 组里，任何时候都能登录（不受预约限制），登录方式保持原样
usermod -aG lab-admin root
for u in $ADMINS; do
    usermod -aG lab-admin "$u"
    if id -nG "$u" | grep -qwE 'sudo|wheel|admin'; then
        info "$u 保留登录权限（有 sudo 权限）"
    else
        info "$u 保留登录权限"
    fi
done

# ---------- 2.5 chisel 服务端（公网服务器经 WebVPN 连入，建反向隧道） ----------
if [ -n "$CHISEL_PORT" ]; then
    [ -n "$CHISEL_SECRET" ] || die "--chisel-port 需要同时提供 --chisel-secret"
    echo "$CHISEL_PORT" | grep -qE '^[0-9]+$' || die "--chisel-port 必须是数字"
    # chisel 可执行文件必须随安装包带来（GPU 此时还不能上外网，无法下载）
    [ -f "$SRC/chisel" ] || die "找不到 chisel 可执行文件（应和安装脚本放在一起）。请用 --platform-url 让脚本从平台下载，或用 tools/add-gpu.sh 部署"
    install -D -m 755 "$SRC/chisel" /opt/lab-agent/chisel

    install -d -m 755 /etc/lab-agent
    # 认证口令放进只有 root 可读的 EnvironmentFile，不出现在进程命令行里
    umask 077
    printf 'AUTH=%s:%s\n' "lab" "$CHISEL_SECRET" > "$CHISEL_ENV"
    KEYARG=""
    if [ -n "$CHISEL_KEY" ] && [ -f "$CHISEL_KEY" ]; then
        install -m 600 "$CHISEL_KEY" /etc/lab-agent/chisel_key
        KEYARG="--keyfile /etc/lab-agent/chisel_key"
    fi
    umask 022

    cat > /etc/systemd/system/lab-chisel.service <<UNIT
[Unit]
Description=Lab chisel server (reverse tunnel over WebVPN)
After=network-online.target
Wants=network-online.target

[Service]
# AUTH=lab:<口令> 从此文件读取，不出现在进程命令行里。只有持口令的公网服务器能建隧道。
EnvironmentFile=$CHISEL_ENV
# --backend 指向无效地址：未带凭证的普通 HTTP 探测拿不到任何东西
ExecStart=/opt/lab-agent/chisel server --reverse --backend http://127.0.0.1:9 --port $CHISEL_PORT $KEYARG
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
UNIT
    systemctl daemon-reload
    systemctl enable lab-chisel >/dev/null 2>&1 || true
    systemctl restart lab-chisel
    info "已启动 chisel 服务端，监听 $CHISEL_PORT"

    # 防火墙：只放行 WebVPN 出口网段访问 chisel 端口。依次处理 ufw / firewalld / iptables（有哪个启用就配哪个）
    OPENED=0
    if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q "Status: active"; then
        ufw allow from "$CHISEL_ALLOW" to any port "$CHISEL_PORT" proto tcp comment "lab chisel (WebVPN)" >/dev/null
        info "ufw：已放行 $CHISEL_ALLOW 访问 $CHISEL_PORT/tcp"; OPENED=1
    fi
    if command -v firewall-cmd >/dev/null && firewall-cmd --state >/dev/null 2>&1; then
        firewall-cmd --permanent --add-rich-rule="rule family=ipv4 source address=$CHISEL_ALLOW port port=$CHISEL_PORT protocol=tcp accept" >/dev/null
        firewall-cmd --reload >/dev/null
        info "firewalld：已放行 $CHISEL_ALLOW 访问 $CHISEL_PORT/tcp"; OPENED=1
    fi
    # iptables：只有 INPUT 默认丢弃、或链里有 REJECT/DROP 规则时才需要加；规则不会持久化，重启后由 lab-chisel 服务重新加
    if command -v iptables >/dev/null && iptables -S INPUT 2>/dev/null | grep -qE '^-P INPUT (DROP|REJECT)|^-A INPUT .*-j (DROP|REJECT)'; then
        RULE="-s $CHISEL_ALLOW -p tcp --dport $CHISEL_PORT -j ACCEPT"
        # shellcheck disable=SC2086
        iptables -C INPUT $RULE 2>/dev/null || iptables -I INPUT 1 $RULE
        mkdir -p /etc/systemd/system/lab-chisel.service.d
        cat > /etc/systemd/system/lab-chisel.service.d/firewall.conf <<FW
[Service]
ExecStartPre=-/bin/sh -c 'iptables -C INPUT $RULE 2>/dev/null || iptables -I INPUT 1 $RULE'
FW
        systemctl daemon-reload
        info "iptables：已放行 $CHISEL_ALLOW 访问 $CHISEL_PORT/tcp（每次 lab-chisel 启动时自动补上）"; OPENED=1
    fi
    [ "$OPENED" -eq 1 ] || info "本机没有启用 ufw / firewalld，也没有 iptables 拦截规则，无需放行 $CHISEL_PORT/tcp"
fi

# ---------- 3. 安装文件 ----------
fetch() {  # 优先用脚本同目录的文件，否则从平台下载
    if [ -f "$SRC/$1" ]; then cp "$SRC/$1" "$2"; else curl -fsSL "$SERVER_URL/agent/$1" -o "$2"; fi
}
mkdir -p /opt/lab-agent /etc/lab-agent /etc/lab-gpu /etc/ssh/lab_authorized_keys /var/lib/lab-agent
fetch lab_agent.py /opt/lab-agent/lab_agent.py && chmod 755 /opt/lab-agent/lab_agent.py
fetch lab-gpus /usr/local/bin/lab-gpus && chmod 755 /usr/local/bin/lab-gpus
fetch lab-gpu.sh /etc/profile.d/lab-gpu.sh && chmod 644 /etc/profile.d/lab-gpu.sh
chmod 755 /etc/lab-gpu /etc/ssh/lab_authorized_keys
chmod 700 /var/lib/lab-agent

# 非登录 shell（tmux 新窗口、`ssh host 命令`、VS Code 等）也加载显卡设置。
# 必须插在文件开头：bash.bashrc 一开始就会对非交互 shell 执行 return
for rc in /etc/bash.bashrc /etc/bashrc; do
    if [ -f "$rc" ] && ! grep -q lab-gpu.sh "$rc"; then
        sed -i '1i [ -r /etc/profile.d/lab-gpu.sh ] \&\& . /etc/profile.d/lab-gpu.sh  # lab-agent' "$rc"
    fi
done

# 写配置（保留已有配置里的其他字段，如 adopt_users）
umask 077
SERVER_URL="$SERVER_URL" TOKEN="$TOKEN" ROOTS="$MIGRATION_ROOTS" LEGACY="$LEGACY_USERS" python3 -c '
import json, os
path = "/etc/lab-agent/config.json"
try:
    cfg = json.load(open(path))
except (IOError, ValueError):
    cfg = {"interval": 60, "adopt_users": []}
split = lambda v: [r.strip() for r in v.split(",") if r.strip()]
cfg.update(server_url=os.environ["SERVER_URL"], token=os.environ["TOKEN"], migration_roots=split(os.environ["ROOTS"]))
if os.environ["LEGACY"]:
    cfg["legacy_users"] = split(os.environ["LEGACY"])
json.dump(cfg, open(path, "w"), indent=2)
'
umask 022

# ---------- 4. 首次同步 ----------
TUNNEL_OK=1
if [ -n "$CHISEL_PORT" ]; then
    info "等待公网服务器经 WebVPN 建立隧道（最多 3 分钟）..."
    for i in $(seq 1 90); do
        curl -fsS -m 5 --noproxy '*' "$SERVER_URL/healthz" >/dev/null 2>&1 && break
        if [ "$i" -eq 90 ]; then
            TUNNEL_OK=0
            warn "3 分钟内隧道还没建立。Agent 仍会装好并每分钟重试，隧道通了就会自动同步，不需要重跑本命令。"
            warn "排查：在公网服务器的 deploy 目录执行 docker compose exec tunnel python -m app.tunnel status 看错误原因；"
            warn "      docker compose logs --tail 50 tunnel 看 chisel 的连接日志；并确认平台「管理 → GPU 服务器」里这台的内网地址就是本机内网 IP。"
            break
        fi
        sleep 2
    done
    if [ "$TUNNEL_OK" -eq 1 ]; then info "隧道已连通"; fi
fi
if [ "$TUNNEL_OK" -eq 1 ]; then
    info "首次同步（会创建成员账号）..."
    python3 /opt/lab-agent/lab_agent.py --once || die "同步失败：请检查本机能否访问 $SERVER_URL，以及 token 是否正确"
else
    warn "隧道未通，跳过首次同步；lab-agent 服务会在隧道通后自动同步（创建成员账号）"
fi

cat > /etc/systemd/system/lab-agent.service <<'UNIT'
[Unit]
Description=Lab GPU agent
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/usr/bin/env python3 /opt/lab-agent/lab_agent.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable lab-agent >/dev/null 2>&1 || true
systemctl restart lab-agent
info "lab-agent 已启动（journalctl -u lab-agent -f 查看日志）"

# ---------- 5. sshd ----------
if [ "$CONFIGURE_SSHD" -eq 1 ]; then
    if ! grep -qE '^\s*Include\s+/etc/ssh/sshd_config\.d/\*\.conf' /etc/ssh/sshd_config; then
        cp /etc/ssh/sshd_config "/etc/ssh/sshd_config.bak.$(date +%s)"
        sed -i '1i Include /etc/ssh/sshd_config.d/*.conf' /etc/ssh/sshd_config
        mkdir -p /etc/ssh/sshd_config.d
    fi
    ROOT_LINE=""
    [ -n "$PERMIT_ROOT" ] && ROOT_LINE="PermitRootLogin $PERMIT_ROOT"
    cat > "$SSHD_DROPIN" <<SSHD
# 由 lab-agent 安装脚本生成。sshd 对同一选项“先出现的生效”，本文件排在最前。
$ROOT_LINE
# 成员可以用平台上设置的密码或登记的公钥登录
PasswordAuthentication yes
PubkeyAuthentication yes
# 只有今天有预约的成员（lab-active）、root / 旧共享账号 / 管理员（lab-admin）可以登录
AllowGroups lab-active lab-admin

# 成员只认在实验室平台登记的公钥（自己往 ~/.ssh 里加的不生效，防止把账号借给别人）
Match Group lab-users
    AuthorizedKeysFile /etc/ssh/lab_authorized_keys/%u
Match all
SSHD
    if reload_sshd; then
        info "sshd 已更新：root 远程登录=$ROOT_LOGIN；旧共享账号照常登录；成员可用密码或公钥登录，且仅在有预约的当天可登录"
    else
        rm -f "$SSHD_DROPIN"
        die "sshd 配置校验失败，已回滚"
    fi
fi

# ---------- 6. 旧共享账号里别人留下的公钥 ----------
# 改密码挡不住公钥登录：同学们以前可能往共享账号的 authorized_keys 里加过自己的公钥
for u in $(echo "$LEGACY_USERS" | tr ',' ' '); do
    AK="$(getent passwd "$u" | cut -d: -f6)/.ssh/authorized_keys"
    [ -f "$AK" ] || continue
    N=$(grep -cE '^[^#]*(ssh-|ecdsa-|sk-)' "$AK" || true)
    [ "$N" -gt 0 ] || continue
    warn "$u 的 $AK 里有 $N 个公钥，持有对应私钥的人改密码后仍能登录 $u："
    awk '!/^#/ && NF {print "      " $NF}' "$AK"
    if [ -t 0 ]; then
        read -r -p "现在清空这些公钥吗（会备份；你自己的公钥稍后可以重新加回）？[y/N] " ans
        case "$ans" in
            y|Y) cp -p "$AK" "$AK.bak-$(date +%Y%m%d%H%M%S)"; : > "$AK"; info "已清空，备份在 $AK.bak-*" ;;
            *) warn "已保留。切换时请手动清理：$AK" ;;
        esac
    fi
done

info "安装完成"
cat <<DONE
接下来请务必：
  1. 【不要关闭当前会话】另开一个终端，用旧共享账号（或管理员账号）重新登录一次，确认可以登录
  2. 修改旧共享账号的密码，只有管理员知道：sudo passwd ${LEGACY_USERS%%,*}
  3. 清理旧共享账号 ~/.ssh/authorized_keys 里同学们的公钥（见上方提示）
  4. 通知同学：在平台上设置用户名和密码 / 公钥，并在平台上迁移自己的文件和环境
DONE
