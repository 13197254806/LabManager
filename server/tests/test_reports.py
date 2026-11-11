"""组会周报：发通知卡片前在飞书知识库建好这周的周报、卡片附链接；旧周报移到历史周报，「周报」下按日期从新到旧排。"""

import json
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace

import pytest

from app import feishu, meetings, reports
from app.config import today
from app.meetings import WEEK, week_of
from app.models import GroupMeeting, User

HOST = "https://lab.feishu.cn/wiki/"


class FakeWiki:
    """内存里的知识库。和飞书一样：新建、移进来的页面排在同级的最后（append=False 时排最前，用来测出错的情况）。"""

    def __init__(self, append=True):
        self.nodes, self.order, self.calls, self.append = {}, {}, [], append

    def add(self, title, parent=""):
        tok = f"wik{len(self.nodes) + 1:06d}Tok"
        self.nodes[tok] = {"title": title, "parent": parent}
        self.order.setdefault(parent, []).append(tok)
        return tok

    def node(self, tok):
        if tok not in self.nodes:
            raise feishu.FeishuError("131005: not found（页面不存在，检查链接是否正确）")
        n = self.nodes[tok]
        return {"space_id": "sp1", "node_token": tok, "title": n["title"], "parent_node_token": n["parent"]}

    def children(self, space, parent):
        return [self.node(t) for t in self.order.get(parent, [])]

    def copy(self, space, tok, to_space, to_parent, title):
        self.calls.append(("copy", self.nodes[tok]["title"], title))
        return self.node(self.add(title, to_parent))

    def move(self, space, tok, to_space, to_parent):
        self.calls.append(("move", self.nodes[tok]["title"], self.nodes[to_parent]["title"]))
        self.order[self.nodes[tok]["parent"]].remove(tok)
        kids = self.order.setdefault(to_parent, [])
        kids.append(tok) if self.append else kids.insert(0, tok)
        self.nodes[tok]["parent"] = to_parent
        return self.node(tok)

    def titles(self, parent):
        return [self.nodes[t]["title"] for t in self.order.get(parent, [])]

    def moves(self):
        return [c for c in self.calls if c[0] == "move"]

    def install(self, monkeypatch):
        monkeypatch.setattr(feishu, "wiki_node", self.node)
        monkeypatch.setattr(feishu, "wiki_children", self.children)
        monkeypatch.setattr(feishu, "wiki_copy", self.copy)
        monkeypatch.setattr(feishu, "wiki_move", self.move)
        return self


def lab_wiki(titles):
    """科研 / 周报 / [周报模板, *titles, 历史周报]，返回 (wiki, 配置)。"""
    w = FakeWiki()
    root = w.add("科研")
    parent = w.add("周报", root)
    tpl = w.add("周报模板 - YYYY/MM/DD", parent)
    for t in titles:
        w.add(t, parent)
    arc = w.add("历史周报", parent)
    cfg = SimpleNamespace(report_template=HOST + tpl, report_parent=HOST + parent, report_archive=HOST + arc, report_keep=8)
    w.parent, w.arc = parent, arc
    return w, cfg


def test_urls_and_titles():
    assert reports.normalize_url(" https://vcnos.feishu.cn/wiki/IcKKwNTLEiijsnkATjac8buYnLd?fromScene=spaceOverview ") \
        == "https://vcnos.feishu.cn/wiki/IcKKwNTLEiijsnkATjac8buYnLd"
    for bad in ("IcKKwNTLEiijsnkATjac8buYnLd", "https://vcnos.feishu.cn/docx/IcKKwNTLEiijsnk", "http://a.cn/wiki/IcKKwNTLEiij"):
        with pytest.raises(ValueError):
            reports.normalize_url(bad)
    assert reports.title_for(date(2026, 11, 6)) == "周报 - 2026/11/6"
    assert reports.title_date("周报 - 2026/11/6") == date(2026, 11, 6)
    assert reports.title_date("周报-2026-10-30（补）") == date(2026, 10, 30)
    assert reports.title_date("2026年11月13日周报") == date(2026, 11, 13)
    assert reports.title_date("周报模板 - YYYY/MM/DD") is None and reports.title_date("周报 - 2026/13/40") is None


def test_ensure_reuses_this_weeks_report_or_copies_template(monkeypatch):
    w, cfg = lab_wiki(["周报 - 2026/11/13", "周报 - 2026/10/30", "周报 - 2026/11/6"])
    w.install(monkeypatch)
    # 已经有同一周的（组会改到周四也算这一周）：直接用，不再复制
    r = reports.ensure(cfg, date(2026, 11, 12))
    assert (r.title, r.created) == ("周报 - 2026/11/13", False) and r.url.startswith(HOST) and not w.calls
    r = reports.ensure(cfg, date(2026, 11, 20))
    assert (r.title, r.created) == ("周报 - 2026/11/20", True)
    assert w.calls == [("copy", "周报模板 - YYYY/MM/DD", "周报 - 2026/11/20")]
    assert w.nodes[r.url.removeprefix(HOST)]["parent"] == w.parent


def test_tidy_sorts_the_current_layout_with_few_moves(monkeypatch):
    # 目录现在的样子：11/13 在前，10/30 和 11/6 是从旧到新
    w, cfg = lab_wiki(["周报 - 2026/11/13", "周报 - 2026/10/30", "周报 - 2026/11/6"])
    w.install(monkeypatch)
    assert reports.tidy(cfg, date(2026, 11, 13)) == {"archived": [], "moved": 2}
    assert w.titles(w.parent) == ["周报模板 - YYYY/MM/DD", "周报 - 2026/11/13", "周报 - 2026/11/6", "周报 - 2026/10/30", "历史周报"]
    assert len(w.moves()) == 4  # 只挪了 10/30 和历史周报（各移出、移回一次）
    # 下周：新建的周报在最后，排到模板后面
    reports.ensure(cfg, date(2026, 11, 20))
    w.calls.clear()
    assert reports.tidy(cfg, date(2026, 11, 20))["moved"] == 4
    assert w.titles(w.parent)[:3] == ["周报模板 - YYYY/MM/DD", "周报 - 2026/11/20", "周报 - 2026/11/13"]
    assert w.titles(w.parent)[-1] == "历史周报"
    w.calls.clear()
    assert reports.tidy(cfg, date(2026, 11, 20)) == {"archived": [], "moved": 0} and not w.calls  # 排好了就不动


def test_tidy_archives_reports_older_than_keep_weeks(monkeypatch):
    days = [date(2026, 8, 28) + n * WEEK for n in range(13)]  # 8/28 … 11/20，每周一篇
    w, cfg = lab_wiki([reports.title_for(d) for d in reversed(days[:6])] + ["组会纪要"] + [reports.title_for(d) for d in days[6:]])
    w.add("周报 - 2026/8/21", w.arc)  # 历史周报里原来就有的
    w.install(monkeypatch)
    r = reports.tidy(cfg, date(2026, 11, 20))
    # 保留最近 8 周（10/2 … 11/20），更早的从旧到新移进历史周报，排在原有的后面
    assert r["archived"] == ["周报 - 2026/8/28", "周报 - 2026/9/4", "周报 - 2026/9/11", "周报 - 2026/9/18", "周报 - 2026/9/25"]
    assert w.titles(w.arc) == ["周报 - 2026/8/21", *r["archived"]]
    assert w.titles(w.parent) == ["周报模板 - YYYY/MM/DD", *[reports.title_for(d) for d in reversed(days[5:])], "组会纪要", "历史周报"]
    cfg.report_keep = 3
    assert len(reports.tidy(cfg, date(2026, 11, 20))["archived"]) == 5
    assert len([t for t in w.titles(w.parent) if t.startswith("周报 - ")]) == 3


def test_tidy_stops_when_moved_pages_do_not_go_last(monkeypatch):
    w, cfg = lab_wiki(["周报 - 2026/10/30", "周报 - 2026/11/6"])
    w.append = False
    w.install(monkeypatch)
    with pytest.raises(RuntimeError, match="没有排到最后"):
        reports.tidy(cfg, date(2026, 11, 6))
    assert len(w.moves()) == 2  # 只挪了第一个页面就停下


def test_check_reports_titles_and_errors(monkeypatch):
    w, cfg = lab_wiki(["周报 - 2026/11/13"])
    w.install(monkeypatch)
    r = reports.check(cfg)
    assert r["ok"] and [i["title"] for i in r["items"]] == ["周报模板 - YYYY/MM/DD", "周报", "历史周报"]
    assert r["items"][1]["detail"] == "下面有 1 篇周报"
    cfg.report_archive, cfg.report_template = "", HOST + "wikMissingPage1"
    r = reports.check(cfg)
    assert not r["ok"] and "131005" in r["items"][0]["error"] and r["items"][2]["ok"]


# ---------------- 和组会通知一起 ----------------


@pytest.fixture
def lab(db, monkeypatch):
    """下周四 14:00 的组会、周二 14:00 发卡片，打开了周报。返回 (wiki, 组会那周的周一, 组会开始时间, 发出去的卡片)。"""
    us = [User(open_id=f"ou_{n}", name=n, is_admin=(n == "甲")) for n in ("甲", "乙")]
    db.add_all(us)
    db.commit()
    mon = week_of(today()) + WEEK
    meetings.save_config(db, weekday=3, tm=time(14, 0), per_meeting=1, notify_days=2, locations=["实验楼 302"],
                         chats=[{"id": "oc_x", "name": "大群"}], card_extra="")
    meetings.save_roster(db, [u.id for u in us])
    meetings.set_enabled(db, True)
    meetings.set_setting(db, "gm_since", datetime.combine(mon - WEEK, time()).isoformat())
    meetings.set_setting(db, "gm_settled_week", (mon - WEEK).isoformat())
    db.commit()
    start = datetime.combine(mon + timedelta(days=3), time(14))
    old = start.date() - 9 * WEEK
    w, cfg = lab_wiki([reports.title_for(old), reports.title_for(start.date() - WEEK)])
    w.install(monkeypatch)
    meetings.save_report_config(db, enabled=True, template=cfg.report_template, parent=cfg.report_parent,
                                archive=cfg.report_archive, keep=8)
    sent = []
    monkeypatch.setattr(feishu, "send_chat_card", lambda chat, card: sent.append(card))
    return w, mon, start, sent


def test_notify_creates_report_and_links_it(db, lab):
    w, mon, start, sent = lab
    title = reports.title_for(start.date())
    assert meetings.notify_due(db, at=start - timedelta(days=2)) == 1
    text = json.dumps(sent[0], ensure_ascii=False)
    row = db.query(GroupMeeting).filter_by(week=mon).one()
    assert row.report_title == title and row.report_url.startswith(HOST) and not row.report_error
    assert f"📝 **本周周报**：[{title}]({row.report_url})" in text and "请大家在组会开始前完成周报填写。" in text
    button = sent[0]["elements"][-1]["actions"][0]
    assert button["text"]["content"] == "填写周报" and button["url"] == row.report_url
    # 发完整理了目录：9 周前的移到历史周报，新的排在模板后面
    assert w.titles(w.parent) == ["周报模板 - YYYY/MM/DD", title, reports.title_for(start.date() - WEEK), "历史周报"]
    assert w.titles(w.arc) == [reports.title_for(start.date() - 9 * WEEK)]


def test_report_failure_still_sends_card_and_tells_admins(db, lab, monkeypatch):
    w, mon, start, sent = lab

    def denied(*a):
        raise feishu.FeishuError("131006: permission denied（机器人没有这个页面的权限）")
    monkeypatch.setattr(feishu, "wiki_copy", denied)
    texts = []
    monkeypatch.setattr(feishu, "send_text", lambda oid, t: texts.append((oid, t)))
    assert meetings.notify_due(db, at=start - timedelta(days=2)) == 1
    text = json.dumps(sent[0], ensure_ascii=False)
    assert "打开周报目录" in text and "周报目录" in text and "填写周报" not in text
    row = db.query(GroupMeeting).filter_by(week=mon).one()
    assert "131006" in row.report_error and row.notified_at and not row.report_url
    assert texts and texts[0][0] == "ou_甲" and "周报没有建好" in texts[0][1]
    assert w.titles(w.arc) == []  # 周报没建成就不整理目录


def test_preview_card_before_report_is_created(db, lab):
    w, mon, start, sent = lab
    card = meetings.overview(db, at=start - timedelta(days=3))["preview"]["card"]
    text = json.dumps(card, ensure_ascii=False)
    assert f"发到群里时自动创建「{reports.title_for(start.date())}」" in text and not w.calls


def test_report_api(admin, db, monkeypatch):
    w, cfg = lab_wiki(["周报 - 2026/11/6"])
    w.install(monkeypatch)
    base = {"weekday": 4, "time": "14:00", "per_meeting": 1, "notify_days": 2, "locations": ["302"], "chats": []}
    r = admin.put("/api/admin/meetings/config", json={**base, "report_enabled": True, "report_template": cfg.report_template + "?fromScene=x",
                                                        "report_parent": cfg.report_parent, "report_archive": "", "report_keep": 6})
    assert r.status_code == 200, r.text
    c = r.json()["config"]
    assert c["report_enabled"] and c["report_template"] == cfg.report_template and c["report_keep"] == 6
    # 不传周报字段（旧页面）不改周报设置
    assert admin.put("/api/admin/meetings/config", json=base).json()["config"]["report_template"] == cfg.report_template
    bad = admin.put("/api/admin/meetings/config", json={**base, "report_enabled": True, "report_template": "abc", "report_parent": cfg.report_parent})
    assert bad.status_code == 400 and "周报模板" in bad.json()["detail"]
    bad = admin.put("/api/admin/meetings/config", json={**base, "report_enabled": True, "report_template": "", "report_parent": cfg.report_parent})
    assert bad.status_code == 400
    bad = admin.put("/api/admin/meetings/config", json={**base, "report_enabled": True, "report_template": cfg.report_parent, "report_parent": cfg.report_parent})
    assert bad.status_code == 400
    assert admin.post("/api/admin/meetings/report-check").json()["ok"]

    # 手动创建某一周的周报（不发卡片）
    week = admin.get("/api/admin/meetings").json()["upcoming"][0]
    r = admin.post(f"/api/admin/meetings/{week['week']}/report")
    assert r.status_code == 200, r.text
    m = r.json()["upcoming"][0]
    assert m["report_title"] == reports.title_for(date.fromisoformat(m["start_at"][:10])) and m["report_url"].startswith(HOST)
    assert m["notify_state"] != "sent" and w.titles(w.parent)[1] == m["report_title"]
    admin.patch(f"/api/admin/meetings/{week['week']}", json={"cancelled": True})
    assert "这周不开" in admin.post(f"/api/admin/meetings/{week['week']}/report").json()["detail"]


def test_manual_send_creates_report_once(db, lab, monkeypatch):
    w, mon, start, sent = lab
    meetings.send_now(db, mon)
    meetings.send_now(db, mon)  # 再发一次：用同一篇，不重复建
    assert [c for c in w.calls if c[0] == "copy"] == [("copy", "周报模板 - YYYY/MM/DD", reports.title_for(start.date()))]
    assert all("填写周报" in json.dumps(c, ensure_ascii=False) for c in sent) and len(sent) == 2
    # 发给自己预览：不建周报，用已有的链接
    me = db.query(User).filter_by(name="甲").one()
    cards = []
    monkeypatch.setattr(feishu, "send_card", lambda oid, card, raise_errors=False: cards.append(card))
    meetings.send_now(db, mon, me)
    assert "填写周报" in json.dumps(cards[0], ensure_ascii=False) and "<at id=all>" not in json.dumps(cards[0])
