"""组会：每周最多开一次，按名单轮换 PPT 汇报人；组会开始前 N 天往指定的飞书群发通知卡片并 @所有人。

存储
- 每周默认的星期几和时间、地点列表、汇报名单、飞书群等放在 settings 表（gm_* 这些键）；
- group_meetings 表按周（该周周一）记录：单独改过的时间 / 地点 / 备注、这周不开、发过的提醒，
  以及已经过去的组会当时的时间、地点和汇报人（历史）。没有记录的周按默认设置。

汇报顺序 = gm_head（临时调整过的一小段）+ 从 gm_next 开始按名单循环。
- 提前 / 推后 / 跳过：先把要改的位置及之前的人从名单循环里依次取出放进 gm_head，再在里面调换 / 删除。
  后面仍按名单循环，所以临时调整只影响这几次，跳过的人下一轮照常轮到；
- 组会当天结束后，从最前面取走这次的汇报人（同一次里不重复）；不开的那周不消耗顺序；
- 停用的成员不参与轮换。
"""

import json
import logging
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from . import feishu
from .config import now, settings
from .models import GroupMeeting, Setting, User
from .services import set_setting

log = logging.getLogger("meetings")

# 改名单 / 调顺序（网页请求）和组会结束后取走汇报人、发提醒（定时任务）都要读改同一份顺序，串行执行
lock = threading.Lock()

WEEK = timedelta(days=7)
WEEKDAY_CN = "一二三四五六日"
HORIZON = 8  # 管理页「近期安排」显示几周

DEFAULTS = {
    "gm_enabled": "0",  # 1 = 组会进行中（轮换、发提醒）；0 = 暂停（假期 / 还没配置好）
    "gm_since": "",  # 最近一次打开的时间：之前的组会不算开过，之前就该发的提醒不补发
    "gm_weekday": "3",  # 0 = 周一
    "gm_time": "14:00",
    "gm_per_meeting": "1",  # 每次几位汇报人
    "gm_notify_days": "2",  # 提前几天发卡片（与组会同一时刻）
    "gm_locations": "[]",  # 第一个是默认地点
    "gm_chats": "[]",  # [{"id": "oc_xxx", "name": "群名", "avatar": "群头像地址"}]
    "gm_card_extra": "",  # 卡片末尾的附加说明（lark_md）
    "gm_roster": "[]",  # 汇报名单（user_id，按轮换顺序）
    "gm_head": "[]",
    "gm_next": "",
    "gm_settled_week": "",  # 已经处理到哪一周（周一的日期）
}


def _get(db: Session, key: str) -> str:
    row = db.get(Setting, key)
    return row.value if row else DEFAULTS[key]


def _json(db: Session, key: str, default):
    try:
        v = json.loads(_get(db, key) or "null")
    except ValueError:
        return default
    return v if isinstance(v, type(default)) else default


def _int(db: Session, key: str, lo: int, hi: int) -> int:
    try:
        return min(hi, max(lo, int(_get(db, key))))
    except ValueError:
        return int(DEFAULTS[key])


@dataclass
class Cfg:
    enabled: bool
    since: datetime | None
    weekday: int
    time: time
    per_meeting: int
    notify_days: int
    locations: list[str]
    chats: list[dict]
    card_extra: str
    roster: list[int]  # 存的名单（含已停用的成员）
    head: list[int]
    next_uid: int | None

    @property
    def default_location(self) -> str:
        return self.locations[0] if self.locations else ""


def parse_hm(text: str) -> time:
    hh, mm = text.split(":")
    return time(int(hh), int(mm))


def load(db: Session) -> Cfg:
    since = _get(db, "gm_since")
    try:
        tm = parse_hm(_get(db, "gm_time"))
    except ValueError:
        tm = time(14, 0)
    nxt = _get(db, "gm_next")
    return Cfg(
        enabled=_get(db, "gm_enabled") == "1",
        since=datetime.fromisoformat(since) if since else None,
        weekday=_int(db, "gm_weekday", 0, 6),
        time=tm,
        per_meeting=_int(db, "gm_per_meeting", 1, 5),
        notify_days=_int(db, "gm_notify_days", 1, 6),
        locations=[str(x) for x in _json(db, "gm_locations", [])],
        chats=[c for c in _json(db, "gm_chats", []) if isinstance(c, dict) and c.get("id")],
        card_extra=_get(db, "gm_card_extra"),
        roster=[int(x) for x in _json(db, "gm_roster", [])],
        head=[int(x) for x in _json(db, "gm_head", [])],
        next_uid=int(nxt) if nxt.isdigit() else None,
    )


# ---------------- 汇报顺序 ----------------


class Rotation:
    """汇报顺序：head（临时调整过的一段）+ 从 order[p] 开始按名单无限循环。下面说的「位置」都是在这个顺序里的下标。"""

    def __init__(self, order: list[int], head: list[int], next_uid: int | None):
        self.order = list(order)
        self.head = [u for u in head if u in self.order]
        self.p = self.order.index(next_uid) if next_uid in self.order else 0

    def copy(self) -> "Rotation":
        r = Rotation(self.order, [], None)
        r.head, r.p = list(self.head), self.p
        return r

    @property
    def next_uid(self) -> int | None:
        """名单循环接下来轮到谁（临时调整的那一段之后）。"""
        return self.order[self.p] if self.order else None

    def materialize(self, n: int):
        """把名单循环里的人依次取出放进 head，直到 head 至少有 n 个（顺序不变）。"""
        while len(self.head) < n and self.order:
            self.head.append(self.order[self.p])
            self.p = (self.p + 1) % len(self.order)

    def normalize(self):
        """把 head 末尾和名单循环本来就一样的部分还回去（顺序不变，只是让临时调整的那一段尽量短）。"""
        n = len(self.order)
        while self.head and n and self.head[-1] == self.order[(self.p - 1) % n]:
            self.head.pop()
            self.p = (self.p - 1) % n

    def at(self, i: int) -> int | None:
        r = self.copy()
        r.materialize(i + 1)
        return r.head[i] if i < len(r.head) else None

    def swap(self, i: int, j: int):
        self.materialize(max(i, j) + 1)
        self.head[i], self.head[j] = self.head[j], self.head[i]
        self.normalize()

    def skip(self, i: int):
        """这一位这次不讲：后面的人依次提前；名单循环照旧，下一轮还会轮到 TA。"""
        self.materialize(i + 1)
        del self.head[i]
        self.normalize()

    def take(self, k: int) -> list[int]:
        """取走一次组会的 k 位汇报人。同一次里不会出现同一个人（重复的留到下一次）。"""
        k = min(k, len(self.order))
        picked: list[int] = []
        i = 0
        while len(picked) < k:
            self.materialize(i + 1)
            if i >= len(self.head):
                break
            if self.head[i] in picked:
                i += 1
            else:
                picked.append(self.head.pop(i))
        self.normalize()
        return picked

    def schedule(self, counts: list[int]) -> list[list[tuple[int, int]]]:
        """依次给每次组会排 counts[n] 位汇报人（规则同 take，不改变自身）。返回每次的 [(位置, user_id)]。"""
        r = self.copy()
        r.materialize(len(r.head) + 2 * (sum(counts) + len(r.order)))
        buf = list(enumerate(r.head))
        out = []
        for k in counts:
            k = min(k, len(self.order))
            picked: list[tuple[int, int]] = []
            i = 0
            while len(picked) < k and i < len(buf):
                if any(u == buf[i][1] for _, u in picked):
                    i += 1
                else:
                    picked.append(buf.pop(i))
            out.append(picked)
        return out


def rotation(db: Session, cfg: Cfg) -> Rotation:
    """按当前名单建汇报顺序：停用的成员不参与；原来的「下一位」被停用了，就顺延到名单里 TA 后面第一个能参与的人。"""
    active = set(db.scalars(select(User.id).where(User.id.in_(cfg.roster), User.enabled))) if cfg.roster else set()
    order = [u for u in cfg.roster if u in active]
    nxt = cfg.next_uid
    if nxt not in active and nxt in cfg.roster:
        i = cfg.roster.index(nxt)
        nxt = next((u for u in cfg.roster[i + 1:] + cfg.roster[:i] if u in active), None)
    return Rotation(order, cfg.head, nxt)


def _save_rotation(db: Session, rot: Rotation, cfg: Cfg):
    set_setting(db, "gm_head", json.dumps(rot.head))
    if rot.order:  # 名单里暂时没有能参与的人时保留原来的「下一位」
        set_setting(db, "gm_next", str(rot.next_uid))
    elif cfg.next_uid not in cfg.roster:
        set_setting(db, "gm_next", "")


# ---------------- 每周的组会 ----------------


def week_of(d: date) -> date:
    return d - timedelta(days=d.weekday())


def default_start(cfg: Cfg, week: date) -> datetime:
    return datetime.combine(week + timedelta(days=cfg.weekday), cfg.time)


def start_of(cfg: Cfg, week: date, row: GroupMeeting | None) -> datetime:
    return row.start_at if row and row.start_at else default_start(cfg, week)


def _over_at(start: datetime) -> datetime:
    """组会当天结束后才算开过：当天还能改这次的汇报人（比如有人临时来不了）。"""
    return datetime.combine(start.date() + timedelta(days=1), time())


def settled_week(db: Session, at: datetime) -> date:
    v = _get(db, "gm_settled_week")
    if v:
        return date.fromisoformat(v)
    w = week_of(at.date()) - WEEK  # 第一次运行：从本周开始
    set_setting(db, "gm_settled_week", w.isoformat())
    return w


def _row(db: Session, week: date) -> GroupMeeting:
    row = db.scalar(select(GroupMeeting).where(GroupMeeting.week == week))
    if not row:
        row = GroupMeeting(week=week, cancelled=False, note="", settled=False, notify_error="")
        db.add(row)
    return row


@dataclass
class Meeting:
    week: date
    row: GroupMeeting | None
    start: datetime
    location: str
    cancelled: bool
    note: str
    presenters: list[tuple[int, int]] = field(default_factory=list)  # [(在汇报顺序里的位置, user_id)]

    @property
    def uids(self) -> list[int]:
        return [u for _, u in self.presenters]


def upcoming(db: Session, cfg: Cfg, at: datetime, weeks: int = HORIZON) -> list[Meeting]:
    """还没过去的组会（从最早一个没结束的那周起），并排好汇报人。多算几周，最后一次的「下次汇报」也有人。"""
    first = settled_week(db, at) + WEEK
    total = weeks + 4
    rows = {r.week: r for r in db.scalars(
        select(GroupMeeting).where(GroupMeeting.week >= first, GroupMeeting.week < first + total * WEEK))}
    items = []
    for n in range(total):
        w = first + n * WEEK
        row = rows.get(w)
        items.append(Meeting(
            week=w, row=row, start=start_of(cfg, w, row),
            location=(row.location if row and row.location else cfg.default_location),
            cancelled=bool(row and row.cancelled), note=(row.note if row else "") or "",
        ))
    picks = rotation(db, cfg).schedule([0 if m.cancelled else cfg.per_meeting for m in items])
    for m, p in zip(items, picks):
        m.presenters = p
    return items


def settle(db: Session, at: datetime | None = None) -> int:
    """处理已经结束的周：开过的组会记下时间、地点和汇报人，并从汇报顺序里取走这些人。返回处理了几周。"""
    at = at or now()
    with lock:
        cfg = load(db)
        w = settled_week(db, at) + WEEK
        rot = None
        n = 0
        while True:
            row = db.scalar(select(GroupMeeting).where(GroupMeeting.week == w))
            start = start_of(cfg, w, row)
            if at < _over_at(start):
                break
            held = cfg.enabled and not (cfg.since and start < cfg.since) and not (row and row.cancelled)
            if held:
                rot = rot or rotation(db, cfg)
                row = row or _row(db, w)
                row.start_at = start
                row.location = row.location or cfg.default_location
                row.presenters_json = json.dumps(rot.take(cfg.per_meeting))
            if row:
                row.settled = True
            set_setting(db, "gm_settled_week", w.isoformat())
            w += WEEK
            n += 1
        if rot:
            _save_rotation(db, rot, cfg)
        db.commit()
        return n


# ---------------- 飞书通知卡片 ----------------


def _day(dt: datetime) -> str:
    return f"{dt.month}月{dt.day}日（周{WEEKDAY_CN[dt.weekday()]}）"


def _who(users: dict[int, User], uids: list[int]) -> str:
    if not uids:
        return "待定"
    parts = []
    for uid in uids:
        u = users.get(uid)
        if not u:
            continue
        # 飞书用户的 open_id 以 ou_ 开头，用 <at> 显示成可点的 @姓名；本地开发的假账号只写名字
        parts.append(f"<at id={u.open_id}></at>" if u.open_id.startswith("ou_") else u.name)
    return "、".join(parts) or "待定"


def _md(text: str) -> dict:
    return {"tag": "div", "text": {"tag": "lark_md", "content": text}}


def _fields(pairs: list[tuple[str, str]]) -> dict:
    return {"tag": "div", "fields": [
        {"is_short": True, "text": {"tag": "lark_md", "content": f"**{k}**\n{v}"}} for k, v in pairs]}


def build_card(cfg: Cfg, items: list[Meeting], i: int, users: dict[int, User], at_all: bool = True) -> tuple[dict, int]:
    """items[i] 这次组会的通知卡片。返回 (卡片, 附加说明从第几个元素开始)。"""
    m = items[i]
    nxt = next((x for x in items[i + 1:] if not x.cancelled), None)
    mention = "<at id=all></at> " if at_all else ""
    hm = lambda dt: dt.strftime("%H:%M")  # noqa: E731
    if m.cancelled:
        title, template = f"⏸ 组会暂停 · {_day(m.start)}", "grey"
        text = f"{mention}原定 **{_day(m.start)} {hm(m.start)}** 的组会暂停一次。"
        if m.note:
            text += f"\n原因：{m.note}"
        elements = [_md(text)]
        if nxt:
            elements.append(_fields([
                ("🗓 下次组会", f"{_day(nxt.start)} {hm(nxt.start)}\n{nxt.location or '地点待定'}"),
                ("📊 下次汇报", _who(users, nxt.uids)),
            ]))
    else:
        title, template = f"📅 组会通知 · {_day(m.start)}", "blue"
        if nxt and nxt.week - m.week == WEEK:
            nxt_label, nxt_text = "下周汇报", _who(users, nxt.uids)
        elif nxt:
            nxt_label, nxt_text = "下次汇报", f"{_who(users, nxt.uids)}（{nxt.start.month}月{nxt.start.day}日）"
        else:
            nxt_label, nxt_text = "下周汇报", "待定"
        elements = [
            _md(f"{mention}本周组会安排如下，请汇报的同学做好准备。"),
            _fields([("🕑 时间", f"{_day(m.start)} {hm(m.start)}"), ("📍 地点", m.location or "待定")]),
            _fields([("📊 本周汇报", _who(users, m.uids)), (f"⏭ {nxt_label}", nxt_text)]),
        ]
        if m.note:
            elements.append(_md(f"📝 {m.note}"))
    extra_at = len(elements)
    if cfg.card_extra.strip():
        elements += [{"tag": "hr"}, _md(cfg.card_extra.strip())]
    card = {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": title}, "template": template},
        "elements": elements,
    }
    return card, extra_at


def _users(db: Session, ids) -> dict[int, User]:
    ids = set(ids)
    return {u.id: u for u in db.scalars(select(User).where(User.id.in_(ids)))} if ids else {}


def _card_for(db: Session, cfg: Cfg, items: list[Meeting], i: int, at_all: bool = True) -> tuple[dict, int]:
    return build_card(cfg, items, i, _users(db, {u for m in items for u in m.uids}), at_all=at_all)


def _send_to_chats(db: Session, cfg: Cfg, items: list[Meeting], i: int) -> list[str]:
    """发到所有设置的群，返回失败的群和原因。"""
    card, _ = _card_for(db, cfg, items, i)
    errors = []
    for c in cfg.chats:
        try:
            feishu.send_chat_card(c["id"], card)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{c.get('name') or c['id']}：{e}")
    return errors


def _tell_admins(db: Session, m: Meeting, errors: list[str]):
    text = f"{_day(m.start)} 的组会通知卡片没有发出去：\n" + "\n".join(errors) + "\n可以在平台「管理 → 组会」里修正后点「立即发到群里」。"
    for u in db.scalars(select(User).where(User.is_admin, User.enabled)):
        feishu.send_text(u.open_id, text)


def notify_due(db: Session, at: datetime | None = None) -> int:
    """到了提醒时间（组会开始前 N 天的同一时刻）还没发过的，发到群里。返回发了几次。"""
    at = at or now()
    with lock:
        cfg = load(db)
        if not cfg.enabled or not cfg.chats:
            return 0
        items = upcoming(db, cfg, at, weeks=2)
        sent = 0
        for i, m in enumerate(items[:2]):
            if m.row and m.row.notified_at:
                continue
            notify_at = m.start - timedelta(days=cfg.notify_days)
            # 打开组会之前就该发的不补发（免得刚开始配置就发到群里）；组会已经开始了也不再发
            if not notify_at <= at < m.start or (cfg.since and notify_at < cfg.since):
                continue
            errors = _send_to_chats(db, cfg, items, i)
            row = m.row or _row(db, m.week)
            row.notified_at, row.notify_error = now(), "\n".join(errors)
            db.commit()
            sent += 1
            if errors:
                log.warning("组会通知发送失败：%s", errors)
                _tell_admins(db, m, errors)
        return sent


def send_now(db: Session, week: date, me: User | None = None) -> list[str]:
    """手动发送某一周的通知卡片：me 为空发到群里（记为已提醒），否则只私聊发给 me 预览（不 @所有人）。
    返回发送失败的群；全部失败时抛出 ValueError。"""
    at = now()
    with lock:
        cfg = load(db)
        items = upcoming(db, cfg, at)
        i = next((n for n, m in enumerate(items[:HORIZON]) if m.week == week), None)
        if i is None:
            raise ValueError("只能发送近期还没开的组会")
        if me:
            card, _ = _card_for(db, cfg, items, i, at_all=False)
            feishu.send_card(me.open_id, card, raise_errors=True)
            return []
        if not cfg.chats:
            raise ValueError("还没有设置要通知的飞书群")
        errors = _send_to_chats(db, cfg, items, i)
        if len(errors) == len(cfg.chats):
            raise ValueError("发送失败：\n" + "\n".join(errors))
        row = items[i].row or _row(db, week)
        row.notified_at, row.notify_error = now(), "\n".join(errors)
        db.commit()
        return errors


# ---------------- 管理页的修改 ----------------

_chat_info_tried: set[str] = set()


def fill_chat_info(db: Session):
    """缺群名或群头像的群（手动填的 chat_id、旧版本保存的群）向飞书查一次补上并保存。
    每个群每次启动最多查一次：查不到（机器人不在群里、没权限）也不会每次打开页面都去查。"""
    if not settings.feishu_enabled:
        return
    with lock:
        cfg = load(db)
        todo = [c for c in cfg.chats if not (c.get("name") and c.get("avatar")) and c["id"] not in _chat_info_tried]
        if not todo:
            return
        for c in todo:
            _chat_info_tried.add(c["id"])
            try:
                info = feishu.get_chat(c["id"])
            except Exception as e:  # noqa: BLE001
                log.info("查询群信息失败 %s: %s", c["id"], e)
                continue
            c["name"] = c.get("name") or info["name"]
            c["avatar"] = c.get("avatar") or info["avatar"]
        set_setting(db, "gm_chats", json.dumps(cfg.chats, ensure_ascii=False))
        db.commit()


def save_config(db: Session, *, weekday: int, tm: time, per_meeting: int, notify_days: int,
                locations: list[str], chats: list[dict], card_extra: str, at: datetime | None = None):
    at = at or now()
    with lock:
        cfg = load(db)
        # 组会进行中改了每周的时间：最近这次如果已经开过（当天还没结束），或者按新时间算已经过去了，
        # 这次保持原时间，新时间从下次起生效（暂停 / 还没启用时直接按新时间）
        first = settled_week(db, at) + WEEK
        row = db.scalar(select(GroupMeeting).where(GroupMeeting.week == first))
        old_start = start_of(cfg, first, row)
        new_start = datetime.combine(first + timedelta(days=weekday), tm)
        if cfg.enabled and not (row and row.start_at) and old_start != new_start and min(old_start, new_start) <= at:
            _row(db, first).start_at = old_start
        set_setting(db, "gm_weekday", str(weekday))
        set_setting(db, "gm_time", tm.strftime("%H:%M"))
        set_setting(db, "gm_per_meeting", str(per_meeting))
        set_setting(db, "gm_notify_days", str(notify_days))
        set_setting(db, "gm_locations", json.dumps(locations, ensure_ascii=False))
        set_setting(db, "gm_chats", json.dumps(chats, ensure_ascii=False))
        set_setting(db, "gm_card_extra", card_extra)
        db.commit()


def set_enabled(db: Session, on: bool):
    with lock:
        if on and _get(db, "gm_enabled") != "1":
            set_setting(db, "gm_since", now().isoformat())
        set_setting(db, "gm_enabled", "1" if on else "0")
        db.commit()


def save_roster(db: Session, user_ids: list[int]):
    """保存名单顺序。原来的「下一位」被移出名单时，顺延到原名单里 TA 后面第一个还在名单里的人。"""
    with lock:
        cfg = load(db)
        new = list(dict.fromkeys(user_ids))
        nxt = cfg.next_uid
        if nxt not in new:
            old = cfg.roster
            i = old.index(nxt) if nxt in old else None
            nxt = next((u for u in old[i + 1:] + old[:i] if u in new), None) if i is not None else None
        if nxt is None and new:
            nxt = new[0]
        set_setting(db, "gm_roster", json.dumps(new))
        set_setting(db, "gm_next", str(nxt or ""))
        cfg = Cfg(**{**cfg.__dict__, "roster": new, "next_uid": nxt, "head": [u for u in cfg.head if u in new]})
        rot = rotation(db, cfg)
        rot.normalize()
        _save_rotation(db, rot, cfg)
        db.commit()


def candidates(db: Session) -> dict:
    """「添加汇报人」可以选的人：设置了飞书群就列群成员（包括还没登录过平台的），否则列平台上的成员。
    已在名单里、已停用的不列。"""
    cfg = load(db)
    in_roster = set(cfg.roster)
    users = list(db.scalars(select(User).order_by(User.id)))
    by_oid = {u.open_id: u for u in users}

    def person(u: User | None, open_id: str, name: str, avatar: str = "") -> dict:
        return {"open_id": open_id, "user_id": u.id if u else None, "name": (u.name if u else name) or "未命名",
                "avatar_url": (u.avatar_url if u else None) or avatar or None,
                "linux_username": u.linux_username if u else None, "logged_in": bool(u and u.logged_in)}

    platform = [person(u, u.open_id, u.name) for u in users if u.enabled and u.id not in in_roster]
    if not cfg.chats:
        return {"source": "users", "members": platform, "chats": [], "errors": []}
    if not settings.feishu_enabled:
        return {"source": "users", "members": platform, "chats": [], "errors": ["服务端未配置飞书应用，只能从平台成员里选"]}
    members: dict[str, str] = {}
    errors = []
    for c in cfg.chats:
        try:
            for m in feishu.list_chat_members(c["id"]):
                members.setdefault(m["open_id"], m["name"])
        except Exception as e:  # noqa: BLE001
            errors.append(f"读取「{c.get('name') or c['id']}」的成员失败：{e}")
    if not members:  # 一个群都读不到：退回平台成员，并说明原因
        return {"source": "users", "members": platform, "chats": [], "errors": errors}
    new = [oid for oid in members if oid not in by_oid]
    avatars = {}
    if new:
        try:
            avatars = feishu.user_avatars(new)
        except Exception as e:  # noqa: BLE001  拿不到头像就显示姓名首字
            log.info("读取成员头像失败：%s", e)
    out = []
    for oid, name in members.items():
        u = by_oid.get(oid)
        if u and (u.id in in_roster or not u.enabled):
            continue
        out.append(person(u, oid, name, avatars.get(oid, "")))
    return {"source": "chats", "members": out, "chats": [c.get("name") or c["id"] for c in cfg.chats], "errors": errors}


def add_members(db: Session, members: list[dict]) -> list[int]:
    """把飞书群成员加到名单末尾。还没登录过平台的，按 open_id 建一条成员记录（不建服务器账号；以后本人登录就沿用这条）。"""
    ids = []
    for m in members:
        u = db.scalar(select(User).where(User.open_id == m["open_id"]))
        if not u:
            u = User(open_id=m["open_id"], name=m["name"] or "未命名", avatar_url=m.get("avatar_url") or None,
                     logged_in=False, enabled=True, is_admin=False, onboarded=False)
            db.add(u)
            db.flush()
        elif not u.enabled:
            continue
        ids.append(u.id)
    db.commit()
    save_roster(db, load(db).roster + ids)
    return ids


def set_next(db: Session, uid: int):
    """从 uid 开始按名单顺序轮换，清掉临时调整。"""
    with lock:
        if uid not in load(db).roster:
            raise ValueError("TA 不在汇报名单里")
        set_setting(db, "gm_next", str(uid))
        set_setting(db, "gm_head", "[]")
        db.commit()


def adjust(db: Session, op: str, i: int, ui: int, j: int | None = None, uj: int | None = None):
    """临时调整：swap 交换顺序里的两个位置；skip 这一位这次不讲。ui / uj 是页面上看到的人，用来确认安排没有变。"""
    with lock:
        cfg = load(db)
        rot = rotation(db, cfg)
        if rot.at(i) != ui or (op == "swap" and (j is None or rot.at(j) != uj)):
            raise ValueError("安排已经变了，请刷新页面后再试")
        if op == "swap":
            rot.swap(i, j)
        else:
            rot.skip(i)
        _save_rotation(db, rot, cfg)
        db.commit()


_UNSET = object()


def patch_week(db: Session, week: date, *, start_at=_UNSET, location=_UNSET, cancelled=None, note=None,
               at: datetime | None = None):
    at = at or now()
    with lock:
        cfg = load(db)
        if week.weekday() != 0:
            raise ValueError("week 应为周一的日期")
        first = settled_week(db, at) + WEEK
        if week < first:
            raise ValueError("这周的组会已经结束，不能再修改")
        if week > first + 52 * WEEK:
            raise ValueError("只能修改一年以内的组会")
        if start_at not in (_UNSET, None) and not week <= start_at.date() < week + WEEK:
            raise ValueError("每周只开一次：时间只能改到这一周之内（周一到周日）")
        row = _row(db, week)
        if start_at is not _UNSET:
            row.start_at = None if start_at is None or start_at == default_start(cfg, week) else start_at.replace(second=0, microsecond=0)
        if location is not _UNSET:
            loc = (location or "").strip()
            row.location = None if not loc or loc == cfg.default_location else loc
        if cancelled is not None:
            row.cancelled = cancelled
        if note is not None:
            row.note = note.strip()[:256]
        db.commit()


# ---------------- 管理页展示 ----------------


def _notify_state(cfg: Cfg, m: Meeting, at: datetime) -> str:
    if m.row and m.row.notified_at:
        return "sent"
    if not cfg.enabled:
        return "paused"
    if not cfg.chats:
        return "no_chat"
    notify_at = m.start - timedelta(days=cfg.notify_days)
    if at >= m.start or (cfg.since and notify_at < cfg.since):
        return "missed"
    return "due" if at >= notify_at else "scheduled"


def overview(db: Session, at: datetime | None = None) -> dict:
    at = at or now()
    cfg = load(db)
    items = upcoming(db, cfg, at)
    history = db.scalars(
        select(GroupMeeting)
        .where(GroupMeeting.settled, or_(GroupMeeting.presenters_json.is_not(None), GroupMeeting.cancelled))
        .order_by(GroupMeeting.week.desc()).limit(20)
    ).all()
    users = _users(db, set(cfg.roster) | {u for m in items for u in m.uids} | {u for r in history for u in r.presenters})
    rot = rotation(db, cfg)

    def person(uid: int, pos: int | None = None) -> dict:
        u = users.get(uid)
        d = {"id": uid, "name": u.name if u else f"#{uid}", "avatar_url": u.avatar_url if u else None}
        if pos is not None:
            d["pos"] = pos
        return d

    def meeting(m: Meeting) -> dict:
        return {
            "week": m.week.isoformat(),
            "start_at": m.start.isoformat(),
            "default_start": default_start(cfg, m.week).isoformat(),
            "custom_time": bool(m.row and m.row.start_at),
            "location": m.location,
            "cancelled": m.cancelled,
            "note": m.note,
            "presenters": [person(u, pos) for pos, u in m.presenters],
            "notify_at": (m.start - timedelta(days=cfg.notify_days)).isoformat(),
            "notified_at": m.row.notified_at.isoformat() if m.row and m.row.notified_at else None,
            "notify_error": (m.row.notify_error if m.row else "") or "",
            "notify_state": _notify_state(cfg, m, at),
        }

    preview = None
    if items:
        card, extra_at = _card_for(db, cfg, items, 0)
        preview = {"week": items[0].week.isoformat(), "card": card, "extra_at": extra_at,
                   "names": {u.open_id: u.name for u in users.values()}}
    return {
        "config": {
            "enabled": cfg.enabled, "weekday": cfg.weekday, "time": cfg.time.strftime("%H:%M"),
            "per_meeting": cfg.per_meeting, "notify_days": cfg.notify_days, "locations": cfg.locations,
            "chats": cfg.chats, "card_extra": cfg.card_extra,
        },
        "roster": [{**person(uid), "enabled": bool(users.get(uid) and users[uid].enabled)} for uid in cfg.roster],
        "next_uid": rot.next_uid,
        "adjusted": len(rot.head),
        "upcoming": [meeting(m) for m in items[:HORIZON]],
        "history": [{
            "week": r.week.isoformat(),
            "start_at": (r.start_at or default_start(cfg, r.week)).isoformat(),
            "location": r.location or "",
            "cancelled": r.cancelled,
            "note": r.note or "",
            "presenters": [person(u) for u in r.presenters],
        } for r in history],
        "preview": preview,
    }
