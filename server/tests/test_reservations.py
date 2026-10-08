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
    body = {"gpus": [], "system": {"cpu": 12.5, "mem_used": 1000, "mem_total": 4000, "disk_used": 10, "disk_total": 100, "evil": 1,
                       "users": [{"user": "alice", "size": 2048}, {"user": "x", "size": "bad"}],
                       "disks": [{"mount": "/", "total": 100, "used": 10, "home": True}, {"mount": "/data", "total": 3000}]}}
    r = TestClient(app).post("/api/agent/sync", json=body, headers=agent_headers(token)).json()
    assert r["interval"] == 30
    srv = admin.get("/api/admin/servers").json()[0]
    assert srv["system"] == {"cpu": 12.5, "mem_used": 1000, "mem_total": 4000, "disk_used": 10, "disk_total": 100,
                             "users": [{"user": "alice", "size": 2048}],
                             "disks": [{"mount": "/", "total": 100, "used": 10, "home": True}]}
    assert "description" not in srv


def test_long_term_booking_starts_today_and_merges(alice, server, db):
    g = gpu_ids(alice)
    d = today()
    # 后面已有的临时预约会被长期使用覆盖掉（取消），不会合并进来
    assert book(alice, g[:1], d + timedelta(days=7), d + timedelta(days=9), note="后面的").status_code == 200
    r = alice.post("/api/reservations", json={"gpu_ids": g[:1], "long_term": True})
    assert r.status_code == 200, r.text
    assert len(r.json()) == 1
    db.expire_all()
    rows = db.query(Reservation).filter(Reservation.status == "active").all()
    assert len(rows) == 1
    assert (rows[0].start_date, rows[0].end_date) == (d, d + timedelta(days=6))
    assert rows[0].auto_renew is True
    # 今天已经预约了：再选长期使用只是把它标成长期使用，不会重复预约
    r2 = alice.post("/api/reservations", json={"gpu_ids": g[:1], "long_term": True})
    assert r2.status_code == 200
    db.expire_all()
    assert db.query(Reservation).filter(Reservation.status == "active").count() == 1


def test_long_term_blocked_when_full_today(admin, alice, server):
    g = gpu_ids(alice)
    d = today()
    assert book(admin, g[:1], d, d + timedelta(days=2)).status_code == 200
    r = alice.post("/api/reservations", json={"gpu_ids": g[:1], "long_term": True})
    assert r.status_code == 409
    # 整个操作不生效：另一张卡也没有被预约
    r = alice.post("/api/reservations", json={"gpu_ids": g, "long_term": True})
    assert r.status_code == 409
    assert alice.get("/api/me/reservations").json() == []


def test_long_term_stops_before_full_day(admin, alice, server, db):
    g = gpu_ids(alice)
    d = today()
    assert book(admin, g[:1], d + timedelta(days=3), d + timedelta(days=4)).status_code == 200
    assert alice.post("/api/reservations", json={"gpu_ids": g[:1], "long_term": True}).status_code == 200
    db.expire_all()
    mine = db.query(Reservation).filter(Reservation.status == "active", Reservation.user.has(linux_username="alice")).all()
    assert len(mine) == 1 and (mine[0].start_date, mine[0].end_date) == (d, d + timedelta(days=2))


def test_started_reservation_start_date_is_locked(admin, alice, server, db):
    """开始日期在今天之前的预约，开始日期不能改（管理员也一样）；结束日期仍可修改。"""
    g = gpu_ids(alice)
    d = today()
    rid = book(alice, g[:1], d, d + timedelta(days=3)).json()[0]["id"]
    _backdate(db, rid, -2, 3)  # 前天开始，今天还在用
    for who in (alice, admin):
        r = who.patch(f"/api/reservations/{rid}", json={"start_date": str(d + timedelta(days=1))})
        assert r.status_code == 400
    assert alice.patch(f"/api/reservations/{rid}", json={"end_date": str(d + timedelta(days=4))}).status_code == 200


def test_start_date_can_move_from_today_on_but_never_into_the_past(admin, alice, server):
    """今天及以后开始的预约可以改开始日期（拖左边缘）：可以往后缩，但不能挪到今天之前。"""
    g = gpu_ids(alice)
    d = today()
    rid = book(alice, g[:1], d, d + timedelta(days=5)).json()[0]["id"]
    r = alice.patch(f"/api/reservations/{rid}", json={"start_date": str(d + timedelta(days=2))})
    assert r.status_code == 200 and r.json()["start_date"] == str(d + timedelta(days=2))
    for who in (alice, admin):  # 管理员也不能改已过去的时间
        assert who.patch(f"/api/reservations/{rid}", json={"start_date": str(d - timedelta(days=1))}).status_code == 400
    # 往前拉回今天也可以
    assert alice.patch(f"/api/reservations/{rid}", json={"start_date": str(d)}).status_code == 200


def test_long_term_overrides_temp_but_keeps_history(alice, server, db):
    g = gpu_ids(alice)
    d = today()
    uid = alice.get("/api/me").json()["id"]
    # 已经进行了 3 天、还要持续到 5 天后的临时预约（直接写库，网页上不能预约过去的日期）
    db.add(Reservation(user_id=uid, gpu_id=g[0], start_date=d - timedelta(days=3), end_date=d + timedelta(days=5)))
    db.commit()
    assert alice.post("/api/reservations", json={"gpu_ids": g[:1], "long_term": True}).status_code == 200
    db.expire_all()
    rows = db.query(Reservation).filter(Reservation.status == "active").all()
    # 今天之前的历史保留，今天起变成长期使用，合并成一条
    assert len(rows) == 1
    assert (rows[0].start_date, rows[0].end_date) == (d - timedelta(days=3), d + timedelta(days=6))
    assert rows[0].auto_renew is True


def test_qr_login_url(monkeypatch):
    from app.config import settings
    from fastapi.testclient import TestClient
    from app.main import app

    c = TestClient(app)
    assert c.get("/auth/qr").status_code == 500  # 没配置飞书应用
    monkeypatch.setattr(settings, "feishu_app_id", "cli_x")
    monkeypatch.setattr(settings, "feishu_app_secret", "s")
    r = c.get("/auth/qr").json()["goto"]
    assert r.startswith("https://passport.feishu.cn/suite/passport/oauth/authorize?") and "client_id=cli_x" in r and "redirect_uri=http%3A%2F%2Ftestserver%2Fauth%2Fcallback" in r
    state = r.split("state=")[1]
    assert c.get("/auth/qr").json()["goto"].endswith(state)  # 同一会话里重复取二维码，state 不变
    assert c.get("/auth/callback?code=c&state=wrong").status_code == 400


# ---------------- 在管理页配置的规则（原来只能在 .env 里改） ----------------


def test_rules_are_configurable_in_admin(admin, alice, server):
    """管理页保存的值优先于 .env：同一张卡最多共用人数、单次最长天数、最多提前天数。"""
    g = gpu_ids(alice)
    d = today()
    assert alice.put("/api/admin/settings", json={"max_gpu_sharers": "2"}).status_code == 403  # 只有管理员能改
    saved = admin.put("/api/admin/settings", json={
        "max_gpu_sharers": "2", "max_reservation_days": "3", "max_advance_days": "5", "reminder_hour": "9"}).json()
    assert saved["max_gpu_sharers"] == "2" and saved["reminder_hour"] == "9"
    cfg = alice.get("/api/config").json()
    assert (cfg["max_gpu_sharers"], cfg["max_reservation_days"], cfg["max_advance_days"]) == (2, 3, 5)

    # 单次最长 3 天、最多提前 5 天（普通成员受限）
    assert book(alice, g[:1], d, d + timedelta(days=3)).status_code == 400
    assert book(alice, g[:1], d + timedelta(days=6), d + timedelta(days=6)).status_code == 400
    assert book(alice, g[:1], d, d + timedelta(days=2)).status_code == 200
    # 最多 2 人共用：第 2 个人可以，第 3 个人被拒绝，提示里是新的人数
    u1, u2 = _users(2)
    assert book(u1, g[:1], d, d).status_code == 200
    r = book(u2, g[:1], d, d)
    assert r.status_code == 409 and "最多 2 人共用" in r.json()["detail"]


def test_rule_settings_are_validated(admin):
    for key, bad in [("max_gpu_sharers", "0"), ("max_gpu_sharers", "11"), ("max_reservation_days", "abc"),
                     ("reminder_hour", "24"), ("max_advance_days", "-1")]:
        assert admin.put("/api/admin/settings", json={key: bad}).status_code == 400, (key, bad)
    assert admin.put("/api/admin/settings", json={"lab_name": "   "}).status_code == 400
    assert admin.put("/api/admin/settings", json={"lab_name": "x" * 41}).status_code == 400
    # 平台名称、欢迎卡片的群列表
    r = admin.put("/api/admin/settings", json={"lab_name": " 安全与隐私实验室 ", "welcome_chat_ids": "oc_a, oc_b\noc_c，oc_d"})
    assert r.status_code == 200 and r.json()["lab_name"] == "安全与隐私实验室"
    assert r.json()["welcome_chat_ids"] == "oc_a,oc_b,oc_c,oc_d"
    assert admin.get("/api/config").json()["lab_name"] == "安全与隐私实验室"


def test_env_values_are_defaults_until_saved(db, monkeypatch):
    """没在管理页保存过时用 .env 的值；保存过就以管理页为准。"""
    from app.config import settings

    monkeypatch.setattr(settings, "max_gpu_sharers", 4)
    monkeypatch.setattr(settings, "welcome_chat_ids", ["oc_env"])
    assert services.get_int_setting(db, "max_gpu_sharers") == 4
    assert services.welcome_chat_ids(db) == ["oc_env"]
    services.set_setting(db, "max_gpu_sharers", "2")
    services.set_setting(db, "welcome_chat_ids", "")
    db.commit()
    assert services.get_int_setting(db, "max_gpu_sharers") == 2
    assert services.welcome_chat_ids(db) == []  # 管理页里清空 = 所有群，不再回退到 .env


def test_offline_threshold_follows_refresh_interval(admin, db, monkeypatch):
    """刷新间隔调大后，Agent 按这个间隔上报，不能还按固定 180 秒判离线。"""
    from app.config import settings

    monkeypatch.setattr(settings, "agent_offline_seconds", settings.agent_offline_floor)
    assert admin.put("/api/admin/settings", json={"refresh_interval_seconds": "300"}).status_code == 200
    assert settings.agent_offline_seconds == 900
    assert admin.put("/api/admin/settings", json={"refresh_interval_seconds": "10"}).status_code == 200
    assert settings.agent_offline_seconds == settings.agent_offline_floor  # 下限 180 秒


# ---------------- 在日历上拖动色块右边缘：只提交结束日期 ----------------


def test_resize_by_end_date_only(admin, alice, server, db):
    g = gpu_ids(alice)
    d = today()
    rid = book(alice, g[:1], d, d + timedelta(days=3)).json()[0]["id"]
    # 延长、缩短：只传 end_date 即可
    r = alice.patch(f"/api/reservations/{rid}", json={"end_date": str(d + timedelta(days=5))})
    assert r.status_code == 200 and r.json()["end_date"] == str(d + timedelta(days=5))
    r = alice.patch(f"/api/reservations/{rid}", json={"end_date": str(d)})
    assert r.status_code == 200 and r.json()["end_date"] == str(d)  # 最短缩到今天
    # 不能缩到今天之前、不能超过单次最长天数
    assert alice.patch(f"/api/reservations/{rid}", json={"end_date": str(d - timedelta(days=1))}).status_code == 400
    assert alice.patch(f"/api/reservations/{rid}", json={"end_date": str(d + timedelta(days=20))}).status_code == 400
    # 延长时撞上别人的预约（这张卡同一天只能一个人）→ 409，原预约不变
    assert book(admin, g[:1], d + timedelta(days=2), d + timedelta(days=3)).status_code == 200
    r = alice.patch(f"/api/reservations/{rid}", json={"end_date": str(d + timedelta(days=2))})
    assert r.status_code == 409
    db.expire_all()
    assert db.get(Reservation, rid).end_date == d
    # 别人的预约普通成员不能改
    other = [x for x in admin.get("/api/me/reservations").json()][0]["id"]
    assert alice.patch(f"/api/reservations/{other}", json={"end_date": str(d + timedelta(days=4))}).status_code in (403, 404)


# ---------------- 拖动色块：和自己的其他预约重叠 / 相邻时合并；跨过今天的预约在“今天”处拆分 ----------------


def _mine(db, user_name="alice"):
    from app.models import User

    db.expire_all()
    u = db.query(User).filter_by(name=user_name).one()
    rs = db.query(Reservation).filter_by(user_id=u.id, status="active").order_by(Reservation.start_date).all()
    return [(r.start_date, r.end_date) for r in rs]


def test_dragging_into_own_other_reservation_merges_in_database(alice, server, db):
    g = gpu_ids(alice)
    d = today()
    ra = book(alice, g[:1], d + timedelta(days=1), d + timedelta(days=2), note="左").json()[0]["id"]
    rb = book(alice, g[:1], d + timedelta(days=5), d + timedelta(days=6), note="右").json()[0]["id"]
    # 右边的块往左拉到和左边的块相邻 → 合并成一条（数据库里只剩一条）
    r = alice.patch(f"/api/reservations/{rb}", json={"start_date": str(d + timedelta(days=3))})
    assert r.status_code == 200
    assert _mine(db) == [(d + timedelta(days=1), d + timedelta(days=6))]
    assert r.json()["note"] == "左"  # 备注以靠左的（第一条还没结束的）为准


def test_dragging_over_own_reservation_overlap_merges(alice, server, db):
    g = gpu_ids(alice)
    d = today()
    ra = book(alice, g[:1], d, d + timedelta(days=1)).json()[0]["id"]
    book(alice, g[:1], d + timedelta(days=4), d + timedelta(days=6))
    # 左边的块把右边缘一直拖到盖住右边的块的中间：重叠 → 合并，结束日期取较晚的
    assert alice.patch(f"/api/reservations/{ra}", json={"end_date": str(d + timedelta(days=5))}).status_code == 200
    assert _mine(db) == [(d, d + timedelta(days=6))]


def test_merging_by_dragging_respects_max_length(alice, server, db):
    g = gpu_ids(alice)
    d = today()
    ra = book(alice, g[:1], d, d + timedelta(days=5)).json()[0]["id"]
    book(alice, g[:1], d + timedelta(days=7), d + timedelta(days=15))
    # 合并后一共 16 天（> 单次最长 14 天）→ 普通成员不能靠合并绕过限制
    r = alice.patch(f"/api/reservations/{ra}", json={"end_date": str(d + timedelta(days=6))})
    assert r.status_code == 400 and "最长" in r.json()["detail"]
    assert len(_mine(db)) == 2


def test_dragging_into_someone_elses_full_gpu_is_still_rejected(admin, alice, server, db):
    g = gpu_ids(alice)
    d = today()
    rid = book(alice, g[:1], d, d + timedelta(days=1)).json()[0]["id"]
    assert book(admin, g[:1], d + timedelta(days=3), d + timedelta(days=4)).status_code == 200  # 默认一张卡只能一个人
    assert alice.patch(f"/api/reservations/{rid}", json={"end_date": str(d + timedelta(days=3))}).status_code == 409
    assert _mine(db) == [(d, d + timedelta(days=1))]


def test_split_a_running_reservation_at_today(alice, server, db):
    g = gpu_ids(alice)
    d = today()
    rid = book(alice, g[:1], d, d + timedelta(days=5), note="训练", auto_renew=True).json()[0]["id"]
    _backdate(db, rid, -3, 5)  # 3 天前开始，今天还在用，到 5 天后
    r = alice.post(f"/api/reservations/{rid}/split", json={"from_date": str(d + timedelta(days=2))})
    assert r.status_code == 200
    new = r.json()
    assert (new["start_date"], new["end_date"]) == (str(d + timedelta(days=2)), str(d + timedelta(days=5)))
    assert new["note"] == "训练" and new["auto_renew"] is True  # 备注和长期使用留在还在生效的后半段
    # 数据库里是两条：一条全部过期的历史，一条完全没过期的；中间（今天、明天）释放了
    assert _mine(db) == [(d - timedelta(days=3), d - timedelta(days=1)), (d + timedelta(days=2), d + timedelta(days=5))]
    db.expire_all()
    assert db.get(Reservation, rid).auto_renew is False
    assert services.run_auto_renew(db) == 0  # 历史那条不会再被自动延长


def test_split_rules(admin, alice, server, db):
    g = gpu_ids(alice)
    d = today()
    rid = book(alice, g[:1], d, d + timedelta(days=5)).json()[0]["id"]
    # 从今天开始的预约没有“过去的部分”，不能拆
    assert alice.post(f"/api/reservations/{rid}/split", json={"from_date": str(d + timedelta(days=2))}).status_code == 400
    _backdate(db, rid, -3, 5)
    for bad in (d, d - timedelta(days=1), d + timedelta(days=6)):  # 必须在今天之后、结束日期之内
        assert alice.post(f"/api/reservations/{rid}/split", json={"from_date": str(bad)}).status_code == 400
    from conftest import make_client

    bob = make_client("bob")
    assert bob.post(f"/api/reservations/{rid}/split", json={"from_date": str(d + timedelta(days=2))}).status_code in (403, 404)
    assert len(_mine(db)) == 1  # 都被拒绝，没有改动


def test_split_then_dragging_back_merges_again(alice, server, db):
    g = gpu_ids(alice)
    d = today()
    rid = book(alice, g[:1], d, d + timedelta(days=5), auto_renew=True).json()[0]["id"]
    _backdate(db, rid, -3, 5)
    new = alice.post(f"/api/reservations/{rid}/split", json={"from_date": str(d + timedelta(days=2))}).json()
    assert len(_mine(db)) == 2
    # 后半段的左边缘拖回今天：和历史那段相邻 → 合并回一条，长期使用标记取还在生效的那段
    r = alice.patch(f"/api/reservations/{new['id']}", json={"start_date": str(d)})
    assert r.status_code == 200
    assert _mine(db) == [(d - timedelta(days=3), d + timedelta(days=5))]
    assert r.json()["auto_renew"] is True
