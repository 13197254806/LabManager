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
# 日常管理用的普通用户（执行 sudo 的人，或项目目录的所有者）：之后改配置、看日志、执行 docker 都不再需要 sudo
OWNER=${SUDO_USER:-$(stat -c %U .)}
[ "$OWNER" != root ] || OWNER=$(stat -c %U .)

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
if [ "$OWNER" != root ] && ! id -nG "$OWNER" | tr ' ' '\n' | grep -qx docker; then
    usermod -aG docker "$OWNER"
    info "已把 $OWNER 加入 docker 组：重新登录后，docker 命令不需要 sudo"
fi

# ---------- 2. 配置文件（全部配置都在 .env 一个文件里） ----------
gen() { python3 -c 'import secrets, sys; print(secrets.token_urlsafe(int(sys.argv[1])))' "$1"; }
if [ ! -f .env ]; then
    cp .env.example .env
    chmod 600 .env
    NEW_CONFIG=1
fi
chmod 600 .env
mkdir -p data
chown "$OWNER:" .env .env.bak-* data data/config-apply.log 2>/dev/null || true
grep -qE '^SECRET_KEY=(change-me)?$' .env && sed -i "s|^SECRET_KEY=.*|SECRET_KEY=$(gen 48)|" .env
# chisel 认证口令（公网服务器和各 GPU 共用）：没填就自动生成一个
if grep -qE '^CHISEL_SECRET=(change-me)?$' .env; then
    sed -i "s|^CHISEL_SECRET=.*|CHISEL_SECRET=$(gen 24)|" .env
elif ! grep -q '^CHISEL_SECRET=' .env; then
    echo "CHISEL_SECRET=$(gen 24)" >> .env
fi

if [ -n "${NEW_CONFIG:-}" ]; then
    warn "已生成配置文件，请填写后重新执行本脚本（之后再改配置，保存即自动生效）："
    echo "      vim $(pwd)/.env"
    exit 0
fi
# 不 source .env（密码里可能有空格、$ 等字符），只读取需要的几项
envget() { grep -E "^$1=" .env | tail -1 | cut -d= -f2- | sed -e "s/^['\"]//" -e "s/['\"]\$//"; }
COMPOSE_PROFILES=$(envget COMPOSE_PROFILES)
BASE_URL=$(envget BASE_URL)
mkdir -p data/tunnel data/proxy
chown -R "$OWNER:" data/tunnel data/proxy 2>/dev/null || true

# 家宽代理配置（WebVPN 封云 IP，隧道经它出口）
if [[ ",${COMPOSE_PROFILES:-}," == *",campus,"* ]]; then
    if grep -q '^WEBVPN_PROXY_NODES=.\+' .env; then
        python3 gen-proxy-config.py .env data/proxy/config.yaml && info "已生成家宽代理配置 data/proxy/config.yaml"
    else
        warn "WEBVPN_PROXY_NODES 还没填：校园网隧道需要家宽代理才能用（学校封了云服务器 IP）。请在 .env 里填 ss:// 家宽节点"
    fi
fi

# ---------- 3. 启动 ----------
info "构建并启动服务（首次构建会下载 chisel，可能要等一会）"
docker compose up -d --build
docker compose ps

# ---------- 5. 改完 .env 自动生效 ----------
DEPLOY_DIR=$(pwd)
cat > /etc/systemd/system/lab-config.path <<UNIT
[Unit]
Description=Lab platform: watch $DEPLOY_DIR/.env

[Path]
PathChanged=$DEPLOY_DIR/.env
Unit=lab-config.service

[Install]
WantedBy=multi-user.target
UNIT
cat > /etc/systemd/system/lab-config.service <<UNIT
[Unit]
Description=Lab platform: apply .env changes

[Service]
Type=oneshot
# 以普通用户身份执行（经 docker 组操作容器），日志文件也归该用户
User=$OWNER
SupplementaryGroups=docker
# 等编辑器写完
ExecStartPre=/bin/sleep 2
ExecStart=/bin/bash $DEPLOY_DIR/apply-config.sh
UNIT
systemctl daemon-reload
systemctl enable --now lab-config.path >/dev/null 2>&1
info "已开启：保存 .env 后自动生效（日志：$DEPLOY_DIR/data/config-apply.log）"

cat <<EOF

$(info "部署完成")
接下来：
  1. 浏览器打开 ${BASE_URL:-https://你的域名}，用飞书登录（第一个登录的人成为管理员）
     （腾讯云安全组需放行 80、443 端口；域名需已备案并解析到本机）
EOF
if [[ ",${COMPOSE_PROFILES:-}," == *",campus,"* ]]; then
cat <<EOF
  2. 查看 WebVPN 是否登录成功：  docker compose logs -f tunnel     （看到"WebVPN 登录成功"即可）
     滑块验证码会自动识别，无需人工。只有账号或密码错误才会停下（改好 .env 保存即自动重试）。
  3. 在平台「管理 → GPU 服务器」添加 gpu_01 等服务器（填内网地址，名称与 .env 的 TUNNEL_TARGETS 一致），
     点「显示安装命令」拿到命令，到你自己的 Mac 上（有 aTrust）执行：
       bash tools/add-gpu.sh ${BASE_URL:-http://你的平台地址} <Token> --user ubuntu
     （会提示输入 GPU 服务器上 ubuntu 账号的密码，由你自己输入）
  4. 查看隧道状态：  docker compose exec tunnel python -m app.tunnel status
EOF
fi
