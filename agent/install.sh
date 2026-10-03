#!/usr/bin/env bash
# 在 GPU 服务器上安装 lab-agent 并配置 SSH。
#
#   sudo bash install.sh --server-url https://lab.example.com --token lab_xxx --legacy-user ubuntu
#
#   --legacy-user LIST        以前大家共用的账号（如 ubuntu），逗号分隔。保留登录权限（由管理员改密码后自用），
#                             成员可以从它的家目录迁移文件、复制配置和 conda 环境
#   --admin-user LIST         可选：其他本机管理员账号（加入 lab-admin 组，任何时候都能登录）
#   --root-login keep|yes|key|no   root 远程登录：keep=保持系统现状（默认） yes=密码和公钥 key=只允许公钥 no=禁止
#   --migration-roots LIST    旧账号家目录之外、也允许迁移的旧数据目录，逗号分隔（默认 /lab,/data,/workspace）
#   --tunnel-key KEY          公网服务器隧道的公钥：创建受限账号 labtunnel，只允许它开反向端口（由 tunnel bootstrap 自动传入）
#   --tunnel-ports P1,P2      允许 labtunnel 监听的本机端口（默认 18765,13128）
#   --internet-proxy URL      GPU 服务器经隧道上外网的代理（如 http://127.0.0.1:13128），写入全局 shell 和 apt 配置
#   --no-sshd                 只安装 Agent，不修改 sshd 配置
#   --uninstall               停止 Agent 并移除 sshd 配置（账号和数据保留）
set -euo pipefail

SERVER_URL=""
TOKEN=""
ADMIN_USER=""
LEGACY_USERS=""
ROOT_LOGIN=keep
MIGRATION_ROOTS="/lab,/data,/workspace"
TUNNEL_KEY=""
TUNNEL_PORTS="18765,13128"
INTERNET_PROXY=""
CONFIGURE_SSHD=1
UNINSTALL=0
SSHD_DROPIN=/etc/ssh/sshd_config.d/00-lab.conf

die() { echo -e "\033[31m[错误]\033[0m $*" >&2; exit 1; }
info() { echo -e "\033[32m==>\033[0m $*"; }
warn() { echo -e "\033[33m[注意]\033[0m $*"; }

while [ $# -gt 0 ]; do
    case "$1" in
        --server-url) SERVER_URL="${2%/}"; shift 2 ;;
        --token) TOKEN="$2"; shift 2 ;;
        --admin-user) ADMIN_USER="$2"; shift 2 ;;
        --legacy-user) LEGACY_USERS="$2"; shift 2 ;;
        --root-login) ROOT_LOGIN="$2"; shift 2 ;;
        --migration-roots) MIGRATION_ROOTS="$2"; shift 2 ;;
        --tunnel-key) TUNNEL_KEY="$2"; shift 2 ;;
        --tunnel-ports) TUNNEL_PORTS="$2"; shift 2 ;;
        --internet-proxy) INTERNET_PROXY="$2"; shift 2 ;;
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
    # 同时撤销隧道账号的公钥（账号本身保留）
    rm -f /etc/systemd/system/lab-agent.service "$SSHD_DROPIN" /etc/ssh/sshd_config.d/01-lab-tunnel.conf \
          /var/lib/labtunnel/.ssh/authorized_keys /etc/profile.d/lab-proxy.sh /etc/apt/apt.conf.d/95lab-proxy
    systemctl daemon-reload
    reload_sshd
    info "已卸载 lab-agent 并移除 sshd 配置。账号、家目录、/etc/lab-agent 保留。"
    exit 0
fi

[ -n "$SERVER_URL" ] && [ -n "$TOKEN" ] || die "用法: $0 --server-url URL --token TOKEN --legacy-user ubuntu [--admin-user NAME] [--root-login keep|yes|key|no]"
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

# ---------- 1. 防止把自己锁在外面 ----------
for u in $ADMINS; do
    id "$u" >/dev/null 2>&1 || die "账号 $u 不存在"
    [ "$(id -u "$u")" -ne 0 ] || die "$u 是 root，root 用 --root-login 控制"
done
if [ "$CONFIGURE_SSHD" -eq 1 ] && [ -z "${ADMINS// /}" ] && [ "$ROOT_LOGIN" != yes ]; then
    die "请用 --legacy-user 指定旧共享账号（或 --admin-user 指定管理员账号），否则配置 SSH 后没有人能以管理员身份远程登录"
fi

# ---------- 2. 用户组 ----------
for g in lab-users lab-active lab-admin lab-tunnel; do
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

# ---------- 2.5 公网服务器隧道账号（只能开反向端口，不能登录 shell） ----------
if [ -n "$TUNNEL_KEY" ]; then
    echo "$TUNNEL_KEY" | grep -qE '^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp[0-9]+) [A-Za-z0-9+/=]+' || die "--tunnel-key 格式不对"
    if ! id labtunnel >/dev/null 2>&1; then
        useradd -r -m -d /var/lib/labtunnel -s /usr/sbin/nologin -G lab-tunnel labtunnel
        info "创建隧道账号 labtunnel"
    fi
    usermod -aG lab-tunnel labtunnel
    OPTS="restrict,port-forwarding"
    # OpenSSH 7.8+ 支持 permitlisten：只允许监听指定的本机端口
    SSH_VER=$(ssh -V 2>&1 | sed -n 's/^OpenSSH_\([0-9]*\)\.\([0-9]*\).*/\1\2/p')
    if [ "${SSH_VER:-0}" -ge 78 ]; then
        for p in $(echo "$TUNNEL_PORTS" | tr ',' ' '); do OPTS="$OPTS,permitlisten=\"127.0.0.1:$p\""; done
    fi
    install -d -m 700 -o labtunnel -g labtunnel /var/lib/labtunnel/.ssh
    echo "$OPTS $TUNNEL_KEY" > /var/lib/labtunnel/.ssh/authorized_keys
    chown labtunnel:labtunnel /var/lib/labtunnel/.ssh/authorized_keys
    chmod 600 /var/lib/labtunnel/.ssh/authorized_keys
    # 单独的配置文件（即使 --no-sshd 也写入）：隧道账号只允许反向端口转发，禁止 -L 借道访问内网其他机器；
    # 30 秒心跳，断线后旧会话最多 90 秒就会释放端口，方便重连
    if ! grep -qE '^\s*Include\s+/etc/ssh/sshd_config\.d/\*\.conf' /etc/ssh/sshd_config; then
        cp /etc/ssh/sshd_config "/etc/ssh/sshd_config.bak.$(date +%s)"
        sed -i '1i Include /etc/ssh/sshd_config.d/*.conf' /etc/ssh/sshd_config
    fi
    mkdir -p /etc/ssh/sshd_config.d
    cat > /etc/ssh/sshd_config.d/01-lab-tunnel.conf <<'TUNNEL'
# 由 lab-agent 安装脚本生成：公网服务器隧道账号
Match User labtunnel
    AllowTcpForwarding remote
    PermitOpen none
    X11Forwarding no
    AllowAgentForwarding no
    PermitTTY no
    PasswordAuthentication no
    ClientAliveInterval 30
    ClientAliveCountMax 3
Match all
TUNNEL
    if ! reload_sshd; then
        rm -f /etc/ssh/sshd_config.d/01-lab-tunnel.conf
        die "sshd 配置校验失败，已回滚"
    fi
fi

# ---------- 2.6 经隧道上外网的代理 ----------
if [ -n "$INTERNET_PROXY" ]; then
    cat > /etc/profile.d/lab-proxy.sh <<PROXY
# 由 lab-agent 安装：GPU 服务器经公网服务器隧道访问外网（pip / conda / git / wget / curl）
export http_proxy="$INTERNET_PROXY" https_proxy="$INTERNET_PROXY"
export HTTP_PROXY="$INTERNET_PROXY" HTTPS_PROXY="$INTERNET_PROXY"
export no_proxy="localhost,127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"
export NO_PROXY="\$no_proxy"
PROXY
    chmod 644 /etc/profile.d/lab-proxy.sh
    printf 'Acquire::http::Proxy "%s";\nAcquire::https::Proxy "%s";\n' "$INTERNET_PROXY" "$INTERNET_PROXY" > /etc/apt/apt.conf.d/95lab-proxy 2>/dev/null || true
    for rc in /etc/bash.bashrc /etc/bashrc; do
        if [ -f "$rc" ] && ! grep -q lab-proxy.sh "$rc"; then
            sed -i '1i [ -r /etc/profile.d/lab-proxy.sh ] \&\& . /etc/profile.d/lab-proxy.sh  # lab-agent' "$rc"
        fi
    done
    info "已配置上外网代理 $INTERNET_PROXY（新登录的终端生效）"
fi

# ---------- 3. 安装文件 ----------
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
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
if [ -n "$TUNNEL_KEY" ]; then
    info "等待公网服务器建立隧道（最多 3 分钟）..."
    for i in $(seq 1 90); do
        curl -fsS -m 5 --noproxy '*' "$SERVER_URL/healthz" >/dev/null 2>&1 && break
        [ "$i" -eq 90 ] && die "隧道一直没有建立：在公网服务器上执行 docker compose logs tunnel 查看原因"
        sleep 2
    done
    info "隧道已连通"
fi
info "首次同步（会创建成员账号）..."
python3 /opt/lab-agent/lab_agent.py --once || die "同步失败：请检查本机能否访问 $SERVER_URL，以及 token 是否正确"

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
# 只有今天有预约的成员（lab-active）、root / 旧共享账号 / 管理员（lab-admin）和隧道账号（lab-tunnel）可以登录
AllowGroups lab-active lab-admin lab-tunnel

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
