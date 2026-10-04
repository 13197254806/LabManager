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
    g = gpu_ids(alice)
    d = today()
    rid = book(alice, g[:1], d, d, auto_renew=True).json()[0]["id"]
    assert book(admin, g[:1], d + timedelta(days=3), d + timedelta(days=5)).status_code == 200
    assert services.run_auto_renew(db) == 1
    db.expire_all()
    r = db.get(Reservation, rid)
    assert r.end_date == d + timedelta(days=2) and r.auto_renew  # 只续到冲突前一天


def test_auto_renew_disabled_when_nothing_can_be_renewed(admin, alice, server, db):
    g = gpu_ids(alice)
    d = today()
    rid = book(alice, g[:1], d, d, auto_renew=True).json()[0]["id"]
    assert book(admin, g[:1], d + timedelta(days=1), d + timedelta(days=5)).status_code == 200
    assert services.run_auto_renew(db) == 0
    db.expire_all()
    r = db.get(Reservation, rid)
    assert r.end_date == d and not r.auto_renew  # 关闭自动续约，不会每分钟重试
