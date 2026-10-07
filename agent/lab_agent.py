#!/usr/bin/env python3
"""实验室 GPU 服务器 Agent（仅依赖 Python 3.6+ 标准库）。

每隔 interval 秒：
  1. 上报 nvidia-smi 信息、各成员账号的同步状态、可迁移的已有目录
  2. 取回“这台机器此刻应该是什么状态”，在本机对齐：
     - 为每位成员维护 Linux 账号（按 uid 识别：平台上改了用户名，这里就改名并移动家目录）
     - 同步登录密码（平台只下发 sha512-crypt 哈希）和 SSH 公钥
     - 维护 lab-active 组（sshd 只允许该组和 lab-admin 登录）：平台上“预约才能登录”开关关闭时，
       所有成员都在组里；开启时，只有今天预约了本机显卡的成员在组里
     - 显卡硬限制：对 /dev/nvidia<N> 设 ACL，lab-users 组默认不能打开，只有今天预约了这张卡的成员例外
       （没预约的卡 nvidia-smi 看不到、PyTorch 用不了）；另写 /etc/lab-gpu/<用户名>[.uuid]，登录时据此设置
       CUDA_VISIBLE_DEVICES 作为双保险。登录本身不需要预约
     - 平台下发“在没预约的卡上超过缓冲期”的进程清单时，核对后终止（SIGTERM → SIGKILL）并上报
     - 被停用的成员：锁定账号，保留家目录
  3. 另有两个后台线程：浏览请求长轮询（成员在网页上选文件时，列目录 / 统计大小）、各用户家目录占用统计
  4. 在后台线程执行成员发起的文件迁移，每隔几秒上报进度：
     - 旧共享账号（如 ubuntu）家目录里的项目目录：移动或复制给成员
     - 旧共享账号的配置文件、用户级软件、选中的 conda 环境：复制给成员，并把路径改成新家目录

安全约束（即使云端被攻破，也不会扩大到 root 权限）：
  - 只管理 lab-users 组里的账号；不碰 UID < 1000、sudo/admin/lab-admin 组成员
  - 已存在的非托管账号默认不接管（除非写进配置 adopt_users）
  - 从不授予 sudo，从不删除任何用户
  - 不碰旧共享账号本身（不锁定、不改密码），它由管理员自行管理
  - 迁移只接受旧账号家目录和 migration_roots 之下的内容（成员在网页的文件浏览器里选择，浏览同样限制在这些位置，
    且不进入其他成员的目录）；不跟随符号链接、不修改目录外硬链接指向的文件、不把设备文件交给普通用户；
    复制时只复制原属主自己的或公开可读的文件

用法：
  lab_agent.py                     常驻运行（systemd 使用）
  lab_agent.py --once              只同步一次
  lab_agent.py --once --dry-run    只打印将要执行的操作
"""

import argparse
import errno
import grp
import json
import logging
import os
import pwd
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

VERSION = "0.8.0"
CONFIG_PATH = "/etc/lab-agent/config.json"
STATE_DIR = "/var/lib/lab-agent"
GROUP_MANAGED = "lab-users"   # 所有由 Agent 管理的账号
GROUP_ACTIVE = "lab-active"   # 今天允许 SSH 登录的账号（是否要求有预约，由平台上的开关决定）
GROUP_ADMIN = "lab-admin"     # 本机管理员和 root（由安装脚本设置，Agent 不改动）
PROTECTED_GROUPS = ("sudo", "admin", "wheel", GROUP_ADMIN)
KEYS_DIR = "/etc/ssh/lab_authorized_keys"
GPU_DIR = "/etc/lab-gpu"
GECOS_PREFIX = "lab-agent #"
USERNAME_RE = re.compile(r"^[a-z][a-z0-9_-]{1,30}$")
KEY_RE = re.compile(r"^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp\d+|sk-[a-z0-9-]+@openssh\.com) [A-Za-z0-9+/=]+( [\w.@+=:/-]*)?$")
HASH_RE = re.compile(r"^\$[0-9a-z]+\$[^:\s]+$")
DEST_RE = re.compile(r"^[\w.-][\w .+@-]{0,126}$")
ENV_RE = re.compile(r"^[\w.-]{1,64}$")
DEFAULT_ROOTS = ["/lab", "/data", "/workspace"]
O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)

# 旧共享账号家目录里可以复制给成员的内容：(键, 显示名, 分组, 相对家目录的路径)
#   config     配置文件，默认勾选；文本里的旧家目录路径会改成新家目录
#   software   用户级软件（pip --user、~/.local/bin 等）；同样改写路径
#   credential 共享账号的密钥 / 令牌，默认不选（选了才复制，否则连同所在目录一起排除）
CATALOG = [
    ("shell", "Shell 配置（.bashrc、.bash_aliases、.profile、.inputrc）", "config",
     [".bashrc", ".bash_aliases", ".profile", ".inputrc"]),
    ("git", "Git 配置（.gitconfig）", "config", [".gitconfig", ".config/git"]),
    ("vim", "Vim 配置（.vimrc、.vim）", "config", [".vimrc", ".vim"]),
    ("tmux", "tmux / screen 配置", "config", [".tmux.conf", ".screenrc"]),
    ("condarc", "Conda / Pip 配置（.condarc、pip.conf）", "config", [".condarc", ".config/pip", ".pip"]),
    ("ssh_config", "SSH 客户端配置（.ssh/config、known_hosts）", "config", [".ssh/config", ".ssh/known_hosts"]),
    ("jupyter", "Jupyter / IPython 配置", "config", [".jupyter", ".ipython"]),
    ("local_bin", "用户级命令行工具（~/.local/bin、~/bin）", "software", [".local/bin", "bin"]),
    ("local_py", "pip --user 安装的 Python 包（~/.local/lib）", "software", [".local/lib"]),
    ("ssh_keys", "SSH 密钥（共享账号的 GitHub 等密钥）", "credential",
     [".ssh/id_rsa", ".ssh/id_rsa.pub", ".ssh/id_ed25519", ".ssh/id_ed25519.pub", ".ssh/id_ecdsa", ".ssh/id_ecdsa.pub"]),
    ("tokens", "其他凭据（.git-credentials、.netrc、Hugging Face token）", "credential",
     [".git-credentials", ".netrc", ".cache/huggingface/token", ".huggingface/token"]),
]

log = logging.getLogger("lab-agent")


def human(n):
    for unit in ("B", "K", "M", "G", "T"):
        if n < 1024:
            return "%.1f%s" % (n, unit)
        n /= 1024.0
    return "%.1fP" % n


def group_members(name):
    try:
        return set(grp.getgrnam(name).gr_mem)
    except KeyError:
        return set()


def read_shadow():
    """{用户名: (密码哈希, 过期字段)}"""
    out = {}
    try:
        with open("/etc/shadow") as f:
            for line in f:
                p = line.rstrip("\n").split(":")
                if len(p) > 7:
                    out[p[0]] = (p[1], p[7])
    except IOError:
        pass
    return out


def conda_binary_prefix_files(prefix):
    """conda 安装时做过二进制路径替换的文件（conda-meta 里 file_mode=binary 且有 prefix_placeholder）。
    只有这些文件可以按 C 字符串改写路径；.pyc 等其他二进制文件格式不同，改写会损坏文件。"""
    out = set()
    meta = os.path.join(prefix, "conda-meta")
    for name in os.listdir(meta) if os.path.isdir(meta) else []:
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(meta, name)) as f:
                info = json.load(f)
        except (IOError, ValueError):
            continue
        for item in (info.get("paths_data") or {}).get("paths") or []:
            if item.get("file_mode") == "binary" and item.get("prefix_placeholder"):
                out.add(item.get("_path", ""))
    return out


def is_conda_root(path):
    return os.path.isdir(os.path.join(path, "conda-meta")) and os.path.exists(os.path.join(path, "bin", "conda"))


def has_processes(uid):
    for pid in os.listdir("/proc"):
        if pid.isdigit():
            try:
                if os.stat("/proc/" + pid).st_uid == uid:
                    return True
            except OSError:
                pass
    return False


def proc_owner(pid):
    """显卡进程的（Linux 用户名, 所在目录）。
    本机 /proc 里找不到这个进程（刚退出、在别的 PID 命名空间里，或进程已死但驱动还没释放显存）时用户名为空，
    由平台显示“未知”；uid 在 /etc/passwd 里没有对应用户（如容器里 --user 指定的 uid）时写成 uid:数字。"""
    try:
        uid = os.stat("/proc/%d" % pid).st_uid
    except (OSError, TypeError):
        return "", ""
    try:
        user = pwd.getpwuid(uid).pw_name
    except KeyError:
        user = "uid:%d" % uid
    path = ""
    for link in ("cwd", "exe"):  # 进程所在的目录；读不到时退回可执行文件路径
        try:
            path = os.readlink("/proc/%d/%s" % (pid, link))
            break
        except OSError:
            pass
    return user, path


class Agent(object):
    def __init__(self, cfg, dry_run=False):
        self.cfg = cfg
        self.dry_run = dry_run
        self.adopt = set(cfg.get("adopt_users") or [])
        self.home_base = cfg.get("home_base", "/home")
        self.home_usage = None  # [{"user": 名字, "size": MiB}]，由 usage_loop 在后台更新
        self.roots = [os.path.realpath(r) for r in (cfg.get("migration_roots") or DEFAULT_ROOTS)]
        self.accounts = None       # 上一轮对齐的结果，下次同步时上报
        self.candidates = None     # 后台扫描的可迁移文件 / 目录
        self.legacy = None         # 旧共享账号里可复制的配置和 conda 环境
        self.home_free = None
        self.interval = max(5, int(cfg.get("interval", 60)))  # 同步间隔（秒）；平台设置了刷新间隔就以平台为准
        self._cpu_prev = None
        self._last_summary = None
        self.migration = None      # 正在执行的 Migration
        self.finished = set()      # 本进程内已经执行完的迁移 id
        self.pending_migrations = False
        self.wake = threading.Event()  # 扫描完成 / 迁移结束时立即触发一次同步
        self._size_cache = {}
        self.gpu_uuid = {}         # nvidia-smi 序号 → UUID（上一次上报时采集）
        self.killed = []           # 已终止、等待上报给平台的进程
        self.killing = set()       # 正在终止的 pid
        self._lock = threading.Lock()
        self._warned_acl = False
        self.relocating = {}       # uid → 改名后修正路径的后台线程
        self.relocate_notes = {}   # uid → 修正没做完整时的提示

    # ---------------- 工具 ----------------

    def run(self, *cmd, **kw):
        stdin = kw.get("stdin")
        log.info("$ %s", " ".join(cmd))
        if self.dry_run:
            return True, ""
        p = subprocess.run(cmd, input=stdin, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
        if p.returncode != 0:
            log.warning("命令失败(%d): %s", p.returncode, p.stdout.strip())
        return p.returncode == 0, p.stdout.strip()

    def write_file(self, path, content, mode=0o644):
        try:
            with open(path) as f:
                if f.read() == content:
                    return
        except IOError:
            pass
        log.info("写入 %s", path)
        if self.dry_run:
            return
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            f.write(content)
        os.chmod(tmp, mode)
        os.rename(tmp, path)

    def remove_file(self, path):
        if os.path.exists(path):
            log.info("删除 %s", path)
            if not self.dry_run:
                os.remove(path)

    def post(self, path, payload, timeout=20):
        req = urllib.request.Request(
            self.cfg["server_url"].rstrip("/") + path,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + self.cfg["token"]},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())

    # ---------------- 采集 GPU 信息 ----------------

    @staticmethod
    def _num(v):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _smi(*args):
        p = subprocess.run(
            ("nvidia-smi",) + args + ("--format=csv,noheader,nounits",),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=30,
        )
        if p.returncode != 0:
            raise RuntimeError(p.stderr.strip())
        return [[c.strip() for c in line.split(",")] for line in p.stdout.strip().splitlines() if line.strip()]

    def collect_gpus(self):
        try:
            rows = self._smi("--query-gpu=index,uuid,name,memory.total,memory.used,utilization.gpu")
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as e:
            log.warning("nvidia-smi 不可用: %s", e)
            return []
        gpus, by_uuid = [], {}
        for r in rows:
            if len(r) < 6:
                continue
            g = {
                "index": self._num(r[0]), "uuid": r[1], "name": r[2],
                "memory_total": self._num(r[3]), "memory_used": self._num(r[4]),
                "utilization": self._num(r[5]), "processes": [],
            }
            gpus.append(g)
            by_uuid[g["uuid"]] = g
        try:
            for r in self._smi("--query-compute-apps=gpu_uuid,pid,used_memory"):
                if len(r) < 3 or r[0] not in by_uuid:
                    continue
                pid = self._num(r[1])
                user, path = proc_owner(pid)
                by_uuid[r[0]]["processes"].append({"pid": pid, "user": user, "used_memory": self._num(r[2]), "path": path[:512]})
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as e:
            log.warning("查询 GPU 进程失败: %s", e)
        return gpus

    # ---------------- 可迁移内容（后台扫描） ----------------

    def _du(self, path, count_links=False):
        """目录大小（字节）。count_links=True 时硬链接按多份计算（复制后就是多份）。结果缓存 1 小时。"""
        try:
            mtime = os.lstat(path).st_mtime
        except OSError:
            return None
        key = (path, count_links)
        cached = self._size_cache.get(key)
        if cached and cached[0] == mtime and time.time() - cached[2] < 3600:
            return cached[1]
        try:
            cmd = ["du", "-sb"] + (["-l"] if count_links else []) + [path]
            out = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 universal_newlines=True, timeout=600).stdout
            size = int(out.split()[0])
        except Exception:
            size = None
        self._size_cache[key] = (mtime, size, time.time())
        return size

    def legacy_accounts(self):
        """配置里的旧共享账号（如 ubuntu），大家以前都用它登录，数据在它的家目录里。"""
        out = []
        for name in self.cfg.get("legacy_users") or []:
            try:
                pw = pwd.getpwnam(name)
            except KeyError:
                continue
            if os.path.isdir(pw.pw_dir):
                out.append(pw)
        return out

    def scan_legacy(self, pw):
        """旧账号家目录里可以复制的配置、软件和 conda 环境。"""
        home = pw.pw_dir.rstrip("/")
        configs = []
        for key, label, group, paths in CATALOG:
            found = [p for p in paths if os.path.lexists(os.path.join(home, p))]
            if found:
                sizes = [self._du(os.path.join(home, p), True) for p in found]
                configs.append({"key": key, "label": label, "group": group, "paths": found,
                                "size": sum(s or 0 for s in sizes)})
        conda = []
        for name in sorted(os.listdir(home)):
            root = os.path.join(home, name)
            if os.path.islink(root) or not is_conda_root(root):
                continue
            envs = self._scan_envs(os.path.join(root, "envs"))
            total = self._du(root, True)
            pkgs = self._du(os.path.join(root, "pkgs"), True) if os.path.isdir(os.path.join(root, "pkgs")) else 0
            base = None
            if total is not None and pkgs is not None and all(e["size"] is not None for e in envs):
                base = max(0, total - pkgs - sum(e["size"] for e in envs))
            conda.append({"name": name, "base_size": base, "envs": envs})
        user_envs = self._scan_envs(os.path.join(home, ".conda", "envs"))
        if user_envs:
            conda.append({"name": ".conda", "base_size": None, "envs": user_envs})
        return {"user": pw.pw_name, "home": home, "configs": configs, "conda": conda}

    def _scan_envs(self, envs_dir):
        if not os.path.isdir(envs_dir) or os.path.islink(envs_dir):
            return []
        out = []
        for e in sorted(os.listdir(envs_dir)):
            p = os.path.join(envs_dir, e)
            if ENV_RE.match(e) and not os.path.islink(p) and os.path.isdir(os.path.join(p, "conda-meta")):
                out.append({"name": e, "size": self._du(p, True)})
        return out

    def _candidate(self, out, path, managed, home_dev, in_home):
        try:
            st = os.lstat(path)
        except OSError:
            return
        if not (stat.S_ISDIR(st.st_mode) or stat.S_ISREG(st.st_mode)) or st.st_uid in managed:
            return
        try:
            owner = pwd.getpwuid(st.st_uid).pw_name
        except KeyError:
            owner = str(st.st_uid)
        out.append({
            "path": path,
            "size": self._du(path) if stat.S_ISDIR(st.st_mode) else st.st_size,
            "mtime": time.strftime("%Y-%m-%d", time.localtime(st.st_mtime)),
            "owner": owner,
            "same_fs": st.st_dev == home_dev,
            "in_home": in_home,
            "is_dir": stat.S_ISDIR(st.st_mode),
        })

    def scan_candidates(self):
        """可迁移内容：旧账号家目录里的文件 / 目录 + migration_roots 下不属于任何成员的一级目录。"""
        managed = {pwd.getpwnam(n).pw_uid for n in group_members(GROUP_MANAGED) if self._exists(n)}
        try:
            home_dev = os.stat(self.home_base).st_dev
            st = os.statvfs(self.home_base)
            self.home_free = st.f_bavail * st.f_frsize
        except OSError:
            home_dev = None
        out, legacy = [], []
        catalog_top = {p.split("/")[0] for _k, _l, _g, paths in CATALOG for p in paths}
        for pw in self.legacy_accounts():
            info = self.scan_legacy(pw)
            legacy.append(info)
            skip = catalog_top | {c["name"] for c in info["conda"]}
            for name in sorted(os.listdir(info["home"])):
                if not name.startswith(".") and name not in skip:
                    self._candidate(out, os.path.join(info["home"], name), managed, home_dev, True)
        for root in self.roots:
            if os.path.isdir(root):
                for name in sorted(os.listdir(root)):
                    if not name.startswith("."):
                        self._candidate(out, os.path.join(root, name), managed, home_dev, False)
        if out != self.candidates or legacy != self.legacy:
            self.candidates, self.legacy = out, legacy
            self.wake.set()

    def candidate_loop(self):
        while True:
            try:
                self.scan_candidates()
            except Exception:  # noqa: BLE001
                log.exception("扫描可迁移内容失败")
            time.sleep(int(self.cfg.get("candidate_scan_interval", 600)))

    # ---------------- 文件浏览（成员在网页上选择要迁移的文件 / 目录） ----------------

    def browse_bases(self):
        """可浏览 / 可迁移的位置：旧共享账号的家目录，以及 migration_roots。"""
        bases = [os.path.realpath(pw.pw_dir) for pw in self.legacy_accounts()]
        return bases + [r for r in self.roots if os.path.isdir(r) and r not in bases]

    def managed_uids(self):
        out = set()
        for n in group_members(GROUP_MANAGED):
            try:
                out.add(pwd.getpwnam(n).pw_uid)
            except KeyError:
                pass
        return out

    def check_browsable(self, path, managed=None):
        """path 必须是真实路径（不含符号链接），位于某个可浏览位置之下（或就是它），
        且从该位置到 path 之间不经过属于其他成员的目录。返回所在的位置。"""
        if not path.startswith("/") or path != os.path.realpath(path) or path != os.path.normpath(path):
            raise ValueError("路径不合法")
        base = next((b for b in self.browse_bases() if path == b or path.startswith(b.rstrip("/") + "/")), None)
        if base is None:
            raise ValueError("只能浏览旧账号家目录和迁移目录")
        managed = self.managed_uids() if managed is None else managed
        cur = path
        while cur != base:
            if os.lstat(cur).st_uid in managed:
                raise ValueError("这个目录属于其他成员")
            cur = os.path.dirname(cur)
        return base

    def browse_list(self, path):
        managed = self.managed_uids()
        try:
            home_dev = os.stat(self.home_base).st_dev
        except OSError:
            home_dev = None
        homes = [os.path.realpath(pw.pw_dir) for pw in self.legacy_accounts()]

        def info(full, name, st, top=False):
            try:
                owner = pwd.getpwuid(st.st_uid).pw_name
            except KeyError:
                owner = str(st.st_uid)
            is_dir = stat.S_ISDIR(st.st_mode)
            return {
                "name": name, "path": full, "is_dir": is_dir, "top": top,
                "size": None if is_dir else st.st_size,
                "mtime": time.strftime("%Y-%m-%d", time.localtime(st.st_mtime)), "owner": owner,
                "selectable": not top and st.st_uid not in managed,
                "same_fs": st.st_dev == home_dev,
                "in_home": any(full.startswith(h.rstrip("/") + "/") for h in homes),
            }

        if not path:  # 最上层：可以浏览的位置
            out = []
            for b in self.browse_bases():
                try:
                    out.append(info(b, b, os.lstat(b), top=True))
                except OSError:
                    pass
            return out
        self.check_browsable(path, managed)
        out = []
        for name in sorted(os.listdir(path)):
            if name.startswith(".lab-"):  # 迁移程序自己的暂存 / 备份目录，不显示
                continue
            full = os.path.join(path, name)
            try:
                st = os.lstat(full)
            except OSError:
                continue
            if stat.S_ISDIR(st.st_mode) or stat.S_ISREG(st.st_mode):  # 符号链接、设备文件等不显示
                out.append(info(full, name, st))
            if len(out) >= 3000:
                break
        out.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
        return out

    def handle_browse(self, req):
        res = {"ok": True}
        try:
            if req.get("op") == "size":
                path = req.get("path") or ""
                self.check_browsable(path)
                res["size"] = self._du(path) if os.path.isdir(path) else os.lstat(path).st_size
            else:
                res["entries"] = self.browse_list(req.get("path") or "")
        except (OSError, ValueError) as e:
            res = {"ok": False, "error": str(e) if isinstance(e, ValueError) else "读取失败：%s" % e.strerror}
        except Exception as e:  # noqa: BLE001
            log.exception("文件浏览出错")
            res = {"ok": False, "error": "读取失败：%s" % e}
        try:
            self.post("/api/agent/browse/%s" % req["id"], res, timeout=30)
        except Exception as e:  # noqa: BLE001
            log.warning("回复文件浏览请求失败: %s", e)

    def browse_loop(self):
        """长轮询：平台有人在网页上浏览文件时立刻拿到请求，在后台线程里处理并回复。"""
        while True:
            try:
                reply = self.post("/api/agent/browse/poll", {}, timeout=45)
                for req in reply.get("requests") or []:
                    threading.Thread(target=self.handle_browse, args=(req,), daemon=True, name="browse").start()
            except Exception as e:  # noqa: BLE001  网络抖动等：稍后重试
                log.debug("文件浏览轮询失败: %s", e)
                time.sleep(5)

    def scan_home_usage(self):
        """各用户家目录占用的空间（MiB），按从大到小排。家目录可能很大，du 要跑一阵，所以放在后台。"""
        out = []
        for name in sorted(os.listdir(self.home_base)):
            path = os.path.join(self.home_base, name)
            try:
                st = os.lstat(path)
                if not stat.S_ISDIR(st.st_mode) or st.st_uid < 1000:
                    continue
                owner = pwd.getpwuid(st.st_uid).pw_name
                res = subprocess.run(["nice", "-n", "19", "du", "-sb", path], stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, universal_newlines=True, timeout=1800)
                out.append({"user": owner, "size": int(res.stdout.split()[0]) // (1024 * 1024)})
            except Exception:  # noqa: BLE001  某个目录读不了不影响其他
                continue
        out.sort(key=lambda x: -x["size"])
        self.home_usage = out[:100]

    def usage_loop(self):
        while True:
            try:
                self.scan_home_usage()
            except Exception:  # noqa: BLE001
                log.exception("统计家目录占用失败")
            time.sleep(int(self.cfg.get("usage_scan_interval", 1800)))

    # ---------------- 与云端同步 ----------------

    # ---------------- 整机资源（CPU / 内存 / 磁盘），显示在日历的服务器行 ----------------

    @staticmethod
    def _cpu_times():
        with open("/proc/stat") as f:
            parts = [int(x) for x in f.readline().split()[1:]]
        idle = parts[3] + (parts[4] if len(parts) > 4 else 0)  # idle + iowait
        return sum(parts), idle

    def collect_system(self):
        """CPU 利用率（%，与上次采样之间的平均值）、内存 / 磁盘用量（MiB）。磁盘取家目录所在的分区。
        任何一项读不到就省略，不影响其他上报。"""
        out = {}
        try:
            cur = self._cpu_times()
            if self._cpu_prev is None:  # 第一次没有上一次的采样：隔一小会儿现采一次
                self._cpu_prev = cur
                time.sleep(0.3)
                cur = self._cpu_times()
            dt, di = cur[0] - self._cpu_prev[0], cur[1] - self._cpu_prev[1]
            self._cpu_prev = cur
            if dt > 0:
                out["cpu"] = round(max(0.0, min(100.0, 100.0 * (dt - di) / dt)), 1)
        except (IOError, OSError, ValueError, IndexError):
            pass
        try:
            mem = {}
            with open("/proc/meminfo") as f:
                for line in f:
                    k, _, v = line.partition(":")
                    if k in ("MemTotal", "MemAvailable"):
                        mem[k] = int(v.split()[0]) // 1024  # kB → MiB
            if "MemTotal" in mem and "MemAvailable" in mem:
                out["mem_total"] = mem["MemTotal"]
                out["mem_used"] = mem["MemTotal"] - mem["MemAvailable"]
        except (IOError, OSError, ValueError):
            pass
        try:
            st = os.statvfs(self.home_base)
            out["disk_total"] = st.f_blocks * st.f_frsize // (1024 * 1024)
            out["disk_used"] = (st.f_blocks - st.f_bfree) * st.f_frsize // (1024 * 1024)
        except OSError:
            pass
        if self.home_usage is not None:
            out["users"] = self.home_usage
        return out

    def sync(self):
        gpus = self.collect_gpus()
        self.gpu_uuid = {g["index"]: g["uuid"] for g in gpus if g.get("index") is not None} or self.gpu_uuid
        payload = {"hostname": socket.gethostname(), "agent_version": VERSION, "gpus": gpus,
                   "system": self.collect_system()}
        with self._lock:
            sent_kills = list(self.killed)
        if sent_kills:
            payload["killed"] = sent_kills
        if self.accounts is not None:
            payload["accounts"] = self.accounts
            self._sent_accounts = self.accounts
        if self.candidates is not None:
            payload["candidates"] = self.candidates
            payload["browse_roots"] = self.browse_bases()
            payload["legacy"] = self.legacy
            payload["home_free"] = self.home_free
        res = self.post("/api/agent/sync", payload)
        with self._lock:  # 平台收到了才从待上报里去掉
            self.killed = [k for k in self.killed if k not in sent_kills]
        return res

    # ---------------- 账号对齐 ----------------

    @staticmethod
    def _exists(name):
        try:
            pwd.getpwnam(name)
            return True
        except KeyError:
            return False

    def can_manage(self, name):
        """已存在的账号能否由 Agent 管理。"""
        try:
            pw = pwd.getpwnam(name)
        except KeyError:
            return True
        if pw.pw_uid < 1000:
            return False
        for g in PROTECTED_GROUPS:
            if name in group_members(g):
                return False
        return name in group_members(GROUP_MANAGED) or name in self.adopt

    def find_local(self, u, managed):
        """按 uid（其次按 GECOS 里的平台 id）找到这个成员在本机的托管账号。"""
        for name in managed:
            try:
                pw = pwd.getpwnam(name)
            except KeyError:
                continue
            if pw.pw_uid == int(u["uid"]) or pw.pw_gecos == GECOS_PREFIX + str(u["id"]):
                return pw
        return None

    def create_user(self, u):
        name, uid = u["username"], int(u["uid"])
        home = os.path.join(self.home_base, name)
        if os.path.islink(home):
            # 以前改名留下的兼容链接，现在这个名字归新成员了
            log.info("移除旧的兼容链接 %s", home)
            if not self.dry_run:
                os.remove(home)
        try:
            grp.getgrnam(name)
        except KeyError:
            try:
                grp.getgrgid(uid)
                self.run("groupadd", name)
            except KeyError:
                self.run("groupadd", "-g", str(uid), name)
        uid_args = ["-u", str(uid)]
        try:
            pwd.getpwuid(uid)
            log.warning("UID %d 已被占用，%s 将使用系统分配的 UID（多机共享存储时注意）", uid, name)
            uid_args = []
        except KeyError:
            pass
        ok, out = self.run("useradd", "-m", "-d", home, *uid_args, "-g", name, "-G", GROUP_MANAGED,
                           "-s", "/bin/bash", "-p", "*", "-c", GECOS_PREFIX + str(u["id"]), name)
        if ok and not self.dry_run:
            os.chmod(home, 0o750)
        return ok, out

    def rename_user(self, pw, new):
        """改名 + 移动家目录，并修正家目录里写死的旧路径（conda、.bashrc 等，见 Relocator）。
        旧家目录位置另留一个兼容链接，项目代码里写死的旧路径仍可用。"""
        old = pw.pw_name
        if self._exists(new):
            return "error", "本机已存在名为 %s 的账号，无法改名，请联系管理员" % new
        if has_processes(pw.pw_uid):
            return "pending", "旧账号 %s 还有进程在运行，进程全部结束后会自动改名（在此之前请继续用 %s 登录）" % (old, old)
        if self.migration and self.migration.is_alive() and int(self.migration.t.get("uid", -1)) == pw.pw_uid:
            return "pending", "文件迁移进行中，结束后会自动改名（在此之前请继续用 %s 登录）" % old
        ok, out = self.run("usermod", "-l", new, old)
        if not ok:
            return "error", "改名失败：" + out
        try:
            if grp.getgrgid(pw.pw_gid).gr_name == old:
                self.run("groupmod", "-n", new, old)
        except KeyError:
            pass
        for d in ("/var/spool/cron/crontabs", "/var/mail", "/var/lib/systemd/linger"):  # 以用户名命名的：定时任务、邮箱、常驻用户服务
            src, dst = os.path.join(d, old), os.path.join(d, new)
            if os.path.lexists(src) and not os.path.lexists(dst):
                log.info("$ mv %s %s", src, dst)
                if not self.dry_run:
                    try:
                        os.rename(src, dst)
                    except OSError as e:
                        log.warning("移动 %s 失败: %s", src, e)
        new_home = os.path.join(os.path.dirname(pw.pw_dir.rstrip("/")), new)
        if os.path.islink(new_home) and not self.dry_run:
            os.remove(new_home)  # 别人改名留下的兼容链接
        if os.path.basename(pw.pw_dir.rstrip("/")) == old and not os.path.lexists(new_home):
            ok, out = self.run("usermod", "-d", new_home, "-m", new)
            if ok and not self.dry_run:
                if not os.path.lexists(pw.pw_dir):
                    os.symlink(new_home, pw.pw_dir)
                self.start_relocate(pw.pw_uid, pw.pw_dir.rstrip("/"), new_home)
        log.info("已将 %s 改名为 %s", old, new)
        return "ok", "已从 %s 改名" % old

    # ---------------- 改名后的路径修正（后台） ----------------

    def _relocate_marker(self, uid):
        return os.path.join(STATE_DIR, "relocate-%d.json" % uid)

    def start_relocate(self, uid, old_home, new_home):
        """记下“这个家目录要把 old_home 改成 new_home”再开始做：Agent 中途重启后会接着做（改写是幂等的）。"""
        try:
            os.makedirs(STATE_DIR, 0o700, exist_ok=True)
            with open(self._relocate_marker(uid), "w") as f:
                json.dump({"old": old_home, "new": new_home}, f)
        except OSError as e:
            log.warning("无法记录路径修正任务: %s", e)
        self.resume_relocations()

    def resume_relocations(self):
        if self.dry_run or not os.path.isdir(STATE_DIR):
            return
        for fn in os.listdir(STATE_DIR):
            m = re.match(r"^relocate-(\d+)\.json$", fn)
            if not m:
                continue
            uid = int(m.group(1))
            t = self.relocating.get(uid)
            if t and t.is_alive():
                continue
            try:
                with open(os.path.join(STATE_DIR, fn)) as f:
                    job = json.load(f)
                pw = pwd.getpwuid(uid)
                if os.path.realpath(pw.pw_dir) != os.path.realpath(job["new"]) or not os.path.isdir(job["new"]):
                    raise ValueError("家目录已变化")
            except (OSError, ValueError, KeyError):
                self._relocate_done(uid)  # 任务作废
                continue
            t = threading.Thread(target=self._relocate_job, args=(pw, job["old"], job["new"]), daemon=True, name="relocate-%d" % uid)
            self.relocating[uid] = t
            t.start()

    def _relocate_done(self, uid):
        try:
            os.remove(self._relocate_marker(uid))
        except OSError:
            pass

    def _relocate_job(self, pw, old, new):
        log.info("修正 %s 家目录里的旧路径 %s → %s", pw.pw_name, old, new)

        def demote():
            os.setgroups([])
            os.setgid(pw.pw_gid)
            os.setuid(pw.pw_uid)

        env = {"HOME": new, "USER": pw.pw_name, "PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8"}
        try:
            p = subprocess.run([sys.executable, os.path.abspath(__file__), "--relocate", old, new], preexec_fn=demote, cwd=new,
                               env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=6 * 3600)
            if p.returncode != 0:
                raise RuntimeError(p.stderr.strip()[-300:])
            res = json.loads(p.stdout.strip().splitlines()[-1])
        except Exception as e:  # noqa: BLE001
            log.warning("修正 %s 的旧路径失败: %s（旧路径仍可通过兼容链接使用，下次同步会重试）", pw.pw_name, e)
            return  # 保留标记，下一轮同步重试
        self._relocate_done(pw.pw_uid)
        if res.get("failed_count"):
            self.relocate_notes[pw.pw_uid] = "家目录里有 %d 处旧路径没能自动修正（如新用户名更长导致二进制文件放不下），请联系管理员" % res["failed_count"]
            log.warning("未能修正：%s", res.get("failed"))
        else:
            self.relocate_notes.pop(pw.pw_uid, None)
        log.info("%s 家目录路径修正完成，改了 %d 个文件", pw.pw_name, res.get("changed", 0))
        self.wake.set()

    def set_group(self, name, group, member):
        current = name in group_members(group)
        if member and not current:
            self.run("gpasswd", "-a", name, group)
        elif not member and current:
            self.run("gpasswd", "-d", name, group)

    def disable_user(self, name, shadow):
        if not self.can_manage(name):
            return
        self.set_group(name, GROUP_ACTIVE, False)
        self.remove_file(os.path.join(KEYS_DIR, name))
        self.remove_file(os.path.join(GPU_DIR, name))
        if shadow.get(name, ("", ""))[1] != "0":
            log.info("停用账号 %s（保留家目录）", name)
            self.run("chage", "-E", "0", name)

    def reconcile(self, users):
        for d in (KEYS_DIR, GPU_DIR):
            if not os.path.isdir(d) and not self.dry_run:
                os.makedirs(d, 0o755)
        self.resume_relocations()  # 先接上被中断的路径修正：它在跑时账号还有进程，不会被再次改名
        managed = group_members(GROUP_MANAGED)
        shadow = read_shadow()
        wanted, statuses = set(), []
        allowed = {}  # 显卡序号 → 今天有权使用它的账号
        for u in users:
            name = u.get("username") or ""
            status = {"id": u.get("id"), "username": name, "state": "ok", "message": ""}
            statuses.append(status)
            if not USERNAME_RE.match(name):
                status.update(state="error", message="非法用户名")
                continue

            pw = self.find_local(u, managed)
            if pw is None:
                if self._exists(name):
                    if not self.can_manage(name):
                        status.update(state="error", message="本机已存在同名账号（非平台管理），请换一个用户名或联系管理员")
                        continue
                    log.info("接管已有账号 %s（配置 adopt_users）", name)
                    self.run("usermod", "-aG", GROUP_MANAGED, "-c", GECOS_PREFIX + str(u["id"]), name)
                else:
                    ok, out = self.create_user(u)
                    if not ok:
                        status.update(state="error", message="创建账号失败：" + out)
                        continue
                local = name
            elif pw.pw_name != name:
                state, msg = self.rename_user(pw, name)
                status.update(state=state, message=msg)
                local = name if state == "ok" else pw.pw_name
            else:
                local = name
            status["username"] = local
            wanted.add(local)
            try:
                luid = pwd.getpwnam(local).pw_uid
            except KeyError:
                luid = None
            if status["state"] == "ok" and luid is not None:
                t = self.relocating.get(luid)
                if t and t.is_alive():
                    status["message"] = "正在修正家目录里的路径（conda、.bashrc 等），期间旧路径仍可用"
                elif self.relocate_notes.get(luid):
                    status["message"] = self.relocate_notes[luid]

            if shadow.get(local, ("", ""))[1] not in ("", "-1"):
                self.run("chage", "-E", "-1", local)  # 之前被停用过，恢复
            want_hash = u.get("password_hash") or "*"
            if want_hash != "*" and not HASH_RE.match(want_hash):
                want_hash = "*"
            cur = shadow.get(local)
            if cur is None and not self.dry_run:  # 本轮新建的账号
                shadow = read_shadow()
                cur = shadow.get(local)
            if cur is None or cur[0] != want_hash:
                log.info("更新 %s 的登录密码", local)
                self.run("chpasswd", "-e", stdin="%s:%s\n" % (local, want_hash))

            keys = [k for k in u.get("pubkeys") or [] if KEY_RE.match(k)]
            key_path = os.path.join(KEYS_DIR, local)
            if keys:
                self.write_file(key_path, "# 由 lab-agent 管理，请在实验室平台上修改\n" + "\n".join(keys) + "\n")
            else:
                self.remove_file(key_path)

            active = bool(u.get("active"))  # 今天预约了本机的显卡
            # 登录权限：开关关闭时所有成员都能登录；开启时只有今天有预约的成员能登录
            self.set_group(local, GROUP_ACTIVE, active or not getattr(self, "require_reservation", False))
            gpu_path = os.path.join(GPU_DIR, local)
            gpus = [str(int(i)) for i in u.get("gpus") or []] if active else []
            for i in gpus:
                allowed.setdefault(int(i), set()).add(local)
            # 没有预约：写空文件（登录和文件迁移不受影响）。<用户名> 里是序号（lab-gpus 显示用），
            # <用户名>.uuid 里是 UUID（登录时设置 CUDA_VISIBLE_DEVICES 用）：设备权限收紧后 CUDA 只枚举能打开的卡，
            # 序号会重新编号，只有 UUID 在任何情况下都指向同一张卡
            self.write_file(gpu_path, (",".join(gpus) + "\n") if gpus else "")
            self.write_file(gpu_path + ".uuid", (",".join(self.gpu_uuid.get(int(i), i) for i in gpus) + "\n") if gpus else "")

        for name in group_members(GROUP_MANAGED) - wanted:
            self.disable_user(name, shadow)
        for d in (KEYS_DIR, GPU_DIR):
            if os.path.isdir(d):
                for f in os.listdir(d):
                    base = f[:-5] if f.endswith(".uuid") else f
                    if base not in wanted and USERNAME_RE.match(base):
                        self.remove_file(os.path.join(d, f))
        self.apply_gpu_acl(allowed)
        self.accounts = statuses

    # ---------------- 显卡的硬限制：设备文件权限 ----------------

    def gpu_minors(self):
        """nvidia-smi 序号 → /dev/nvidia<N> 的 N。N 是驱动里的设备号，不一定等于序号，按 PCI 地址查 /proc/driver/nvidia。"""
        out = {}
        for r in self._smi("--query-gpu=index,pci.bus_id"):
            idx, bus = self._num(r[0]), r[1].lower()
            if idx is None:
                continue
            dom, rest = bus.split(":", 1)
            minor = idx
            try:
                with open("/proc/driver/nvidia/gpus/%s:%s/information" % (dom[-4:], rest)) as f:
                    m = re.search(r"Device Minor:\s*(\d+)", f.read())
                    if m:
                        minor = int(m.group(1))
            except (IOError, OSError):
                pass
            out[idx] = minor
        return out

    @staticmethod
    def _read_acl(dev):
        """{'u:名字': 权限, 'g:名字': 权限}（只含命名条目）。"""
        p = subprocess.run(["getfacl", "-cp", dev], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        if p.returncode != 0:
            raise RuntimeError(p.stderr.strip())
        out = {}
        for line in p.stdout.splitlines():
            f = line.split("#")[0].split()
            if not f:
                continue
            kind, name, perm = (f[0].split(":") + ["", ""])[:3]
            if name and kind in ("user", "group"):
                out["%s:%s" % (kind[0], name)] = perm
        return out

    def apply_gpu_acl(self, allowed):
        """硬限制：lab-users 组的成员默认不能打开任何 /dev/nvidia<N>，只有今天预约了这张卡的成员例外。
        这样没预约的卡上 nvidia-smi 看不到、PyTorch 等也用不了，不依赖环境变量（环境变量用户自己能改）。
        不属于 lab-users 的账号（root、gdm、旧共享账号、管理员）不受影响；已经打开设备的进程不受影响，
        它们超过缓冲期后由平台通知 Agent 终止。设备文件在驱动重载 / 重启后会重建，所以每次同步都重新对齐。"""
        if not (shutil.which("setfacl") and shutil.which("getfacl")):
            if not self._warned_acl:
                log.warning("未安装 acl（apt install acl）：无法限制显卡设备权限，目前只靠 CUDA_VISIBLE_DEVICES 做软限制")
                self._warned_acl = True
            return
        try:
            minors = self.gpu_minors()
        except (OSError, RuntimeError, subprocess.TimeoutExpired, ValueError) as e:
            log.warning("无法确定显卡设备号，跳过设备权限设置: %s", e)
            return
        managed = group_members(GROUP_MANAGED)
        for idx, minor in sorted(minors.items()):
            dev = "/dev/nvidia%d" % minor
            if not os.path.exists(dev):
                continue
            want = allowed.get(idx, set()) & managed
            try:
                cur = self._read_acl(dev)
            except (OSError, RuntimeError) as e:
                log.warning("读取 %s 的权限失败: %s", dev, e)
                continue
            mods = []
            if cur.get("g:" + GROUP_MANAGED) != "---":
                mods.append("g:%s:---" % GROUP_MANAGED)
            mods += ["u:%s:rw-" % u for u in sorted(want) if cur.get("u:" + u) != "rw-"]
            if mods:
                self.run("setfacl", "-m", ",".join(mods), dev)
            for key in sorted(cur):  # 以前开放过、现在没预约的成员：去掉单独的条目，回到组的“禁止”
                if key.startswith("u:") and key[2:] in managed and key[2:] not in want:
                    self.run("setfacl", "-x", key, dev)

    # ---------------- 终止超过缓冲期的未预约进程 ----------------

    def handle_kills(self, items):
        """平台下发的清单：这些进程在没预约的显卡上超过了缓冲期。逐个核对后终止（先 SIGTERM，不退出再 SIGKILL）。"""
        todo = []
        for it in items:
            try:
                todo.append((int(it["pid"]), str(it["user"]), int(it["gpu"])))
            except (KeyError, TypeError, ValueError):
                continue
        todo = [t for t in todo if t[0] not in self.killing]
        if not todo:
            return
        if self.dry_run:
            for pid, user, gpu in todo:
                log.info("[dry-run] 将终止 %s 在 GPU%d 上的进程 %d", user, gpu, pid)
            return
        self.killing.update(t[0] for t in todo)
        threading.Thread(target=self._kill_job, args=(todo,), daemon=True, name="kill").start()

    @staticmethod
    def _gone(pid):
        try:
            with open("/proc/%d/stat" % pid) as f:
                return f.read().rsplit(")", 1)[1].split()[0] == "Z"  # 僵尸进程已不占用显卡
        except (IOError, OSError, IndexError):
            return True

    def _kill_job(self, todo):
        try:
            using = {(g["index"], p["pid"]) for g in self.collect_gpus() for p in g["processes"]}
            managed = group_members(GROUP_MANAGED)
            for pid, user, gpu in todo:
                try:
                    pw = pwd.getpwnam(user)
                    # 只处理平台托管的普通成员账号；进程必须确实属于他、此刻仍在这张卡上（防止进程号被复用后误杀）
                    if pw.pw_uid < 1000 or user not in managed or not self.can_manage(user):
                        continue
                    if os.stat("/proc/%d" % pid).st_uid != pw.pw_uid or (gpu, pid) not in using:
                        continue
                    log.warning("终止 %s 在 GPU%d 上的进程 %d（超过未预约缓冲期）", user, gpu, pid)
                    os.kill(pid, signal.SIGTERM)
                    for _ in range(20):
                        if self._gone(pid):
                            break
                        time.sleep(1)
                    else:
                        os.kill(pid, signal.SIGKILL)
                        time.sleep(3)
                    if self._gone(pid):
                        with self._lock:
                            self.killed.append({"gpu": gpu, "pid": pid, "user": user})
                except (KeyError, OSError) as e:
                    log.info("跳过进程 %d: %s", pid, e)
        finally:
            self.killing.difference_update(t[0] for t in todo)
            self.wake.set()

    # ---------------- 迁移 ----------------

    def handle_migrations(self, tasks):
        if self.dry_run:
            for t in tasks:
                log.info("[dry-run] 待执行迁移 #%s %s %s → ~/%s (%s)", t["id"], t.get("kind"), t["source"], t["dest_name"], t["mode"])
            return
        if self.migration and self.migration.is_alive():
            return
        for t in tasks:
            if t["id"] not in self.finished:
                self.migration = Migration(self, t)
                self.migration.start()
                return

    def once(self):
        state = self.sync()
        users = state.get("users") or []
        self.require_reservation = bool(state.get("require_reservation_to_login"))
        try:  # 平台上设置的刷新间隔（秒）
            self.interval = max(5, min(600, int(state.get("interval"))))
        except (TypeError, ValueError):
            pass
        summary = "%s %s，成员 %d 人，今日有 GPU 预约 %d 人，%s" % (
            state.get("server"), state.get("date"), len(users), sum(1 for u in users if u.get("active")),
            "仅预约者可登录" if self.require_reservation else "所有成员可登录")
        # 同步很频繁：内容有变化才写 INFO，否则只写 DEBUG，避免日志被刷屏
        log.log(logging.INFO if summary != self._last_summary else logging.DEBUG, "同步成功：%s", summary)
        self._last_summary = summary
        self.reconcile(users)
        self.handle_kills(state.get("kill") or [])
        if self.accounts != getattr(self, "_sent_accounts", None):
            self.sync()  # 账号状态有变化（新建、改名、出错），立即上报给平台
        listed = state.get("migrations") or []
        # 平台已不再把它列为待执行（完成 / 失败已上报）的，就不必再记着：否则管理员或成员点“重试”后，
        # 同一个任务 id 会被一直当作“已执行过”而跳过，直到 Agent 重启
        self.finished &= {t["id"] for t in listed}
        tasks = [t for t in listed if t["id"] not in self.finished]
        self.pending_migrations = bool(tasks)
        self.handle_migrations(tasks)


class MigrationError(Exception):
    pass


def open_beneath(base, rel, last_flags=None):
    """从可信目录 base 开始逐级打开 rel，每一级都不跟随符号链接，返回最后一级的 fd。
    防止旧账号的使用者把中间某级目录换成指向 /etc 等处的符号链接。"""
    fd = os.open(base, os.O_RDONLY | O_DIRECTORY)
    parts = [p for p in rel.split("/") if p and p != "."]
    try:
        for i, part in enumerate(parts):
            if part == "..":
                raise MigrationError("非法路径")
            flags = (os.O_RDONLY | O_DIRECTORY) if i < len(parts) - 1 or last_flags is None else last_flags
            nfd = os.open(part, flags | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nfd
        return fd
    except BaseException:
        os.close(fd)
        raise


def remove_moved_env(env_root, envs_dir):
    """conda 环境的「移动」= 复制 + 自检通过 + 删除源：把共享账号里已迁走的原环境删掉。
    只删 envs_dir 下的、本身不是符号链接的、带 conda-meta 的目录。成功返回 None，否则返回原因。"""
    try:
        parent = os.path.realpath(os.path.dirname(env_root))
        st = os.lstat(env_root)
        if (stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode) or parent != os.path.realpath(envs_dir)
                or not os.path.isdir(os.path.join(env_root, "conda-meta"))):
            return "路径异常，没有删除"
        shutil.rmtree(env_root)
    except OSError as e:
        return str(e)
    return None


def binary_relocate(data, rx, new):
    """在二进制文件的 C 字符串里替换路径前缀（同 conda 的做法），长度差用字符串结尾的 NUL 填充吸收。
    conda 安装时把 255 字节的占位符换成了实际路径，后面留有大量 NUL，所以新路径稍长一些也放得下。
    空间不够时返回 None。"""
    out = bytearray(data)
    pos = 0
    while True:
        m = rx.search(out, pos)
        if not m:
            return bytes(out)
        i = m.start()
        end = out.find(b"\0", i)
        if end < 0:
            return None
        seg = bytes(out[i:end])
        new_seg = rx.sub(new, seg)
        delta = len(new_seg) - len(seg)
        if delta <= 0:
            out[i:end] = new_seg + b"\0" * (-delta)
        else:
            j = end
            while j < len(out) and out[j] == 0 and j - end <= delta:
                j += 1
            if j - end < delta + 1:
                return None
            out[i:end + delta] = new_seg
        pos = i + len(new_seg)


# ---------------- 改名后修正家目录里写死的路径 ----------------
# 家目录从 /home/旧名 搬到 /home/新名后，里面这些内容仍写着旧路径：conda 的 shebang、conda-meta、.bashrc 里的 conda init、
# ~/.conda/environments.txt、Jupyter kernel、venv 的 activate 等。旧路径靠兼容链接还能用，但旧名字一旦分给别人
# 链接就没了，所以改名时把这些路径一并改成新的。以成员本人的身份执行（不跟随符号链接，家目录里的任何把戏只影响他自己）。
RELOCATE_FILES = (".bashrc", ".bash_profile", ".bash_login", ".bash_aliases", ".profile", ".zshrc", ".zprofile", ".zshenv",
                  ".inputrc", ".gitconfig", ".vimrc", ".tmux.conf", ".screenrc", ".condarc", ".mambarc", ".wgetrc", ".ssh/config")
RELOCATE_DIRS = (".config", ".conda", ".ipython", ".jupyter", ".local", ".vim", ".pip",
                 ".vscode-server/data/Machine", ".vscode-server/data/User")
RELOCATE_SKIP_EXT = {".so", ".a", ".pyc", ".pyo", ".png", ".jpg", ".jpeg", ".gif", ".gz", ".bz2", ".xz", ".zst", ".zip",
                     ".whl", ".tar", ".conda", ".pdf", ".npy", ".npz", ".h5", ".ckpt", ".safetensors", ".bin", ".onnx"}
RELOCATE_MAX_TEXT = 4 * 1024 * 1024
RELOCATE_MAX_BIN = 256 * 1024 * 1024
RELOCATE_TMP = ".lab-relocate-tmp"


def path_regex(path):
    """匹配完整路径：前面不能是字母数字（避免 /mnt/home/x 误伤），后面也不能（避免 /home/ubuntu 误伤 /home/ubuntu602）。"""
    return re.compile(rb"(?<![\w.-])" + re.escape(path.encode()) + rb"(?![\w.-])")


class Relocator(object):
    def __init__(self, old, new):
        self.rx = path_regex(old)
        self.new = new.encode()
        self.changed = 0
        self.failed = []       # 路径太长放不下的二进制文件 / 写不了的文件
        self.prefixes = {}     # conda 环境根目录 → conda-meta 里记录的、可做二进制替换的文件

    def _sub(self, data):
        return self.rx.sub(lambda _m: self.new, data)

    def link(self, path):
        try:
            target = os.readlink(path)
        except OSError:
            return
        b = target.encode("utf-8", "surrogateescape")
        nb = self._sub(b)
        if nb == b:
            return
        tmp = path + RELOCATE_TMP
        try:
            try:
                os.remove(tmp)
            except OSError:
                pass
            os.symlink(nb.decode("utf-8", "surrogateescape"), tmp)
            os.replace(tmp, path)
            self.changed += 1
        except OSError as e:
            self.failed.append("%s（%s）" % (path, e.strerror))

    def _binary_set(self, path):
        best = None
        for p in self.prefixes:
            if path.startswith(p + "/") and (best is None or len(p) > len(best)):
                best = p
        return (self.prefixes[best], path[len(best) + 1:]) if best else (None, None)

    def file(self, path, st):
        if os.path.splitext(path)[1].lower() in RELOCATE_SKIP_EXT:
            return
        binset, rel = self._binary_set(path)
        is_bin_ok = binset is not None and rel in binset
        if st.st_size == 0 or st.st_size > (RELOCATE_MAX_BIN if is_bin_ok else RELOCATE_MAX_TEXT):
            return
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0))
            with os.fdopen(fd, "rb") as f:
                fst = os.fstat(f.fileno())
                if not stat.S_ISREG(fst.st_mode):
                    return
                data = f.read()
        except OSError:
            return
        if not self.rx.search(data):
            return
        if b"\0" in data:
            if not is_bin_ok:
                return  # 其他二进制文件（.pyc 等）格式不同，改写会损坏，原样保留
            out = binary_relocate(data, self.rx, self.new)
            if out is None:
                self.failed.append("%s（新路径比原路径长太多）" % path)
                return
        else:
            out = self._sub(data)
        tmp = path + RELOCATE_TMP
        try:
            try:
                os.remove(tmp)
            except OSError:
                pass
            wfd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(wfd, "wb") as w:
                w.write(out)
            os.chmod(tmp, stat.S_IMODE(fst.st_mode))
            os.utime(tmp, ns=(fst.st_atime_ns, fst.st_mtime_ns))
            os.rename(tmp, path)  # 原子替换：正在运行的程序不受影响；同时断开与 conda 包缓存的硬链接
            self.changed += 1
        except OSError as e:
            self.failed.append("%s（%s）" % (path, e.strerror))
            try:
                os.remove(tmp)
            except OSError:
                pass

    def walk(self, top):
        try:
            st = os.lstat(top)
        except OSError:
            return
        if stat.S_ISLNK(st.st_mode):
            return self.link(top)
        if stat.S_ISREG(st.st_mode):
            return self.file(top, st)
        for dirpath, dirnames, filenames in os.walk(top):
            if "conda-meta" in dirnames and dirpath not in self.prefixes:
                self.prefixes[dirpath] = conda_binary_prefix_files(dirpath)
            # 包缓存里装的是安装前的占位路径，不含家目录路径，也最大；.pyc 不需要改
            dirnames[:] = [d for d in dirnames if d != "__pycache__" and not (d == "pkgs" and dirpath in self.prefixes)]
            for name in dirnames + filenames:
                p = os.path.join(dirpath, name)
                try:
                    st = os.lstat(p)
                except OSError:
                    continue
                if stat.S_ISLNK(st.st_mode):
                    self.link(p)
                elif stat.S_ISREG(st.st_mode):
                    self.file(p, st)

    @staticmethod
    def find_roots(home, limit=20000):
        """家目录里的 conda 安装目录（第一层）和 Python 虚拟环境（含 pyvenv.cfg，最多找 4 层）。"""
        roots, seen = [], 0
        stack = [(home, 0)]
        while stack and seen < limit:
            d, depth = stack.pop()
            try:
                names = os.listdir(d)
            except OSError:
                continue
            seen += 1
            if "pyvenv.cfg" in names:
                roots.append(d)
                continue
            if depth == 0:
                roots += [os.path.join(d, n) for n in names
                          if not n.startswith(".") and not os.path.islink(os.path.join(d, n)) and is_conda_root(os.path.join(d, n))]
            if depth >= 4:
                continue
            for n in names:
                p = os.path.join(d, n)
                hidden = n.startswith(".") and n not in (".venv", ".env")
                if hidden or n in ("node_modules", "__pycache__", "envs", "pkgs") or os.path.islink(p) or not os.path.isdir(p):
                    continue
                if depth == 0 and is_conda_root(p):
                    continue
                stack.append((p, depth + 1))
        return roots

    def run(self, home):
        targets = [os.path.join(home, r) for r in RELOCATE_FILES + RELOCATE_DIRS]
        try:
            for n in os.listdir(home):  # 家目录第一层的符号链接（如 ~/data → /home/旧名/...）
                p = os.path.join(home, n)
                if os.path.islink(p):
                    self.link(p)
        except OSError:
            pass
        targets += self.find_roots(home)
        for t in targets:
            self.walk(t)
        return {"changed": self.changed, "failed": self.failed[:20], "failed_count": len(self.failed)}


def relocate_main(old, new):
    """以成员身份在子进程里执行：改写 new 家目录里的 old 路径，结果以 JSON 打印。"""
    try:
        os.nice(10)
    except OSError:
        pass
    print(json.dumps(Relocator(old, new).run(new), ensure_ascii=False))



# 以成员身份执行：把准备好的文件放进家目录（已有的同名文件先备份）。
# 用成员自己的权限做这一步，家目录里的任何符号链接把戏都只能影响他自己。
PLACE_SCRIPT = r'''
import json, os, shutil, subprocess, sys, time
inc, home, rels, envs = sys.argv[1], sys.argv[2], json.loads(sys.argv[3]), json.loads(sys.argv[4])
bk = os.path.join(home, ".lab-backup", time.strftime("%Y%m%d-%H%M%S"))
replaced = []
for rel in rels:
    src, dst = os.path.join(inc, rel), os.path.join(home, rel)
    if not os.path.lexists(src):
        continue
    if os.path.lexists(dst):
        b = os.path.join(bk, rel)
        os.makedirs(os.path.dirname(b), exist_ok=True)
        os.rename(dst, b)
        replaced.append(rel)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    os.rename(src, dst)
if os.path.isdir(os.path.join(home, ".ssh")):
    os.chmod(os.path.join(home, ".ssh"), 0o700)
shutil.rmtree(inc, ignore_errors=True)
checks = {}
for env in envs:
    py = os.path.join(env, "bin", "python")
    if not os.path.exists(py):
        continue
    try:
        r = subprocess.run([py, "-c", "import sys, ssl, sqlite3; print(sys.prefix)"], stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, universal_newlines=True, timeout=120)
        checks[env] = "ok" if r.returncode == 0 and r.stdout.strip().splitlines()[-1] == env else r.stdout.strip()[-200:]
    except Exception as e:
        checks[env] = str(e)
print(json.dumps({"backup": bk if replaced else "", "replaced": replaced, "checks": checks}))
'''


class Migration(threading.Thread):
    """执行一个迁移任务。

    kind=dir：把一个文件 / 目录交给成员
      move  移动到家目录（同盘瞬间完成，跨盘复制后删除源）
      copy  复制到家目录，原文件不动（只复制原属主自己的文件和公开可读的文件）
      link  原地保留、改为成员所有，在家目录建快捷方式（不适用于旧账号家目录里的内容）
    kind=profile：从旧共享账号复制配置文件、用户级软件和选中的 conda 环境（自动修正路径）

    所有加工都在只有 root 能进入的暂存目录（.lab-migrate，权限 700）里完成，成员和旧账号都碰不到；
    最后放进家目录的一步以成员自己的身份执行。
    """

    def __init__(self, agent, task):
        threading.Thread.__init__(self, daemon=True, name="migration-%s" % task["id"])
        self.agent = agent
        self.t = task
        self.id = int(task["id"])
        self.kind = task.get("kind") or "dir"
        self.p = {"phase": "", "bytes_done": 0, "bytes_total": 0, "files_done": 0, "files_total": 0}
        self.notes = []
        self.skipped_private = 0
        self.skipped_devices = 0
        self.reloc_failed = []
        self._last = 0
        self.marker = os.path.join(STATE_DIR, "migration-%d.json" % self.id)
        # 任务指纹：平台数据库重建后任务 id 会重新从 1 开始，不能只凭 id 认定“这个任务以前做完了”
        self.sig = json.dumps(task, sort_keys=True, ensure_ascii=False)

    def report(self, status="running", force=False, **kw):
        self.p.update(kw)
        if not force and time.time() - self._last < 2:
            return
        self._last = time.time()
        payload = dict(self.p, status=status)
        payload["phase"] = payload["phase"][:60]  # 平台限制 64 字符，过长会被拒绝，进度就不再更新
        payload.setdefault("message", "")
        try:
            self.agent.post("/api/agent/migrations/%d" % self.id, payload, timeout=10)
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("上报迁移进度失败: %s", e)
            return False

    def run(self):
        log.info("开始迁移 #%d (%s): %s", self.id, self.kind, self.t.get("source"))
        try:
            dest = self.execute_profile() if self.kind == "profile" else self.execute_dir()
            if self.skipped_private:
                self.notes.append("%d 个文件出于安全考虑未迁移或未改归属（属于 root 且不可公开读取、不属于原账号，或与目录外的系统文件是硬链接），如需要请联系管理员" % self.skipped_private)
            if self.skipped_devices:
                self.notes.append("跳过了 %d 个设备文件" % self.skipped_devices)
            if self.reloc_failed:
                self.notes.append("%d 个二进制文件中的路径无法自动修正（新路径比原路径长太多）：%s" % (
                    len(self.reloc_failed), "、".join(self.reloc_failed[:5])))
            if self.report("done", force=True, phase="完成", message="；".join(self.notes), dest_path=dest):
                self._clear_marker()
            log.info("迁移 #%d 完成: %s", self.id, dest)
        except MigrationError as e:
            log.warning("迁移 #%d 失败: %s", self.id, e)
            self.report("failed", force=True, phase="失败", message=str(e))
        except Exception as e:  # noqa: BLE001
            log.exception("迁移 #%d 出错", self.id)
            self.report("failed", force=True, phase="失败", message="内部错误：%s" % e)
        finally:
            self.agent.finished.add(self.id)
            try:
                self.agent.scan_candidates()  # 刷新可迁移列表
            except Exception:  # noqa: BLE001
                pass
            self.agent.wake.set()

    # ---- 公共 ----

    def _staging(self, parent, ref_dev):
        """在 parent 下准备 root 专用暂存目录。"""
        d = os.path.join(parent, ".lab-migrate")
        try:
            os.mkdir(d, 0o700)
        except OSError as e:
            if e.errno != errno.EEXIST:
                raise
        st = os.lstat(d)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != 0:
            raise MigrationError("暂存目录 %s 异常，请联系管理员" % d)
        os.chmod(d, 0o700)
        if ref_dev is not None and st.st_dev != ref_dev:
            raise MigrationError("暂存目录 %s 与目标不在同一磁盘" % d)
        return d

    @staticmethod
    def _rmdir_quiet(d):
        try:
            os.rmdir(d)  # 暂存目录空了就删掉
        except OSError:
            pass

    def _user(self):
        try:
            pw = pwd.getpwuid(int(self.t["uid"]))
        except KeyError:
            raise MigrationError("本机还没有你的账号，请稍后重试")
        if pw.pw_name not in group_members(GROUP_MANAGED):
            raise MigrationError("账号 %s 不是平台管理的账号" % pw.pw_name)
        st = os.lstat(pw.pw_dir)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != pw.pw_uid:
            raise MigrationError("家目录 %s 异常" % pw.pw_dir)
        return pw

    def _managed_uids(self):
        out = set()
        for n in group_members(GROUP_MANAGED):
            try:
                out.add(pwd.getpwnam(n).pw_uid)
            except KeyError:
                pass
        return out

    def _save_marker(self, final, check=None):
        try:
            if not os.path.isdir(STATE_DIR):
                os.makedirs(STATE_DIR, 0o700)
            with open(self.marker, "w") as f:
                json.dump({"sig": self.sig, "final": final, "check": check or final,
                           "totals": {k: self.p[k] for k in ("bytes_total", "files_total")}}, f)
        except OSError:
            pass

    def _clear_marker(self):
        try:
            os.remove(self.marker)
        except OSError:
            pass

    def _done_before(self):
        """中断后重启：最后一步其实已经完成了（只认同一个任务留下的标记）。"""
        try:
            with open(self.marker) as f:
                m = json.load(f)
        except (OSError, ValueError):
            return None
        if m.get("sig") != self.sig:  # 别的任务（或旧版本）遗留的标记：作废
            self._clear_marker()
            return None
        if m.get("final") and os.path.lexists(m.get("check") or m["final"]):
            self.p.update(m.get("totals") or {})
            self.p["bytes_done"], self.p["files_done"] = self.p["bytes_total"], self.p["files_total"]
            return m["final"]
        return None

    @staticmethod
    def _candidates(home, dest_name):
        yield os.path.join(home, dest_name)
        for i in range(1, 100):
            yield os.path.join(home, "%s_%d" % (dest_name, i))

    def move_into_home(self, path, home, dest_name):
        self.report(force=True, phase="放入家目录")
        for target in self._candidates(home, dest_name):
            if os.path.lexists(target):
                continue
            try:
                os.rename(path, target)
                if os.path.basename(target) != dest_name:
                    self.notes.append("~/%s 已存在，改放在 ~/%s" % (dest_name, os.path.basename(target)))
                return target
            except OSError as e:
                if e.errno not in (errno.EEXIST, errno.ENOTEMPTY, errno.ENOTDIR, errno.EISDIR):
                    raise
        raise MigrationError("家目录里同名文件夹太多")

    def place_link(self, src, home, dest_name, uid, gid):
        for target in self._candidates(home, dest_name):
            try:
                os.symlink(src, target)
            except OSError as e:
                if e.errno == errno.EEXIST:
                    continue
                raise
            os.chown(target, uid, gid, follow_symlinks=False)
            return target
        raise MigrationError("家目录里同名文件夹太多")

    def scan(self, path, skip=(), accumulate=False):
        """统计文件数 / 字节数，并记录每个多链接 inode 在树内出现的次数。skip：顶层要跳过的名字。"""
        files = self.p["files_total"] if accumulate else 0
        size = self.p["bytes_total"] if accumulate else 0
        seen = {}
        st = os.lstat(path)
        if not stat.S_ISDIR(st.st_mode):
            self.report(force=True, files_total=files + 1, bytes_total=size + st.st_size)
            return seen
        for dirpath, dirnames, filenames in os.walk(path):
            if dirpath == path and skip:
                dirnames[:] = [d for d in dirnames if d not in skip]
                filenames = [f for f in filenames if f not in skip]
            files += 1
            for name in dirnames + filenames:
                try:
                    st = os.lstat(os.path.join(dirpath, name))
                except OSError:
                    continue
                if stat.S_ISDIR(st.st_mode):
                    continue
                files += 1
                if stat.S_ISREG(st.st_mode):
                    key = (st.st_dev, st.st_ino)
                    if st.st_nlink > 1:
                        seen[key] = seen.get(key, 0) + 1
                        if seen[key] > 1:
                            continue
                    size += st.st_size
            if files % 2000 == 0:
                self.report(files_total=files, bytes_total=size)
        self.report(force=True, files_total=files, bytes_total=size)
        return seen

    @staticmethod
    def _root_private(st):
        """属于 root 且不可公开读取的文件：不交给成员（以前用 sudo 生成的普通输出一般是 644，不受影响）。"""
        return st.st_uid == 0 and not st.st_mode & 0o004

    @staticmethod
    def _external_link(st, links):
        """该文件是否还有指向目录外的硬链接。"""
        return st.st_nlink > 1 and links.get((st.st_dev, st.st_ino), 0) < st.st_nlink

    def _fix_one(self, path, st, uid, gid, links):
        mode = st.st_mode
        if stat.S_ISLNK(mode):
            os.chown(path, uid, gid, follow_symlinks=False)
        elif stat.S_ISDIR(mode):
            os.chown(path, uid, gid)
            os.chmod(path, (stat.S_IMODE(mode) | 0o700) & ~stat.S_ISUID)
        elif stat.S_ISREG(mode):
            if self._root_private(st):
                self.skipped_private += 1  # 例如脱离了原文件的 /etc/shadow 硬链接副本：保持 root 所有
                return
            if self._external_link(st, links):
                if not mode & 0o004:
                    self.skipped_private += 1
                    return
                # 可公开读取的外部硬链接（如 conda 的包缓存）：复制一份断开链接，避免改到目录外的文件
                tmp = path + ".lab-migrate-tmp"
                shutil.copy2(path, tmp, follow_symlinks=False)
                os.rename(tmp, path)
            os.chown(path, uid, gid)
            os.chmod(path, (stat.S_IMODE(mode) | 0o600) & ~(stat.S_ISUID | stat.S_ISGID))
        elif stat.S_ISCHR(mode) or stat.S_ISBLK(mode):
            self.skipped_devices += 1  # 设备文件不能交给普通用户
        else:
            os.chown(path, uid, gid, follow_symlinks=False)

    def fix_tree(self, top, uid, gid, links):
        """在暂存目录里把整棵树交给成员：属主改为成员，且成员对每个文件都可读写。"""
        self.report(force=True, phase="修改归属与权限", files_done=0)
        done = 0
        self._fix_one(top, os.lstat(top), uid, gid, links)
        for dirpath, dirnames, filenames in os.walk(top):
            for name in dirnames + filenames:
                p = os.path.join(dirpath, name)
                try:
                    st = os.lstat(p)
                except OSError:
                    continue
                self._fix_one(p, st, uid, gid, links)
                done += 1
                if done % 500 == 0:
                    self.report(files_done=done)
        self.report(force=True, files_done=done + 1, bytes_done=self.p["bytes_total"])

    # ---- 复制（源目录可能仍在被别人使用：全程用目录 fd 打开，不跟随符号链接） ----

    def copy_tree(self, base, rel, dst, uid, gid, allow_file, allow_dir=None, text_rules=(), bin_rule=None, skip=()):
        """把 base/rel（文件或目录）复制到 dst，复制时直接设好归属和权限。
        text_rules：[(正则, 新字节串)]，对文本文件做路径替换；bin_rule：二进制文件里的前缀替换（conda）。
        skip：相对 rel 的路径，不复制。已复制且大小、时间一致的文件跳过（中断后续传）。"""
        parent_rel, name = os.path.split(rel.rstrip("/"))
        pfd = open_beneath(base, parent_rel)
        try:
            st = os.stat(name, dir_fd=pfd, follow_symlinks=False)
            if not stat.S_ISDIR(st.st_mode):
                self._copy_entry(name, pfd, st, dst, uid, gid, allow_file, text_rules, bin_rule, {}, name)
                return
            if allow_dir and not allow_dir(st):
                self.skipped_private += 1
                return
            top = os.open(name, os.O_RDONLY | O_DIRECTORY | os.O_NOFOLLOW, dir_fd=pfd)
        finally:
            os.close(pfd)
        inodes, dirs = {}, []
        try:
            for dirpath, dirnames, filenames, dfd in os.fwalk(".", dir_fd=top):
                drel = os.path.normpath(dirpath)
                ddir = dst if drel == "." else os.path.join(dst, drel)
                if not os.path.isdir(ddir):
                    os.mkdir(ddir, 0o700)
                dirs.append((ddir, os.fstat(dfd)))
                self.p["files_done"] += 1
                keep = []
                for n in dirnames + filenames:
                    nrel = n if drel == "." else os.path.join(drel, n)
                    if nrel in skip:
                        continue
                    try:
                        est = os.stat(n, dir_fd=dfd, follow_symlinks=False)
                    except OSError:
                        continue
                    if stat.S_ISDIR(est.st_mode):
                        if allow_dir and not allow_dir(est):
                            self.skipped_private += 1
                        else:
                            keep.append(n)
                        continue
                    self._copy_entry(n, dfd, est, os.path.join(ddir, n), uid, gid, allow_file, text_rules, bin_rule, inodes, nrel)
                dirnames[:] = [n for n in dirnames if n in keep]
        finally:
            os.close(top)
        for ddir, sst in reversed(dirs):
            os.chown(ddir, uid, gid)
            os.chmod(ddir, (stat.S_IMODE(sst.st_mode) | 0o700) & ~stat.S_ISUID)
            os.utime(ddir, ns=(sst.st_atime_ns, sst.st_mtime_ns))
        self.report()

    def _copy_entry(self, name, dfd, st, dpath, uid, gid, allow_file, text_rules, bin_rule, inodes, rel):
        mode = st.st_mode
        self.p["files_done"] += 1
        if stat.S_ISLNK(mode):
            target = os.readlink(name, dir_fd=dfd)
            if text_rules:
                b = target.encode("utf-8", "surrogateescape")
                for rx, new in text_rules:
                    b = rx.sub(new, b)
                target = b.decode("utf-8", "surrogateescape")
            if not os.path.lexists(dpath):
                os.symlink(target, dpath)
            os.chown(dpath, uid, gid, follow_symlinks=False)
            return
        if stat.S_ISFIFO(mode):
            if not os.path.lexists(dpath):
                os.mkfifo(dpath)
            os.chown(dpath, uid, gid)
            return
        if not stat.S_ISREG(mode):
            self.skipped_devices += 1
            return
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0), dir_fd=dfd)
        with os.fdopen(fd, "rb") as fin:
            fst = os.fstat(fin.fileno())  # 以打开后的文件为准，避免检查和使用之间被替换
            if not stat.S_ISREG(fst.st_mode) or not allow_file(fst):
                self.skipped_private += 1
                return
            key = (fst.st_dev, fst.st_ino)
            if fst.st_nlink > 1 and key in inodes:
                if not os.path.lexists(dpath):
                    os.link(inodes[key], dpath)
                return
            rewrite = bool(text_rules or bin_rule)
            same = False
            if not rewrite:
                try:
                    dst_st = os.lstat(dpath)
                    same = stat.S_ISREG(dst_st.st_mode) and dst_st.st_size == fst.st_size and int(dst_st.st_mtime) == int(fst.st_mtime)
                except OSError:
                    pass
            if same:
                self.p["bytes_done"] += fst.st_size
            elif rewrite and fst.st_size <= 256 * 1024 * 1024:
                data = fin.read()
                data = self._rewrite(data, text_rules, bin_rule, rel)
                with open(dpath, "wb") as fout:
                    fout.write(data)
                self.p["bytes_done"] += fst.st_size
            else:
                with open(dpath, "wb") as fout:
                    while True:
                        buf = fin.read(8 * 1024 * 1024)
                        if not buf:
                            break
                        fout.write(buf)
                        self.p["bytes_done"] += len(buf)
                        self.report()
        os.chown(dpath, uid, gid)
        os.chmod(dpath, (stat.S_IMODE(fst.st_mode) | 0o600) & ~(stat.S_ISUID | stat.S_ISGID))
        os.utime(dpath, ns=(fst.st_atime_ns, fst.st_mtime_ns))
        if fst.st_nlink > 1:
            inodes[key] = dpath
        self.report()

    def _rewrite(self, data, text_rules, bin_rule, rel):
        if b"\0" in data:
            # bin_rule = (正则, 新前缀, conda 记录的二进制路径文件集合)；不在集合里的二进制文件原样复制
            if bin_rule and rel in bin_rule[2] and bin_rule[0].search(data):
                res = binary_relocate(data, bin_rule[0], bin_rule[1])
                if res is None:
                    self.reloc_failed.append(rel)
                    return data
                return res
            return data
        for rx, new in text_rules:
            data = rx.sub(new, data)
        return data

    # ---- kind=dir ----

    def _allowed_parents(self):
        homes = {os.path.realpath(pw.pw_dir) for pw in self.agent.legacy_accounts()}
        return set(self.agent.roots), homes

    def execute_dir(self):
        src = os.path.normpath(self.t["source"])
        dest_name = self.t["dest_name"]
        mode = self.t.get("mode") or "move"
        if not DEST_RE.match(dest_name) or dest_name in (".", ".."):
            raise MigrationError("目标文件夹名不合法")
        parent, base = os.path.split(src)
        roots, homes = self._allowed_parents()
        bases = roots | homes
        try:  # 必须在旧账号家目录或迁移目录之下，且不经过其他成员的目录
            if base.startswith(".lab-") or not any(parent == b or parent.startswith(b.rstrip("/") + "/") for b in bases):
                raise ValueError
            self.agent.check_browsable(parent, self._managed_uids())
        except (ValueError, OSError):
            raise MigrationError("只允许迁移旧账号家目录或 %s 下的内容" % "、".join(sorted(roots)))
        in_home = any(parent == h or parent.startswith(h.rstrip("/") + "/") for h in homes)
        if in_home and mode == "link":
            raise MigrationError("旧账号家目录里的内容不能“原地保留”，请选择移动或复制")
        pw = self._user()
        uid, gid, home = pw.pw_uid, pw.pw_gid, pw.pw_dir
        done = self._done_before()
        if done:
            return done
        if mode == "copy":
            return self._copy_dir(src, parent, base, dest_name, uid, gid, home)

        stage_parent = self._staging(parent, None)
        stage = os.path.join(stage_parent, str(self.id))
        if os.path.lexists(src):
            st = os.lstat(src)
            if stat.S_ISLNK(st.st_mode) or not (stat.S_ISDIR(st.st_mode) or stat.S_ISREG(st.st_mode)):
                raise MigrationError("源路径不是普通文件或目录")
            if st.st_uid != 0 and st.st_uid != uid and st.st_uid in self._managed_uids():
                raise MigrationError("该目录已属于其他成员")
            if os.path.lexists(stage):
                raise MigrationError("存在残留的暂存目录 %s，请联系管理员" % stage)
            os.rename(src, stage)  # 同一目录下改名，瞬间完成；之后旧账号就碰不到这些文件了
        elif not os.path.lexists(stage):
            raise MigrationError("源路径 %s 不存在（可能已被移动或删除）" % src)

        self.report(force=True, phase="统计文件")
        links = self.scan(stage)
        home_stage_parent = None
        if mode == "link":
            self.fix_tree(stage, uid, gid, links)
            os.rename(stage, src)
            final = self.place_link(src, home, dest_name, uid, gid)
            self.notes.append("数据仍在 %s，已归你所有" % src)
        elif os.lstat(stage).st_dev == os.stat(home).st_dev:
            self.fix_tree(stage, uid, gid, links)
            final = self.move_into_home(stage, home, dest_name)
        else:
            self._check_space(home, self.p["bytes_total"], restore=(stage, src))
            home_stage_parent = self._staging(os.path.dirname(home.rstrip("/")), os.stat(home).st_dev)
            dst = os.path.join(home_stage_parent, str(self.id))
            self.report(force=True, phase="复制文件", files_done=0, bytes_done=0)
            self.copy_tree(stage_parent, str(self.id), dst, uid, gid,
                           allow_file=lambda fst: not self._root_private(fst)
                           and not (self._external_link(fst, links) and not fst.st_mode & 0o004))
            final = self.move_into_home(dst, home, dest_name)
            self.report(force=True, phase="清理源目录")
            if os.path.isdir(stage):
                shutil.rmtree(stage)
            else:
                os.remove(stage)
        self._save_marker(final)
        self._rmdir_quiet(stage_parent)
        if home_stage_parent:
            self._rmdir_quiet(home_stage_parent)
        if in_home:
            self.notes.append("代码里写死的 %s 路径需要改成 %s" % (src, final))
        return final

    def _check_space(self, home, need_bytes, restore=None):
        st = os.statvfs(home)
        free = st.f_bavail * st.f_frsize
        need = need_bytes + 1024 ** 3
        if free < need:
            if restore:
                os.rename(*restore)  # 还原
            raise MigrationError("家目录所在磁盘空间不足（需要约 %s，剩余 %s）" % (human(need), human(free)))

    def _copy_dir(self, src, parent, base, dest_name, uid, gid, home):
        """复制模式：原文件保持不动，只复制原属主自己的文件和公开可读的文件。"""
        try:
            owner = os.lstat(src).st_uid
        except OSError:
            raise MigrationError("源路径 %s 不存在" % src)
        self.report(force=True, phase="统计文件")
        self.scan(src)
        self._check_space(home, self.p["bytes_total"])
        stage_parent = self._staging(os.path.dirname(home.rstrip("/")), os.stat(home).st_dev)
        dst = os.path.join(stage_parent, str(self.id))
        if os.path.lexists(dst):
            shutil.rmtree(dst) if os.path.isdir(dst) and not os.path.islink(dst) else os.remove(dst)
        self.report(force=True, phase="复制文件", files_done=0, bytes_done=0)
        self.copy_tree(parent, base, dst, uid, gid,
                       allow_file=lambda fst: fst.st_uid == owner or fst.st_mode & 0o004,
                       allow_dir=lambda dst_: dst_.st_uid == owner or dst_.st_mode & 0o005 == 0o005)
        final = self.move_into_home(dst, home, dest_name)
        self._save_marker(final)
        self._rmdir_quiet(stage_parent)
        return final

    # ---- kind=profile ----

    def execute_profile(self):
        opts = self.t.get("options") or {}
        legacy_name = opts.get("legacy_user") or ""
        if legacy_name not in (self.agent.cfg.get("legacy_users") or []):
            raise MigrationError("%s 不是本机配置的旧共享账号" % legacy_name)
        lpw = pwd.getpwnam(legacy_name)
        lhome = lpw.pw_dir.rstrip("/")
        pw = self._user()
        uid, gid, home = pw.pw_uid, pw.pw_gid, pw.pw_dir.rstrip("/")
        done = self._done_before()
        if done:
            return done

        def bound(path):  # 匹配完整路径（后面不能紧跟字母数字，避免 /home/ubuntu 误伤 /home/ubuntu602）
            return re.compile(re.escape(path.encode()) + rb"(?![\w.-])")

        home_rule = (bound(lhome), home.encode())
        owner_ok = lambda fst: fst.st_uid == lpw.pw_uid or fst.st_mode & 0o004  # noqa: E731
        dir_ok = lambda dst_: dst_.st_uid == lpw.pw_uid or dst_.st_mode & 0o005 == 0o005  # noqa: E731

        items, envs, labels = [], [], []
        moved = []  # (共享账号里的原环境, 成员家目录里的新环境, 所在 envs 目录)：自检通过后删掉原环境
        selected = set(opts.get("configs") or [])
        unselected_creds = {p for k, _l, g, ps in CATALOG if g == "credential" and k not in selected for p in ps}
        for key, label, group, paths in CATALOG:
            if key not in selected:
                continue
            labels.append(label.split("（")[0])
            for rel in paths:
                if not os.path.lexists(os.path.join(lhome, rel)):
                    continue
                skip = {c[len(rel) + 1:] for c in unselected_creds if c.startswith(rel + "/")}
                rules = [home_rule] if group in ("config", "software") else []
                items.append({"rel": rel, "text": rules, "bin": None, "skip": skip})
        for c in opts.get("conda") or []:
            name = c.get("name") or ""
            if not name or "/" in name or name in (".", ".."):
                raise MigrationError("conda 目录名不合法")
            root = os.path.join(lhome, name)
            user_envs = name == ".conda"
            if not user_envs and not is_conda_root(root):
                raise MigrationError("%s 不是 conda 安装目录" % root)
            new_root = os.path.join(home, name)
            prefix_rule = (bound(root), new_root.encode())
            text = [prefix_rule, home_rule]
            env_names = c.get("envs") or []
            copy_base = not user_envs and not os.path.lexists(new_root) and (env_names or c.get("base"))
            if copy_base:
                items.append({"rel": name, "text": text, "bin": prefix_rule + (conda_binary_prefix_files(root),),
                              "skip": {"envs", "pkgs"}})
                envs.append(new_root)
                labels.append("conda 本体（%s）" % name)
            for e in env_names:
                if not ENV_RE.match(e) or not os.path.isdir(os.path.join(root, "envs", e, "conda-meta")):
                    raise MigrationError("conda 环境 %s 不存在" % e)
                env_root = os.path.join(root, "envs", e)
                items.append({"rel": os.path.join(name, "envs", e), "text": text,
                              "bin": prefix_rule + (conda_binary_prefix_files(env_root),), "skip": set()})
                envs.append(os.path.join(new_root, "envs", e))
                moved.append((env_root, os.path.join(new_root, "envs", e), os.path.join(root, "envs")))
            if env_names:
                labels.append("conda 环境 " + "、".join(env_names))
        if not items:
            raise MigrationError("旧账号里没有找到所选的内容")

        self.report(force=True, phase="统计文件", files_total=0, bytes_total=0)
        for it in items:
            self.scan(os.path.join(lhome, it["rel"]), skip=it["skip"], accumulate=True)
        self._check_space(home, self.p["bytes_total"])

        stage_parent = self._staging(os.path.dirname(home), os.stat(home).st_dev)
        stage = os.path.join(stage_parent, str(self.id))
        if os.path.lexists(stage):
            shutil.rmtree(stage)
        os.mkdir(stage, 0o700)
        self.p.update(files_done=0, bytes_done=0)
        for it in items:
            dst = os.path.join(stage, it["rel"])
            self._makedirs_owned(stage, os.path.dirname(it["rel"]), uid, gid)
            self.report(force=True, phase="复制 " + it["rel"])
            self.copy_tree(lhome, it["rel"], dst, uid, gid, owner_ok, dir_ok, it["text"], it["bin"], it["skip"])

        # 交给成员：先整体移进家目录里的临时文件夹，再以成员身份放到各自的位置
        self.report(force=True, phase="放入家目录", bytes_done=self.p["bytes_total"])
        os.chown(stage, uid, gid)
        incoming = self.move_into_home(stage, home, ".lab-incoming-%d" % self.id)
        rels = sorted({it["rel"] for it in items})
        top = [r for r in rels if not any(r.startswith(o + "/") for o in rels if o != r)]
        if any(os.path.lexists(os.path.join(home, r)) for r in top):
            self.notes.append("家目录里原有的同名文件已备份到 ~/.lab-backup/")
        result = self._place_as_user(pw, incoming, home, top, envs)
        bad = {k: v for k, v in (result.get("checks") or {}).items() if v != "ok"}
        if envs:
            if bad:
                self.notes.append("conda 自检失败：" + "；".join("%s：%s" % (os.path.basename(k), v[:100]) for k, v in bad.items()))
            else:
                self.notes.append("conda 自检通过（%s），重新登录后用 conda activate 环境名 使用" % "、".join(os.path.basename(e) for e in envs))
        # conda 环境是「移动」：自检通过的才删掉共享账号里的原环境，没通过的保留，方便重试
        for src, new, envs_dir in moved:
            name = os.path.basename(src)
            if (result.get("checks") or {}).get(new) != "ok":
                self.notes.append("conda 环境 %s 自检未通过，原环境仍保留在共享账号里" % name)
                continue
            self.report(force=True, phase="清理共享账号里的原环境 " + name)
            err = remove_moved_env(src, envs_dir)
            self.notes.append("conda 环境 %s 已从共享账号移走" % name if err is None
                              else "conda 环境 %s 已复制到你的家目录，但没能删除共享账号里的原环境：%s" % (name, err))
        self._rmdir_quiet(stage_parent)
        final = "%s（%s）" % (home, "、".join(labels))
        self._save_marker(final, check=home)
        return final

    @staticmethod
    def _makedirs_owned(stage, rel_dir, uid, gid):
        cur = stage
        for part in [p for p in rel_dir.split("/") if p]:
            cur = os.path.join(cur, part)
            if not os.path.isdir(cur):
                os.mkdir(cur, 0o700 if part == ".ssh" else 0o755)
                os.chown(cur, uid, gid)

    def _place_as_user(self, pw, incoming, home, rels, envs):
        def demote():
            os.setgroups([])
            os.setgid(pw.pw_gid)
            os.setuid(pw.pw_uid)

        env = {"HOME": home, "USER": pw.pw_name, "PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8"}
        p = subprocess.run([sys.executable, "-c", PLACE_SCRIPT, incoming, home, json.dumps(rels), json.dumps(envs)],
                           preexec_fn=demote, cwd=home, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True, timeout=3600)
        if p.returncode != 0:
            raise MigrationError("放入家目录失败：%s" % p.stderr.strip()[-300:])
        try:
            return json.loads(p.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return {}


def main():
    ap = argparse.ArgumentParser(description="Lab GPU server agent")
    ap.add_argument("--config", default=CONFIG_PATH)
    ap.add_argument("--once", action="store_true", help="只同步一次")
    ap.add_argument("--dry-run", action="store_true", help="只打印操作，不修改系统")
    ap.add_argument("--relocate", nargs=2, metavar=("OLD_HOME", "NEW_HOME"), help=argparse.SUPPRESS)  # 内部使用：改名后修正路径
    ap.add_argument("--report-only", action="store_true", help="只打印采集到的 GPU 信息")
    args = ap.parse_args()
    if args.relocate:
        return relocate_main(*args.relocate)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    with open(args.config) as f:
        cfg = json.load(f)
    agent = Agent(cfg, dry_run=args.dry_run)
    if args.report_only:
        print(json.dumps(agent.collect_gpus(), indent=2, ensure_ascii=False))
        return
    if not args.dry_run and os.geteuid() != 0:
        sys.exit("需要 root 权限运行（或使用 --dry-run）")

    if args.once:
        agent.once()
        if agent.migration:
            agent.migration.join()
        for t in list(agent.relocating.values()):
            t.join()
        return
    threading.Thread(target=agent.candidate_loop, daemon=True, name="candidates").start()
    threading.Thread(target=agent.usage_loop, daemon=True, name="usage").start()
    threading.Thread(target=agent.browse_loop, daemon=True, name="browse-poll").start()
    while True:
        try:
            agent.once()
        except urllib.error.HTTPError as e:
            log.error("平台返回错误 %s: %s", e.code, e.read()[:200])
        except Exception as e:  # noqa: BLE001  网络抖动等：保持现状，下次再试
            log.error("同步失败（保持当前状态）: %s", e)
        # 迁移进行中时缩短间隔，让新任务和状态更快生效
        busy = (agent.migration and agent.migration.is_alive()) or agent.pending_migrations
        agent.wake.wait(min(10, agent.interval) if busy else agent.interval)
        agent.wake.clear()


if __name__ == "__main__":
    main()
