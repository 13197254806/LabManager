"""Agent 的同步循环：不碰真实系统（sync / reconcile 都打桩），只验证迁移任务的调度。"""
import importlib.util
import os
import urllib.error
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("lab_agent", Path(__file__).resolve().parents[2] / "agent" / "lab_agent.py")
lab_agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lab_agent)


def make_agent(listed, attempts=None):
    a = lab_agent.Agent({"server_url": "http://x", "token": "t"})
    a.sync = lambda: {"server": "s", "date": "d", "users": [], "migrations": [{"id": i} for i in listed[0]],
                      "migration_attempts": dict(attempts or {})}
    a.reconcile = lambda users: setattr(a, "accounts", [])
    a._sent_accounts = []
    started = []
    a.handle_migrations = lambda tasks: started.extend((t["id"], t["_attempt"]) for t in tasks)
    return a, started


def test_failed_migration_can_be_retried_without_restarting_agent():
    listed = [[7]]
    a, started = make_agent(listed)
    a.once()
    assert started == [(7, 0)]
    a.finished.add((7, 0))     # 迁移线程跑完（失败）后记下
    a.once()                   # 平台还没处理上报：仍列着它，不能重复执行
    assert started == [(7, 0)]
    listed[0] = []             # 平台已记为 failed，不再列出
    a.once()
    assert not a.finished
    listed[0] = [7]            # 成员点了“重试”：重新排队
    a.once()
    assert started == [(7, 0), (7, 0)]


def test_quick_retry_is_not_mistaken_for_already_done():
    """结果上报后很快就点了重试：Agent 从没看到它“不在列表里”，但次数变了，仍要重新执行。"""
    listed, attempts = [[7]], {"7": 0}
    a, started = make_agent(listed, attempts)
    a.sync = lambda: {"server": "s", "date": "d", "users": [], "migrations": [{"id": 7}], "migration_attempts": dict(attempts)}
    a.once()
    a.finished.add((7, 0))
    attempts["7"] = 1
    a.once()
    assert started == [(7, 0), (7, 1)]


def test_remove_moved_env(tmp_path):
    envs = tmp_path / "anaconda3" / "envs"
    (envs / "torch" / "conda-meta").mkdir(parents=True)
    (envs / "torch" / "bin").mkdir()
    (envs / "torch" / "bin" / "python").write_text("x")
    # 正常的环境目录被删除
    assert lab_agent.remove_moved_env(str(envs / "torch"), str(envs)) is None
    assert not (envs / "torch").exists()
    # 不带 conda-meta 的目录、符号链接、不在 envs 目录下的路径都不会被删
    (envs / "plain").mkdir()
    assert lab_agent.remove_moved_env(str(envs / "plain"), str(envs)) is not None
    assert (envs / "plain").exists()
    (tmp_path / "outside" / "conda-meta").mkdir(parents=True)
    (envs / "link").symlink_to(tmp_path / "outside")
    assert lab_agent.remove_moved_env(str(envs / "link"), str(envs)) is not None
    assert (tmp_path / "outside" / "conda-meta").exists()
    assert lab_agent.remove_moved_env(str(tmp_path / "outside"), str(envs)) is not None
    assert (tmp_path / "outside").exists()


def test_stale_marker_from_another_task_is_ignored(tmp_path, monkeypatch):
    """平台数据库重建后任务 id 会重复：旧任务留下的“已完成”标记不能让新任务直接报告完成。"""
    monkeypatch.setattr(lab_agent, "STATE_DIR", str(tmp_path))
    home = tmp_path / "home"
    home.mkdir()
    old = {"id": 1, "kind": "profile", "uid": 1001, "source": "/home/ubuntu", "options": {"configs": ["bashrc"]},
           "created_at": "2026-10-03T10:00:00"}
    new = dict(old, created_at="2026-10-05T14:00:00")
    m_old = lab_agent.Migration(None, old)
    m_old.p.update(bytes_total=5, files_total=2)
    m_old._save_marker("%s（旧）" % home, check=str(home))
    # 同一个任务中断后重启：沿用标记，并带回当时的统计
    m_same = lab_agent.Migration(None, old)
    assert m_same._done_before() == "%s（旧）" % home
    assert m_same.p["files_total"] == 2
    # id 相同但不是同一个任务：作废并删除标记，必须真正执行
    m_new = lab_agent.Migration(None, new)
    assert m_new._done_before() is None
    assert not (tmp_path / "migration-1.json").exists()


def _fake_home(tmp_path, old="/home/sct"):
    """旧路径 /home/sct 的家目录（已搬到 tmp_path/new）：conda 本体 + 一个环境 + 配置 + venv + 符号链接。"""
    home = tmp_path / "new"
    root = home / "anaconda3"
    (root / "conda-meta").mkdir(parents=True)
    (root / "bin").mkdir()
    (root / "pkgs" / "x").mkdir(parents=True)
    (root / "pkgs" / "x" / "info.txt").write_text(old + "/anaconda3/pkgs/x\n")
    # conda 记录了 bin/python 是做过二进制路径替换的文件
    (root / "conda-meta" / "python-3.9.json").write_text(
        '{"extracted_package_dir": "%s/anaconda3/pkgs/python", "paths_data": {"paths": '
        '[{"_path": "bin/python", "file_mode": "binary", "prefix_placeholder": "/opt/placeholder"}]}}' % old)
    binary = b"\x7fELF\0" + (old + "/anaconda3").encode() + b"\0" * 200
    (root / "bin" / "python").write_bytes(binary)
    (root / "bin" / "other.bin").write_bytes(b"\0" + old.encode() + b"\0")   # 不在 conda 记录里：不能动
    (root / "bin" / "conda").write_text("#!%s/anaconda3/bin/python\nprint(1)\n" % old)
    (root / "bin" / "conda").chmod(0o755)
    env = root / "envs" / "e1"
    (env / "conda-meta").mkdir(parents=True)
    (env / "bin").mkdir()
    (env / "bin" / "pip").write_text("#!%s/anaconda3/envs/e1/bin/python\n" % old)
    (home / ".bashrc").write_text("# >>> conda init >>>\n__conda_setup=\"$('%s/anaconda3/bin/conda' shell.bash hook)\"\n" % old)
    (home / ".conda").mkdir()
    (home / ".conda" / "environments.txt").write_text("%s/anaconda3/envs/e1\n/home/sct2/other\n" % old)
    (home / "proj" / ".venv" / "bin").mkdir(parents=True)
    (home / "proj" / ".venv" / "pyvenv.cfg").write_text("home = %s/anaconda3/bin\n" % old)
    (home / "proj" / ".venv" / "bin" / "activate").write_text('VIRTUAL_ENV="%s/proj/.venv"\n' % old)
    (home / "proj" / "train.py").write_text("DATA = '%s/data'\n" % old)  # 用户自己的代码：不动，靠兼容链接
    (home / "data").symlink_to(old + "/datasets")
    (home / "keep").symlink_to("/mnt/home/sct/x")
    return home, binary


def test_relocate_fixes_conda_and_configs_after_rename(tmp_path):
    old, new = "/home/sct", str(tmp_path / "new")
    home, binary = _fake_home(tmp_path)
    res = lab_agent.Relocator(old, new).run(new)
    assert res["failed_count"] == 0
    r = home / "anaconda3"
    assert (r / "bin" / "conda").read_text().startswith("#!%s/anaconda3/bin/python" % new)
    assert (r / "bin" / "conda").stat().st_mode & 0o777 == 0o755
    assert old not in (r / "conda-meta" / "python-3.9.json").read_text()
    assert (home / "anaconda3" / "envs" / "e1" / "bin" / "pip").read_text() == "#!%s/anaconda3/envs/e1/bin/python\n" % new
    assert new in (home / ".bashrc").read_text() and old not in (home / ".bashrc").read_text()
    assert (home / ".conda" / "environments.txt").read_text() == "%s/anaconda3/envs/e1\n/home/sct2/other\n" % new
    assert new in (home / "proj" / ".venv" / "bin" / "activate").read_text()
    assert new in (home / "proj" / ".venv" / "pyvenv.cfg").read_text()
    assert os.readlink(home / "data") == new + "/datasets"
    assert os.readlink(home / "keep") == "/mnt/home/sct/x"       # 只是前缀碰巧相似的别处路径
    # 二进制：只改 conda 记录过的文件，长度不变
    out = (r / "bin" / "python").read_bytes()
    assert len(out) == len(binary) and (new + "/anaconda3").encode() in out
    assert (r / "bin" / "other.bin").read_bytes() == b"\0" + old.encode() + b"\0"
    # 包缓存和用户自己的代码不动
    assert old in (r / "pkgs" / "x" / "info.txt").read_text()
    assert old in (home / "proj" / "train.py").read_text()
    # 幂等：再跑一遍什么都不改
    assert lab_agent.Relocator(old, new).run(new)["changed"] == 0


def test_relocate_reports_binary_that_cannot_fit(tmp_path):
    home = tmp_path / "h"
    (home / "c" / "conda-meta").mkdir(parents=True)
    (home / "c" / "bin").mkdir()
    (home / "c" / "conda-meta" / "p.json").write_text(
        '{"paths_data": {"paths": [{"_path": "bin/p", "file_mode": "binary", "prefix_placeholder": "/x"}]}}')
    (home / "c" / "bin" / "conda").write_text("x")
    (home / "c" / "bin" / "p").write_bytes(b"\0/home/a\0\x01\x02")      # 后面没有 NUL 余量
    res = lab_agent.Relocator("/home/a", "/home/much_longer_name").run(str(home))
    assert res["failed_count"] == 1
    assert (home / "c" / "bin" / "p").read_bytes() == b"\0/home/a\0\x01\x02"


def test_relocate_does_not_follow_symlinks_out_of_home(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("/home/sct/secret\n")
    home = tmp_path / "h"
    (home / ".config").mkdir(parents=True)
    (home / ".config" / "evil").symlink_to(outside)
    (home / ".config" / "evildir").symlink_to(tmp_path)
    lab_agent.Relocator("/home/sct", str(home)).run(str(home))
    assert outside.read_text() == "/home/sct/secret\n"


@pytest.mark.skipif(not os.path.isdir("/proc/self"), reason="需要 Linux 的 /proc")
def test_proc_owner_unknown_process_has_no_user():
    assert lab_agent.proc_owner(2 ** 22 + 12345) == ("", "")
    user, path = lab_agent.proc_owner(os.getpid())
    assert user and path


def test_rename_moves_home_leaves_link_and_schedules_path_fix(tmp_path, monkeypatch):
    base = tmp_path / "home"
    (base / "sct").mkdir(parents=True)
    a = lab_agent.Agent({"server_url": "http://x", "token": "t"})
    ran, relocated = [], []

    def fake_run(*cmd, **kw):
        ran.append(cmd)
        if cmd[:2] == ("usermod", "-d"):
            os.rename(base / "sct", cmd[2])
        return True, ""

    monkeypatch.setattr(a, "run", fake_run)
    monkeypatch.setattr(a, "start_relocate", lambda uid, old, new: relocated.append((uid, old, new)))
    monkeypatch.setattr(lab_agent, "has_processes", lambda uid: False)
    monkeypatch.setattr(a, "_exists", lambda name: False)
    pw = lab_agent.pwd.struct_passwd(("sct", "x", 4242, 4242, "", str(base / "sct"), "/bin/bash"))
    assert a.rename_user(pw, "tian")[0] == "ok"
    assert (base / "tian").is_dir() and os.readlink(base / "sct") == str(base / "tian")
    assert relocated == [(4242, str(base / "sct"), str(base / "tian"))]

    # 进程还在跑 / 迁移进行中：不改名
    relocated.clear()
    monkeypatch.setattr(lab_agent, "has_processes", lambda uid: True)
    assert a.rename_user(pw, "tian2")[0] == "pending"
    monkeypatch.setattr(lab_agent, "has_processes", lambda uid: False)
    a.migration = lab_agent.Migration(a, {"id": 1, "uid": 4242, "user_id": 5})
    a.migration.is_alive = lambda: True
    assert a.rename_user(pw, "tian2")[0] == "pending"
    # 接管的已有账号：本机 uid 与平台不同，按 GECOS 里的平台 id 认出是同一个人
    a.migration = lab_agent.Migration(a, {"id": 1, "uid": 10005, "user_id": 5})
    a.migration.is_alive = lambda: True
    pw_adopted = lab_agent.pwd.struct_passwd(("sct", "x", 1001, 1001, "lab-agent #5", str(base / "sct"), "/bin/bash"))
    assert a.rename_user(pw_adopted, "tian2")[0] == "pending"
    assert relocated == []


def test_migration_finds_adopted_account_whose_uid_differs_from_platform(tmp_path, monkeypatch):
    """接管的已有账号（如 wsm，本机 uid 1001，平台 uid 10013）：迁移要按 GECOS 里的平台 id 找到它，
    而不是按平台 uid 找不到就报“本机还没有你的账号”。"""
    home = tmp_path / "wsm"
    home.mkdir()
    me = os.getuid()
    users = {
        "wsm": lab_agent.pwd.struct_passwd(("wsm", "x", me, me, "lab-agent #13", str(home), "/bin/bash")),
        "other": lab_agent.pwd.struct_passwd(("other", "x", 10013, 10013, "lab-agent #99", "/nonexistent", "/bin/bash")),
    }

    def getpwnam(n):
        return users[n]

    monkeypatch.setattr(lab_agent.pwd, "getpwnam", getpwnam)
    monkeypatch.setattr(lab_agent, "group_members", lambda g: {"wsm"})
    a = lab_agent.Agent({"server_url": "http://x", "token": "t"})
    assert lab_agent.Migration(a, {"id": 1, "uid": 10013, "user_id": 13})._user().pw_name == "wsm"
    # 平台 uid 在本机属于别的（非托管）账号、平台 id 也对不上：仍然找不到，不会错把文件交给别人
    with pytest.raises(lab_agent.MigrationError, match="还没有你的账号"):
        lab_agent.Migration(a, {"id": 2, "uid": 10013, "user_id": 14})._user()


def test_final_migration_result_is_retried_until_platform_receives_it(monkeypatch):
    """完成时隧道正好断了：结果要一直重试，不能丢（否则平台上永远显示“进行中”）；平台明确拒绝才放弃。"""
    a = lab_agent.Agent({"server_url": "http://x", "token": "t"})
    sent, failures = [], [OSError("Connection refused"), OSError("timed out")]

    def post(path, payload, timeout=20):
        if failures:
            raise failures.pop(0)
        sent.append((path, payload["status"], payload["dest_path"]))
        return {"ok": True}

    a.post = post
    m = lab_agent.Migration(a, {"id": 9, "uid": 10014})
    waits = []
    m.retry_wait = waits.append
    assert m.report_final("done", phase="完成", dest_path="/home/lgq")
    assert sent == [("/api/agent/migrations/9", "done", "/home/lgq")]
    assert waits == [5, 10]

    def rejected(path, payload, timeout=20):
        raise urllib.error.HTTPError(path, 404, "Not Found", {}, None)

    a.post = rejected
    assert not m.report_final("failed", phase="失败")


def _pw(name, uid, gecos, home="/nonexistent"):
    return lab_agent.pwd.struct_passwd((name, "x", uid, uid, gecos, home, "/bin/bash"))


def test_find_local_prefers_platform_id_over_uid(monkeypatch):
    """系统分配的 uid 可能正好等于另一个成员的平台 uid：GECOS 里记了别人 id 的账号，uid 对上也不能认。"""
    users = {
        "alice": _pw("alice", 10015, "lab-agent #3"),        # 建号时平台 uid 被占，系统分配到 10015
        "bob": _pw("bob", 1002, "lab-agent #15,Room 1,,"),   # 平台 uid 10015 的成员（chfn 加过房间号）
        "old": _pw("old", 10020, "Old User,,,"),             # 早期没记 id 的账号：只能按 uid 认
    }
    monkeypatch.setattr(lab_agent.pwd, "getpwnam", lambda n: users[n])
    a = lab_agent.Agent({"server_url": "http://x", "token": "t"})
    managed = set(users)
    assert a.find_local({"id": 15, "uid": 10015}, managed).pw_name == "bob"
    assert a.find_local({"id": 3, "uid": 10003}, managed).pw_name == "alice"
    assert a.find_local({"id": 15, "uid": 10015}, {"alice"}) is None  # 只有 uid 对上、id 是别人的
    assert a.find_local({"id": 20, "uid": 10020}, managed).pw_name == "old"
    m = lab_agent.Migration(a, {"id": 1, "uid": 10015, "user_id": 15})
    assert m.owns(users["bob"]) and not m.owns(users["alice"])


def test_reconcile_does_not_adopt_another_members_account(monkeypatch):
    """A 改名（sct→tian）还没生效时，B 在平台上用了 sct 这个名字：不能把 A 的账号当成已有账号接管给 B。"""
    users = {"sct": _pw("sct", 10001, "lab-agent #1")}

    def getpwnam(n):
        return users[n]

    monkeypatch.setattr(lab_agent.pwd, "getpwnam", getpwnam)
    monkeypatch.setattr(lab_agent, "group_members", lambda g: {"sct"} if g == lab_agent.GROUP_MANAGED else set())
    monkeypatch.setattr(lab_agent, "read_shadow", lambda: {"sct": ("*", "")})
    monkeypatch.setattr(lab_agent, "has_processes", lambda uid: True)  # A 还有进程：改名暂缓
    a = lab_agent.Agent({"server_url": "http://x", "token": "t"}, dry_run=True)
    ran = []
    monkeypatch.setattr(a, "run", lambda *cmd, **kw: (ran.append(cmd), (True, ""))[1])
    monkeypatch.setattr(a, "apply_gpu_acl", lambda allowed: None)
    a.reconcile([{"id": 1, "uid": 10001, "username": "tian"}, {"id": 2, "uid": 10002, "username": "sct"}])
    st = {s["id"]: s for s in a.accounts}
    assert st[1]["state"] == "pending" and st[1]["username"] == "sct"
    assert st[2]["state"] == "pending"
    assert not any(c[0] in ("usermod", "useradd") for c in ran)


def test_cancelled_task_is_not_executed(monkeypatch):
    """成员在 Agent 取到任务之后、开始之前取消了：认领时平台回复已取消，什么都不做。"""
    a = lab_agent.Agent({"server_url": "http://x", "token": "t"})
    m = lab_agent.Migration(a, {"id": 5, "uid": 10001, "user_id": 1, "_attempt": 2})
    m.execute_dir = lambda: pytest.fail("不应开始迁移")
    a.post = lambda path, payload, timeout=20: {"ok": False, "status": "cancelled"}
    m.run()
    assert a.finished == {(5, 2)}
    # 连不上平台：不开始，也不记为执行过（下次同步再试）
    a.finished.clear()

    def down(path, payload, timeout=20):
        raise OSError("Connection refused")

    a.post = down
    lab_agent.Migration(a, {"id": 5, "uid": 10001, "user_id": 1}).run()
    assert a.finished == set()


def test_report_payload_fits_platform_limits():
    a = lab_agent.Agent({"server_url": "http://x", "token": "t"})
    sent = []
    a.post = lambda path, payload, timeout=20: sent.append(payload) or {"ok": True}
    m = lab_agent.Migration(a, {"id": 1, "uid": 10001})
    m.report_final("done", phase="复制 " + "x" * 100, message="m" * 5000, dest_path="/home/u（" + "环境、" * 300 + "）")
    p = sent[-1]
    assert len(p["phase"]) <= 64 and len(p["message"]) <= 2000 and len(p["dest_path"]) <= 512


def test_gpu_acl_denies_group_and_allows_reserved_users(monkeypatch):
    a = lab_agent.Agent({"server_url": "http://x", "token": "t"})
    calls = []
    acls = {"/dev/nvidia0": {"g:lab-users": "---", "u:sct": "rw-", "u:old": "rw-", "u:gdm": "rw-"},
            "/dev/nvidia3": {}}                                   # 设备号 3 对应 nvidia-smi 序号 1
    monkeypatch.setattr(lab_agent.shutil, "which", lambda n: "/usr/bin/" + n)
    monkeypatch.setattr(a, "gpu_minors", lambda: {0: 0, 1: 3})
    monkeypatch.setattr(lab_agent.os.path, "exists", lambda p: p in acls)
    monkeypatch.setattr(a, "_read_acl", lambda dev: dict(acls[dev]))
    monkeypatch.setattr(a, "run", lambda *cmd, **kw: calls.append(cmd) or (True, ""))
    monkeypatch.setattr(lab_agent, "group_members", lambda g: {"sct", "old", "tian"})
    a.apply_gpu_acl({0: {"sct"}, 1: {"tian"}})
    assert ("setfacl", "-x", "u:old", "/dev/nvidia0") in calls                      # 以前开放过、现在没预约
    assert not any("u:gdm" in c for c in calls)                                    # 非成员的条目不碰
    assert ("setfacl", "-m", "g:lab-users:---,u:tian:rw-", "/dev/nvidia3") in calls
    assert not any(c[1] == "-m" and c[3] == "/dev/nvidia0" for c in calls)         # 0 号卡已经是对的：不重复设置


def test_kill_job_only_kills_verified_processes(monkeypatch):
    a = lab_agent.Agent({"server_url": "http://x", "token": "t"})
    killed = []
    gone = set()
    monkeypatch.setattr(a, "collect_gpus", lambda: [{"index": 0, "processes": [{"pid": 11}, {"pid": 12}, {"pid": 13}]}])
    monkeypatch.setattr(lab_agent, "group_members", lambda g: {"alice"})
    monkeypatch.setattr(a, "can_manage", lambda n: n == "alice")
    users = {"alice": 2001, "root": 0}
    monkeypatch.setattr(lab_agent.pwd, "getpwnam", lambda n: type("P", (), {"pw_uid": users[n]})())
    monkeypatch.setattr(lab_agent.os, "stat", lambda p: type("S", (), {"st_uid": 2001 if p.endswith("/11") else 2002})())
    monkeypatch.setattr(lab_agent.os, "kill", lambda pid, sig: (killed.append((pid, sig)), gone.add(pid)))
    monkeypatch.setattr(a, "_gone", lambda pid: pid in gone)
    a._kill_job([(11, "alice", 0),      # 正常
                 (12, "alice", 0),      # 进程属主不是 alice（进程号被复用）
                 (13, "root", 0),       # 不是托管账号
                 (99, "alice", 0),      # 已不在这张卡上
                 (11, "alice", 1)])     # 序号对不上
    assert killed == [(11, lab_agent.signal.SIGTERM)]
    assert a.killed == [{"gpu": 0, "pid": 11, "user": "alice"}]


def test_heal_tunnel_restarts_chisel_only_after_long_silence(monkeypatch):
    a = lab_agent.Agent({"server_url": "http://127.0.0.1:18765", "token": "t"})
    monkeypatch.setattr(lab_agent.os.path, "exists", lambda p: True)
    calls = []
    monkeypatch.setattr(lab_agent.subprocess, "run",
                        lambda cmd, **kw: calls.append(cmd) or type("P", (), {"returncode": 0, "stdout": ""})())
    t = [1000.0]
    monkeypatch.setattr(lab_agent.time, "time", lambda: t[0])
    a.last_contact = 1000.0
    t[0] += lab_agent.TUNNEL_HEAL_SECONDS - 1
    a.heal_tunnel()
    assert calls == []                      # 断得不够久
    t[0] += 2
    a.heal_tunnel()
    assert calls == [["systemctl", "restart", "lab-chisel"]]
    t[0] += 10
    a.heal_tunnel()
    assert len(calls) == 1                  # 冷却期内不重复重启


def test_heal_tunnel_skips_direct_connection(monkeypatch):
    a = lab_agent.Agent({"server_url": "https://lab.example.com", "token": "t"})
    monkeypatch.setattr(lab_agent.os.path, "exists", lambda p: True)
    monkeypatch.setattr(lab_agent.subprocess, "run", lambda *a, **k: pytest.fail("不该重启"))
    a.last_contact = 0
    a.heal_tunnel()


class _Resp:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self.body


def test_post_falls_back_to_tunnel_and_retries_direct_later(monkeypatch):
    a = lab_agent.Agent({"server_url": "http://1.2.3.4", "fallback_url": "http://127.0.0.1:18765", "token": "t"})
    direct_up = [False]
    seen = []

    def urlopen(req, timeout):
        seen.append(req.full_url)
        if req.full_url.startswith("http://1.2.3.4"):
            if not direct_up[0]:
                return _Resp(b"<html>campus login</html>")  # 认证掉线：被重定向到认证页
        return _Resp(b'{"ok": 1}')

    monkeypatch.setattr(lab_agent.urllib.request, "urlopen", urlopen)
    t = [1000.0]
    monkeypatch.setattr(lab_agent.time, "time", lambda: t[0])
    assert a.post("/x", {}) == {"ok": 1}
    assert seen == ["http://1.2.3.4/x", "http://127.0.0.1:18765/x"] and a.base_url.startswith("http://127.0.0.1")
    seen.clear()
    a.post("/x", {})
    assert seen == ["http://127.0.0.1:18765/x"]          # 刚失败过，先走隧道
    direct_up[0] = True
    t[0] += lab_agent.PRIMARY_RETRY_SECONDS + 1
    seen.clear()
    a.post("/x", {})
    assert seen == ["http://1.2.3.4/x"] and a.base_url == "http://1.2.3.4"


def test_sync_reports_link_and_lan_ip_only_with_fallback(monkeypatch):
    sent = []
    for cfg, has_ip in (({"server_url": "http://127.0.0.1:18765", "token": "t"}, False),
                        ({"server_url": "http://1.2.3.4", "fallback_url": "http://127.0.0.1:18765", "token": "t"}, True)):
        a = lab_agent.Agent(cfg)
        monkeypatch.setattr(a, "collect_gpus", lambda: [])
        monkeypatch.setattr(a, "collect_system", lambda: {})
        monkeypatch.setattr(a, "lan_ip", lambda: "10.0.0.5")
        monkeypatch.setattr(a, "post", lambda path, payload, **kw: sent.append(payload) or {})
        a.sync()
        assert ("lan_ip" in sent[-1]) is has_ip
    assert [p["link"] for p in sent] == ["tunnel", "direct"]


def _clock_agent(monkeypatch, synced=False):
    a = lab_agent.Agent({"server_url": "http://127.0.0.1:18765", "token": "t"})
    monkeypatch.setattr(a, "ntp_synced", lambda: synced)
    t = [10000.0]
    monkeypatch.setattr(lab_agent.time, "time", lambda: t[0])
    stepped = []

    def settime(clk, value):
        stepped.append(value - t[0])
        t[0] = value

    monkeypatch.setattr(lab_agent.time, "clock_settime", settime, raising=False)
    return a, t, stepped


def test_clock_is_set_to_platform_time_when_off(monkeypatch):
    a, t, stepped = _clock_agent(monkeypatch)
    a.last_contact = 10000.0
    a.fix_clock(server_time=10000.5 - 960, t0=10000.0, t1=10001.0)  # 本机快 16 分钟
    assert stepped == [-960]
    assert a.last_contact == 10000.0 - 960   # 记下的时刻跟着平移
    assert a.last_heal == 0.0                # 没记过的不动


def test_clock_left_alone_when_close_ntp_synced_slow_or_old_platform(monkeypatch):
    a, t, stepped = _clock_agent(monkeypatch)
    a.fix_clock(10000.5 + 1, 10000.0, 10001.0)   # 差 1 秒：不动
    a.fix_clock(10002.5 - 600, 10000.0, 10005.0)  # 往返 5 秒：误差太大，这轮不校
    a.fix_clock(None, 10000.0, 10001.0)           # 旧平台不下发时间
    assert stepped == []
    a, t, stepped = _clock_agent(monkeypatch, synced=True)
    a.fix_clock(10000.5 - 960, 10000.0, 10001.0)  # 本机 NTP 正常：交给系统
    assert stepped == []


def test_data_disk_dir_is_created_for_member_and_rejects_others(tmp_path):
    mount = tmp_path / "zdd"
    mount.mkdir()
    a = lab_agent.Agent({"server_url": "http://x", "token": "t"})
    a.data_disks = lambda: [str(mount)]
    me, gid = os.getuid(), os.getgid()
    pw = lambda name, uid=me: lab_agent.pwd.struct_passwd((name, "x", uid, gid, "lab-agent #1", str(tmp_path / "home"), "/bin/bash"))
    m = lab_agent.Migration(a, {"id": 1, "uid": me, "dest_root": str(mount)})
    assert m.dest_dir(pw("alice"), "move") == (str(mount / "alice"),) * 2
    assert (mount / "alice").is_dir()
    assert m.dest_dir(pw("alice"), "copy")[0] == str(mount / "alice")   # 已有的自己的目录：直接用
    assert m.dest_dir(pw("alice"), "link") == (str(tmp_path / "home"), "~")  # 原地保留：快捷方式总在家目录
    (mount / "bob").mkdir()
    with pytest.raises(lab_agent.MigrationError, match="不属于你"):
        m.data_dir(str(mount), pw("bob", uid=me + 1))
    (mount / "eve").symlink_to(tmp_path)                  # 被抢先放了一个指向别处的链接
    with pytest.raises(lab_agent.MigrationError, match="不是目录"):
        m.data_dir(str(mount), pw("eve"))
    with pytest.raises(lab_agent.MigrationError, match="不是这台服务器上的数据盘"):
        m.data_dir("/etc", pw("alice"))
    assert lab_agent.Migration(a, {"id": 2, "uid": me}).dest_dir(pw("alice"), "move") == (str(tmp_path / "home"), "~")


def test_data_disk_usage_lists_top_level_entries(tmp_path, monkeypatch):
    mount = tmp_path / "zdd"
    (mount / "alice").mkdir(parents=True)
    (mount / ".lab-migrate").mkdir()
    (mount / "big.tar").write_bytes(b"x" * 10)
    a = lab_agent.Agent({"server_url": "http://x", "token": "t"})
    a.data_disks = lambda: [str(mount)]
    monkeypatch.setattr(lab_agent.subprocess, "run",
                        lambda cmd, **kw: type("P", (), {"stdout": "%d\t%s\n" % (3 * 1024 * 1024, cmd[-1])})())
    a.scan_data_usage()
    rows = a.data_usage[str(mount)]
    assert [r["path"] for r in rows] == ["alice", "big.tar"]   # 迁移暂存目录不列出
    assert rows[0]["size"] == 3 and rows[0]["user"] == lab_agent.pwd.getpwuid(os.getuid()).pw_name
