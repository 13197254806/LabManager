"""Agent 的同步循环：不碰真实系统（sync / reconcile 都打桩），只验证迁移任务的调度。"""
import importlib.util
import os
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("lab_agent", Path(__file__).resolve().parents[2] / "agent" / "lab_agent.py")
lab_agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lab_agent)


def make_agent(listed):
    a = lab_agent.Agent({"server_url": "http://x", "token": "t"})
    a.sync = lambda: {"server": "s", "date": "d", "users": [], "migrations": [{"id": i} for i in listed[0]]}
    a.reconcile = lambda users: setattr(a, "accounts", [])
    a._sent_accounts = []
    started = []
    a.handle_migrations = lambda tasks: started.extend(t["id"] for t in tasks)
    return a, started


def test_failed_migration_can_be_retried_without_restarting_agent():
    listed = [[7]]
    a, started = make_agent(listed)
    a.once()
    assert started == [7]
    a.finished.add(7)          # 迁移线程跑完（失败）后记下
    a.once()                   # 平台还没处理上报：仍列着它，不能重复执行
    assert started == [7]
    listed[0] = []             # 平台已记为 failed，不再列出
    a.once()
    assert 7 not in a.finished
    listed[0] = [7]            # 成员点了“重试”：重新排队
    a.once()
    assert started == [7, 7]


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
    a.migration = type("M", (), {"is_alive": lambda self: True, "t": {"uid": 4242}})()
    assert a.rename_user(pw, "tian2")[0] == "pending"
    assert relocated == []


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
