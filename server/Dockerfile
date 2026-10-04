# 构建上下文是仓库根目录（见 deploy/docker-compose.yml）
FROM python:3.12-slim

# 国内服务器构建时使用镜像源（在 .env 里可改；留空则用官方源）
ARG APT_MIRROR=mirrors.cloud.tencent.com
ARG PIP_INDEX_URL=https://mirrors.cloud.tencent.com/pypi/simple

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 AGENT_DIR=/app/agent
WORKDIR /app/server

# curl：healthcheck 和构建时下载 chisel
RUN if [ -n "$APT_MIRROR" ]; then sed -i "s@deb.debian.org@$APT_MIRROR@g" /etc/apt/sources.list.d/debian.sources; fi \
 && apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

COPY server/requirements.txt .
RUN pip install ${PIP_INDEX_URL:+-i $PIP_INDEX_URL} -r requirements.txt

COPY server/app ./app
COPY agent /app/agent

# chisel：tunnel 服务（client）经 WebVPN 连 GPU 建隧道；同一份二进制也通过 /agent/chisel 提供给 GPU（server）。
# GPU 是 x86_64，公网服务器也是 amd64，用同一个 linux_amd64 二进制。
# 若仓库 agent/ 里已放了 chisel（见 agent/chisel，提前下载好可避免构建时访问 GitHub），直接用；否则下载。
ARG CHISEL_VERSION=1.10.1
RUN set -eux; \
    if [ ! -f /app/agent/chisel ]; then \
      url="https://github.com/jpillora/chisel/releases/download/v${CHISEL_VERSION}/chisel_${CHISEL_VERSION}_linux_amd64.gz"; \
      for i in 1 2 3; do curl -fL --connect-timeout 20 "$url" -o /tmp/chisel.gz && break || sleep 3; done; \
      gzip -d /tmp/chisel.gz; install -m 755 /tmp/chisel /app/agent/chisel; rm -f /tmp/chisel; \
    fi; \
    chmod 755 /app/agent/chisel; \
    ln -sf /app/agent/chisel /usr/local/bin/chisel; \
    chisel --version

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
