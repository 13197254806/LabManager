"""Agent 的同步循环：不碰真实系统（sync / reconcile 都打桩），只验证迁移任务的调度。"""
import importlib.util
from pathlib import Path

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
