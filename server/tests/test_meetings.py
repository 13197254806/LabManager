"""组会：汇报人轮换、临时调整、组会结束后结算、飞书群提醒、管理页接口。"""

import json
from datetime import datetime, time, timedelta

import pytest

from app import feishu, meetings
from app.config import now, today
from app.meetings import WEEK, Rotation, week_of
from app.models import GroupMeeting, User


# ---------------- 汇报顺序（纯逻辑） ----------------


def names(picks):
    return ["".join(chr(64 + u) for _, u in p) for p in picks]  # 1 → A, 2 → B ...


def test_rotation_cycles_and_skips_cancelled():
    r = Rotation([1, 2, 3], [], 2)
    assert names(r.schedule([1, 1, 0, 1, 1])) == ["B", "C", "", "A", "B"]  # 不开的那周不消耗顺序
    assert names(r.schedule([2, 2, 2])) == ["BC", "AB", "CA"]


def test_swap_and_skip_only_affect_near_term():
    r = Rotation([1, 2, 3, 4], [], 1)
    r.swap(0, 1)  # 下一次和下下次对调
    assert names(r.schedule([1] * 6)) == ["B", "A", "C", "D", "A", "B"]
    assert r.head == [2, 1] and r.next_uid == 3
    r.swap(0, 1)  # 再调回来：临时那一段被还原
    assert r.head == [] and r.next_uid == 1

    r = Rotation([1, 2, 3, 4], [], 1)
    r.skip(1)  # B 这次不讲，后面依次提前；下一轮 B 照常
    assert names(r.schedule([1] * 7)) == ["A", "C", "D", "A", "B", "C", "D"]

    r = Rotation([1, 2, 3], [], 3)
    r.swap(0, 1)  # 跨过一轮的边界也可以对调
    assert names(r.schedule([1] * 5)) == ["A", "C", "B", "C", "A"]


def test_take_matches_schedule_and_never_repeats_in_one_meeting():
    r = Rotation([1, 2], [], 1)
    r.skip(1)  # A A B A B …：同一次不能两个 A
    plan = names(r.schedule([2, 2]))
    assert plan == ["AB", "AB"]
    assert r.take(2) == [1, 2] and r.take(2) == [1, 2]

    r = Rotation([1, 2, 3, 4, 5], [4, 2], 1)
    expected = r.schedule([2, 1, 2])
    got = [r.take(2), r.take(1), r.take(2)]
    assert got == [[u for _, u in p] for p in expected]


# ---------------- 数据库里的组会 ----------------


@pytest.fixture
def people(db):
    us = [User(open_id=f"ou_{n}", name=n) for n in ("甲", "乙", "丙", "丁")]
    db.add_all(us)
    db.commit()
    return [u.id for u in us]


def setup(db, ids, weekday=None, enabled_at=None, chats=True, per=1):
    """组会设在本周的 weekday（默认今天往后两天对应的星期），时间 14:00。"""
    wd = (today() + timedelta(days=2)).weekday() if weekday is None else weekday
    meetings.save_config(db, weekday=wd, tm=time(14, 0), per_meeting=per, notify_days=2,
                         locations=["实验楼 302", "腾讯会议"], chats=[{"id": "oc_x", "name": "大群"}] if chats else [],
                         card_extra="")
    meetings.save_roster(db, ids)
    if enabled_at is not None:
        meetings.set_enabled(db, True)
        meetings.set_setting(db, "gm_since", enabled_at.isoformat())
        db.commit()


def start_from(db, week):
    """测试里固定「最早一个还没结束的组会」在 week 这周（否则取决于今天是星期几）。"""
    meetings.set_setting(db, "gm_settled_week", (week - WEEK).isoformat())
    db.commit()


def upcoming(db, at=None):
    return meetings.upcoming(db, meetings.load(db), at or now())


def test_settle_records_history_and_rotates(db, people):
    a, b, c, d = people
    mon = week_of(today())
    setup(db, people, weekday=0, enabled_at=datetime.combine(mon - WEEK, time()))
    start_from(db, mon - WEEK)
    # 上周一 14:00 的组会，当天还没结束 → 不结算，还能改
    assert meetings.settle(db, at=datetime.combine(mon - WEEK, time(20))) == 0
    # 第二天结算：上周一甲讲；本周一的乙
    assert meetings.settle(db, at=datetime.combine(mon - WEEK + timedelta(days=1), time(9))) == 1
    row = db.query(GroupMeeting).filter_by(week=mon - WEEK).one()
    assert row.settled and row.presenters == [a] and row.location == "实验楼 302"
    assert [m.uids for m in upcoming(db, datetime.combine(mon, time(9)))[:2]] == [[b], [c]]


def test_cancelled_and_paused_weeks_do_not_consume(db, people):
    a, b, c, d = people
    mon = week_of(today())
    setup(db, people, weekday=2, enabled_at=datetime.combine(mon - WEEK, time()))
    start_from(db, mon)
    meetings.patch_week(db, mon, cancelled=True, note="放假", at=datetime.combine(mon, time(1)))
    plan = upcoming(db, datetime.combine(mon, time(1)))
    assert plan[0].cancelled and plan[0].uids == [] and plan[1].uids == [a]
    meetings.settle(db, at=datetime.combine(mon + timedelta(days=3), time(1)))
    row = db.query(GroupMeeting).filter_by(week=mon).one()
    assert row.settled and row.presenters == [] and row.cancelled
    # 暂停期间开过的周不算
    meetings.set_enabled(db, False)
    meetings.settle(db, at=datetime.combine(mon + WEEK + timedelta(days=3), time(1)))
    assert upcoming(db, datetime.combine(mon + WEEK + timedelta(days=3), time(1)))[0].uids == [a]


def test_roster_changes_keep_pointer(db, people):
    a, b, c, d = people
    setup(db, people)
    meetings.set_next(db, c)
    assert [m.uids[0] for m in upcoming(db)[:3]] == [c, d, a]
    meetings.save_roster(db, [a, b, d])  # 下一位丙被移出 → 顺延到丁
    assert [m.uids[0] for m in upcoming(db)[:3]] == [d, a, b]
    db.get(User, a).enabled = False  # 停用的成员不参与
    db.commit()
    assert [m.uids[0] for m in upcoming(db)[:3]] == [d, b, d]


def test_notify_due_sends_once_at_the_right_time(db, people, monkeypatch):
    sent = []
    monkeypatch.setattr(feishu, "send_chat_card", lambda chat, card: sent.append((chat, card)))
    a, b, c, d = people
    mon = week_of(today()) + WEEK  # 下周四的组会：周二 14:00 提醒
    setup(db, people, weekday=3, enabled_at=datetime.combine(mon - WEEK, time()))
    start_from(db, mon)
    start = datetime.combine(mon + timedelta(days=3), time(14))
    assert meetings.notify_due(db, at=start - timedelta(days=2, minutes=1)) == 0
    assert meetings.notify_due(db, at=start - timedelta(days=2)) == 1
    assert meetings.notify_due(db, at=start - timedelta(days=1)) == 0  # 只发一次
    chat, card = sent[0]
    text = json.dumps(card, ensure_ascii=False)
    assert chat == "oc_x" and "<at id=all></at>" in text and "实验楼 302" in text
    assert "<at id=ou_甲></at>" in text and "下周汇报" in text and "<at id=ou_乙></at>" in text
    assert db.query(GroupMeeting).filter_by(week=mon).one().notified_at


def test_notify_not_backfilled_before_enabled(db, people, monkeypatch):
    sent = []
    monkeypatch.setattr(feishu, "send_chat_card", lambda chat, card: sent.append(card))
    mon = week_of(today()) + WEEK
    start = datetime.combine(mon + timedelta(days=3), time(14))
    setup(db, people, weekday=3, enabled_at=start - timedelta(days=1))  # 打开时已过提醒时间
    start_from(db, mon)
    assert meetings.notify_due(db, at=start - timedelta(hours=20)) == 0 and not sent


def test_cancelled_meeting_card(db, people, monkeypatch):
    sent = []
    monkeypatch.setattr(feishu, "send_chat_card", lambda chat, card: sent.append(card))
    a, *_ = people
    mon = week_of(today()) + WEEK
    setup(db, people, weekday=3, enabled_at=datetime.combine(mon - WEEK, time()))
    start_from(db, mon)
    meetings.patch_week(db, mon, cancelled=True, note="学院活动", at=datetime.combine(mon, time(1)))
    start = datetime.combine(mon + timedelta(days=3), time(14))
    assert meetings.notify_due(db, at=start - timedelta(days=2)) == 1
    text = json.dumps(sent[0], ensure_ascii=False)
    assert "组会暂停" in text and "学院活动" in text and "<at id=ou_甲></at>" in text  # 下次汇报还是甲


def test_failed_send_tells_admins(db, people, monkeypatch):
    def boom(chat, card):
        raise feishu.FeishuError("230002: bot not in chat")
    texts = []
    monkeypatch.setattr(feishu, "send_chat_card", boom)
    monkeypatch.setattr(feishu, "send_text", lambda oid, t: texts.append((oid, t)))
    db.get(User, people[0]).is_admin = True
    db.commit()
    mon = week_of(today()) + WEEK
    setup(db, people, weekday=3, enabled_at=datetime.combine(mon - WEEK, time()))
    start_from(db, mon)
    start = datetime.combine(mon + timedelta(days=3), time(14))
    assert meetings.notify_due(db, at=start - timedelta(days=2)) == 1
    row = db.query(GroupMeeting).filter_by(week=mon).one()
    assert "230002" in row.notify_error and texts and texts[0][0] == "ou_甲"


def test_changing_weekday_keeps_this_weeks_meeting(db, people):
    mon = week_of(today())
    setup(db, people, weekday=3, enabled_at=datetime.combine(mon - WEEK, time()))
    start_from(db, mon)
    thu = datetime.combine(mon + timedelta(days=3), time(14))
    # 周四 16:00（刚开完，当天还没结束）改成每周一：这周仍记为周四开过，下周起周一
    meetings.save_config(db, weekday=0, tm=time(14, 0), per_meeting=1, notify_days=2, locations=[], chats=[],
                         card_extra="", at=thu + timedelta(hours=2))
    plan = upcoming(db, thu + timedelta(hours=2))
    assert plan[0].start == thu and plan[1].start == datetime.combine(mon + WEEK, time(14))
    # 暂停时改时间直接生效
    meetings.set_enabled(db, False)
    meetings.save_config(db, weekday=4, tm=time(9, 0), per_meeting=1, notify_days=2, locations=[], chats=[],
                         card_extra="", at=thu + timedelta(hours=2))
    assert upcoming(db, thu + timedelta(hours=2))[1].start == datetime.combine(mon + WEEK + timedelta(days=4), time(9))


# ---------------- 接口 ----------------


def test_api_flow(admin, alice, db, monkeypatch):
    sent = []
    monkeypatch.setattr(feishu, "send_chat_card", lambda chat, card: sent.append(card))
    bob = User(open_id="ou_bob", name="bob")
    db.add(bob)
    db.commit()
    ids = [u.id for u in db.query(User).order_by(User.id)]
    r = admin.put("/api/admin/meetings/config", json={
        "weekday": 4, "time": "09:30", "per_meeting": 1, "notify_days": 2,
        "locations": ["302", " 302 ", "腾讯会议"], "chats": [{"id": "oc_abc", "name": "大群"}], "card_extra": "PPT 提前发群里"})
    assert r.status_code == 200, r.text
    assert r.json()["config"]["locations"] == ["302", "腾讯会议"]
    r = admin.put("/api/admin/meetings/roster", json={"user_ids": ids})
    d = r.json()
    assert [p["id"] for p in d["roster"]] == ids
    up = d["upcoming"]
    assert [m["presenters"][0]["id"] for m in up[:3]] == ids
    assert up[0]["start_at"].endswith("09:30:00") and up[0]["location"] == "302"
    assert d["preview"]["card"]["elements"][-1]["text"]["content"] == "PPT 提前发群里"

    # 普通成员不能看
    assert alice.get("/api/admin/meetings").status_code == 403

    # 推后：第一位和第二位对调；用旧的人来调整会被拒绝
    p0, p1 = up[0]["presenters"][0], up[1]["presenters"][0]
    r = admin.post("/api/admin/meetings/adjust", json={"op": "swap", "i": p0["pos"], "ui": p0["id"], "j": p1["pos"], "uj": p1["id"]})
    assert [m["presenters"][0]["id"] for m in r.json()["upcoming"][:2]] == [p1["id"], p0["id"]]
    assert r.json()["adjusted"] == 2
    r = admin.post("/api/admin/meetings/adjust", json={"op": "skip", "i": 0, "ui": p0["id"]})
    assert r.status_code == 409
    r = admin.post("/api/admin/meetings/adjust", json={"op": "skip", "i": 0, "ui": p1["id"]})
    assert [m["presenters"][0]["id"] for m in r.json()["upcoming"][:3]] == [p0["id"], ids[2], ids[0]]

    # 这周不开 → 汇报人顺延；时间只能改在这一周之内
    week = up[0]["week"]
    r = admin.patch(f"/api/admin/meetings/{week}", json={"cancelled": True, "note": "放假"})
    d = r.json()
    assert d["upcoming"][0]["cancelled"] and d["upcoming"][0]["presenters"] == []
    assert d["upcoming"][1]["presenters"][0]["id"] == p0["id"]
    nxt = d["upcoming"][1]["week"]
    assert admin.patch(f"/api/admin/meetings/{nxt}", json={"start_at": f"{week}T10:00"}).status_code == 400
    r = admin.patch(f"/api/admin/meetings/{nxt}", json={"start_at": f"{nxt}T10:00", "location": "腾讯会议"})
    m = r.json()["upcoming"][1]
    assert m["custom_time"] and m["start_at"] == f"{nxt}T10:00:00" and m["location"] == "腾讯会议"
    r = admin.patch(f"/api/admin/meetings/{nxt}", json={"start_at": None, "location": ""})
    m = r.json()["upcoming"][1]
    assert not m["custom_time"] and m["location"] == "302"

    # 从某人开始：清掉临时调整
    r = admin.post("/api/admin/meetings/next", json={"user_id": ids[1]})
    assert r.json()["adjusted"] == 0 and r.json()["upcoming"][1]["presenters"][0]["id"] == ids[1]

    # 手动发到群里
    r = admin.post(f"/api/admin/meetings/{nxt}/send", json={"to": "chat"})
    assert r.status_code == 200, r.text
    assert sent and r.json()["upcoming"][1]["notify_state"] == "sent"
    # 没配置飞书应用时，发给自己预览会提示原因
    r = admin.post(f"/api/admin/meetings/{nxt}/send", json={"to": "me"})
    assert r.status_code == 400 and "未配置飞书" in r.json()["detail"]


def test_api_enable_and_validation(admin):
    r = admin.put("/api/admin/meetings/enabled", json={"enabled": True})
    assert r.json()["config"]["enabled"] is True
    bad = {"weekday": 7, "time": "25:00", "per_meeting": 1, "notify_days": 2, "locations": [], "chats": []}
    assert admin.put("/api/admin/meetings/config", json=bad).status_code == 422
    bad = {"weekday": 1, "time": "14:00", "per_meeting": 1, "notify_days": 2, "locations": [], "chats": [{"id": "not-a-chat"}]}
    assert admin.put("/api/admin/meetings/config", json=bad).status_code == 422
    assert admin.put("/api/admin/meetings/roster", json={"user_ids": [999]}).status_code == 400
    assert admin.get("/api/admin/feishu/chats").status_code == 400  # 测试环境没有配置飞书应用
