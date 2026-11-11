"""组会周报：发组会通知卡片之前，在飞书知识库的「周报」页面下复制模板，建好这周的周报（这周已经有了就直接用），
卡片上附上链接，提醒大家在组会开始前填写。每次发完卡片再整理一下「周报」：
- 早于最近 N 周（含本周）的周报移到「历史周报」下面；
- 模板排在最前，接着是周报（按日期从新到旧），其他页面（如「历史周报」）排在最后。

周报按标题里的日期认（如「周报 - 2026/11/13」，月日补不补 0 都行），每周只建一篇。

飞书知识库的接口不能指定页面放在哪个位置：新建或移进来的页面都排在最后。所以排序时只能把页面「移出再移回来」，
让它排到最后。为了少挪，开头已经排对的部分不动，只依次挪后面的；第一次挪完先确认它确实排到了最后，
不是的话（比如飞书改了规则）就停下报错，免得越挪越乱。
"""

import re
import threading
from dataclasses import dataclass
from datetime import date, timedelta

from . import feishu

# 同一时间只有一个任务在改「周报」目录（定时发卡片和管理页手动操作可能同时进来）
lock = threading.Lock()

WEEK = timedelta(days=7)
_URL = re.compile(r"^https://([A-Za-z0-9.-]+)/wiki/([A-Za-z0-9]{8,64})(?:[/?#]\S*)?$")
_DATE = re.compile(r"(20\d\d)\s*[/.\-年]\s*(\d{1,2})\s*[/.\-月]\s*(\d{1,2})")


def normalize_url(text: str) -> str:
    """知识库页面链接 → https://域名/wiki/token（去掉 ?fromScene= 之类的参数）。不是知识库页面的链接时抛 ValueError。"""
    m = _URL.match(text.strip())
    if not m:
        raise ValueError("请填飞书知识库页面的链接（在飞书里打开页面后复制地址栏，形如 https://xxx.feishu.cn/wiki/xxxx）")
    return f"https://{m[1]}/wiki/{m[2]}"


def _token(url: str) -> str:
    return _URL.match(url)[2]


def title_for(d: date) -> str:
    return f"周报 - {d.year}/{d.month}/{d.day}"


def title_date(title: str) -> date | None:
    m = _DATE.search(title or "")
    if not m:
        return None
    try:
        return date(int(m[1]), int(m[2]), int(m[3]))
    except ValueError:
        return None


def week_of(d: date) -> date:
    return d - timedelta(days=d.weekday())


@dataclass
class Dirs:
    host: str
    template: dict
    parent: dict
    archive: dict | None

    def url(self, node: dict) -> str:
        return f"https://{self.host}/wiki/{node['node_token']}"


PAGES = (("template", "周报模板"), ("parent", "周报目录"), ("archive", "历史周报"))


def _resolve(cfg) -> Dirs:
    if not (cfg.report_template and cfg.report_parent):
        raise ValueError("还没有设置周报模板和周报目录的链接")
    nodes = {}
    for key, label in PAGES:
        url = getattr(cfg, f"report_{key}")
        if url:
            try:
                nodes[key] = feishu.wiki_node(_token(url))
            except feishu.FeishuError as e:
                raise feishu.FeishuError(f"读取「{label}」失败：{e}") from None
    return Dirs(_URL.match(cfg.report_parent)[1], nodes["template"], nodes["parent"], nodes.get("archive"))


@dataclass
class Report:
    url: str
    title: str
    created: bool


def ensure(cfg, day: date) -> Report:
    """day 那天的组会的周报：「周报」下已经有同一周的就用它，没有就复制模板新建一篇。"""
    with lock:
        d = _resolve(cfg)
        skip = {d.template["node_token"], d.archive["node_token"] if d.archive else ""}
        for k in feishu.wiki_children(d.parent["space_id"], d.parent["node_token"]):
            kd = title_date(k.get("title") or "")
            if k["node_token"] not in skip and kd and week_of(kd) == week_of(day):
                return Report(d.url(k), k.get("title") or "", False)
        title = title_for(day)
        node = feishu.wiki_copy(d.template["space_id"], d.template["node_token"],
                                d.parent["space_id"], d.parent["node_token"], title)
        return Report(d.url(node), title, True)


def tidy(cfg, day: date) -> dict:
    """整理「周报」：早于最近 report_keep 周（day 那周算最近一周）的周报移到「历史周报」，剩下的排好顺序。
    返回 {"archived": [移走的周报标题], "moved": 为了排序挪了几个页面}。"""
    with lock:
        d = _resolve(cfg)
        kids = feishu.wiki_children(d.parent["space_id"], d.parent["node_token"])
        tpl = d.template["node_token"]
        arc = d.archive["node_token"] if d.archive else ""
        reports, others = [], []  # reports: (日期, token, 标题)
        for k in kids:
            tok = k["node_token"]
            kd = None if tok in (tpl, arc) else title_date(k.get("title") or "")
            if kd:
                reports.append((kd, tok, k.get("title") or ""))
            elif tok != tpl:
                others.append(tok)
        archived = []
        if d.archive:
            oldest = week_of(day) - (cfg.report_keep - 1) * WEEK
            # 从旧到新依次移过去（历史周报里新移进来的排在最后）
            for kd, tok, title in sorted((r for r in reports if week_of(r[0]) < oldest), key=lambda r: r[0]):
                feishu.wiki_move(d.parent["space_id"], tok, d.archive["space_id"], arc)
                archived.append(title)
            reports = [r for r in reports if week_of(r[0]) >= oldest]
        keep = {tok for _, tok, _ in reports} | set(others) | {tpl}
        cur = [k["node_token"] for k in kids if k["node_token"] in keep]
        want = ([tpl] if tpl in cur else []) + [tok for _, tok, _ in sorted(reports, key=lambda r: r[0], reverse=True)] + others
        return {"archived": archived, "moved": _arrange(d, cur, want)}


def _arrange(d: Dirs, cur: list[str], want: list[str]) -> int:
    """把「周报」下的页面从 cur 的顺序排成 want 的顺序，返回挪了几个页面。
    只能把页面挪到最后：want 开头已经按顺序出现在 cur 里的那一段不动，后面的依次移出再移回来。"""
    if cur == want:
        return 0
    k = 0
    for tok in cur:
        if k < len(want) and tok == want[k]:
            k += 1
    sp, par = d.parent["space_id"], d.parent["node_token"]
    for n, tok in enumerate(want[k:]):
        # 临时放到「周报」下另一个页面里（优先历史周报、模板），再移回来
        tmp = next(x for x in [d.archive["node_token"] if d.archive else "", d.template["node_token"], *want]
                   if x and x != tok and x in want)
        feishu.wiki_move(sp, tok, sp, tmp)
        feishu.wiki_move(sp, tok, sp, par)
        if n == 0 and [x["node_token"] for x in feishu.wiki_children(sp, par)][-1:] != [tok]:
            raise RuntimeError("移回「周报」的页面没有排到最后，无法自动排序，已停止（只挪了一个页面，顺序需要手动调整）")
    if [x["node_token"] for x in feishu.wiki_children(sp, par)] != want:
        raise RuntimeError("排好后「周报」下的顺序和预期不一致（可能有人同时在调整），下次发卡片时会再整理")
    return len(want) - k


def check(cfg) -> dict:
    """管理页的「检查」：这几个页面机器人能不能读到，「周报」下现在有几篇周报。不会改动任何页面。"""
    items, nodes = [], {}
    for key, label in PAGES:
        url = getattr(cfg, f"report_{key}")
        item = {"key": key, "label": label, "ok": False, "title": "", "detail": "", "error": ""}
        items.append(item)
        if not url:
            item["ok"] = key == "archive"
            item["detail" if key == "archive" else "error"] = "没有填写，不移动旧周报" if key == "archive" else "还没有填写"
            continue
        try:
            nodes[key] = feishu.wiki_node(_token(url))
            item.update(ok=True, title=nodes[key].get("title") or "")
        except Exception as e:  # noqa: BLE001
            item["error"] = str(e)
    if "parent" in nodes:
        p, item = nodes["parent"], items[1]
        try:
            kids = feishu.wiki_children(p["space_id"], p["node_token"])
            n = sum(1 for k in kids if title_date(k.get("title") or ""))
            item["detail"] = f"下面有 {n} 篇周报" if n else "下面还没有周报"
        except Exception as e:  # noqa: BLE001
            item.update(ok=False, error=f"读取子页面失败：{e}")
    return {"ok": all(i["ok"] for i in items), "items": items}
