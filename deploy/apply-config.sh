#!/usr/bin/env bash
# .env 保存后由 systemd（lab-config.path）自动调用：校验配置，然后只重建配置有变化的容器。
# 也可以手动执行：bash apply-config.sh（不需要 sudo）
set -uo pipefail
cd "$(dirname "$0")"
mkdir -p data
LOG=data/config-apply.log

log() { echo "$(date '+%F %T') $*" | tee -a "$LOG"; }

DOCKER=(docker)
docker info >/dev/null 2>&1 || DOCKER=(sudo -n docker)
DC=("${DOCKER[@]}" compose)

chmod 600 .env 2>/dev/null || true

# 1. 校验：语法错误、缺少必填项时不应用，保持当前运行状态
if ! err=$("${DC[@]}" config -q 2>&1); then
    log "❌ 配置有误，未生效（当前服务保持不变）：$err"
    exit 1
fi

# 1.5 家宽代理配置：从 .env 的 WEBVPN_PROXY_NODES 生成 mihomo 配置，变化了就重启 proxy
PROXY_CHANGED=
if grep -q '^COMPOSE_PROFILES=.*campus' .env 2>/dev/null; then
    if grep -q '^WEBVPN_PROXY_NODES=.\+' .env 2>/dev/null; then
        mkdir -p data/proxy
        if python3 gen-proxy-config.py .env data/proxy/config.yaml.new 2>>"$LOG"; then
            if ! cmp -s data/proxy/config.yaml.new data/proxy/config.yaml 2>/dev/null; then
                mv data/proxy/config.yaml.new data/proxy/config.yaml
                PROXY_CHANGED=1
            else
                rm -f data/proxy/config.yaml.new
            fi
        fi
    else
        log "⚠️ 未配置 WEBVPN_PROXY_NODES：校园网隧道需要家宽代理才能工作，请在 .env 里填 ss:// 节点"
    fi
fi

# 2. 只重建配置有变化的容器（没变化的不动，比如只改飞书配置时校园网连接不会断）
log "检测到配置变化，开始应用"
out=$("${DC[@]}" up -d --remove-orphans 2>&1)
status=$?
changed=$(echo "$out" | grep -oE "Container [^ ]+ +(Recreated|Started|Created)" | awk '{print $2}' | sort -u | tr '\n' ' ')
if [ $status -ne 0 ]; then
    log "❌ 应用失败：$(echo "$out" | tail -5 | tr '\n' ' ')"
    exit 1
fi
"${DC[@]}" exec -T caddy caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null 2>&1 || true
# 家宽节点变了：重启 proxy 让 mihomo 读新配置（mihomo 不会自动热加载挂载的配置文件）
[ -n "$PROXY_CHANGED" ] && { "${DC[@]}" restart proxy >/dev/null 2>&1 && log "家宽代理节点已更新，proxy 已重启"; }

# 3. 健康检查
for i in $(seq 1 30); do
    if "${DC[@]}" exec -T web curl -fsS http://127.0.0.1:8000/healthz >/dev/null 2>&1; then
        log "✅ 已生效。重建的容器：${changed:-无（配置没有影响任何容器）}"
        exit 0
    fi
    sleep 2
done
log "❌ web 没有正常启动，请查看：docker compose logs --tail 50 web"
exit 1
