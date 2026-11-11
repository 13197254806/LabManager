#!/usr/bin/env bash
# Mac 上常驻的 GPU 连接守护，开机自启。在你自己的 Mac 上执行，不要放到服务器上跑。
#
#   bash tools/mac-netkeeper.sh install     # 安装并启动（之后每次登录 Mac 自动运行）
#   bash tools/mac-netkeeper.sh status      # 看当前状态
#   bash tools/mac-netkeeper.sh uninstall   # 停止并卸载
#
#   sudo bash tools/mac-netkeeper.sh install-vpn    # 安装校园网 VPN（无头 aTrust），开机自启
#   sudo bash tools/mac-netkeeper.sh vpn-password   # 校园网密码改了之后更新
#   sudo bash tools/mac-netkeeper.sh uninstall-vpn  # 卸载 VPN，改回官方客户端
#
# 它做四件事：
#   1. 给每台 GPU 保持一条反向隧道 GPU 127.0.0.1:7890 → Mac 上的代理池，代替在 Termius 里手动开端口转发。
#      GPU 上的 Codex 靠这个代理联网（~/.profile 里 http_proxy 指向它）。
#   2. 维护代理池：一个专用 mihomo，只连当前网络能通的入口 IP，一个挂了自动换下一个。
#      代理节点的域名对应几十个入口 IP，DNS 每几秒随机换顺序，校园网到其中一部分不通，
#      Shadowrocket 和普通 mihomo 轮到这些 IP 就超时，所以才时好时坏。
#   3. 定时检查：路由变了就立刻重连隧道，隔几分钟从 GPU 上实测一次代理。
#   4. 校园网内 GPU 能直连，用不上 aTrust，它掉线时直接关掉客户端免得弹窗；
#      校外 GPU 不通时才拉起 aTrust，需要登录就发系统通知。不会替你登录。
#      aTrust 掉线多半不是网络问题：键鼠闲置 1 小时客户端就会自己登出（日志里的 InputMonitor）。
#
# 装了校园网 VPN 后，第 4 条换成 root 守护进程（vpn-run）：用 zju-connect 在校外自动连接、掉线重连，
# 校园网内能直连就不登录。官方客户端用不上了，同账号还会互相踢下线，它在运行时 VPN 让路。
#
# 校园网密码只存在系统钥匙串里，由你在 install-vpn 时录入，启动 zju-connect 时读取，不写明文文件。
# 代理节点配置读自 openai-ip-monitor，生成的文件只有本人可读。
# 日志：~/Library/Logs/lab-netkeeper.log
set -u

: "${HOME:=/var/root}"    # LaunchDaemon 的环境里没有 HOME

GPUS=(gpu_01 gpu_02)      # ~/.ssh/config 里的别名
PROXY_PORT=7890           # GPU 上绑的端口，~/.profile 的 http_proxy 指向它
MAC_PROXY_PORT=17893      # 本脚本管理的代理池，隧道连到这里
PROBE_URL=https://api.openai.com/
ATRUST_HOST=vpnu.whut.edu.cn

TICK=10                   # 看路由和 aTrust 状态的间隔（秒）
REACH_EVERY=30            # 探测 GPU 22 端口的间隔
PROXY_EVERY=180           # 从 GPU 上实测代理的间隔
POOL_CHECK_EVERY=60       # 在 Mac 上实测代理池的间隔
POOL_REFRESH_MIN=300      # 代理池连续不通时，重新挑 IP 的最小间隔

LABEL=local.lab-netkeeper
APP_DIR="$HOME/Library/Application Support/lab-netkeeper"
INSTALLED="$APP_DIR/mac-netkeeper.sh"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/lab-netkeeper.log"
ATRUST_LOGS="$HOME/Library/Application Support/aTrust/logs"
POOL_DIR="$APP_DIR/pool"
NODE_SOURCE="$HOME/Project/Feishu/openai-ip-monitor/run/mihomo/config.yaml"
MIHOMO=/opt/homebrew/bin/mihomo
PYTHON=/opt/homebrew/bin/python3

VPN_LABEL=local.lab-netkeeper-vpn
VPN_DIR="/Library/Application Support/lab-netkeeper"   # root 所有，普通用户改不了里面的程序
VPN_PLIST="/Library/LaunchDaemons/$VPN_LABEL.plist"
VPN_LOG=/Library/Logs/lab-netkeeper-vpn.log
VPN_KEYCHAIN=/Library/Keychains/System.keychain
VPN_KC_SERVICE=lab-netkeeper-vpn
VPN_PROBE_ADDRS="192.168.247.150 192.168.247.151"
# 学校服务器在登录约 8 小时后结束会话：实测 4 次都是 8 小时零 23～51 秒，官方客户端也一样
VPN_SESSION_MAX=28800
VPN_SESSION_SLACK=120      # root 读不到你的 ~/.ssh/config，直接写地址
ZC_VERSION=v1.3.1
ZC_URL="https://github.com/Mythologyli/zju-connect/releases/download/$ZC_VERSION/zju-connect-darwin-arm64.zip"
ZC_SHA256=1003088f144a178c3fe0b8ebb4e00ee93f2aa8a53876db97b521e5afc82cdba2

SSH_BASE=(-T -o BatchMode=yes -o ConnectTimeout=10 -o ControlMaster=no -o ControlPath=none)

# GPU 端的看门狗：45 秒收不到 Mac 的心跳就杀掉自己这条 sshd，立刻释放 7890。
# GPU 的 sshd 没开 ClientAlive，断线后的旧会话会占着端口两个多小时，新隧道绑不上。
REMOTE_WATCHDOG='while read -r -t 45 _; do :; done; kill -TERM $PPID'

log() { printf '%s %s\n' "$(date '+%F %T')" "$*" >>"$LOG"; }

notify() { /usr/bin/osascript -e "display notification \"$1\" with title \"Lab 网络\"" >/dev/null 2>&1; }

gpu_addr() { /usr/bin/ssh -G "$1" 2>/dev/null | awk '/^hostname /{print $2; exit}'; }

route_iface() { /sbin/route -n get "$1" 2>/dev/null | awk '/interface:/{print $2; exit}'; }

tcp_open() { /usr/bin/nc -G "$3" -z "$1" "$2" >/dev/null 2>&1; }

# 读到 SSH 横幅才算通。只看 TCP 不够：酒店、公共 Wi-Fi 的认证门户可能代答任意端口。
# stdin 要撑 2 秒：stdin 一结束 nc 就关闭发送方向，经 VPN 隧道时连接会在横幅到达前断开，造成误报。
# 不能用 nc -d：它读完横幅也不退出，会在 GPU 上占着未认证的 SSH 名额，直到 sshd 超时（约 120 秒）
ssh_banner_ok() { [ "$(sleep 2 | /usr/bin/nc -G 4 "$1" 22 2>/dev/null | head -c 4)" = SSH- ]; }

# 带真实网关的默认路由网卡（跳过 Shadowrocket、aTrust 的虚拟网卡）
physical_iface() {
    /usr/sbin/netstat -rn -f inet | awk '$1 == "default" && $2 ~ /^[0-9.]+$/ && $4 !~ /^(utun|ipsec|ppp|bridge|gif|stf)/ {print $4; exit}'
}

network_id() {
    local iface
    iface=$(physical_iface)
    [ -n "$iface" ] && echo "$iface $(/usr/sbin/ipconfig getifaddr "$iface")"
}

# 取节点、挑出当前网络能通的入口 IP、写成 mihomo 配置。stdout 是一行摘要，不含密码。
# 返回 0 = 配置变了，3 = 没变，其他 = 没结果（保留旧配置）。
build_pool_config() {
    mkdir -p "$POOL_DIR" && chmod 700 "$POOL_DIR"
    "$PYTHON" -I - "$NODE_SOURCE" "$POOL_DIR" "$1" "$MAC_PROXY_PORT" <<'PY'
import concurrent.futures as cf, json, os, re, subprocess, sys

source, pool_dir, iface, listen = sys.argv[1:]
node_file = os.path.join(pool_dir, "node.json")
config_file = os.path.join(pool_dir, "config.yaml")


def write_private(path, text):
    tmp = path + ".tmp"
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        f.write(text)
    os.replace(tmp, path)


# openai-ip-monitor 的配置不在了就用上次存下的节点
try:
    node = json.load(open(source))["proxies"][0]
    write_private(node_file, json.dumps(node))
except (OSError, ValueError, LookupError):
    try:
        node = json.load(open(node_file))
    except (OSError, ValueError):
        print("找不到节点配置")
        sys.exit(2)

host, port = node["server"], str(node["port"])
if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", host):
    ips = [host]
else:
    answer = subprocess.run(["/usr/bin/dig", "+short", "+time=2", "+tries=1", host, "A", "@223.5.5.5"],
                            capture_output=True, text=True).stdout
    ips = sorted(set(re.findall(r"(?m)^\d+\.\d+\.\d+\.\d+$", answer)), key=lambda ip: [int(x) for x in ip.split(".")])
if not ips:
    print("解析不到入口 IP")
    sys.exit(2)


def reachable(ip):
    return all(subprocess.run(["/usr/bin/nc", "-G", "3", "-z", "-b", iface, ip, port],
                              capture_output=True).returncode == 0 for _ in range(2))


with cf.ThreadPoolExecutor(16) as ex:
    good = [ip for ip, ok in zip(ips, ex.map(reachable, ips)) if ok]
if not good:
    print(f"入口 IP {len(ips)} 个全部连不上")
    sys.exit(2)

pool = good[:: max(1, len(good) // 12)][:12]   # 均匀取，不要全挤在一个网段
names = ["n" + ip for ip in pool]
text = json.dumps({
    "mixed-port": int(listen), "bind-address": "127.0.0.1", "allow-lan": False,
    "mode": "rule", "log-level": "error", "ipv6": False,
    "interface-name": iface,   # 绑物理网卡，绕过 Shadowrocket 和 aTrust
    "proxies": [dict(node, name=n, server=ip) for n, ip in zip(names, pool)],
    "proxy-groups": [{"name": "pool", "type": "fallback", "url": "https://www.gstatic.com/generate_204",
                      "interval": 30, "timeout": 3000, "lazy": False, "proxies": names}],
    "rules": ["MATCH,pool"],
}, indent=1)
unchanged = os.path.exists(config_file) and open(config_file).read() == text
if not unchanged:
    write_private(config_file, text)
print(f"入口 IP {len(ips)} 个，能通 {len(good)} 个，使用 {len(pool)} 个，网卡 {iface}")
sys.exit(3 if unchanged else 0)
PY
}

pool_running() { [ -n "$POOL_PID" ] && kill -0 "$POOL_PID" 2>/dev/null; }

start_pool() {
    POOL_STARTED=$SECONDS
    [ -x "$MIHOMO" ] && [ -f "$POOL_DIR/config.yaml" ] || return 0
    "$MIHOMO" -d "$POOL_DIR" >"$POOL_DIR/mihomo.log" 2>&1 &
    POOL_PID=$!
}

stop_pool() {
    pool_running || return 0
    kill "$POOL_PID" 2>/dev/null
    wait "$POOL_PID" 2>/dev/null
    POOL_PID=""
}

pool_probe() {
    /usr/bin/curl -s -m 8 -o /dev/null -w '%{http_code}' -x "http://127.0.0.1:$MAC_PROXY_PORT" "$PROBE_URL" 2>/dev/null
}

refresh_pool() {
    local iface summary rc
    POOL_REFRESHED=$SECONDS
    iface=$(physical_iface)
    [ -n "$iface" ] || return 0
    summary=$(build_pool_config "$iface")
    rc=$?
    case $rc in
        0)
            log "代理池已更新（$1）：$summary"
            stop_pool
            start_pool
            ;;
        3) log "代理池没有变化（$1）：$summary" ;;
        *) log "代理池没更新（$1）：${summary:-生成配置出错}" ;;
    esac
}

maintain_pool() {
    local net
    net=$(network_id)
    if [ "$net" != "$POOL_NET" ]; then
        POOL_NET=$net
        LOGIN_NOTIFIED=0
        [ -z "$net" ] || refresh_pool "启动或网络变化"
    fi
    if ! pool_running && [ $((SECONDS - POOL_STARTED)) -ge 60 ]; then
        log "代理池没在运行，启动"
        start_pool
    fi
    [ "$SECONDS" -ge "$NEXT_POOL_CHECK" ] || return 0
    NEXT_POOL_CHECK=$((SECONDS + POOL_CHECK_EVERY))
    if [ "$(pool_probe)" != 000 ]; then
        POOL_FAILS=0
        return
    fi
    POOL_FAILS=$((POOL_FAILS + 1))
    # 单个 IP 挂了 fallback 组自己会换。连续不通说明挑出来的 IP 都不行了，重新挑一次
    if [ "$POOL_FAILS" -ge 3 ] && [ $((SECONDS - POOL_REFRESHED)) -ge "$POOL_REFRESH_MIN" ]; then
        POOL_FAILS=0
        refresh_pool "连续不通"
    fi
}

# 这台 GPU 的隧道 ssh 还活着就输出它的 pid
tunnel_pid() {
    local pid
    pid=$(cat "$APP_DIR/$1.pid" 2>/dev/null) || return 1
    [ -n "$pid" ] && [ "$(ps -o comm= -p "$pid" 2>/dev/null)" = /usr/bin/ssh ] && echo "$pid"
}

kill_tunnel() {
    local pid
    pid=$(tunnel_pid "$1") && kill "$pid" 2>/dev/null
}

tunnel_loop() {
    local host=$1 delay=3 began err last_err="" err_file="$APP_DIR/$1.err" wake_file="$APP_DIR/$1.wake"
    while :; do
        rm -f "$wake_file"
        began=$SECONDS
        /usr/bin/ssh "${SSH_BASE[@]}" -o ServerAliveInterval=15 -o ServerAliveCountMax=3 \
            -o ExitOnForwardFailure=yes -R "127.0.0.1:$PROXY_PORT:127.0.0.1:$MAC_PROXY_PORT" \
            "$host" "$REMOTE_WATCHDOG" < <(while echo; do sleep 15; done) >/dev/null 2>"$err_file" &
        echo $! >"$APP_DIR/$host.pid"
        wait $!
        if [ ! -e "$wake_file" ]; then
            err=$(tail -n 1 "$err_file" 2>/dev/null | cut -c1-160)
            if [ $((SECONDS - began)) -ge 60 ]; then
                delay=3 last_err=""
            else
                delay=$((delay * 2))
                [ "$delay" -le 60 ] || delay=60
            fi
            # 同样的错误只记一次，不然断网或端口被 Termius 占着时日志会刷屏
            if [ "$err" != "$last_err" ]; then
                log "$host 隧道断开：${err:-没有错误信息}（${delay}s 后重试）"
                last_err=$err
            fi
            /bin/sleep "$delay" &
            echo $! >"$APP_DIR/$host.sleep"
            wait $!
        fi
        # 退避是给“连不上”用的，路由变了就该在新路由上马上重试
        [ ! -e "$wake_file" ] || delay=3 last_err=""
    done
}

# 杀掉旧连接；正在退避的也叫醒，不然 VPN 恢复后还要干等最多 60 秒
wake_tunnel() {
    local pid
    : >"$APP_DIR/$1.wake"
    kill_tunnel "$1"
    pid=$(cat "$APP_DIR/$1.sleep" 2>/dev/null) || return 0
    [ -n "$pid" ] && [ "$(ps -o comm= -p "$pid" 2>/dev/null)" = /bin/sleep ] && kill "$pid" 2>/dev/null
}

# 路由变了（aTrust 上线/掉线、换网络）旧连接基本都废了，不等心跳超时
watch_routes() {
    local i iface
    for i in "${!GPUS[@]}"; do
        iface=$(route_iface "${ADDRS[$i]}")
        if [ -n "${IFACES[$i]:-}" ] && [ "$iface" != "${IFACES[$i]}" ]; then
            log "${GPUS[$i]} 路由 ${IFACES[$i]} → ${iface:-无}，重连隧道"
            wake_tunnel "${GPUS[$i]}"
        fi
        IFACES[$i]=$iface
    done
}

atrust_status() {
    /usr/bin/pgrep -x aTrust >/dev/null || { echo stopped; return; }
    local s
    s=$(ls -t "$ATRUST_LOGS"/aTrustTray.2*.log 2>/dev/null | head -2 | while IFS= read -r f; do
        grep 'setTrayStatus, status:' "$f" | tail -n 1 | awk '{print $NF}'
    done | head -1)
    echo "${s:-unknown}"
}

gpus_reachable() {
    local addr
    for addr in "${ADDRS[@]}"; do
        ssh_banner_ok "$addr" && return 0
    done
    return 1
}

check_network() {
    local status=$1
    # 改用校园网 VPN 后官方客户端用不上。它没登录就关掉，免得弹窗，也免得 VPN 一直让路
    if vpn_mode; then
        case "$status" in
            logout|unknown)
                log "已改用校园网 VPN，关闭官方 aTrust 客户端"
                /usr/bin/pkill -x aTrust
                ;;
        esac
    fi
    if gpus_reachable; then
        [ "$REACHABLE" = 1 ] || log "GPU 可达（路由 ${IFACES[0]:-未知}）"
        REACHABLE=1 UNREACHABLE_COUNT=0 LOGIN_NOTIFIED=0 ATRUST_OPENED=0
        # aTrust 掉线会弹登录窗。GPU 能直连就用不上它，关掉省得弹窗。
        # 只在「刚掉线」时关，你自己打开客户端准备登录时不去动它。
        if [ "$PENDING_CLOSE" = 1 ] && [ "$status" = logout ]; then
            log "aTrust 已掉线，GPU 可直连，关闭客户端"
            /usr/bin/pkill -x aTrust
            PENDING_CLOSE=0
        fi
        return
    fi
    # 连续两次才算不通：刚唤醒、刚换网络时会短暂连不上，不能为此弹出 aTrust
    UNREACHABLE_COUNT=$((UNREACHABLE_COUNT + 1))
    [ "$UNREACHABLE_COUNT" -ge 2 ] || return 0
    [ "$REACHABLE" = 0 ] || log "GPU 全部不可达（路由 ${IFACES[0]:-未知}，aTrust：${status}）"
    REACHABLE=0
    if vpn_mode; then
        if [ "$(vpn_state)" = attention ] && [ "$LOGIN_NOTIFIED" = 0 ]; then
            log "校园网 VPN 连续登录失败"
            notify "校园网 VPN 连续登录失败，确认密码后运行 vpn-password"
            LOGIN_NOTIFIED=1
        fi
        return 0
    fi
    tcp_open "$ATRUST_HOST" 443 4 || return 0   # 本机没网，等网络恢复
    case "$status" in
        stopped)
            # 每次断连只拉起一次，你手动退出后不再反复弹窗
            if [ "$ATRUST_OPENED" = 0 ]; then
                log "GPU 不通且 aTrust 没开，启动客户端"
                /usr/bin/open -g -a aTrust
                ATRUST_OPENED=1
            fi
            ;;
        logout|unknown)
            if [ "$LOGIN_NOTIFIED" = 0 ]; then
                log "aTrust 需要登录"
                notify "GPU 连不上，aTrust 需要登录"
                LOGIN_NOTIFIED=1
            fi
            ;;
    esac
}

# 从 GPU 上实测代理，输出 ok / node / tunnel，ssh 本身没连上时不输出。
# CONNECT 有响应（哪怕是 503）说明隧道和 Mac 上的代理都活着，这时候不通是代理节点的问题，重连隧道没用。
probe_proxy() {
    local codes
    codes=$(/usr/bin/ssh "${SSH_BASE[@]}" "$1" \
        "curl -s -m 8 -o /dev/null -w '%{http_connect} %{http_code}' -x http://127.0.0.1:$PROXY_PORT $PROBE_URL" 2>/dev/null)
    case "$codes" in
        "") ;;
        "000 "*) echo tunnel ;;
        "200 000"|"200 ") echo node ;;
        "200 "*) echo ok ;;
        *) echo node ;;
    esac
}

check_proxy() {
    local i host state
    # Mac 上的代理没开时 GPU 上也测不通，这时重连隧道没用
    if ! tcp_open 127.0.0.1 "$MAC_PROXY_PORT" 2; then
        [ "$MAC_PROXY_UP" = 0 ] || log "Mac 上的代理（127.0.0.1:${MAC_PROXY_PORT}）没开，GPU 上不了网"
        MAC_PROXY_UP=0
        return
    fi
    MAC_PROXY_UP=1
    for i in "${!GPUS[@]}"; do
        host=${GPUS[$i]}
        state=$(probe_proxy "$host")
        [ -n "$state" ] || continue   # ssh 本身没连上（首次连接偶尔被重置），交给 check_network
        if [ "$state" = tunnel ]; then
            # 连续两次才算，隧道刚重建时可能正好没绑上
            PROXY_FAILS[$i]=$((${PROXY_FAILS[$i]:-0} + 1))
            [ "${PROXY_FAILS[$i]}" -ge 2 ] || continue
            PROXY_FAILS[$i]=0
            kill_tunnel "$host"
        else
            PROXY_FAILS[$i]=0
        fi
        if [ "$state" != "${PROXY_STATE[$i]:-}" ]; then
            case "$state" in
                ok) log "$host 代理正常" ;;
                node) log "$host 隧道正常，但代理节点不通（换节点或等恢复，不是隧道的问题）" ;;
                tunnel) log "$host 隧道不通，重连" ;;
            esac
            PROXY_STATE[$i]=$state
        fi
    done
}

vpn_mode() { [ -f "$VPN_PLIST" ]; }

vpn_state() { cat "$VPN_DIR/state" 2>/dev/null; }

vpn_log() { printf '%s %s\n' "$(date '+%F %T')" "$*" >>"$VPN_LOG"; }

# 状态文件给用户级守护进程和 status 读，内容只有一个词，不敏感
vpn_set_state() {
    [ "$1" != "$VPN_STATE" ] || return 0
    VPN_STATE=$1
    (umask 022; echo "$1" >"$VPN_DIR/state")
    vpn_log "状态：$1"
}

official_running() { /usr/bin/pgrep -x aTrust >/dev/null; }

vpn_alive() { [ -n "$VPN_PID" ] && kill -0 "$VPN_PID" 2>/dev/null; }

vpn_direct_ok() {
    local addr
    for addr in $VPN_PROBE_ADDRS; do
        [ "$(sleep 2 | /usr/bin/nc -b "$1" -G 3 "$addr" 22 2>/dev/null | head -c 4)" = SSH- ] && return 0
    done
    return 1
}

vpn_tunnel_ok() {
    local addr
    for addr in $VPN_PROBE_ADDRS; do
        ssh_banner_ok "$addr" && return 0
    done
    return 1
}

# 同时试 IP 和域名：有的网络封 53 端口，域名又可能被透明代理解析成 Fake IP
vpn_internet_ok() {
    /usr/bin/nc -G 4 -z -b "$1" 223.5.5.5 53 >/dev/null 2>&1 || /usr/bin/nc -G 4 -z -b "$1" "$ATRUST_HOST" 443 >/dev/null 2>&1
}

vpn_account() { /usr/bin/security find-generic-password -s "$VPN_KC_SERVICE" "$VPN_KEYCHAIN" 2>/dev/null | awk -F'"' '/"acct"/{print $4}'; }

# 会话是否快到 8 小时。没有记录时当作到期，下次登录会写上
vpn_session_due() {
    local started
    started=$(cat "$VPN_DIR/session-start" 2>/dev/null)
    case $started in ''|*[!0-9]*) return 0 ;; esac
    [ $(($(date +%s) - started)) -ge $((VPN_SESSION_MAX - VPN_SESSION_SLACK)) ]
}

# 会话没到期时重启进程不重新计时：11-04 中途重启两次，仍在第一次登录 8 小时后被断开
vpn_note_login() {
    vpn_session_due && date +%s >"$VPN_DIR/session-start"
}

vpn_start() {
    local account password
    account=$(vpn_account)
    password=$(/usr/bin/security find-generic-password -s "$VPN_KC_SERVICE" -w "$VPN_KEYCHAIN" 2>/dev/null)
    if [ -z "$account" ] || [ -z "$password" ]; then
        vpn_log "钥匙串里读不到账号密码"
        vpn_set_state attention
        return 1
    fi
    # 只监听本机：默认绑全部网卡，在校园网里等于把入口暴露给别人
    ZJU_CONNECT_PASSWORD=$password "$VPN_DIR/zju-connect" --protocol atrust --server "$ATRUST_HOST" \
        --auth-type auth/psw --login-domain radius.com --username "$account" \
        --client-data-file "$VPN_DIR/client-data.json" \
        --socks-bind 127.0.0.1:1080 --http-bind 127.0.0.1:1081 \
        --local-dns-server 223.5.5.5 --bind-interface "$1" \
        --tun-mode --add-route >>"$VPN_DIR/zju.log" 2>&1 &
    VPN_PID=$!
    vpn_note_login
    vpn_log "启动 zju-connect，网卡 $1"
}

vpn_stop() {
    vpn_alive || { VPN_PID=""; return 0; }
    kill "$VPN_PID" 2>/dev/null
    wait "$VPN_PID" 2>/dev/null
    VPN_PID=""
}

# 退避要保守：密码错了反复重试可能锁号
vpn_backoff() {
    VPN_START_FAILS=$((VPN_START_FAILS + 1))
    case $VPN_START_FAILS in
        1) VPN_NEXT_START=$((SECONDS + 30)) ;;
        2) VPN_NEXT_START=$((SECONDS + 300)) ;;
        *) VPN_NEXT_START=$((SECONDS + 1800)) ;;
    esac
    [ "$VPN_START_FAILS" -lt 3 ] || vpn_set_state attention
}

vpn_tick() {
    local net iface
    net=$(network_id)
    iface=${net%% *}
    if [ -z "$iface" ]; then
        vpn_set_state offline
        return 0
    fi
    if [ "$net" != "$VPN_NET" ]; then
        [ -z "$VPN_NET" ] || vpn_log "网络变化：$VPN_NET → $net"
        VPN_NET=$net
        vpn_stop    # 绑定的网卡和底层连接都失效了，换网后重新登录
        VPN_NEXT_START=0 VPN_START_FAILS=0 VPN_TUNNEL_FAILS=0 VPN_DIRECT_OK=0 VPN_DIRECT_FAILS=0
    fi
    # 同账号只能一处在线，官方客户端在运行就让路，不然两边会互相踢
    if official_running; then
        vpn_stop
        vpn_set_state official
        return 0
    fi
    # 连续两次才切换：GPU 的 ssh 偶尔会重置连接，不能因为一次失败就去登录
    if vpn_direct_ok "$iface"; then
        VPN_DIRECT_OK=$((VPN_DIRECT_OK + 1)) VPN_DIRECT_FAILS=0
    else
        VPN_DIRECT_OK=0 VPN_DIRECT_FAILS=$((VPN_DIRECT_FAILS + 1))
    fi
    [ "$VPN_DIRECT_OK" != 1 ] || return 0
    if [ "$VPN_DIRECT_OK" -ge 2 ]; then
        vpn_stop
        VPN_START_FAILS=0 VPN_TUNNEL_FAILS=0
        vpn_set_state direct
        return 0
    fi
    [ "$VPN_STATE" != direct ] || [ "$VPN_DIRECT_FAILS" -ge 2 ] || return 0

    if vpn_alive; then
        if vpn_tunnel_ok; then
            VPN_TUNNEL_FAILS=0 VPN_START_FAILS=0
            vpn_set_state online
            return 0
        fi
        if vpn_session_due; then
            # 快满 8 小时时连不上，基本就是会话到期，不用等 4 次失败和退避。
            # 登录后计时重置，所以约 8 小时才走一次这里，不会变成反复重试
            vpn_log "会话已满约 8 小时且连不上 GPU，立刻重新登录"
            vpn_stop
            VPN_TUNNEL_FAILS=0
        else
            VPN_TUNNEL_FAILS=$((VPN_TUNNEL_FAILS + 1))
            [ "$VPN_TUNNEL_FAILS" -ge 4 ] || return 0
            vpn_log "连续 4 次连不上 GPU，重新登录"
            vpn_stop
            VPN_TUNNEL_FAILS=0
            vpn_backoff
            return 0
        fi
    fi
    if [ -n "$VPN_PID" ]; then
        vpn_log "zju-connect 退出了：$(tail -n 1 "$VPN_DIR/zju.log" 2>/dev/null | cut -c1-160)"
        VPN_PID=""
        vpn_backoff
        return 0
    fi
    [ "$SECONDS" -ge "$VPN_NEXT_START" ] || return 0
    if [ "$VPN_START_FAILS" -ge 6 ]; then
        vpn_set_state attention
        return 0
    fi
    vpn_internet_ok "$iface" || return 0
    [ "$VPN_START_FAILS" -ge 3 ] || vpn_set_state connecting
    vpn_start "$iface"
}

# root 守护进程的主循环，由 launchd 启动
vpn_run() {
    umask 077
    VPN_PID="" VPN_NET="" VPN_STATE="" VPN_NEXT_START=0 VPN_START_FAILS=0 VPN_TUNNEL_FAILS=0 VPN_DIRECT_OK=0 VPN_DIRECT_FAILS=0
    trap 'trap - TERM INT; vpn_log "退出"; vpn_stop; exit 0' TERM INT
    vpn_log "启动"
    while :; do
        vpn_tick
        if [ "$(wc -c <"$VPN_DIR/zju.log" 2>/dev/null || echo 0)" -gt 5242880 ]; then
            : >"$VPN_DIR/zju.log"
        fi
        sleep 15 &
        wait $!
    done
}

vpn_require_root() {
    [ "$(id -u)" = 0 ] || { echo "需要管理员权限：sudo bash $0 $1"; exit 1; }
}

vpn_fetch_binary() {
    if [ -x "$VPN_DIR/zju-connect" ] && "$VPN_DIR/zju-connect" --version 2>/dev/null | grep -q "${ZC_VERSION#v}"; then
        return 0
    fi
    local tmp rc=1
    tmp=$(mktemp -d) || return 1
    echo "下载 zju-connect ${ZC_VERSION}……"
    if /usr/bin/curl -fsSL -m 180 -o "$tmp/zc.zip" "$ZC_URL"; then
        if [ "$(/usr/bin/shasum -a 256 "$tmp/zc.zip" | awk '{print $1}')" = "$ZC_SHA256" ]; then
            /usr/bin/unzip -q -o "$tmp/zc.zip" -d "$tmp/x" &&
                /usr/bin/install -m 755 -o root -g wheel "$tmp/x/zju-connect" "$VPN_DIR/zju-connect" && rc=0
        else
            echo "校验值不一致，已放弃安装"
        fi
    else
        echo "下载失败，确认能访问 GitHub 后重试"
    fi
    rm -rf "$tmp"
    return $rc
}

# 密码经管道交给 security，不出现在命令行参数里，也不落临时文件
vpn_store_password() {
    local account password escaped check
    read -r -p "校园网账号（和 aTrust 相同）: " account
    read -r -s -p "校园网密码（输入时不显示）: " password
    echo
    if [ -z "$account" ] || [ -z "$password" ]; then
        echo "账号或密码为空"
        return 1
    fi
    escaped=${password//\\/\\\\}
    escaped=${escaped//\"/\\\"}
    printf 'add-generic-password -U -a "%s" -s %s -l "Lab campus VPN" -T /usr/bin/security -w "%s" %s\n' \
        "$account" "$VPN_KC_SERVICE" "$escaped" "$VPN_KEYCHAIN" | /usr/bin/security -i >/dev/null 2>&1
    check=$(/usr/bin/security find-generic-password -s "$VPN_KC_SERVICE" -w "$VPN_KEYCHAIN" 2>/dev/null)
    if [ "$check" != "$password" ]; then
        echo "写入钥匙串失败"
        return 1
    fi
    echo "已存入系统钥匙串"
}

install_vpn() {
    vpn_require_root install-vpn
    mkdir -p "$VPN_DIR" && chown root:wheel "$VPN_DIR" && chmod 755 "$VPN_DIR"
    vpn_fetch_binary || exit 1
    /usr/bin/install -m 755 -o root -g wheel "$0" "$VPN_DIR/mac-netkeeper.sh"
    if [ -n "$(vpn_account)" ]; then
        echo "钥匙串里已有账号，沿用（改密码用 vpn-password）"
    else
        vpn_store_password || exit 1
    fi
    cat >"$VPN_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$VPN_LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$VPN_DIR/mac-netkeeper.sh</string>
    <string>vpn-run</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>$VPN_LOG</string>
  <key>StandardErrorPath</key><string>$VPN_LOG</string>
</dict>
</plist>
EOF
    chown root:wheel "$VPN_PLIST" && chmod 644 "$VPN_PLIST"
    /usr/bin/plutil -lint "$VPN_PLIST" >/dev/null || { echo "plist 校验失败：$VPN_PLIST"; exit 1; }
    touch "$VPN_LOG" && chmod 644 "$VPN_LOG"
    /bin/launchctl bootout "system/$VPN_LABEL" 2>/dev/null
    /usr/bin/pkill -x zju-connect    # 手动测试留下的进程
    local i
    for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
        /bin/launchctl bootstrap system "$VPN_PLIST" 2>/dev/null && {
            echo "校园网 VPN 已安装并启动，开机自动运行。日志：$VPN_LOG"
            echo "以后可以关掉官方 aTrust 的「开机自启动」。"
            return
        }
        sleep 2
    done
    echo "启动失败，看日志：$VPN_LOG"
    exit 1
}

update_vpn_password() {
    vpn_require_root vpn-password
    vpn_store_password || exit 1
    /bin/launchctl kickstart -k "system/$VPN_LABEL" 2>/dev/null
    echo "已更新并重启 VPN"
}

uninstall_vpn() {
    vpn_require_root uninstall-vpn
    /bin/launchctl bootout "system/$VPN_LABEL" 2>/dev/null
    /usr/bin/pkill -x zju-connect
    rm -f "$VPN_PLIST"
    /usr/bin/security delete-generic-password -s "$VPN_KC_SERVICE" "$VPN_KEYCHAIN" >/dev/null 2>&1
    rm -rf "$VPN_DIR"
    echo "已卸载校园网 VPN，并从钥匙串删除了密码（日志保留在 ${VPN_LOG}）"
}

# 把 aTrust 状态变化喂给 check_network，单独拆出来方便测试
on_status_change() {
    local status=$1
    log "aTrust 状态 ${LAST_STATUS:-启动} → $status"
    [ "$status" = logout ] || PENDING_CLOSE=0
    case "$LAST_STATUS:$status" in
        online:logout|connect:logout|disconnect:logout) PENDING_CLOSE=1 ;;
    esac
}

init_state() {
    ADDRS=() IFACES=() PROXY_STATE=() PROXY_FAILS=()
    local host
    for host in "${GPUS[@]}"; do ADDRS+=("$(gpu_addr "$host")"); done
    REACHABLE="" UNREACHABLE_COUNT=0 LOGIN_NOTIFIED=0 ATRUST_OPENED=0 PENDING_CLOSE=0 LAST_STATUS="" MAC_PROXY_UP=""
    POOL_PID="" POOL_NET="" POOL_FAILS=0 POOL_REFRESHED=0 POOL_STARTED=-60 NEXT_POOL_CHECK=0
}

run() {
    mkdir -p "$APP_DIR"
    if [ "$(wc -c <"$LOG" 2>/dev/null || echo 0)" -gt 1048576 ]; then
        tail -n 2000 "$LOG" >"$LOG.tmp" && cat "$LOG.tmp" >"$LOG"
        rm -f "$LOG.tmp"
    fi
    init_state
    log "启动，守护：${GPUS[*]}"
    # launchd 下脚本自成一个进程组，kill 0 能把隧道、心跳和代理池一起带走
    trap 'trap - TERM INT; log "退出"; kill 0; exit 0' TERM INT
    local host status now last_tick=$SECONDS next_reach=0 next_proxy=$((SECONDS + 20))
    NEXT_POOL_CHECK=$((SECONDS + POOL_CHECK_EVERY))
    maintain_pool
    for host in "${GPUS[@]}"; do tunnel_loop "$host" & done
    while :; do
        now=$SECONDS
        # 主循环隔了很久才转回来说明 Mac 刚睡醒，再提醒一次，不然夜里那条通知错过就没了
        [ $((now - last_tick)) -lt 120 ] || LOGIN_NOTIFIED=0
        last_tick=$now
        maintain_pool
        watch_routes
        status=$(atrust_status)
        if [ "$status" != "$LAST_STATUS" ]; then
            on_status_change "$status"
            next_reach=0
        fi
        if [ "$now" -ge "$next_reach" ]; then
            check_network "$status"
            next_reach=$((now + REACH_EVERY))
        fi
        LAST_STATUS=$status
        if [ "$now" -ge "$next_proxy" ]; then
            check_proxy
            next_proxy=$((now + PROXY_EVERY))
        fi
        sleep "$TICK" &    # 放后台再 wait，收到退出信号才能立刻响应
        wait $!
    done
}

install_agent() {
    mkdir -p "$APP_DIR" "$(dirname "$PLIST")"
    cp "$0" "$INSTALLED"
    cat >"$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$INSTALLED</string>
    <string>run</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
EOF
    /usr/bin/plutil -lint "$PLIST" >/dev/null || { echo "plist 校验失败：$PLIST"; exit 1; }
    /bin/launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null
    # bootout 是异步的，旧实例退干净之前 bootstrap 会报 Input/output error
    local i
    for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
        /bin/launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null && {
            echo "已安装并启动。状态：bash $0 status    日志：$LOG"
            return
        }
        sleep 2
    done
    echo "启动失败，看日志：$LOG"
    exit 1
}

uninstall_agent() {
    /bin/launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null
    rm -f "$PLIST"
    rm -rf "$APP_DIR"
    echo "已卸载（日志保留在 ${LOG}）"
}

show_status() {
    local host addr proxy holder
    if /bin/launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
        echo "守护进程：运行中"
    else
        echo "守护进程：未运行"
    fi
    if vpn_mode; then
        case "$(vpn_state)" in
            online) echo "校园网 VPN：已连接" ;;
            direct) echo "校园网 VPN：不需要（校园网内直连 GPU）" ;;
            connecting) echo "校园网 VPN：连接中" ;;
            official) echo "校园网 VPN：让路中（官方 aTrust 客户端在运行）" ;;
            offline) echo "校园网 VPN：本机没网" ;;
            attention) echo "校园网 VPN：连续登录失败，看日志 $VPN_LOG" ;;
            *) echo "校园网 VPN：状态未知" ;;
        esac
    else
        echo "aTrust：$(atrust_status)"
    fi
    if tcp_open 127.0.0.1 "$MAC_PROXY_PORT" 2; then
        echo "代理池 ${MAC_PROXY_PORT}：开，$(grep -c '"server"' "$POOL_DIR/config.yaml" 2>/dev/null) 个入口 IP，实测 HTTP $(pool_probe)"
    else
        echo "代理池 ${MAC_PROXY_PORT}：关"
    fi
    for host in "${GPUS[@]}"; do
        addr=$(gpu_addr "$host")
        if tunnel_pid "$host" >/dev/null; then holder="本脚本"; else holder="其他（如 Termius）或无"; fi
        case "$(probe_proxy "$host")" in
            ok) proxy="正常" ;;
            node) proxy="隧道正常，代理节点不通" ;;
            tunnel) proxy="隧道不通" ;;
            *) proxy="ssh 失败" ;;
        esac
        printf '%s  路由 %s  SSH %s  7890 转发 %s  代理 %s\n' "$host" "$(route_iface "$addr")" \
            "$(ssh_banner_ok "$addr" && echo 通 || echo 不通)" "$holder" "$proxy"
    done
    echo "--- 最近日志"
    tail -n 8 "$LOG" 2>/dev/null
}

[ "${BASH_SOURCE[0]}" = "$0" ] || return 0   # 被 source 时只加载函数（测试用）

case "${1:-}" in
    run) run ;;
    install) install_agent ;;
    uninstall) uninstall_agent ;;
    status) show_status ;;
    vpn-run) vpn_run ;;
    install-vpn) install_vpn ;;
    vpn-password) update_vpn_password ;;
    uninstall-vpn) uninstall_vpn ;;
    *) echo "用法：bash $0 install|status|uninstall，或 sudo bash $0 install-vpn|vpn-password|uninstall-vpn"; exit 2 ;;
esac
