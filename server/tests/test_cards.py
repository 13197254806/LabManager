"""管理 → 飞书卡片：编辑一张卡片发给自己或发到群；「新人欢迎卡片」模板和自动欢迎的群。"""

import json

import pytest

from app import feishu, services
from app.config import settings
from app.models import Setting

CARD = {"title": " 停电通知 ", "color": "orange", "markdown": "周六 9:00-12:00 机房停电", "links": "打开平台|{base_url}\n说明|https://example.com/a"}


@pytest.fixture
def feishu_on(monkeypatch):
    monkeypatch.setattr(settings, "feishu_app_id", "x")
    monkeypatch.setattr(settings, "feishu_app_secret", "y")
    monkeypatch.setattr(feishu, "get_chat", lambda chat_id: {"name": f"群{chat_id[-1]}", "avatar": ""})


def test_send_to_chats_with_at_all(admin, db, feishu_on, monkeypatch):
    sent = []
    monkeypatch.setattr(feishu, "send_chat_card", lambda chat, card: sent.append((chat, card)))
    chats = [{"id": "oc_a", "name": "甲群"}, {"id": "oc_b", "name": "乙群"}, {"id": "oc_a", "name": "甲群"}]
    r = admin.post("/api/admin/cards/send", json={**CARD, "to": "chats", "chats": chats, "at_all": True})
    assert r.status_code == 200 and r.json() == {"sent": 2, "errors": []}  # 重复的群只发一次
    assert [c for c, _ in sent] == ["oc_a", "oc_b"]
    card = sent[0][1]
    assert card["header"] == {"title": {"tag": "plain_text", "content": "停电通知"}, "template": "orange"}
    assert card["elements"][0]["text"]["content"] == "<at id=all></at> 周六 9:00-12:00 机房停电"
    btns = card["elements"][1]["actions"]
    assert [(b["text"]["content"], b["url"], b["type"]) for b in btns] == [
        ("打开平台", settings.base_url, "primary"), ("说明", "https://example.com/a", "default")]
    # 下次打开页面默认还选这些群
    assert [c["id"] for c in admin.get("/api/admin/cards").json()["chats"]] == ["oc_a", "oc_b"]


def test_send_reports_failed_chats(admin, feishu_on, monkeypatch):
    def send(chat, card):
        if chat == "oc_b":
            raise feishu.FeishuError("230002: bot not in chat")

    monkeypatch.setattr(feishu, "send_chat_card", send)
    r = admin.post("/api/admin/cards/send", json={**CARD, "to": "chats", "chats": [{"id": "oc_a"}, {"id": "oc_b", "name": "乙群"}]})
    assert r.json() == {"sent": 1, "errors": ["乙群：230002: bot not in chat"]}


def test_send_to_me_without_at_all(admin, feishu_on, monkeypatch):
    sent = []
    monkeypatch.setattr(feishu, "send_card", lambda oid, card, raise_errors=False: sent.append(card))
    r = admin.post("/api/admin/cards/send", json={**CARD, "to": "me", "at_all": True})
    assert r.status_code == 200
    assert sent[0]["elements"][0]["text"]["content"] == "周六 9:00-12:00 机房停电"  # 私聊里 @所有人 没用


def test_card_is_validated(admin, alice, feishu_on, monkeypatch):
    monkeypatch.setattr(feishu, "send_chat_card", lambda chat, card: pytest.fail("不该发出去"))
    send = lambda **kw: admin.post("/api/admin/cards/send", json={**CARD, "to": "chats", "chats": [{"id": "oc_a"}], **kw})  # noqa: E731
    assert "标题" in send(title="  ").json()["detail"]
    assert send(color="pink").status_code == 400
    assert "第 2 行" in send(links="好|https://a.com\n坏的一行").json()["detail"]
    assert "第 1 行" in send(links="文字|ftp://a.com").json()["detail"]
    assert send(markdown=" ", links="").status_code == 400
    assert send(chats=[]).status_code == 400
    assert send(chats=[{"id": "not_a_chat"}]).status_code == 422
    # 只有按钮、没有正文也可以
    monkeypatch.setattr(feishu, "send_chat_card", lambda chat, card: None)
    assert send(markdown="").json()["sent"] == 1
    # 普通成员不能发
    assert alice.post("/api/admin/cards/send", json={**CARD, "to": "me"}).status_code == 403


def test_send_needs_feishu(admin):
    r = admin.post("/api/admin/cards/send", json={**CARD, "to": "me"})
    assert r.status_code == 400 and "未配置飞书" in r.json()["detail"]


def test_save_welcome_template(admin, db, monkeypatch):
    sent = []
    monkeypatch.setattr(feishu, "send_card", lambda oid, card, raise_errors=False: sent.append(card) or True)
    w = admin.get("/api/admin/cards").json()["welcome"]
    assert w["title"] == services.DEFAULT_SETTINGS["welcome_title"] and w["color"] == "blue"
    assert admin.put("/api/admin/cards/welcome", json={**CARD, "links": "坏"}).status_code == 400
    r = admin.put("/api/admin/cards/welcome", json=CARD)
    assert r.status_code == 200 and r.json()["welcome"]["title"] == "停电通知"
    # 新成员自动收到的就是保存的这一版
    assert services.send_welcome(db, "ou_new")
    assert sent[-1]["header"]["template"] == "orange" and sent[-1]["elements"][0]["text"]["content"] == CARD["markdown"]
    assert not services.send_welcome(db, "ou_new")  # 每人只自动发一次


def test_welcome_chats(admin, db, feishu_on, monkeypatch):
    # .env 里的默认值（逗号分隔）：显示时补上群名，但不落库
    monkeypatch.setattr(settings, "welcome_chat_ids", ["oc_1", "oc_2"])
    r = admin.get("/api/admin/cards").json()
    assert [(c["id"], c["name"]) for c in r["welcome_chats"]] == [("oc_1", "群1"), ("oc_2", "群2")]
    assert db.get(Setting, "welcome_chat_ids") is None
    # 在管理页保存后以管理页为准，存成带群名的 JSON
    r = admin.put("/api/admin/cards/welcome-chats", json={"chats": [{"id": "oc_3", "name": "丙群", "avatar": "https://a/b.png"}]})
    assert r.status_code == 200 and r.json()["welcome_chats"] == [{"id": "oc_3", "name": "丙群", "avatar": "https://a/b.png"}]
    db.expire_all()
    assert json.loads(db.get(Setting, "welcome_chat_ids").value)[0]["id"] == "oc_3"
    assert services.welcome_chat_ids(db) == ["oc_3"]
    # 清空 = 机器人所在的所有群
    admin.put("/api/admin/cards/welcome-chats", json={"chats": []})
    db.expire_all()
    assert services.welcome_chat_ids(db) == []


def test_old_comma_separated_welcome_chats_still_work(db):
    services.set_setting(db, "welcome_chat_ids", "oc_a, oc_b\noc_c，oc_d")
    db.commit()
    assert services.welcome_chat_ids(db) == ["oc_a", "oc_b", "oc_c", "oc_d"]
