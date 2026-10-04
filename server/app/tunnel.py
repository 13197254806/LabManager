"""校园网隧道（运行在公网服务器上，只依赖标准库 + 同目录的 webvpn.py）。

GPU 服务器在校园内网，不能被公网访问。本程序登录学校 WebVPN（支持 WebSocket），
经它用 chisel 连到每台 GPU 服务器上的 chisel 服务端，建立一条反向隧道：

    GPU 服务器 127.0.0.1:18765  →  平台 web:8000     （Agent 回连平台：上报显卡、取预约/账号）

云服务器只做"管理"，不接管 GPU 的外网——GPU 上外网由同学各自挂代理。

连接路径：本程序(chisel client) → [家宽代理] → https://webvpn.whut.edu.cn/ws-<端口>/<主机编码>/ → GPU(chisel server)。
学校 WebVPN 封了云服务器 IP，所以经家宽代理(mihomo，proxy 服务)出口访问，让 WebVPN 看到的是家宽 IP。
WebVPN 会话 Cookie 过期（被挤或超时）时自动重新登录（含滑块验证码识别），再重连 chisel。

用法：
    python -m app.tunnel run         常驻运行（docker compose 的 tunnel 服务）
    python -m app.tunnel status      查看各隧道状态
    python -m app.tunnel login-test  只测一次 WebVPN 登录（排查用）
    python -m app.tunnel url NAME    打印某台 GPU 的 WebVPN 连接地址（排查用）

环境变量：
    TUNNEL_TARGETS        gpu_01=192.168.247.150:9000,gpu_02=192.168.247.151:9000（名称与平台上一致，端口是 GPU 上 chisel 监听端口）
    WEBVPN_PORTAL         https://webvpn.whut.edu.cn
    WEBVPN_USER / WEBVPN_PASSWORD
    WEBVPN_PROXY          家宽代理地址，如 http://proxy:7891（WebVPN 封云 IP，必须经它出口）
    CHISEL_SECRET         chisel 认证口令（client 和每台 GPU 的 server 共用），setup-server.sh 自动生成
    CHISEL_FINGERPRINT    可选：GPU chisel 服务端公钥指纹，填了就固定校验，防中间人
    CHISEL_BIN            chisel 可执行文件（默认 chisel）
    TUNNEL_DIR            /data/tunnel（状态文件）
    PLATFORM_ADDR         web:8000
    REMOTE_PLATFORM_PORT  18765
    WEBVPN_CHECK_INTERVAL 校验会话间隔秒数（默认 60）
    WEBVPN_MAX_ATTEMPTS_PER_HOUR  每小时最多登录尝试次数（默认 4，硬限速防封号）
"""

import argparse
import json
import logging
import os
import subprocess
import sys
import threading
import time
from datetime import datetime

from app import webvpn

log = logging.getLogger("tunnel")


def _env(name, default=""):
    return os.environ.get(name, default).strip()


CHISEL_PORT = int(_env("CHISEL_PORT", "9000"))

# GPU 服务器列表从平台数据库读取（在平台「管理 → GPU 服务器」里添加即可，无需手动维护）。
# 读库失败时退回 .env 的 TUNNEL_TARGETS（可选）。TARGETS: name -> (host, port)
TARGETS = {}


def _env_targets():
    t = {}
    for item in _env("TUNNEL_TARGETS").split(","):
        if "=" in item:
            name, addr = item.split("=", 1)
            host, _, port = addr.strip().rpartition(":")
            t[name.strip()] = (host or addr.strip(), int(port) if host else CHISEL_PORT)
    return t


def refresh_targets():
    """从平台数据库读取填了内网地址的 GPU 服务器。无需手动维护 TUNNEL_TARGETS。"""
    global TARGETS
    try:
        from sqlalchemy import select
        from app.db import SessionLocal
        from app.models import Server
        with SessionLocal() as db:
            rows = db.scalars(select(Server)).all()
            TARGETS = {s.name: (s.host.strip(), CHISEL_PORT) for s in rows if (s.host or "").strip()}
    except Exception as e:  # noqa: BLE001
        env_t = _env_targets()
        if env_t:
            TARGETS = env_t
        log.debug("读平台服务器列表失败，用 .env TUNNEL_TARGETS 回退：%s", e)
    return TARGETS


WEBVPN_PORTAL = _env("WEBVPN_PORTAL", "https://webvpn.whut.edu.cn")
WEBVPN_USER = _env("WEBVPN_USER")
WEBVPN_PASSWORD = _env("WEBVPN_PASSWORD")
# 学校 WebVPN 封了云服务器 IP：经家宽代理出口访问（mihomo，见 proxy 服务）。如 http://proxy:7891
WEBVPN_PROXY = _env("WEBVPN_PROXY")
CHISEL_SECRET = _env("CHISEL_SECRET")
CHISEL_FINGERPRINT = _env("CHISEL_FINGERPRINT")
CHISEL_BIN = _env("CHISEL_BIN", "chisel")
CHISEL_USER = _env("CHISEL_USER", "lab")

TUNNEL_DIR = _env("TUNNEL_DIR", "/data/tunnel")
PLATFORM_ADDR = _env("PLATFORM_ADDR", "web:8000")
REMOTE_PLATFORM_PORT = int(_env("REMOTE_PLATFORM_PORT", "18765"))
CHECK_INTERVAL = int(_env("WEBVPN_CHECK_INTERVAL", "60"))

STATUS = os.path.join(TUNNEL_DIR, "status.json")


def now():
    return datetime.now().isoformat(timespec="seconds")


def webvpn_url(host, port):
    """GPU 服务器在 WebVPN 里的 WebSocket 地址：/ws-<端口>/<主机名编码>/。"""
    base = WEBVPN_PORTAL.rstrip("/")
    return f"{base}/ws-{port}/{webvpn.wrdvpn_encrypt(host, 'wrdvpnisthebest!')}/"


# ---------------- 状态文件 ----------------
_status_lock = threading.Lock()


def set_status(name, state, **extra):
    with _status_lock:
        try:
            with open(STATUS) as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        prev = data.get(name, {})
        entry = {"state": state, "updated_at": now(),
                 "since": prev.get("since") if prev.get("state") == state else now()}
        if name in TARGETS:
            entry["host"] = "%s:%d" % TARGETS[name]
        entry.update(extra)
        data[name] = entry
        tmp = STATUS + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATUS)


def set_login_status(state, **extra):
    set_status("_webvpn", state, **extra)


def prune_status():
    """删掉状态文件里已不在目标列表的服务器（比如平台里删了、或从 .env 改用数据库后的旧条目）。"""
    with _status_lock:
        try:
            with open(STATUS) as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        keep = set(TARGETS) | {"_webvpn"}
        new = {k: v for k, v in data.items() if k in keep}
        if new != data:
            tmp = STATUS + ".tmp"
            with open(tmp, "w") as f:
                json.dump(new, f, ensure_ascii=False, indent=2)
            os.replace(tmp, STATUS)


# ---------------- WebVPN 会话 + chisel 隧道管理 ----------------
ATTEMPT_LOG = os.path.join(TUNNEL_DIR, "webvpn_attempts.json")
# 每小时最多尝试这么多次 WebVPN 登录（持久化、跨容器重启生效）。
# 硬上限，防止任何故障（崩溃重启、反复失败）把 WebVPN 登录刷爆导致 IP 被封。
MAX_ATTEMPTS_PER_HOUR = int(_env("WEBVPN_MAX_ATTEMPTS_PER_HOUR", "4"))


class Tunnels:
    def __init__(self):
        self.client = webvpn.WebVPNClient(WEBVPN_PORTAL)
        self.procs = {}       # name -> Popen(chisel client)
        self.cookie = ""
        self.stopping = False

    def _throttle(self):
        """登录前的硬限速：读持久化的尝试时间戳，一小时内超过上限就等到名额释放再试。
        跨容器重启有效——即使崩溃重启，WebVPN 登录频率也绝不会超过上限。"""
        while not self.stopping:
            now_ts = time.time()
            try:
                with open(ATTEMPT_LOG) as f:
                    stamps = [t for t in json.load(f) if now_ts - t < 3600]
            except (OSError, ValueError):
                stamps = []
            if len(stamps) < MAX_ATTEMPTS_PER_HOUR:
                stamps.append(now_ts)
                try:
                    os.makedirs(TUNNEL_DIR, exist_ok=True)
                    with open(ATTEMPT_LOG, "w") as f:
                        json.dump(stamps[-MAX_ATTEMPTS_PER_HOUR:], f)
                except OSError:
                    pass
                return
            wait = int(3600 - (now_ts - min(stamps))) + 5
            log.warning("已达 WebVPN 登录限速（每小时 %d 次），等待 %d 秒后再试，避免 IP 被封",
                        MAX_ATTEMPTS_PER_HOUR, wait)
            set_login_status("throttled", error=f"达到登录限速，等待 {wait} 秒")
            self._sleep(wait)

    # --- WebVPN 登录 ---
    def login(self):
        # 失败时退避较长（首次就 60 秒），避免频繁重试触发 WebVPN 的"多次失败自动封禁"
        backoff = 60
        while not self.stopping:
            try:
                self._throttle()  # 硬限速：每小时最多 MAX_ATTEMPTS_PER_HOUR 次，绝不刷爆 WebVPN
                if self.stopping:
                    return False
                set_login_status("logging_in")
                self.client = webvpn.WebVPNClient(WEBVPN_PORTAL, proxy=WEBVPN_PROXY)
                self.client.login(WEBVPN_USER, WEBVPN_PASSWORD)
                name, _ = self.client.ticket_cookie()
                self.cookie = self.client.cookie_header()
                log.info("WebVPN 登录成功（%s）", name)
                set_login_status("logged_in")
                return True
            except webvpn.WrongCredentials as e:
                log.error("WebVPN 账号或密码错误，停止自动登录（避免锁定）：%s", e)
                set_login_status("bad_credentials", error="账号或密码错误，请更新 deploy/.env 里的 WEBVPN_USER/WEBVPN_PASSWORD")
                while not self.stopping:  # 不可重试：等 .env 改好、容器被重建
                    time.sleep(3600)
                return False
            except webvpn.Banned as e:
                log.warning("%s；等待 6 分钟后再试", e)
                set_login_status("banned", error=str(e))
                self._sleep(360)
            except Exception as e:  # noqa: BLE001
                log.warning("WebVPN 登录失败：%s，%d 秒后重试", e, backoff)
                set_login_status("login_failed", error=str(e))
                self._sleep(backoff)
                backoff = min(backoff * 2, 600)
        return False

    def _sleep(self, secs):
        for _ in range(secs):
            if self.stopping:
                return
            time.sleep(1)

    # --- chisel client ---
    def chisel_cmd(self, host, port):
        # 无限重试：短暂网络抖动、GPU 重启由 chisel 自愈，不触发重新登录；
        # 会话真正过期（Cookie 失效）由主循环的 logged_in() 轮询发现后重登并换新 Cookie 重连。
        cmd = [CHISEL_BIN, "client",
               "--auth", f"{CHISEL_USER}:{CHISEL_SECRET}",
               "--keepalive", "25s", "--max-retry-count", "-1", "--max-retry-interval", "30s",
               "--header", f"Cookie: {self.cookie}"]
        if WEBVPN_PROXY:
            cmd += ["--proxy", WEBVPN_PROXY]   # 经家宽代理连 WebVPN（封了云 IP）
        if CHISEL_FINGERPRINT:
            cmd += ["--fingerprint", CHISEL_FINGERPRINT]
        # 只建一条反向隧道：GPU 上的 Agent 回连平台。GPU 上外网由同学各自挂代理，云服务器不接管
        cmd += [webvpn_url(host, port),
                f"R:127.0.0.1:{REMOTE_PLATFORM_PORT}:{PLATFORM_ADDR}"]
        return cmd

    def start_one(self, name, host, port):
        self.stop_one(name)
        set_status(name, "connecting")
        p = subprocess.Popen(self.chisel_cmd(host, port),
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.procs[name] = p
        threading.Thread(target=self._watch, args=(name, p), daemon=True).start()

    def _watch(self, name, p):
        connected = False
        for line in iter(p.stdout.readline, ""):
            line = line.strip()
            if "Connected" in line:
                connected = True
                log.info("[%s] 隧道已建立", name)
                set_status(name, "connected")
            elif "handshake" in line or "Connection error" in line or "ERR" in line:
                set_status(name, "error", error=line[-200:])
        p.wait()
        if not self.stopping and self.procs.get(name) is p:
            set_status(name, "disconnected", error=("会话可能已过期" if not connected else "进程退出"))

    def stop_one(self, name):
        p = self.procs.pop(name, None)
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()

    def start_all(self):
        for name, (host, port) in TARGETS.items():
            self.start_one(name, host, port)

    def stop_all(self):
        for name in list(self.procs):
            self.stop_one(name)

    def restart_all(self):
        self.stop_all()
        self.start_all()

    # --- 主循环：登录 → 起隧道 → 定期校验会话，掉线/过期就重登并重连 ---
    def run(self):
        if not CHISEL_SECRET:
            log.error("未设置 CHISEL_SECRET，无法建立隧道（请运行 setup-server.sh 生成）")
            set_login_status("misconfigured", error="缺少 CHISEL_SECRET")
            return
        if not (WEBVPN_USER and WEBVPN_PASSWORD):
            log.error("未设置 WEBVPN_USER / WEBVPN_PASSWORD")
            set_login_status("misconfigured", error="缺少 WEBVPN_USER/WEBVPN_PASSWORD")
            return
        refresh_targets()
        prune_status()  # 启动即清理旧条目（即使登录被限速也先清）
        if not self.login():
            return
        refresh_targets()
        self.start_all()
        while not self.stopping:
            time.sleep(CHECK_INTERVAL)
            refresh_targets()  # 平台里新增/删除服务器后自动生效
            if not self.client.logged_in():
                # WebVPN 会话失效（被挤或超时）：重新登录，换新 Cookie 重连全部
                log.info("WebVPN 会话失效，重新登录")
                self.stop_all()
                if self.login():
                    self.start_all()
                continue
            # 平台里已删除的服务器：停掉隧道并清理状态
            for name in list(self.procs):
                if name not in TARGETS:
                    log.info("[%s] 已从平台移除，停止隧道", name)
                    self.stop_one(name)
            prune_status()
            # 新增的、或进程退出的（崩溃/GPU 重启）：启动/重启
            for name, (host, port) in TARGETS.items():
                p = self.procs.get(name)
                if p is None or p.poll() is not None:
                    self.start_one(name, host, port)


# ---------------- 子命令 ----------------
def cmd_run(_args):
    os.makedirs(TUNNEL_DIR, exist_ok=True)
    # GPU 列表从平台数据库动态读取：没有服务器时登录 WebVPN 空转，等平台里添加后自动连上
    Tunnels().run()


def cmd_status(_args):
    try:
        with open(STATUS) as f:
            print(json.dumps(json.load(f), ensure_ascii=False, indent=2))
    except OSError:
        print("还没有状态（tunnel 服务是否在运行？）")


def cmd_url(args):
    refresh_targets()
    if args.name not in TARGETS:
        sys.exit(f"未知服务器 {args.name}（当前：{', '.join(TARGETS) or '空'}）")
    host, port = TARGETS[args.name]
    print(webvpn_url(host, port))


def cmd_login_test(_args):
    """只尝试一次 WebVPN 登录并打印结果（不重试、不建隧道）。排查登录问题用，一次不会触发封禁。"""
    if not (WEBVPN_USER and WEBVPN_PASSWORD):
        sys.exit("未设置 WEBVPN_USER / WEBVPN_PASSWORD")
    c = webvpn.WebVPNClient(WEBVPN_PORTAL, debug=True, proxy=WEBVPN_PROXY)
    try:
        c.login(WEBVPN_USER, WEBVPN_PASSWORD)
    except webvpn.WrongCredentials as e:
        print("结果：账号或密码错误 —", e)
        sys.exit(2)
    except webvpn.Banned as e:
        print("结果：被临时封禁（之前失败太多次）—", e, "。请等约 5 分钟后再试。")
        sys.exit(3)
    except Exception as e:  # noqa: BLE001
        print("结果：登录失败 —", repr(e))
        sys.exit(1)
    name, value = c.ticket_cookie()
    print(f"结果：登录成功！Cookie {name} 长度 {len(value or '')}")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description="校园网隧道（WebVPN + chisel）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run")
    sub.add_parser("status")
    sub.add_parser("login-test", help="只测一次 WebVPN 登录，排查问题用")
    u = sub.add_parser("url", help="打印某台 GPU 的 WebVPN 连接地址")
    u.add_argument("name")
    args = ap.parse_args()
    {"run": cmd_run, "status": cmd_status, "url": cmd_url, "login-test": cmd_login_test}[args.cmd](args)


if __name__ == "__main__":
    main()
