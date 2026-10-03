#!/usr/bin/env bash
# 在公网服务器（腾讯云 Ubuntu）上一键部署平台 + 校园网隧道。可重复执行。
#
#   cd lab/deploy && sudo bash setup-server.sh
#
# 第一次执行会生成 .env 并退出，编辑好再执行一次即可。
set -euo pipefail
cd "$(dirname "$0")"

info() { echo -e "\033[32m==>\033[0m $*"; }
warn() { echo -e "\033[33m[注意]\033[0m $*"; }
die() { echo -e "\033[31m[错误]\033[0m $*" >&2; exit 1; }
[ "$(id -u)" -eq 0 ] || die "请用 sudo 执行"

# ---------- 1. Docker ----------
if ! command -v docker >/dev/null; then
    info "安装 Docker"
    apt-get update
    apt-get install -y docker.io
fi
if ! docker compose version >/dev/null 2>&1; then
    apt-get install -y docker-compose-v2 || apt-get install -y docker-compose-plugin \
        || die "安装 docker compose 失败，请参考 https://docs.docker.com/compose/install/"
fi
# 腾讯云内网的 Docker Hub 镜像加速（拉取 python / caddy 等基础镜像）
if [ ! -f /etc/docker/daemon.json ]; then
    echo '{"registry-mirrors": ["https://mirror.ccs.tencentyun.com"]}' > /etc/docker/daemon.json
    systemctl restart docker
    info "已配置 Docker 镜像加速 mirror.ccs.tencentyun.com"
fi
systemctl enable --now docker >/dev/null 2>&1 || true

# ---------- 2. 配置文件 ----------
if [ ! -f .env ]; then
    cp .env.example .env
    sed -i "s|^SECRET_KEY=.*|SECRET_KEY=$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')|" .env
    chmod 600 .env
    NEW_CONFIG=1
fi
if [ ! -f atrust.env ]; then
    cp atrust.env.example atrust.env
    sed -i "s|^PASSWORD=.*|PASSWORD=$(python3 -c 'import secrets; print(secrets.token_urlsafe(9))')|" atrust.env
    chmod 600 atrust.env
    NEW_CONFIG=1
fi
if [ -n "${NEW_CONFIG:-}" ]; then
    warn "已生成配置文件，请填写后重新执行本脚本："
    echo "      vim $(pwd)/.env          # 域名、飞书应用、GPU 服务器列表"
    echo "      vim $(pwd)/atrust.env    # 校园网 aTrust 账号密码"
    exit 0
fi
chmod 600 atrust.env
chmod 600 .env
# 不 source .env（密码里可能有空格、$ 等字符），只读取需要的几项
envget() { grep -E "^$1=" .env | tail -1 | cut -d= -f2- | sed -e "s/^['\"]//" -e "s/['\"]\$//"; }
COMPOSE_PROFILES=$(envget COMPOSE_PROFILES)
ATRUST_IMAGE=$(envget ATRUST_IMAGE); ATRUST_IMAGE=${ATRUST_IMAGE:-kenvix/docker-atrust-autologin:latest}
BASE_URL=$(envget BASE_URL)
mkdir -p data/atrust data/tunnel

# ---------- 3. aTrust 镜像 ----------
if [[ ",${COMPOSE_PROFILES:-}," == *",campus,"* ]] && ! docker image inspect "$ATRUST_IMAGE" >/dev/null 2>&1; then
    info "拉取 aTrust 镜像 $ATRUST_IMAGE"
    if ! timeout 600 docker pull "$ATRUST_IMAGE"; then
        ARCH=$(dpkg --print-architecture)   # amd64 / arm64
        URL="https://modelscope.cn/models/kenvix/aTrustLoginRepo/resolve/master/docker-atrust-autologin-$ARCH.tar.xz"
        info "Docker Hub 拉取失败，改从 ModelScope 国内地址下载：$URL"
        curl -fL "$URL" | xz -dc | docker load
        docker tag "kenvix/docker-atrust-autologin:$ARCH" "$ATRUST_IMAGE"
    fi
fi

# ---------- 4. 启动 ----------
info "构建并启动服务"
docker compose up -d --build
docker compose ps

cat <<EOF

$(info "部署完成")
接下来：
  1. 浏览器打开 ${BASE_URL:-https://你的域名}，用飞书登录（第一个登录的人成为管理员）
     （腾讯云安全组需放行 80、443 端口；域名需已备案并解析到本机）
EOF
if [[ ",${COMPOSE_PROFILES:-}," == *",campus,"* ]]; then
cat <<EOF
  2. 查看 aTrust 是否登录成功：  docker compose logs -f atrust     （看到 Login Success 即可）
     如果卡在图形验证码：在自己电脑上执行  ssh -L 5901:127.0.0.1:5901 ubuntu@本服务器
     然后用 VNC 客户端连接 127.0.0.1:5901（密码见 atrust.env 的 PASSWORD），在浏览器窗口里输入验证码登录，
     再在服务器上执行  docker attach lab-atrust  按一次回车，按 Ctrl-P Ctrl-Q 退出
  3. 在平台「管理 → GPU 服务器」添加 gpu_01 等服务器（名称与 .env 的 TUNNEL_TARGETS 一致），拿到 Token 后执行：
       docker compose exec tunnel python -m app.tunnel bootstrap gpu_01 --user ubuntu --token <Token>
     （会提示输入 GPU 服务器上 ubuntu 账号的密码，由你自己输入）
  4. 查看隧道状态：  docker compose exec tunnel python -m app.tunnel status
EOF
fi
