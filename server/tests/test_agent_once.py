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
