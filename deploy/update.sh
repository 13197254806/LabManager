#!/usr/bin/env bash
# 用当前目录的代码重新构建并重启平台（GitHub Actions 自动部署时调用，也可以手动执行）。
# 不会改动 .env 和 data/，不需要 sudo。首次部署请先手动执行一次 sudo bash setup-server.sh。
set -euo pipefail
cd "$(dirname "$0")"

[ -f .env ] || { echo "[错误] deploy/.env 不存在：请先在服务器上手动执行一次 sudo bash setup-server.sh"; exit 1; }

# 部署用户在 docker 组里就直接用，否则用免密 sudo
DOCKER=(docker)
docker info >/dev/null 2>&1 || DOCKER=(sudo -n docker)
DC=("${DOCKER[@]}" compose)

echo "==> 构建并重启（只重建代码有变化的容器，WebVPN 隧道不受影响）"
"${DC[@]}" up -d --build --remove-orphans

echo "==> 健康检查"
for i in $(seq 1 30); do
    if "${DC[@]}" exec -T web curl -fsS http://127.0.0.1:8000/healthz >/dev/null 2>&1; then
        echo "web 启动正常"
        break
    fi
    if [ "$i" -eq 30 ]; then
        echo "[错误] web 60 秒内没有启动成功，最近日志："
        "${DC[@]}" logs --tail 80 web
        exit 1
    fi
    sleep 2
done

# Caddyfile 有改动时生效（Caddy 不会自动重读配置文件）
"${DC[@]}" exec -T caddy caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null 2>&1 || true

"${DC[@]}" ps
# 清理构建留下的无用镜像层，避免磁盘越用越满
"${DOCKER[@]}" image prune -f >/dev/null 2>&1 || true
