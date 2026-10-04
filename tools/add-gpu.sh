#!/usr/bin/env bash
# 增加一台 GPU 服务器（首次部署 Agent）。在你平时用来管理 GPU 的电脑上执行——
# 它能经校园网（你自己的 aTrust / 校内网络）直连 GPU 服务器。装好后此步不再需要，更新都走隧道。
#
#   bash tools/add-gpu.sh <平台地址> <服务器Token> [--user ubuntu]
#
# 例：bash tools/add-gpu.sh http://122.51.221.158 lab_xxxxx --user ubuntu
#
# Token 在平台「管理 → GPU 服务器 → 显示安装命令」里获得（只显示一次）。
# 脚本会：用 Token 从平台取回该服务器的内网地址、chisel 端口和口令 → 下载 agent 文件和 chisel →
# 传到 GPU 服务器并运行安装脚本。GPU 账号密码由你在提示时自己输入，不经过脚本。
set -euo pipefail

PLATFORM="${1:-}"; TOKEN="${2:-}"; shift 2 2>/dev/null || true
USER_NAME="ubuntu"
EXTRA=()
while [ $# -gt 0 ]; do
    case "$1" in
        --user) USER_NAME="$2"; shift 2 ;;
        *) EXTRA+=("$1"); shift ;;   # 其余参数原样转给 install.sh，如 --migration-roots /data
    esac
done
[ -n "$PLATFORM" ] && [ -n "$TOKEN" ] || { echo "用法：bash tools/add-gpu.sh <平台地址> <服务器Token> [--user ubuntu]"; exit 1; }
PLATFORM="${PLATFORM%/}"

info() { echo -e "\033[32m==>\033[0m $*"; }
die() { echo -e "\033[31m[错误]\033[0m $*" >&2; exit 1; }
need() { command -v "$1" >/dev/null || die "需要 $1"; }
need curl; need ssh; need scp; need python3

info "从平台取回该服务器的部署参数"
CFG=$(curl -fsS -H "Authorization: Bearer $TOKEN" "$PLATFORM/api/agent/bootstrap-config") \
    || die "取配置失败：确认平台地址正确、Token 没错、你的 aTrust 能访问平台"
get() { printf '%s' "$CFG" | python3 -c "import sys,json;print(json.load(sys.stdin).get('$1',''))"; }
HOST=$(get host); NAME=$(get name)
CHISEL_PORT=$(get chisel_port); CHISEL_SECRET=$(get chisel_secret)
CHISEL_ALLOW=$(get chisel_allow); SERVER_URL=$(get server_url)
[ -n "$HOST" ] || die "平台里这台服务器还没填内网地址（管理 → GPU 服务器 → 编辑）"
[ -n "$CHISEL_SECRET" ] || die "平台没有返回 chisel 口令：确认服务器 .env 里设置了 CHISEL_SECRET"
info "服务器 $NAME（$HOST），chisel 端口 $CHISEL_PORT"

BUNDLE=$(mktemp -d)
trap 'rm -rf "$BUNDLE"' EXIT
info "下载 agent 文件和 chisel（从平台）"
for f in install.sh lab_agent.py lab-gpus lab-gpu.sh lab_migrate.py chisel; do
    curl -fsS "$PLATFORM/agent/$f" -o "$BUNDLE/$f" || die "下载 $f 失败"
done
chmod +x "$BUNDLE/chisel"

info "连接 $USER_NAME@$HOST（经你本机 aTrust），请按提示输入该账号密码"
ssh -o ConnectTimeout=15 "$USER_NAME@$HOST" "mkdir -p /tmp/lab-agent-install" || die "连不上：确认 aTrust 在线、账号正确"
scp -q "$BUNDLE"/* "$USER_NAME@$HOST:/tmp/lab-agent-install/" || die "上传失败"

info "在 GPU 服务器上安装（sudo 可能再要一次密码）"
ssh -t "$USER_NAME@$HOST" "sudo bash /tmp/lab-agent-install/install.sh \
    --server-url '$SERVER_URL' --token '$TOKEN' --legacy-user '$USER_NAME' \
    --chisel-port '$CHISEL_PORT' --chisel-secret '$CHISEL_SECRET' --chisel-allow '$CHISEL_ALLOW' \
    ${EXTRA[*]:-}"

info "完成。回到平台「管理 → GPU 服务器」确认隧道变成「已连接」、显卡开始上报。"
