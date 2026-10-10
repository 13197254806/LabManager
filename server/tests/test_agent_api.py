from datetime import timedelta

from app.config import today
from conftest import agent_headers, make_client
from fastapi.testclient import TestClient
from app.main import app


def test_agent_needs_token(server):
    c = TestClient(app)
    assert c.post("/api/agent/sync", json={"gpus": []}).status_code == 401
    assert c.post("/api/agent/sync", json={"gpus": []}, headers=agent_headers("bad")).status_code == 401


def test_desired_state(admin, alice, server):
    _, token = server
    agent = TestClient(app)
    gids = [g["id"] for s in admin.get("/api/calendar").json()["servers"] for g in s["gpus"]]
    d = today()
    alice.post("/api/reservations", json={"gpu_ids": gids[:1], "start_date": str(d), "end_date": str(d)})
    key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl test@x"
    assert alice.post("/api/me/keys", json={"title": "", "public_key": key}).status_code == 200
    assert alice.put("/api/me/password", json={"password": "abcdefgh1"}).status_code == 200
    r = agent.post("/api/agent/sync", json={"gpus": []}, headers=agent_headers(token)).json()
    users = {u["username"]: u for u in r["users"]}
    assert users["alice"]["active"] and users["alice"]["gpus"] == [0]
    assert users["alice"]["password_hash"].startswith("$6$")
    assert users["alice"]["pubkeys"] == [key]
    assert not users["adminuser"]["active"]


def test_sync_returns_platform_time(server):
    import time

    _, token = server
    r = TestClient(app).post("/api/agent/sync", json={"gpus": []}, headers=agent_headers(token)).json()
    assert abs(r["server_time"] - time.time()) < 5


def test_violation_alert_does_not_crash(admin, alice, server):
    _, token = server
    agent = TestClient(app)
    body = {"gpus": [{"index": 0, "uuid": "u0", "name": "A", "memory_total": 1, "memory_used": 1, "utilization": 1,
                      "processes": [{"pid": 1, "user": "alice", "used_memory": 1}]}]}
    assert agent.post("/api/agent/sync", json=body, headers=agent_headers(token)).status_code == 200
    cal = alice.get("/api/calendar").json()
    assert cal["servers"][0]["gpus"][0]["alerts"] == ["alice"]


def test_migration_flow(admin, alice, server):
    srv, token = server
    agent = TestClient(app)
    cand = {"path": "/data/proj", "size": 10, "mtime": "2026-01-01", "owner": "root", "same_fs": True, "in_home": False, "is_dir": True}
    agent.post("/api/agent/sync", json={"gpus": [], "candidates": [cand]}, headers=agent_headers(token))
    sid = admin.get("/api/admin/servers").json()[0]["id"]
    r = alice.post("/api/migrations", json={"server_id": sid, "source": "/data/proj", "mode": "move", "dest_name": "proj"})
    assert r.status_code == 200, r.text
    tid = r.json()["id"]
    # 同一目录不能被第二个人“移动”
    assert admin.post("/api/migrations", json={"server_id": sid, "source": "/data/proj", "mode": "move", "dest_name": "p"}).status_code == 409
    # 不在候选列表里的路径被拒绝
    assert alice.post("/api/migrations", json={"server_id": sid, "source": "/etc", "mode": "copy", "dest_name": "etc"}).status_code == 400
    sync = agent.post("/api/agent/sync", json={"gpus": []}, headers=agent_headers(token)).json()
    assert [m["id"] for m in sync["migrations"]] == [tid]
    assert agent.post(f"/api/agent/migrations/{tid}", json={"status": "failed", "message": "boom"}, headers=agent_headers(token)).status_code == 200
    assert alice.post(f"/api/migrations/{tid}/retry").status_code == 200
    sync = agent.post("/api/agent/sync", json={"gpus": []}, headers=agent_headers(token)).json()
    assert [m["id"] for m in sync["migrations"]] == [tid]


def test_stalled_migration_can_be_marked_failed_and_retried(admin, alice, server):
    """完成时连不上平台、结果丢了：任务一直停在“进行中”。超过 30 分钟没有进度可以标记为失败再重试；
    Agent 晚些送到的真实结果照样接受。管理员也能操作别人的任务。"""
    from app.db import SessionLocal
    from app.models import MigrationTask

    srv, token = server
    agent = TestClient(app)
    cand = {"path": "/data/proj", "size": 10, "mtime": "2026-01-01", "owner": "root", "same_fs": True, "in_home": False, "is_dir": True}
    agent.post("/api/agent/sync", json={"gpus": [], "candidates": [cand]}, headers=agent_headers(token))
    sid = admin.get("/api/admin/servers").json()[0]["id"]
    tid = alice.post("/api/migrations", json={"server_id": sid, "source": "/data/proj", "mode": "move", "dest_name": "proj"}).json()["id"]
    agent.post(f"/api/agent/migrations/{tid}", json={"status": "running", "phase": "复制文件"}, headers=agent_headers(token))
    # 刚有进度：不能标记
    assert not alice.get("/api/me/migrations").json()[0]["stalled"]
    assert alice.post(f"/api/migrations/{tid}/abandon").status_code == 400

    with SessionLocal() as db:
        t = db.get(MigrationTask, tid)
        t.updated_at = t.updated_at - timedelta(minutes=31)
        db.commit()
    assert alice.get("/api/me/migrations").json()[0]["stalled"]
    r = admin.post(f"/api/migrations/{tid}/abandon")
    assert r.status_code == 200 and r.json()["status"] == "failed"
    sync = agent.post("/api/agent/sync", json={"gpus": []}, headers=agent_headers(token)).json()
    assert sync["migrations"] == []

    # 标记之后 Agent 才把真实结果送到：接受
    agent.post(f"/api/agent/migrations/{tid}", json={"status": "done", "phase": "完成", "dest_path": "/home/alice/proj"},
               headers=agent_headers(token))
    t = alice.get("/api/me/migrations").json()[0]
    assert t["status"] == "done" and t["dest_path"] == "/home/alice/proj"

    # 管理员可以重试别人失败的任务
    agent.post(f"/api/agent/migrations/{tid}", json={"status": "failed"}, headers=agent_headers(token))  # 已完成：忽略
    with SessionLocal() as db:
        db.get(MigrationTask, tid).status = "failed"
        db.commit()
    assert admin.post(f"/api/migrations/{tid}/retry").status_code == 200
    assert alice.get("/api/me/migrations").json()[0]["status"] == "queued"


def _age_task(tid, minutes):
    from app.db import SessionLocal
    from app.models import MigrationTask

    with SessionLocal() as db:
        t = db.get(MigrationTask, tid)
        t.updated_at = t.updated_at - timedelta(minutes=minutes)
        db.commit()


def _dir_task(admin, client, server, path="/data/proj", mode="move"):
    _, token = server
    cand = {"path": path, "size": 10, "mtime": "2026-01-01", "owner": "root", "same_fs": True, "in_home": False, "is_dir": True}
    TestClient(app).post("/api/agent/sync", json={"gpus": [], "candidates": [cand]}, headers=agent_headers(token))
    sid = admin.get("/api/admin/servers").json()[0]["id"]
    return client.post("/api/migrations", json={"server_id": sid, "source": path, "mode": mode, "dest_name": "proj"})


def test_stalled_uses_agent_reported_current_migration(admin, alice, server):
    """新版 Agent 报告正在执行哪个迁移：它在做的，再久没进度也不算卡住；它没在做的，几分钟没进度就算。"""
    _, token = server
    agent = TestClient(app)
    tid = _dir_task(admin, alice, server).json()["id"]
    agent.post(f"/api/agent/migrations/{tid}", json={"status": "running"}, headers=agent_headers(token))
    agent.post("/api/agent/sync", json={"gpus": [], "current_migration": tid}, headers=agent_headers(token))
    _age_task(tid, 120)  # conda 自检等步骤很久不报进度
    assert not alice.get("/api/me/migrations").json()[0]["stalled"]
    agent.post("/api/agent/sync", json={"gpus": [], "current_migration": 0}, headers=agent_headers(token))
    assert alice.get("/api/me/migrations").json()[0]["stalled"]


def test_retry_bumps_attempt_and_rechecks_claims(admin, alice, server):
    _, token = server
    agent = TestClient(app)
    tid = _dir_task(admin, alice, server).json()["id"]
    agent.post(f"/api/agent/migrations/{tid}", json={"status": "failed", "message": "boom"}, headers=agent_headers(token))
    # alice 失败期间 admin 申请移动同一个目录：alice 不能再重试
    other = _dir_task(admin, admin, server)
    assert other.status_code == 200, other.text
    assert alice.post(f"/api/migrations/{tid}/retry").status_code == 409
    agent.post(f"/api/agent/migrations/{other.json()['id']}", json={"status": "failed"}, headers=agent_headers(token))
    assert alice.post(f"/api/migrations/{tid}/retry").status_code == 200
    sync = agent.post("/api/agent/sync", json={"gpus": []}, headers=agent_headers(token)).json()
    assert sync["migration_attempts"] == {str(tid): 1}
    # 次数不放进任务本身：旧版 Agent 的任务指纹（完成标记）不受影响
    assert "attempt" not in sync["migrations"][0]


def test_conda_env_cannot_be_moved_by_two_members(admin, alice, server):
    _, token = server
    agent = TestClient(app)
    legacy = {"user": "ubuntu", "home": "/home/ubuntu", "configs": [],
              "conda": [{"name": "anaconda3", "base_size": 1, "envs": [{"name": "torch", "size": 1}, {"name": "tf", "size": 1}]}]}
    agent.post("/api/agent/sync", json={"gpus": [], "legacy": [legacy], "candidates": []}, headers=agent_headers(token))
    sid = admin.get("/api/admin/servers").json()[0]["id"]

    def pick(c, envs):
        return c.post("/api/migrations/profile", json={"server_id": sid, "legacy_user": "ubuntu",
                                                      "conda": [{"name": "anaconda3", "envs": envs}]})

    assert pick(alice, ["torch"]).status_code == 200
    r = pick(admin, ["tf", "torch"])
    assert r.status_code == 409 and "torch" in r.json()["detail"]
    assert pick(admin, ["tf"]).status_code == 200


def test_long_final_result_is_clipped_not_rejected(admin, alice, server):
    """结果太长时平台截断后接受：拒收的话 Agent 永远送不到，任务一直显示“进行中”。"""
    _, token = server
    tid = _dir_task(admin, alice, server).json()["id"]
    r = TestClient(app).post(f"/api/agent/migrations/{tid}", headers=agent_headers(token),
                             json={"status": "done", "phase": "完成" * 50, "message": "m" * 5000, "dest_path": "/home/" + "x" * 800})
    assert r.status_code == 200
    t = alice.get("/api/me/migrations").json()[0]
    assert t["status"] == "done" and len(t["dest_path"]) == 512


def test_sync_accepts_unknown_pid_and_matches_local_username(admin, alice, server):
    """nvidia-smi 读不出进程号也不能让整次同步失败；改名还没生效时进程按本机旧名字上报，仍算预约者本人。"""
    _, token = server
    agent = TestClient(app)
    me = alice.get("/api/me").json()
    gid = admin.get("/api/calendar").json()["servers"][0]["gpus"][0]["id"]
    d = str(today())
    assert alice.post("/api/reservations", json={"gpu_ids": [gid], "start_date": d, "end_date": d}).status_code == 200
    body = {"gpus": [{"index": 0, "uuid": "u0", "name": "A", "memory_total": 10, "memory_used": 5, "utilization": 1,
                      "processes": [{"pid": None, "user": "", "used_memory": 1},
                                    {"pid": 8, "user": "alice_old", "used_memory": 3}]}],
            "accounts": [{"id": me["id"], "username": "alice_old", "state": "pending", "message": "改名暂缓"}]}
    r = agent.post("/api/agent/sync", json=body, headers=agent_headers(token))
    assert r.status_code == 200, r.text
    assert r.json()["kill"] == []
    alerts = admin.get("/api/calendar").json()["servers"][0]["gpus"][0]["alerts"]
    assert "alice_old" not in alerts


def test_username_rules(admin, alice):
    assert alice.put("/api/me/linux-username", json={"username": "Root"}).status_code == 400
    assert alice.put("/api/me/linux-username", json={"username": "ubuntu"}).status_code == 400
    assert alice.put("/api/me/linux-username", json={"username": "adminuser"}).status_code == 400
    assert alice.put("/api/me/linux-username", json={"username": "alice2"}).status_code == 200


def test_permissions(admin, alice):
    assert alice.get("/api/admin/users").status_code == 403
    assert TestClient(app).get("/api/me").status_code == 401
    assert alice.patch("/api/admin/users/1", json={"is_admin": False}).status_code == 403
    me = admin.get("/api/me").json()
    assert admin.patch(f"/api/admin/users/{me['id']}", json={"is_admin": False}).status_code == 400


def test_cannot_lock_self_out(alice):
    assert alice.put("/api/me/password", json={"password": "abcdefgh1"}).status_code == 200
    # 没有公钥时不能关闭密码登录，否则就没有任何登录方式了
    assert alice.delete("/api/me/password").status_code == 400
    key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl test@x"
    assert alice.post("/api/me/keys", json={"title": "", "public_key": key}).status_code == 200
    assert alice.delete("/api/me/password").status_code == 200
    assert alice.put("/api/me/password", json={"password": "short"}).status_code == 400


def test_process_path_and_avatar_reported(admin, alice, server):
    _, token = server
    agent = TestClient(app)
    body = {"gpus": [{"index": 0, "uuid": "u0", "name": "A", "memory_total": 10, "memory_used": 5, "utilization": 1,
                      "processes": [{"pid": 7, "user": "alice", "used_memory": 3, "path": "/home/alice/proj"}]}]}
    assert agent.post("/api/agent/sync", json=body, headers=agent_headers(token)).status_code == 200
    gpu = admin.get("/api/calendar").json()["servers"][0]["gpus"][0]
    assert gpu["processes"] == [{"pid": 7, "user": "alice", "used_memory": 3, "path": "/home/alice/proj"}]
    gid = gpu["id"]
    d = str(today())
    assert alice.post("/api/reservations", json={"gpu_ids": [gid], "start_date": d, "end_date": d}).status_code == 200
    res = admin.get("/api/calendar").json()["reservations"][0]
    assert "user_avatar" in res


def test_browse_roundtrip_and_nested_migration(admin, alice, server):
    import threading
    srv, token = server
    sid = admin.get("/api/admin/servers").json()[0]["id"]
    agent = TestClient(app)
    legacy = {"user": "ubuntu", "home": "/home/ubuntu", "configs": [], "conda": []}
    sync = {"gpus": [], "candidates": [], "legacy": [legacy], "browse_roots": ["/home/ubuntu", "/data"]}
    assert agent.post("/api/agent/sync", json=sync, headers=agent_headers(token)).status_code == 200

    def fake_agent():
        c = TestClient(app)
        for req in c.post("/api/agent/browse/poll", json={}, headers=agent_headers(token)).json()["requests"]:
            entries = [{"name": "proj", "path": "/home/ubuntu/proj", "is_dir": True, "selectable": True}]
            r = c.post(f"/api/agent/browse/{req['id']}", json={"ok": True, "entries": entries}, headers=agent_headers(token))
            assert r.status_code == 200, r.text

    t = threading.Thread(target=fake_agent)
    t.start()
    r = alice.post(f"/api/servers/{sid}/browse", json={"op": "list", "path": "/home/ubuntu"})
    t.join()
    assert r.status_code == 200, r.text
    assert r.json()["entries"][0]["path"] == "/home/ubuntu/proj" and r.json()["entries"][0]["claimed_by"] is None

    # 嵌套路径可以迁移；根目录之外的不行；别人移走上级目录后下级也算冲突
    ok = alice.post("/api/migrations", json={"server_id": sid, "source": "/home/ubuntu/proj/sub", "mode": "move", "dest_name": "sub"})
    assert ok.status_code == 200, ok.text
    assert admin.post("/api/migrations", json={"server_id": sid, "source": "/home/ubuntu/proj", "mode": "copy", "dest_name": "p"}).status_code == 409
    assert alice.post("/api/migrations", json={"server_id": sid, "source": "/etc/passwd", "mode": "copy", "dest_name": "x"}).status_code == 400
    assert alice.post("/api/migrations", json={"server_id": sid, "source": "/home/ubuntu/.bashrc", "mode": "copy", "dest_name": ".bashrc"}).status_code == 200
    assert alice.post("/api/migrations", json={"server_id": sid, "source": "/home/ubuntu/.lab-migrate", "mode": "copy", "dest_name": "x"}).status_code == 400
    assert alice.post("/api/migrations", json={"server_id": sid, "source": "/home/ubuntu/a", "mode": "link", "dest_name": "a"}).status_code == 400


def test_browse_times_out_without_agent(admin, alice, server, monkeypatch):
    from app import browse
    sid = admin.get("/api/admin/servers").json()[0]["id"]
    orig = browse.broker.ask
    monkeypatch.setattr(browse.broker, "ask", lambda *a, **k: orig(a[0], a[1], a[2], timeout=0.2))
    assert alice.post(f"/api/servers/{sid}/browse", json={"op": "list", "path": ""}).status_code == 504


def test_login_restricted_to_admin_tenant(monkeypatch):
    from app import auth
    from app.db import SessionLocal
    from app.models import User

    def callback(open_id, tenant):
        with SessionLocal() as db:
            info = {"open_id": open_id, "tenant_key": tenant}
            return auth._tenant_allowed(db, info), db

    # 还没有任何用户：第一个人放行并成为管理员，记下组织
    assert callback("ou_admin", "T1")[0]
    with SessionLocal() as db:
        auth._login(type("R", (), {"session": {}})(), db, {"open_id": "ou_admin", "name": "a", "tenant_key": "T1"})
        assert db.query(User).one().is_admin
    assert callback("ou_member", "T1")[0]          # 同组织放行
    assert not callback("ou_outsider", "T2")[0]    # 其他组织（个人飞书号）拒绝
    assert not callback("ou_nobody", None)[0]
    # 开关关闭后不再限制
    with SessionLocal() as db:
        from app import services
        services.set_setting(db, "restrict_to_admin_tenant", "0")
        db.commit()
    assert callback("ou_outsider", "T2")[0]
    # 环境变量优先
    monkeypatch.setattr(auth.settings, "feishu_tenant_key", "T9", raising=False)
    assert not callback("ou_admin", "T1")[0]


def test_unknown_gpu_process_owner_is_not_shown_as_a_user(admin, alice, server):
    _, token = server
    agent = TestClient(app)
    body = {"gpus": [{"index": 0, "uuid": "u0", "name": "A", "memory_total": 1, "memory_used": 1, "utilization": 1,
                      "processes": [{"pid": 81315, "user": "", "used_memory": 1}, {"pid": 7, "user": "?", "used_memory": 1}]}]}
    assert agent.post("/api/agent/sync", json=body, headers=agent_headers(token)).status_code == 200
    cal = alice.get("/api/calendar").json()
    assert cal["servers"][0]["gpus"][0]["alerts"] == ["未知进程 PID 7", "未知进程 PID 81315"]


def _sync(token, procs, **extra):
    body = {"gpus": [{"index": 0, "uuid": "u0", "name": "A", "memory_total": 1, "memory_used": 1, "utilization": 1,
                      "processes": procs}], **extra}
    r = TestClient(app).post("/api/agent/sync", json=body, headers=agent_headers(token))
    assert r.status_code == 200, r.text
    return r.json()


def test_unreserved_process_is_killed_only_after_grace(admin, alice, server, db):
    from datetime import datetime
    from app.models import GpuViolation

    _, token = server
    procs = [{"pid": 11, "user": "alice", "used_memory": 1}, {"pid": 12, "user": "alice", "used_memory": 1},
             {"pid": 13, "user": "", "used_memory": 1}, {"pid": 14, "user": "stranger", "used_memory": 1}]
    assert _sync(token, procs)["kill"] == []                       # 刚发现：开始计时
    v = db.query(GpuViolation).one()
    assert (v.username, v.gpu_index) == ("alice", 0)               # 属主未知 / 平台上没有的账号不记录
    v.first_seen -= timedelta(hours=23)
    db.commit()
    assert _sync(token, procs)["kill"] == []                       # 缓冲期内
    v = db.query(GpuViolation).one()
    v.first_seen -= timedelta(hours=2)
    db.commit()
    kill = _sync(token, procs)["kill"]
    assert sorted(k["pid"] for k in kill) == [11, 12] and all(k["user"] == "alice" and k["gpu"] == 0 for k in kill)

    # 缓冲时间设为 0：不自动终止
    assert admin.put("/api/admin/settings", json={"process_kill_grace_hours": "0"}).status_code == 200
    assert _sync(token, procs)["kill"] == []
    assert admin.put("/api/admin/settings", json={"process_kill_grace_hours": "24"}).status_code == 200

    # 进程没了，记录清掉；之后再出现重新计时
    assert _sync(token, [])["kill"] == []
    assert db.query(GpuViolation).count() == 0
    assert _sync(token, procs)["kill"] == []

    # 补了预约：不再算违规
    gid = admin.get("/api/calendar").json()["servers"][0]["gpus"][0]["id"]
    alice.post("/api/reservations", json={"gpu_ids": [gid], "start_date": str(today()), "end_date": str(today())})
    assert _sync(token, procs)["kill"] == []
    assert db.query(GpuViolation).count() == 0
    # Agent 上报已终止的进程：平台正常收下（飞书未配置时不发送）
    assert _sync(token, [], killed=[{"gpu": 0, "pid": 11, "user": "alice"}])["kill"] == []


def test_editing_reservation_sends_no_card(admin, alice, server, monkeypatch):
    from app import services

    sent = []
    monkeypatch.setattr(services, "notify_booked", lambda *a, **k: sent.append(a[2:]))
    gid = admin.get("/api/calendar").json()["servers"][0]["gpus"][0]["id"]
    d = today() + timedelta(days=1)
    r = alice.post("/api/reservations", json={"gpu_ids": [gid], "start_date": str(d), "end_date": str(d)})
    assert r.status_code == 200 and len(sent) == 1               # 新建：发
    rid = r.json()[0]["id"]
    assert alice.patch(f"/api/reservations/{rid}", json={"end_date": str(d + timedelta(days=1))}).status_code == 200
    assert alice.delete(f"/api/reservations/{rid}").status_code == 200
    assert len(sent) == 1                                          # 修改、删除：不发


def test_direct_agent_reports_lan_ip_and_platform_follows(admin, server):
    _, token = server
    agent = TestClient(app)
    body = {"gpus": [], "link": "direct", "lan_ip": "10.86.14.146"}
    assert agent.post("/api/agent/sync", json=body, headers=agent_headers(token)).status_code == 200
    s = admin.get("/api/admin/servers").json()[0]
    assert s["host"] == "10.86.14.146" and s["link"] == "direct"
    # IP 变了：跟着更新；公网 / 回环等不像内网地址的值忽略
    agent.post("/api/agent/sync", json={**body, "lan_ip": "10.86.20.7"}, headers=agent_headers(token))
    for bad in ("8.8.8.8", "127.0.0.1", "169.254.1.1", "not-an-ip"):
        agent.post("/api/agent/sync", json={**body, "lan_ip": bad}, headers=agent_headers(token))
    assert admin.get("/api/admin/servers").json()[0]["host"] == "10.86.20.7"


def test_ssh_login_address_is_separate_from_tunnel_address(admin, alice, server):
    """本机网卡 IP 在校园网里连不上（经 NAT）时，管理员单独填成员 SSH 登录地址；Agent 自动更新内网地址不影响它。"""
    srv, token = server
    sid = admin.get("/api/admin/servers").json()[0]["id"]
    assert admin.patch(f"/api/admin/servers/{sid}", json={"name": "gpu_01", "host": "10.86.14.146", "ssh_host": "a b"}).status_code == 400
    r = admin.patch(f"/api/admin/servers/{sid}", json={"name": "gpu_01", "host": "10.86.14.146", "ssh_host": " 218.197.141.221 "})
    assert r.status_code == 200 and r.json()["ssh_host"] == "218.197.141.221"
    TestClient(app).post("/api/agent/sync", json={"gpus": [], "lan_ip": "10.86.20.7"}, headers=agent_headers(token))
    mine = alice.get("/api/me/servers").json()[0]
    assert mine["host"] == "10.86.20.7" and mine["ssh_host"] == "218.197.141.221"
    assert alice.get("/api/calendar").json()["servers"][0]["ssh_host"] == "218.197.141.221"


def test_migrate_to_data_disk(admin, alice, server):
    """有外挂数据盘时，迁移可以放到 <挂载点>/<用户名>/；Agent 太旧（会忽略目标盘、照旧放进家目录）时不允许。"""
    _, token = server
    agent = TestClient(app)
    sid = admin.get("/api/admin/servers").json()[0]["id"]
    cands = [{"path": p, "size": 10, "mtime": "2026-01-01", "owner": "root", "same_fs": True, "in_home": False, "is_dir": True}
             for p in ("/data/proj", "/data/other")]
    disks = [{"mount": "/", "total": 1000, "used": 900, "home": True},
             {"mount": "/media/zdd", "total": 3000, "used": 300, "home": False, "usage": [{"path": "alice", "user": "alice", "size": 5}]}]

    def sync(version):
        body = {"gpus": [], "agent_version": version, "candidates": cands, "system": {"cpu": 1, "disks": disks}}
        return agent.post("/api/agent/sync", json=body, headers=agent_headers(token)).json()

    sync("0.9.4")
    me = alice.get("/api/me/servers").json()[0]
    assert me["data_disks"] == [] and me["data_disks_need_agent"] == ["/media/zdd"]
    body = {"server_id": sid, "source": "/data/proj", "mode": "move", "dest_name": "proj", "dest_root": "/media/zdd"}
    assert alice.post("/api/migrations", json=body).status_code == 400          # Agent 太旧
    sync("0.9.5")
    me = alice.get("/api/me/servers").json()[0]
    assert me["data_disks"] == [{"mount": "/media/zdd", "free": 2700 * 1024 * 1024}] and me["data_disks_need_agent"] == []
    assert alice.post("/api/migrations", json={**body, "dest_root": "/etc"}).status_code == 400   # 不是上报过的数据盘
    assert alice.post("/api/migrations", json={**body, "mode": "link"}).status_code == 400        # 原地保留不搬数据
    r = alice.post("/api/migrations", json=body)
    assert r.status_code == 200 and r.json()["dest_hint"] == "/media/zdd/alice/proj"
    assert alice.post("/api/migrations", json={**body, "source": "/data/other", "dest_root": ""}).status_code == 200
    tasks = {t["source"]: t for t in sync("0.9.5")["migrations"]}
    assert tasks["/data/proj"]["dest_root"] == "/media/zdd"
    assert "dest_root" not in tasks["/data/other"]   # 放家目录的任务不带这个字段：指纹和旧版一致
    disk = alice.get("/api/calendar").json()["servers"][0]["system"]["disks"][1]
    assert disk["usage"] == [{"path": "alice", "user": "alice", "size": 5}]
