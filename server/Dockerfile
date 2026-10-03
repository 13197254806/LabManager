# 构建上下文是仓库根目录（见 deploy/docker-compose.yml）
FROM python:3.12-slim

# 国内服务器构建时使用镜像源（在 .env 里可改；留空则用官方源）
ARG APT_MIRROR=mirrors.cloud.tencent.com
ARG PIP_INDEX_URL=https://mirrors.cloud.tencent.com/pypi/simple

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 AGENT_DIR=/app/agent
WORKDIR /app/server

# openssh-client / netcat：tunnel 服务经 aTrust 的 SOCKS5 代理 SSH 到 GPU 服务器
RUN if [ -n "$APT_MIRROR" ]; then sed -i "s@deb.debian.org@$APT_MIRROR@g" /etc/apt/sources.list.d/debian.sources; fi \
 && apt-get update \
 && apt-get install -y --no-install-recommends openssh-client netcat-openbsd curl \
 && rm -rf /var/lib/apt/lists/*

COPY server/requirements.txt .
RUN pip install ${PIP_INDEX_URL:+-i $PIP_INDEX_URL} -r requirements.txt

COPY server/app ./app
COPY agent /app/agent

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
