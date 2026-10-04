from datetime import datetime, timedelta

from app import services
from app.config import now, today
from app.models import Reservation
from conftest import agent_headers


def gpu_ids(client):
    cal = client.get("/api/calendar").json()
    return [g["id"] for s in cal["servers"] for g in s["gpus"]]


def book(client, gids, start, end, **kw):
    return client.post("/api/reservations", json={"gpu_ids": gids, "start_date": str(start), "end_date": str(end), **kw})


def _renew_window(db, hours="24"):
    """默认是到期前 4 小时（最后一天 20:00）才续约；这些用例不关心时间点，把窗口放宽到一整天。"""
    services.set_setting(db, "auto_renew_before_hours", hours)
    db.commit()


def test_booking_and_conflict(admin, alice, server):
    g = gpu_ids(alice)
    d = today()
    r = book(alice, g[:1], d, d + timedelta(days=2))
    assert r.status_code == 200, r.text
    # 同一张卡时间重叠 → 409
    r = book(admin, g[:1], d + timedelta(days=2), d + timedelta(days=3))
    assert r.status_code == 409
    # 另一张卡没问题
    assert book(admin, g[1:], d, d).status_code == 200


def test_booking_rules(alice, server):
    g = gpu_ids(alice)
    d = today()
    assert book(alice, g[:1], d - timedelta(days=1), d).status_code == 400  # 过去
    assert book(alice, g[:1], d, d + timedelta(days=14)).status_code == 400  # 超过 14 天
    assert book(alice, g[:1], d + timedelta(days=31), d + timedelta(days=32)).status_code == 400  # 太久以后
    assert book(alice, g[:1], d + timedelta(days=2), d + timedelta(days=1)).status_code == 400  # 倒置


def _backdate(db, rid, start_offset, end_offset):
    r = db.get(Reservation, rid)
    d = today()
    r.start_date, r.end_date = d + timedelta(days=start_offset), d + timedelta(days=end_offset)
    db.commit()


def test_edit_note_of_long_reservation(admin, alice, server, db):
    """自动续约会让总时长超过单次上限；之后改备注 / 关闭自动续约不应被“最长 14 天”规则拦住。"""
    g = gpu_ids(alice)
    d = today()
    rid = book(alice, g[:1], d, d + timedelta(days=5), auto_renew=True).json()[0]["id"]
    _backdate(db, rid, -12, 4)  # 总共 17 天，从今天起还剩 5 天
    r = alice.patch(f"/api/reservations/{rid}", json={
        "start_date": str(d - timedelta(days=12)), "end_date": str(d + timedelta(days=4)),
        "note": "新备注", "auto_renew": False})
    assert r.status_code == 200, r.text
    assert r.json()["note"] == "新备注"


def test_manual_extension_of_running_reservation(alice, server, db):
    """已进行多天的预约，到期前可以继续延长：限制的是“从今天起还剩几天”，不是从开始日起算。"""
    g = gpu_ids(alice)
    d = today()
    rid = book(alice, g[:1], d, d + timedelta(days=2)).json()[0]["id"]
    _backdate(db, rid, -10, 2)
    assert alice.patch(f"/api/reservations/{rid}", json={"end_date": str(d + timedelta(days=13))}).status_code == 200
    assert alice.patch(f"/api/reservations/{rid}", json={"end_date": str(d + timedelta(days=14))}).status_code == 400


def test_release(alice, server):
    g = gpu_ids(alice)
    d = today()
    rid = book(alice, g[:1], d, d + timedelta(days=3)).json()[0]["id"]
    assert alice.delete(f"/api/reservations/{rid}").status_code == 200
    assert alice.get("/api/me/reservations").json() == []
    # 已经开始多天的：截止到昨天
    rid = book(alice, g[:1], d + timedelta(days=1), d + timedelta(days=3)).json()[0]["id"]
    assert alice.delete(f"/api/reservations/{rid}").status_code == 200


def test_other_user_cannot_edit(admin, alice, server):
    g = gpu_ids(alice)
    d = today()
    rid = book(admin, g[:1], d, d).json()[0]["id"]
    assert alice.patch(f"/api/reservations/{rid}", json={"note": "x"}).status_code == 403
    assert alice.delete(f"/api/reservations/{rid}").status_code == 403


def test_auto_renew_single_step(admin, alice, server, db):
    _renew_window(db)
    g = gpu_ids(alice)
    d = today()
    rid = book(alice, g[:1], d, d, auto_renew=True).json()[0]["id"]
    assert services.run_auto_renew(db) == 1
    db.expire_all()
    assert db.get(Reservation, rid).end_date == d + timedelta(days=7)
    # 还没到续约时间，不会重复续
    assert services.run_auto_renew(db) == 0


def test_auto_renew_catches_up_in_one_pass(admin, alice, server, db):
    """“提前小时数”大于“每次延长天数”时，应一次续到不再需要续约，而不是每分钟续一次、一直发通知。"""
    services.set_setting(db, "auto_renew_before_hours", "72")
    services.set_setting(db, "auto_renew_days", "1")
    db.commit()
    g = gpu_ids(alice)
    d = today()
    rid = book(alice, g[:1], d, d, auto_renew=True).json()[0]["id"]
    assert services.run_auto_renew(db) == 1
    db.expire_all()
    r = db.get(Reservation, rid)
    deadline = r.end_date + timedelta(days=1)
    assert now() < datetime.combine(deadline, datetime.min.time()) - timedelta(hours=72)
    assert services.run_auto_renew(db) == 0


def test_auto_renew_stops_at_conflict(admin, alice, server, db):
    _renew_window(db)
    g = gpu_ids(alice)
    d = today()
    rid = book(alice, g[:1], d, d, auto_renew=True).json()[0]["id"]
    assert book(admin, g[:1], d + timedelta(days=3), d + timedelta(days=5)).status_code == 200
    assert services.run_auto_renew(db) == 1
    db.expire_all()
    r = db.get(Reservation, rid)
    assert r.end_date == d + timedelta(days=2) and r.auto_renew  # 只续到冲突前一天


def test_auto_renew_disabled_when_nothing_can_be_renewed(admin, alice, server, db):
    _renew_window(db)
    g = gpu_ids(alice)
    d = today()
    rid = book(alice, g[:1], d, d, auto_renew=True).json()[0]["id"]
    assert book(admin, g[:1], d + timedelta(days=1), d + timedelta(days=5)).status_code == 200
    assert services.run_auto_renew(db) == 0
    db.expire_all()
    r = db.get(Reservation, rid)
    assert r.end_date == d and not r.auto_renew  # 关闭自动续约，不会每分钟重试


# ---------------- 多人共用同一张卡 ----------------


def _users(n):
    """创建 n 个普通用户（alice 之外），返回已登录的客户端列表。"""
    from conftest import make_client

    out = []
    for i in range(n):
        c = make_client(f"user{i}")
        assert c.put("/api/me/linux-username", json={"username": f"user{i}"}).status_code == 200
        out.append(c)
    return out


def test_gpu_can_be_shared_up_to_limit(admin, alice, server, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "max_gpu_sharers", 3)
    g = gpu_ids(alice)
    d = today()
    u1, u2 = _users(2)
    assert book(alice, g[:1], d, d + timedelta(days=2)).status_code == 200
    assert book(u1, g[:1], d, d + timedelta(days=1)).status_code == 200
    assert book(u2, g[:1], d + timedelta(days=1), d + timedelta(days=3)).status_code == 200
    # 第 4 个人：第 2 天已经有 3 人
    r = book(admin, g[:1], d + timedelta(days=1), d + timedelta(days=1))
    assert r.status_code == 409 and "最多 3 人共用" in r.json()["detail"]
    # 错开到没满的日子可以
    assert book(admin, g[:1], d + timedelta(days=4), d + timedelta(days=4)).status_code == 200


def test_same_user_cannot_double_book_a_gpu(alice, server, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "max_gpu_sharers", 3)
    g = gpu_ids(alice)
    d = today()
    assert book(alice, g[:1], d, d + timedelta(days=2)).status_code == 200
    r = book(alice, g[:1], d + timedelta(days=2), d + timedelta(days=3))
    assert r.status_code == 409 and "已经有一条预约" in r.json()["detail"]
    # 首尾相接（不重叠）可以：日历上会合并成一条
    assert book(alice, g[:1], d + timedelta(days=3), d + timedelta(days=4)).status_code == 200


def test_editing_a_shared_reservation_is_checked_against_others(admin, alice, server, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "max_gpu_sharers", 2)
    g = gpu_ids(alice)
    d = today()
    rid = book(alice, g[:1], d, d + timedelta(days=1)).json()[0]["id"]
    assert book(admin, g[:1], d + timedelta(days=2), d + timedelta(days=3)).status_code == 200
    # 延长到第 4 天：此时只有 alice 和 admin 两人同时在，没超过上限
    assert alice.patch(f"/api/reservations/{rid}", json={"end_date": str(d + timedelta(days=3))}).status_code == 200


def test_auto_renew_with_shared_gpu(admin, alice, server, db, monkeypatch):
    """自动续约只在共用人数已满的日子前停下，没满就照常续。"""
    _renew_window(db)
    from app.config import settings

    monkeypatch.setattr(settings, "max_gpu_sharers", 2)
    g = gpu_ids(alice)
    d = today()
    u1, = _users(1)
    rid = book(alice, g[:1], d, d, auto_renew=True).json()[0]["id"]
    assert book(admin, g[:1], d + timedelta(days=1), d + timedelta(days=10)).status_code == 200  # 第 2 天起 2 人共用里的 1 人
    assert book(u1, g[:1], d + timedelta(days=3), d + timedelta(days=10)).status_code == 200  # 第 4 天起 admin+u1 已满 2 人
    assert services.run_auto_renew(db) == 1
    db.expire_all()
    r = db.get(Reservation, rid)
    assert r.end_date == d + timedelta(days=2) and r.auto_renew  # 第 4 天起已满 → 只续到前一天


def test_config_and_login_switch(admin, alice, server):
    from fastapi.testclient import TestClient
    from app.main import app

    cfg = alice.get("/api/config").json()
    assert cfg["max_gpu_sharers"] == 1 and cfg["require_reservation_to_login"] is False
    _, token = server
    sync = lambda: TestClient(app).post("/api/agent/sync", json={"gpus": []}, headers=agent_headers(token)).json()
    assert sync()["require_reservation_to_login"] is False  # 默认关：所有成员都能登录
    assert admin.put("/api/admin/settings", json={"require_reservation_to_login": "1"}).status_code == 200
    assert alice.get("/api/config").json()["require_reservation_to_login"] is True
    assert sync()["require_reservation_to_login"] is True
    assert admin.put("/api/admin/settings", json={"require_reservation_to_login": "0"}).status_code == 200
    assert sync()["require_reservation_to_login"] is False
    assert admin.put("/api/admin/settings", json={"require_reservation_to_login": "2"}).status_code == 400


# ---------------- 自动续约时间点 / 合并连续预约 ----------------


def test_auto_renew_default_is_4_hours_before_end(alice, server, db, monkeypatch):
    """默认：预约最后一天的 20:00（距到期 4 小时）才续约 7 天；之前不动。"""
    assert services.get_int_setting(db, "auto_renew_before_hours") == 4
    assert services.get_int_setting(db, "auto_renew_days") == 7
    g = gpu_ids(alice)
    d = today()
    rid = book(alice, g[:1], d, d, auto_renew=True).json()[0]["id"]
    at = lambda h, m=0: datetime.combine(d, datetime.min.time()) + timedelta(hours=h, minutes=m)
    monkeypatch.setattr(services, "now", lambda: at(19, 59))
    assert services.run_auto_renew(db) == 0
    monkeypatch.setattr(services, "now", lambda: at(20))
    assert services.run_auto_renew(db) == 1
    db.expire_all()
    assert db.get(Reservation, rid).end_date == d + timedelta(days=7)


def test_adjacent_reservations_are_merged_in_database(alice, server, db):
    g = gpu_ids(alice)
    d = today()
    a = book(alice, g[:1], d, d + timedelta(days=1), note="第一段").json()[0]["id"]
    b = book(alice, g[:1], d + timedelta(days=2), d + timedelta(days=3), note="第二段", auto_renew=True).json()
    assert len(b) == 1 and b[0]["id"] == a  # 返回的是合并后的那条
    db.expire_all()
    rows = db.query(Reservation).filter(Reservation.status == "active").all()
    assert len(rows) == 1
    r = rows[0]
    # 两条都还没结束：备注和自动续约以连续预约里第一条没结束的（即第一段）为准
    assert (r.start_date, r.end_date) == (d, d + timedelta(days=3))
    assert r.note == "第一段" and r.auto_renew is False
    # 不相邻的不合并；另一张卡不合并
    assert book(alice, g[:1], d + timedelta(days=5), d + timedelta(days=5)).status_code == 200
    assert book(alice, g[1:], d, d).status_code == 200
    db.expire_all()
    assert db.query(Reservation).filter(Reservation.status == "active").count() == 3


def test_merge_uses_first_unfinished_reservation_attributes(alice, server, db):
    """连续预约里已经结束的部分不决定备注 / 自动续约。"""
    g = gpu_ids(alice)
    d = today()
    old = book(alice, g[:1], d, d, note="已结束那段", auto_renew=True).json()[0]["id"]
    _backdate(db, old, -3, -1)  # 昨天结束
    cur = book(alice, g[:1], d, d + timedelta(days=1), note="当前这段").json()[0]["id"]  # 与昨天相接 → 合并
    db.expire_all()
    rows = db.query(Reservation).filter(Reservation.status == "active").all()
    assert len(rows) == 1
    assert rows[0].start_date == d - timedelta(days=3) and rows[0].end_date == d + timedelta(days=1)
    assert rows[0].note == "当前这段" and rows[0].auto_renew is False


def test_startup_merge_of_existing_data(alice, server, db):
    """历史数据里首尾相接的预约，启动时会被合并（可重复执行）。"""
    g = gpu_ids(alice)
    d = today()
    from app.models import User

    alice_id = db.query(User).filter(User.linux_username == "alice").one().id
    for s, e, note in [(0, 1, "a"), (2, 2, "b"), (4, 5, "c")]:
        db.add(Reservation(user_id=alice_id, gpu_id=g[0], start_date=d + timedelta(days=s), end_date=d + timedelta(days=e), note=note))
    db.commit()
    assert len(services.merge_reservations(db)) == 1
    db.commit()
    rows = sorted(db.query(Reservation).filter(Reservation.status == "active").all(), key=lambda r: r.start_date)
    assert [(r.start_date, r.end_date) for r in rows] == [(d, d + timedelta(days=2)), (d + timedelta(days=4), d + timedelta(days=5))]
    assert services.merge_reservations(db) == {}  # 幂等


def test_refresh_interval_setting_and_system_report(admin, alice, server):
    from fastapi.testclient import TestClient
    from app.main import app

    assert alice.get("/api/config").json()["refresh_interval"] == 10
    assert admin.put("/api/admin/settings", json={"refresh_interval_seconds": "2"}).status_code == 400
    assert admin.put("/api/admin/settings", json={"refresh_interval_seconds": "30"}).status_code == 200
    _, token = server
    body = {"gpus": [], "system": {"cpu": 12.5, "mem_used": 1000, "mem_total": 4000, "disk_used": 10, "disk_total": 100, "evil": 1}}
    r = TestClient(app).post("/api/agent/sync", json=body, headers=agent_headers(token)).json()
    assert r["interval"] == 30
    srv = admin.get("/api/admin/servers").json()[0]
    assert srv["system"] == {"cpu": 12.5, "mem_used": 1000, "mem_total": 4000, "disk_used": 10, "disk_total": 100}
    assert "description" not in srv
