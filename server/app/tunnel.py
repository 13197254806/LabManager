"""校园网隧道（运行在公网服务器上，只依赖标准库）。

GPU 服务器在校园内网，既不能被公网访问，也不能访问外网。公网服务器上的 atrust 容器
登录校园网 VPN 并提供 SOCKS5 代理；本程序经它 SSH 到每台 GPU 服务器，建立反向端口转发：

    GPU 服务器 127.0.0.1:18765  →  平台 web:8000          （Agent 同步、安装脚本下载）
    GPU 服务器 127.0.0.1:13128  →  本程序的 HTTP 代理      （GPU 服务器访问外网：pip / conda / git / apt）

所有连接都由公网服务器主动发起；GPU 服务器上只需要一个受限的 labtunnel 账号
（authorized_keys 里限制为只能开这两个反向端口，不能执行命令）。

用法：
    python -m app.tunnel run                 常驻运行（docker compose 的 tunnel 服务）
    python -m app.tunnel pubkey              打印隧道使用的公钥
    python -m app.tunnel status              查看各隧道状态
    python -m app.tunnel bootstrap gpu_01 --user ubuntu --token lab_xxx
                                             首次部署某台 GPU 服务器：用共享账号登录（手动输入密码），
                                             安装 Agent、创建 labtunnel 账号、配置上网代理

环境变量：
    TUNNEL_TARGETS        gpu_01=192.168.247.150:22,gpu_02=192.168.247.151:22（名称与平台上的服务器名一致）
    TUNNEL_SOCKS          atrust:1080（留空表示直连，用于本机测试）
    TUNNEL_USER           labtunnel
    TUNNEL_DIR            /data/tunnel（密钥、known_hosts、状态文件）
    PLATFORM_ADDR         web:8000
    REMOTE_PLATFORM_PORT  18765
    REMOTE_PROXY_PORT     13128
    PROXY_LISTEN          127.0.0.1:3128
    PROXY_ALLOWED_PORTS   80,443
    UPSTREAM_PROXY        可选：本机访问外网也需要代理时填 http://host:port
"""

import argparse
import asyncio
import ipaddress
import json
import logging
import os
import shlex
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime
from urllib.parse import urlsplit

log = logging.getLogger("tunnel")


def _env(name, default=""):
    return os.environ.get(name, default).strip()


TARGETS = {}
for item in _env("TUNNEL_TARGETS").split(","):
    if "=" in item:
        name, addr = item.split("=", 1)
        host, _, port = addr.strip().rpartition(":")
        TARGETS[name.strip()] = (host or addr.strip(), int(port) if host else 22)

SOCKS = _env("TUNNEL_SOCKS", "atrust:1080")
TUNNEL_USER = _env("TUNNEL_USER", "labtunnel")
TUNNEL_DIR = _env("TUNNEL_DIR", "/data/tunnel")
PLATFORM_ADDR = _env("PLATFORM_ADDR", "web:8000")
REMOTE_PLATFORM_PORT = int(_env("REMOTE_PLATFORM_PORT", "18765"))
REMOTE_PROXY_PORT = int(_env("REMOTE_PROXY_PORT", "13128"))
PROXY_LISTEN = _env("PROXY_LISTEN", "127.0.0.1:3128")
PROXY_ALLOWED_PORTS = {int(p) for p in _env("PROXY_ALLOWED_PORTS", "80,443").split(",") if p.strip()}
UPSTREAM_PROXY = _env("UPSTREAM_PROXY")
AGENT_DIR = _env("AGENT_DIR", "/app/agent")

KEY = os.path.join(TUNNEL_DIR, "id_ed25519")
KNOWN_HOSTS = os.path.join(TUNNEL_DIR, "known_hosts")
STATUS = os.path.join(TUNNEL_DIR, "status.json")


def now():
    return datetime.now().isoformat(timespec="seconds")


# ---------------- SSH 反向隧道 ----------------


def ensure_key():
    os.makedirs(TUNNEL_DIR, mode=0o700, exist_ok=True)
    if not os.path.exists(KEY):
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "lab-tunnel", "-f", KEY], check=True)
        log.info("已生成隧道密钥 %s.pub", KEY)
    with open(KEY + ".pub") as f:
        return f.read().strip()


def ssh_base(host, port):
    """连接某台 GPU 服务器的公共 ssh 参数（经 aTrust 的 SOCKS5 代理）。"""
    cmd = [
        "ssh", "-p", str(port),
        "-o", f"UserKnownHostsFile={KNOWN_HOSTS}",
        "-o", "StrictHostKeyChecking=accept-new",  # 第一次连接时记录主机指纹，之后变化会拒绝连接
        "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3",
        "-o", "ConnectTimeout=30",
    ]
    if SOCKS:
        cmd += ["-o", f"ProxyCommand=nc -X 5 -x {SOCKS} %h %p"]
    return cmd


_status_lock = threading.Lock()


def set_status(name, state, **extra):
    with _status_lock:
        try:
            with open(STATUS) as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        prev = data.get(name, {})
        entry = {"state": state, "host": "%s:%d" % TARGETS[name], "updated_at": now(),
                 "since": prev.get("since") if prev.get("state") == state else now()}
        entry.update(extra)
        data[name] = entry
        tmp = STATUS + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATUS)


def keep_tunnel(name, host, port):
    cmd = ssh_base(host, port) + [
        "-N", "-i", KEY, "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
        "-o", "ExitOnForwardFailure=yes", "-o", "LogLevel=ERROR",
        "-o", "PermitLocalCommand=yes", "-o", "LocalCommand=echo READY",
        "-R", f"127.0.0.1:{REMOTE_PLATFORM_PORT}:{PLATFORM_ADDR}",
        "-R", f"127.0.0.1:{REMOTE_PROXY_PORT}:{PROXY_LISTEN}",
        f"{TUNNEL_USER}@{host}",
    ]
    backoff = 5
    while True:
        set_status(name, "connecting")
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if p.stdout.readline().strip() == "READY":
            log.info("[%s] 隧道已建立", name)
            set_status(name, "connected")
            backoff = 5
        err = (p.stderr.read() or "").strip().splitlines()
        p.wait()
        msg = err[-1] if err else f"ssh 退出码 {p.returncode}"
        log.warning("[%s] 隧道断开：%s，%d 秒后重连", name, msg, backoff)
        set_status(name, "disconnected", error=msg)
        time.sleep(backoff)
        backoff = min(backoff * 2, 60)


# ---------------- HTTP 代理（给 GPU 服务器上外网） ----------------


async def _resolve_public(host, port):
    """解析目标地址，只允许公网地址（防止经代理访问公网服务器本机、云厂商元数据服务和其他内网）。"""
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    addrs = []
    for _family, _type, _proto, _canon, sockaddr in infos:
        ip = ipaddress.ip_address(sockaddr[0])
        if not ip.is_global:
            raise PermissionError(f"禁止访问非公网地址 {ip}")
        addrs.append(sockaddr[0])
    if not addrs:
        raise OSError("无法解析 " + host)
    return addrs[0]


async def _open(host, port):
    """连接目标；配置了上游代理时经上游 CONNECT。"""
    if UPSTREAM_PROXY:
        try:
            if not ipaddress.ip_address(host).is_global:
                raise PermissionError(f"禁止访问非公网地址 {host}")
        except ValueError:
            if host in ("localhost",) or host.endswith((".local", ".internal")):
                raise PermissionError("禁止访问 " + host)
        up = urlsplit(UPSTREAM_PROXY)
        r, w = await asyncio.open_connection(up.hostname, up.port or 80)
        w.write(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode())
        await w.drain()
        status = await r.readline()
        while (await r.readline()) not in (b"\r\n", b"\n", b""):
            pass
        if b" 200 " not in status:
            w.close()
            raise OSError("上游代理拒绝：" + status.decode(errors="replace").strip())
        return r, w
    ip = await _resolve_public(host, port)
    return await asyncio.open_connection(ip, port)


async def _pipe(r, w):
    try:
        while data := await r.read(65536):
            w.write(data)
            await w.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        try:
            w.close()
        except Exception:  # noqa: BLE001
            pass


async def _handle(cr, cw):
    peer = cw.get_extra_info("peername")
    try:
        line = await asyncio.wait_for(cr.readline(), 30)
        parts = line.decode("latin-1").split()
        if len(parts) != 3:
            return cw.close()
        method, target, version = parts
        headers = []
        while True:
            h = await asyncio.wait_for(cr.readline(), 30)
            if h in (b"\r\n", b"\n", b""):
                break
            headers.append(h)
        if method == "CONNECT":
            host, _, port = target.rpartition(":")
            host, port = host.strip("[]"), int(port)
        else:
            u = urlsplit(target)
            if u.scheme != "http" or not u.hostname:
                cw.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
                return cw.close()
            host, port = u.hostname, u.port or 80
        if port not in PROXY_ALLOWED_PORTS:
            raise PermissionError(f"只允许访问端口 {sorted(PROXY_ALLOWED_PORTS)}")
        ur, uw = await _open(host, port)
        if method == "CONNECT":
            cw.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        else:
            u = urlsplit(target)
            path = (u.path or "/") + (f"?{u.query}" if u.query else "")
            uw.write(f"{method} {path} {version}\r\n".encode("latin-1"))
            for h in headers:
                if not h.lower().startswith((b"proxy-", b"connection:", b"keep-alive:")):
                    uw.write(h)
            uw.write(b"Connection: close\r\n\r\n")
        log.info("代理 %s %s:%d（来自 %s）", method, host, port, peer)
        await asyncio.gather(_pipe(cr, uw), _pipe(ur, cw))
    except PermissionError as e:
        cw.write(f"HTTP/1.1 403 Forbidden\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n{e}\n".encode())
        cw.close()
    except (OSError, asyncio.TimeoutError, ValueError) as e:
        try:
            cw.write(f"HTTP/1.1 502 Bad Gateway\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n{e}\n".encode())
            cw.close()
        except Exception:  # noqa: BLE001
            pass


async def serve_proxy():
    host, _, port = PROXY_LISTEN.rpartition(":")
    server = await asyncio.start_server(_handle, host, int(port))
    log.info("HTTP 代理监听 %s（允许端口 %s）", PROXY_LISTEN, sorted(PROXY_ALLOWED_PORTS))
    async with server:
        await server.serve_forever()


# ---------------- 子命令 ----------------


def cmd_run(_args):
    ensure_key()
    if not TARGETS:
        log.warning("TUNNEL_TARGETS 为空：只运行代理，不建立隧道")
    for name, (host, port) in TARGETS.items():
        threading.Thread(target=keep_tunnel, args=(name, host, port), daemon=True, name=name).start()
    asyncio.run(serve_proxy())


def cmd_pubkey(_args):
    print(ensure_key())


def cmd_status(_args):
    try:
        with open(STATUS) as f:
            print(json.dumps(json.load(f), ensure_ascii=False, indent=2))
    except OSError:
        print("还没有状态（tunnel 服务是否在运行？）")


def cmd_bootstrap(args):
    """首次部署：用共享账号（交互输入密码）登录 GPU 服务器，上传并运行安装脚本。"""
    if args.name not in TARGETS:
        sys.exit(f"未知服务器 {args.name}，请先在 TUNNEL_TARGETS 里配置（当前：{', '.join(TARGETS) or '空'}）")
    host, port = TARGETS[args.name]
    pub = ensure_key()
    ctl = f"/tmp/lab-bootstrap-{args.name}.sock"
    base = ssh_base(host, port) + ["-o", f"ControlPath={ctl}", "-o", "ControlMaster=auto",
                                   "-o", "ControlPersist=300", "-l", args.user]
    print(f"==> 连接 {args.user}@{host}:{port}（经 {SOCKS or '直连'}），请按提示输入 {args.user} 的密码")
    if subprocess.run(base + [host, "mkdir -p /tmp/lab-agent-install && echo 已连接"]).returncode != 0:
        sys.exit("连接失败：请确认 atrust 容器已登录（docker compose logs atrust），以及账号密码正确")

    print("==> 上传安装文件")
    files = [f for f in ("install.sh", "lab_agent.py", "lab-gpus", "lab-gpu.sh", "lab_migrate.py")
             if os.path.exists(os.path.join(AGENT_DIR, f))]
    tar = subprocess.Popen(["tar", "cf", "-", "-C", AGENT_DIR] + files, stdout=subprocess.PIPE)
    up = subprocess.run(base + [host, "tar xf - -C /tmp/lab-agent-install"], stdin=tar.stdout)
    tar.wait()
    if up.returncode != 0:
        sys.exit("上传失败")

    install = [
        "sudo", "bash", "/tmp/lab-agent-install/install.sh",
        "--server-url", f"http://127.0.0.1:{REMOTE_PLATFORM_PORT}",
        "--token", args.token,
        "--legacy-user", args.legacy or args.user,
        "--tunnel-key", pub,
        "--tunnel-ports", f"{REMOTE_PLATFORM_PORT},{REMOTE_PROXY_PORT}",
    ]
    if not args.no_proxy:
        install += ["--internet-proxy", f"http://127.0.0.1:{REMOTE_PROXY_PORT}"]
    install += args.extra
    print("==> 在 GPU 服务器上安装（sudo 可能会再要一次密码）")
    rc = subprocess.run(base + ["-t", host, " ".join(shlex.quote(x) for x in install)]).returncode
    subprocess.run(base + ["-O", "exit", host], stderr=subprocess.DEVNULL)
    sys.exit(rc)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description="校园网隧道")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run")
    sub.add_parser("pubkey")
    sub.add_parser("status")
    b = sub.add_parser("bootstrap", help="首次部署一台 GPU 服务器")
    b.add_argument("name", help="TUNNEL_TARGETS 里的名称，如 gpu_01")
    b.add_argument("--user", required=True, help="用于登录的共享账号（需要 sudo），如 ubuntu")
    b.add_argument("--token", required=True, help="平台「管理 → GPU 服务器」里得到的 Agent Token")
    b.add_argument("--legacy", help="旧共享账号，默认同 --user")
    b.add_argument("--no-proxy", action="store_true", help="不给 GPU 服务器配置上外网代理")
    # 其余未识别的参数（如 --migration-roots /data）原样传给 install.sh
    args, extra = ap.parse_known_args()
    if extra and args.cmd != "bootstrap":
        ap.error("未知参数：" + " ".join(extra))
    args.extra = extra
    {"run": cmd_run, "pubkey": cmd_pubkey, "status": cmd_status, "bootstrap": cmd_bootstrap}[args.cmd](args)


if __name__ == "__main__":
    main()
