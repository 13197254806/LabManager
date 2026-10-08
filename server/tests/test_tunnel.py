"""校园网隧道的健壮性：会话沿用、巡检出错不退出、半死不活的隧道自动重启。不会真的联网或启动 chisel。"""

import json
from datetime import datetime, timedelta

import pytest

from app import tunnel, webvpn
from app.config import now


@pytest.fixture
def tun(monkeypatch, tmp_path):
    monkeypatch.setattr(tunnel, "SESSION_FILE", str(tmp_path / "session.json"))
    monkeypatch.setattr(tunnel, "TUNNEL_DIR", str(tmp_path))
    monkeypatch.setattr(tunnel, "STATUS", str(tmp_path / "status.json"))
    monkeypatch.setattr(tunnel, "CHISEL_SECRET", "s")
    monkeypatch.setattr(tunnel, "WEBVPN_USER", "u")
    monkeypatch.setattr(tunnel, "WEBVPN_PASSWORD", "p")
    monkeypatch.setattr(tunnel, "CHECK_INTERVAL", 0)
    monkeypatch.setattr(tunnel, "refresh_targets", lambda: tunnel.TARGETS)
    monkeypatch.setattr(tunnel, "prune_status", lambda: None)
    t = tunnel.Tunnels()
    monkeypatch.setattr(t, "_sleep", lambda secs: None)
    return t


def _client_with_ticket():
    c = webvpn.WebVPNClient("https://webvpn.example.edu.cn")
    c.import_cookies([{"name": "wengine_vpn_ticketwebvpn_example_edu_cn", "value": "abc", "domain": "webvpn.example.edu.cn"}])
    return c


def test_cookie_export_import_roundtrip():
    c = _client_with_ticket()
    items = c.export_cookies()
    c2 = webvpn.WebVPNClient("https://webvpn.example.edu.cn")
    c2.import_cookies(json.loads(json.dumps(items)))
    assert c2.ticket_cookie() == ("wengine_vpn_ticketwebvpn_example_edu_cn", "abc")
    assert c2.cookie_header() == c.cookie_header()


def test_restart_reuses_a_valid_session_without_logging_in(tun, monkeypatch):
    tun.client = _client_with_ticket()
    tun.save_session()
    fresh = tunnel.Tunnels()
    monkeypatch.setattr(webvpn.WebVPNClient, "logged_in", lambda self: True)
    assert fresh.restore_session() is True
    assert "abc" in fresh.cookie


def test_expired_or_missing_session_means_login(tun, monkeypatch):
    assert tun.restore_session() is False  # 没有保存过
    tun.client = _client_with_ticket()
    tun.save_session()
    monkeypatch.setattr(webvpn.WebVPNClient, "logged_in", lambda self: False)
    assert tunnel.Tunnels().restore_session() is False  # 明确失效 → 需要重新登录


def test_unknown_session_state_is_kept(tun, monkeypatch):
    """网络抖动、代理还没起来时无法确认：先沿用，别急着登录（登录要过验证码、每小时限次）。"""
    tun.client = _client_with_ticket()
    tun.save_session()
    monkeypatch.setattr(webvpn.WebVPNClient, "logged_in", lambda self: None)
    assert tunnel.Tunnels().restore_session() is True


def test_session_file_is_private(tun):
    import os
    import stat

    tun.client = _client_with_ticket()
    tun.save_session()
    assert stat.S_IMODE(os.stat(tunnel.SESSION_FILE).st_mode) == 0o600


def test_a_failing_check_does_not_kill_the_process(tun, monkeypatch):
    """巡检里任何异常都只记日志：进程一退出，Docker 会重启容器、重新登录并断掉所有隧道。"""
    monkeypatch.setattr(tun, "restore_session", lambda: True)
    monkeypatch.setattr(tun, "start_all", lambda: None)
    monkeypatch.setattr(tun, "stop_all", lambda: None)
    calls = []

    def tick():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("磁盘写入失败之类的意外")
        tun.stopping = True

    monkeypatch.setattr(tun, "tick", tick)
    tun.run()  # 不应抛异常
    assert len(calls) == 2  # 出错后下一轮照常进行


def test_missing_config_waits_instead_of_exiting(tun, monkeypatch):
    """缺配置时返回会让 Docker 立刻重启容器，形成重启循环：应该一直等到配置改好。"""
    monkeypatch.setattr(tunnel, "WEBVPN_PASSWORD", "")
    waited = []
    monkeypatch.setattr(tun, "_wait_for_fix", lambda: waited.append(1))
    tun.run()
    assert waited == [1]
    status = json.load(open(tunnel.STATUS))
    assert status["_webvpn"]["state"] == "misconfigured"


class _Proc:
    def poll(self):
        return None


def _watchdog_setup(tun, monkeypatch, *, connected_ago, seen_ago):
    import time

    monkeypatch.setattr(tunnel, "TARGETS", {"gpu1": ("10.0.0.1", 9000)})
    monkeypatch.setattr(tunnel, "LAST_SEEN", {"gpu1": (now() - timedelta(seconds=seen_ago)) if seen_ago is not None else None})
    tun.procs["gpu1"] = _Proc()
    tun.connected_at["gpu1"] = time.time() - connected_ago
    monkeypatch.setattr(tun, "_refresh_interval", lambda: 10)
    restarted = []
    monkeypatch.setattr(tun, "start_one", lambda name, host, port: restarted.append(name))
    return restarted


def test_watchdog_restarts_a_silent_tunnel(tun, monkeypatch):
    restarted = _watchdog_setup(tun, monkeypatch, connected_ago=900, seen_ago=900)
    tun.watchdog()
    assert restarted == ["gpu1"]
    tun.watchdog()  # 10 分钟内不重复重启
    assert restarted == ["gpu1"]


def test_watchdog_leaves_healthy_tunnels_alone(tun, monkeypatch):
    # Agent 刚上报过
    restarted = _watchdog_setup(tun, monkeypatch, connected_ago=900, seen_ago=20)
    tun.watchdog()
    assert restarted == []
    # 刚连上，给 Agent 一点时间
    restarted = _watchdog_setup(tun, monkeypatch, connected_ago=30, seen_ago=None)
    tun.watchdog()
    assert restarted == []


def test_status_time_uses_platform_timezone_and_strips_chisel_timestamp(tun, monkeypatch):
    import io
    monkeypatch.setattr(tunnel, "TARGETS", {"gpu1": ("10.0.0.1", 9000)})
    line = "2026/10/08 12:48:23 client: Connection error: server: Server cannot listen on R:127.0.0.1:18765=>web:8000\n"
    p = type("P", (), {"stdout": io.StringIO(line), "wait": lambda self: 0})()
    tun.stopping = True
    tun._watch("gpu1", p)
    st = json.load(open(tunnel.STATUS))["gpu1"]
    assert st["error"].startswith("client: Connection error")
    assert abs((now() - datetime.fromisoformat(st["updated_at"])).total_seconds()) < 5


def test_tick_reconnects_when_host_changes(tun, monkeypatch):
    started = []
    monkeypatch.setattr(tun, "start_one", lambda n, h, p: (started.append((n, h)), tun.target_of.__setitem__(n, (h, p))))
    monkeypatch.setattr(tun.client, "logged_in", lambda: True)
    monkeypatch.setattr(tun, "watchdog", lambda: None)
    alive = type("P", (), {"poll": lambda self: None})()
    tun.procs["gpu1"] = alive
    tun.target_of["gpu1"] = ("10.86.14.146", 9000)
    monkeypatch.setattr(tunnel, "TARGETS", {"gpu1": ("10.86.14.146", 9000)})
    tun.tick()
    assert started == []
    monkeypatch.setattr(tunnel, "TARGETS", {"gpu1": ("10.86.20.7", 9000)})
    tun.tick()
    assert started == [("gpu1", "10.86.20.7")]
