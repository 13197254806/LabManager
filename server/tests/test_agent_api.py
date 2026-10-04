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
