import ipaddress
import json
import logging
import posixpath
import re
import time
from datetime import date, timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from . import browse, feishu, services
from .auth import admin_user, current_user
from .config import now, settings, today
from .db import SessionLocal, get_db
from .models import GPU, MigrationTask, Reservation, Server, SSHKey, User

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api")

_booking_lock = services.booking_lock


def _bg(fn, *args):
    """在后台任务里用独立的数据库会话执行（请求的会话届时已关闭）。"""
    db = SessionLocal()
    try:
        fn(db, *args)
    finally:
        db.close()


# ---------------- 序列化 ----------------


def user_out(u: User, with_keys: bool = False) -> dict:
    d = {
        "id": u.id,
        "name": u.name,
        "avatar_url": u.avatar_url,
        "linux_username": u.linux_username,
        "uid": u.uid,
        "is_admin": u.is_admin,
        "enabled": u.enabled,
        "key_count": len(u.ssh_keys),
        "has_password": bool(u.password_hash),
        "onboarded": u.onboarded,
    }
    if with_keys:
        d["ssh_keys"] = [
            {"id": k.id, "title": k.title, "fingerprint": k.fingerprint, "created_at": k.created_at.isoformat()}
            for k in u.ssh_keys
        ]
    return d


def res_out(r: Reservation, me: User) -> dict:
    return {
        "id": r.id,
        "gpu_id": r.gpu_id,
        "server_id": r.gpu.server_id,
        "server_name": r.gpu.server.name,
        "gpu_index": r.gpu.index,
        "user_id": r.user_id,
        "user_name": r.user.name,
        "user_avatar": r.user.avatar_url,
        "linux_username": r.user.linux_username,
        "start_date": r.start_date.isoformat(),
        "end_date": r.end_date.isoformat(),
        "note": r.note,
        "auto_renew": r.auto_renew,
        "renew_count": r.renew_count,
        "editable": me.is_admin or r.user_id == me.id,
    }


def server_out(s: Server, holders: dict[int, set[str]] | None = None, admin: bool = False) -> dict:
    d = {
        "id": s.id,
        "name": s.name,
        "host": s.host,
        "ssh_host": s.ssh_host or "",  # 成员 SSH 登录地址，空 = 用 host
        "online": s.online,
        "last_seen_at": s.last_seen_at.isoformat() if s.last_seen_at else None,
        # 整机资源（CPU 利用率 %、内存 / 磁盘用量 MiB），离线时不显示
        "system": s.sysinfo if s.online else None,
        "gpus": [
            {
                "id": g.id,
                "index": g.index,
                "name": g.name,
                "memory_total": g.memory_total,
                "memory_used": g.memory_used,
                "utilization": g.utilization,
                "updated_at": g.updated_at.isoformat() if g.updated_at else None,
                "processes": g.processes,
                "alerts": services.gpu_alerts(g, holders.get(g.id, set())) if holders is not None and s.online else [],
            }
            for g in s.gpus
        ],
    }
    if admin:
        d.update(hostname=s.hostname, agent_version=s.agent_version, token=s.token, link=s.link,
                 legacy_users=[l.get("user") for l in s.legacy if l.get("user")])
    return d


# ---------------- 公共 / 个人 ----------------


@router.get("/config")
def public_config(db: Session = Depends(get_db)):
    return {
        "auto_renew_before_hours": services.get_int_setting(db, "auto_renew_before_hours"),
        "auto_renew_days": services.get_int_setting(db, "auto_renew_days"),
        "lab_name": services.get_setting(db, "lab_name"),
        "dev_login": settings.dev_login,
        "feishu_enabled": settings.feishu_enabled,
        "max_reservation_days": services.get_int_setting(db, "max_reservation_days"),
        "max_advance_days": services.get_int_setting(db, "max_advance_days"),
        "max_gpu_sharers": services.get_int_setting(db, "max_gpu_sharers"),
        "refresh_interval": services.get_int_setting(db, "refresh_interval_seconds"),
        "require_reservation_to_login": services.get_setting(db, "require_reservation_to_login") == "1",
        "today": today().isoformat(),
    }


@router.get("/me")
def me(user: User = Depends(current_user)):
    return user_out(user, with_keys=True)


class UsernameIn(BaseModel):
    username: str


def _change_username(db: Session, user: User, raw: str):
    """设置 / 修改 Linux 用户名。各服务器上的 Agent 按 uid 识别账号，会把原账号改名并移动家目录。"""
    try:
        name = services.validate_linux_username(raw)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if name == user.linux_username:
        return
    other = db.scalar(select(User).where(User.linux_username == name))
    if other and other.id != user.id:
        raise HTTPException(400, "该用户名已被占用")
    legacy_names = {l.get("user") for s in db.scalars(select(Server)) for l in s.legacy}
    if name in legacy_names or name.startswith("ubuntu"):
        raise HTTPException(400, "该用户名是服务器上的旧共享账号，请换一个")
    busy = db.scalar(
        select(MigrationTask).where(MigrationTask.user_id == user.id, MigrationTask.status.in_(MigrationTask.ACTIVE))
    )
    if busy:
        raise HTTPException(400, "你有文件迁移正在进行，完成后再修改用户名")
    user.linux_username = name
    db.commit()


@router.put("/me/linux-username")
def set_linux_username(body: UsernameIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    _change_username(db, user, body.username)
    return user_out(user, with_keys=True)


class PasswordIn(BaseModel):
    password: str = Field(max_length=128)


@router.put("/me/password")
def set_password(body: PasswordIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """服务器登录密码：平台只保存 sha512-crypt 哈希，由 Agent 写入各服务器的 /etc/shadow。"""
    if not user.linux_username:
        raise HTTPException(400, "请先设置 Linux 用户名")
    try:
        user.password_hash = services.hash_password(body.password, user.linux_username)
    except ValueError as e:
        raise HTTPException(400, str(e))
    user.password_updated_at = now()
    db.commit()
    return user_out(user, with_keys=True)


@router.delete("/me/password")
def clear_password(user: User = Depends(current_user), db: Session = Depends(get_db)):
    if not user.ssh_keys:
        raise HTTPException(400, "你还没有添加 SSH 公钥，关闭密码登录后将无法登录服务器。请先添加公钥")
    user.password_hash = None
    user.password_updated_at = now()
    db.commit()
    return user_out(user, with_keys=True)


@router.post("/me/onboarded")
def finish_onboarding(user: User = Depends(current_user), db: Session = Depends(get_db)):
    user.onboarded = True
    db.commit()
    return user_out(user, with_keys=True)


@router.get("/me/servers")
def my_servers(user: User = Depends(current_user), db: Session = Depends(get_db)):
    """我在每台服务器上的账号同步状态，以及可以迁移的已有目录。"""
    servers = db.scalars(select(Server).order_by(Server.name)).all()
    claims = {
        (t.server_id, t.source): t
        for t in db.scalars(
            select(MigrationTask).where(
                MigrationTask.kind == "dir",
                MigrationTask.mode.in_(("move", "link")),
                MigrationTask.status.in_(MigrationTask.ACTIVE + ("done",)),
            )
        )
    }
    out = []
    for s in servers:
        acct = s.accounts.get(str(user.id))
        cands = []
        for c in s.candidates:
            t = claims.get((s.id, c.get("path")))
            cands.append({**c, "claimed_by": t.user.name if t and t.user_id != user.id else None, "mine": bool(t and t.user_id == user.id)})
        out.append(
            {
                "id": s.id,
                "name": s.name,
                "host": s.host,
                "ssh_host": s.ssh_host or "",
                "online": s.online,
                "home_free": s.home_free,
                # 迁移可以放到的数据盘（剩余空间，字节）；Agent 太旧时为空，并提示更新
                "data_disks": [{"mount": d["mount"], "free": max(0, d["total"] - d["used"]) * 1024 * 1024}
                               for d in s.data_disks] if s.supports_data_disk else [],
                "data_disks_need_agent": [d["mount"] for d in s.data_disks] if not s.supports_data_disk else [],
                "account": acct,
                "candidates": cands,
                "legacy": s.legacy,
                "browse_roots": s.browse_roots,
            }
        )
    return out


class KeyIn(BaseModel):
    title: str = Field(default="", max_length=64)
    public_key: str


@router.post("/me/keys")
def add_key(body: KeyIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    try:
        key, fp = services.parse_ssh_key(body.public_key)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if db.scalar(select(SSHKey).where(SSHKey.fingerprint == fp)):
        raise HTTPException(400, "这个公钥已经被添加过了（每个公钥只能属于一个人）")
    if len(user.ssh_keys) >= 10:
        raise HTTPException(400, "最多添加 10 个公钥")
    title = body.title.strip() or (key.split(" ", 2)[2] if key.count(" ") >= 2 else "未命名")
    db.add(SSHKey(user_id=user.id, title=title[:64], public_key=key, fingerprint=fp))
    db.commit()
    db.refresh(user)
    return user_out(user, with_keys=True)


@router.delete("/me/keys/{key_id}")
def delete_key(key_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    key = db.get(SSHKey, key_id)
    if not key or key.user_id != user.id:
        raise HTTPException(404)
    db.delete(key)
    db.commit()
    db.refresh(user)
    return user_out(user, with_keys=True)


# ---------------- 日历 & 预约 ----------------


@router.get("/calendar")
def calendar(start: date | None = None, days: int = 14, user: User = Depends(current_user), db: Session = Depends(get_db)):
    days = max(1, min(days, 366))
    start = start or today()
    end = start + timedelta(days=days - 1)
    servers = db.scalars(select(Server).options(joinedload(Server.gpus)).order_by(Server.name)).unique().all()
    d = today()
    holders = services.today_holders(db)
    return {
        "start": start.isoformat(),
        "days": days,
        "today": d.isoformat(),
        "servers": [server_out(s, holders) for s in servers],
        "reservations": [res_out(r, user) for r in services.overlapping(db, start, end)],
    }


@router.get("/me/reservations")
def my_reservations(user: User = Depends(current_user), db: Session = Depends(get_db)):
    d = today()
    rs = [r for r in services.overlapping(db, d, d + timedelta(days=3650)) if r.user_id == user.id]
    rs.sort(key=lambda r: (r.start_date, r.gpu.server.name, r.gpu.index))
    return [res_out(r, user) for r in rs]


def _check_rules(db: Session, user: User, start: date, end: date, is_new_start: bool):
    if end < start:
        raise HTTPException(400, "结束日期不能早于开始日期")
    if end < today():
        raise HTTPException(400, "结束日期不能早于今天")
    if user.is_admin:
        return
    if is_new_start and start < today():
        raise HTTPException(400, "不能预约过去的日期")
    # 已经开始的预约按“从今天起还剩几天”计算：长期使用的自动延长会把总时长延到上限以上，
    # 不能因此改个备注、改为临时使用，或在到期前继续延长，就被“最长 N 天”拦住
    max_days = services.get_int_setting(db, "max_reservation_days")
    max_advance = services.get_int_setting(db, "max_advance_days")
    if (end - max(start, today())).days + 1 > max_days:
        raise HTTPException(400, f"单次预约最长 {max_days} 天，到期前可以续约")
    if start > today() + timedelta(days=max_advance):
        raise HTTPException(400, f"最多提前 {max_advance} 天预约")


def _check_conflicts(db: Session, gpu_ids: list[int], start: date, end: date, user_id: int, exclude_id: int | None = None,
                     allow_self: bool = False):
    try:
        services.check_capacity(db, gpu_ids, start, end, user_id, exclude_id, allow_self)
    except ValueError as e:
        raise HTTPException(409, str(e))


class ReservationIn(BaseModel):
    gpu_ids: list[int] = Field(min_length=1, max_length=16)
    start_date: date | None = None
    end_date: date | None = None
    note: str = Field(default="", max_length=256)
    auto_renew: bool = False  # 长期使用：到期前自动延长（临时使用为 False）
    long_term: bool = False  # 长期使用：不需要日期，从今天起预约一个周期，之后自动延长
    user_id: int | None = None  # 仅管理员可以替别人预约


@router.post("/reservations")
def create_reservation(
    body: ReservationIn, bg: BackgroundTasks, user: User = Depends(current_user), db: Session = Depends(get_db)
):
    owner = user
    if body.user_id and body.user_id != user.id:
        if not user.is_admin:
            raise HTTPException(403, "只能为自己预约")
        owner = db.get(User, body.user_id)
        if not owner:
            raise HTTPException(404, "用户不存在")
    if not owner.linux_username:
        raise HTTPException(400, "请先在「我的账号」里设置 Linux 用户名")
    gpus = db.scalars(select(GPU).where(GPU.id.in_(body.gpu_ids)).options(joinedload(GPU.server))).all()
    if len(gpus) != len(set(body.gpu_ids)):
        raise HTTPException(404, "GPU 不存在")
    if body.long_term:
        with _booking_lock:
            try:
                rs = services.book_long_term(db, owner, list(gpus), body.note.strip())
            except ValueError as e:
                db.rollback()
                raise HTTPException(409, str(e))
            db.commit()
            rs = [db.get(Reservation, i) for i in rs]
        bg.add_task(_bg, services.notify_booked, owner.id, [r.id for r in rs], "预约成功")
        return [res_out(r, user) for r in rs]
    if not body.start_date or not body.end_date:
        raise HTTPException(400, "请选择开始日期和结束日期")
    _check_rules(db, user, body.start_date, body.end_date, is_new_start=True)
    with _booking_lock:
        _check_conflicts(db, body.gpu_ids, body.start_date, body.end_date, owner.id)
        rs = [
            Reservation(
                user_id=owner.id, gpu_id=g.id, start_date=body.start_date, end_date=body.end_date,
                note=body.note.strip(), auto_renew=body.auto_renew,
            )
            for g in gpus
        ]
        db.add_all(rs)
        db.flush()
        # 和自己已有的预约首尾相接时合并成一条（底层数据也合并，不只是日历上显示）
        merged = {}
        for g in gpus:
            merged.update(services.merge_reservations(db, owner.id, g.id))
        db.commit()
        rs = [db.get(Reservation, merged.get(r.id, r.id)) for r in rs]
    bg.add_task(_bg, services.notify_booked, owner.id, [r.id for r in rs], "预约成功")
    return [res_out(r, user) for r in rs]


def _own_reservation(db: Session, rid: int, user: User) -> Reservation:
    r = db.get(Reservation, rid)
    if not r or r.status != "active":
        raise HTTPException(404, "预约不存在")
    if r.user_id != user.id and not user.is_admin:
        raise HTTPException(403, "只能修改自己的预约")
    return r


class ReservationPatch(BaseModel):
    start_date: date | None = None
    end_date: date | None = None
    note: str | None = Field(default=None, max_length=256)
    auto_renew: bool | None = None


@router.patch("/reservations/{rid}")
def update_reservation(rid: int, body: ReservationPatch, user: User = Depends(current_user), db: Session = Depends(get_db)):
    with _booking_lock:
        r = _own_reservation(db, rid, user)
        if r.end_date < today():
            raise HTTPException(400, "已结束的预约不能修改")
        start = body.start_date or r.start_date
        end = body.end_date or r.end_date
        # 已经过去的日期不能改：开始日期在今天之前的预约不能改开始日期；开始日期也不能挪到今天之前。
        # 今天及以后开始的预约可以改开始日期（拖动左边缘）。要把跨过今天的预约拆开，用 /split
        if start != r.start_date and r.start_date < today():
            raise HTTPException(400, "预约已经开始，不能修改开始日期（已过去的部分不能改）")
        if start != r.start_date and start < today():
            raise HTTPException(400, "不能把开始日期改到今天之前")
        _check_rules(db, user, start, end, is_new_start=start != r.start_date)
        # 和自己在这张卡上别的预约重叠 / 相邻：保存后合并成一条（见下面的 merge_reservations），合并后的整段也要满足长度限制
        span_start, span_end = start, end
        for o in services.overlapping(db, start - timedelta(days=1), end + timedelta(days=1), gpu_ids=[r.gpu_id]):
            if o.id != r.id and o.user_id == r.user_id:
                span_start, span_end = min(span_start, o.start_date), max(span_end, o.end_date)
        if (span_start, span_end) != (start, end):
            _check_rules(db, user, span_start, span_end, is_new_start=False)
        _check_conflicts(db, [r.gpu_id], start, end, r.user_id, exclude_id=r.id, allow_self=True)
        r.start_date, r.end_date = start, end
        if body.note is not None:
            r.note = body.note.strip()
        if body.auto_renew is not None:
            r.auto_renew = body.auto_renew
        db.flush()
        rid_after = services.merge_reservations(db, r.user_id, r.gpu_id).get(r.id, r.id)
        db.commit()
        r = db.get(Reservation, rid_after)
    return res_out(r, user)


class ReservationSplit(BaseModel):
    from_date: date  # 拆分后后半段的开始日期


@router.post("/reservations/{rid}/split")
def split_reservation(rid: int, body: ReservationSplit, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """把跨过今天的预约拆成两条：今天之前的部分保留为已结束的历史（原记录），从 from_date 起另存一条新的，
    中间（今天 ~ from_date 前一天）释放掉。用来在日历上拖动“今天”这一条边界，缩短后半段的开始日期。"""
    with _booking_lock:
        r = _own_reservation(db, rid, user)
        t = today()
        if not (r.start_date < t <= r.end_date):
            raise HTTPException(400, "只有已经开始、且还没结束的预约才能这样拆分")
        if not (t < body.from_date <= r.end_date):
            raise HTTPException(400, "后半段的开始日期要在今天之后、结束日期之内")
        new = Reservation(
            user_id=r.user_id, gpu_id=r.gpu_id, start_date=body.from_date, end_date=r.end_date,
            note=r.note, auto_renew=r.auto_renew, renew_count=r.renew_count, status="active",
        )
        r.end_date = t - timedelta(days=1)
        r.auto_renew = False  # 已结束的历史部分不再自动延长；长期使用标记留在还在生效的后半段
        db.add(new)
        db.commit()
        db.refresh(new)
    return res_out(new, user)


@router.delete("/reservations/{rid}")
def release_reservation(rid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """取消 / 提前释放：未开始或今天开始的直接取消；已进行多天的截止到昨天，今天起立即释放。"""
    with _booking_lock:
        r = _own_reservation(db, rid, user)
        d = today()
        if r.end_date < d:
            raise HTTPException(400, "已结束的预约不能取消")
        if r.start_date >= d:
            r.status = "cancelled"
        else:
            r.end_date = d - timedelta(days=1)
        db.commit()
    return {"ok": True}


# ---------------- 管理员 ----------------


@router.get("/admin/users")
def admin_users(_: User = Depends(admin_user), db: Session = Depends(get_db)):
    users = db.scalars(select(User).options(joinedload(User.ssh_keys)).order_by(User.id)).unique()
    return [user_out(u) for u in users]


class UserPatch(BaseModel):
    is_admin: bool | None = None
    enabled: bool | None = None
    linux_username: str | None = None


@router.patch("/admin/users/{uid}")
def admin_update_user(uid: int, body: UserPatch, me: User = Depends(admin_user), db: Session = Depends(get_db)):
    u = db.get(User, uid)
    if not u:
        raise HTTPException(404)
    if u.id == me.id and (body.is_admin is False or body.enabled is False):
        raise HTTPException(400, "不能取消自己的管理员身份或停用自己")
    if body.is_admin is not None:
        u.is_admin = body.is_admin
    if body.enabled is not None:
        u.enabled = body.enabled
    db.commit()
    if body.linux_username:
        _change_username(db, u, body.linux_username)
    return user_out(u)


def _tunnel_status() -> dict:
    try:
        with open(settings.tunnel_status_file) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


@router.get("/admin/servers")
def admin_servers(_: User = Depends(admin_user), db: Session = Depends(get_db)):
    servers = db.scalars(select(Server).options(joinedload(Server.gpus)).order_by(Server.name)).unique()
    tunnels = _tunnel_status()
    out = []
    for s in servers:
        d = server_out(s, admin=True)
        # 隧道会连接所有填了内网地址的服务器（列表从数据库动态读取，不用再维护 TUNNEL_TARGETS）
        d["tunnel"] = tunnels.get(s.name) if (s.host or "").strip() else None
        out.append(d)
    return out


@router.get("/admin/tunnel")
def admin_tunnel(_: User = Depends(admin_user)):
    # 配置了校园网接入（有 chisel 口令）即视为启用；GPU 列表由隧道从数据库自动读取
    return {"enabled": bool(settings.chisel_secret), "status": _tunnel_status()}


class ServerIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    host: str = Field(default="", max_length=128)
    ssh_host: str = Field(default="", max_length=128)


SSH_HOST_RE = re.compile(r"^[A-Za-z0-9.:-]*$")


def _ssh_host(body: ServerIn) -> str:
    """成员 SSH 登录地址：只允许 IP / 域名（会拼进成员复制的 ssh 命令里）。"""
    v = body.ssh_host.strip()
    if not SSH_HOST_RE.fullmatch(v):
        raise HTTPException(400, "SSH 登录地址只能填 IP 或域名")
    return v


@router.post("/admin/servers")
def admin_create_server(body: ServerIn, _: User = Depends(admin_user), db: Session = Depends(get_db)):
    if db.scalar(select(Server).where(Server.name == body.name.strip())):
        raise HTTPException(400, "服务器名称已存在")
    token, token_hash = services.new_agent_token()
    s = Server(name=body.name.strip(), host=body.host.strip(), ssh_host=_ssh_host(body), token=token, token_hash=token_hash)
    db.add(s)
    db.commit()
    return {"server": server_out(s, admin=True), "token": token}


@router.patch("/admin/servers/{sid}")
def admin_update_server(sid: int, body: ServerIn, _: User = Depends(admin_user), db: Session = Depends(get_db)):
    s = db.get(Server, sid)
    if not s:
        raise HTTPException(404)
    s.name, s.host, s.ssh_host = body.name.strip(), body.host.strip(), _ssh_host(body)
    db.commit()
    return server_out(s, admin=True)


@router.post("/admin/servers/{sid}/token")
def admin_reset_token(sid: int, _: User = Depends(admin_user), db: Session = Depends(get_db)):
    s = db.get(Server, sid)
    if not s:
        raise HTTPException(404)
    token, s.token_hash = services.new_agent_token()
    s.token = token
    db.commit()
    return {"token": token}


@router.delete("/admin/servers/{sid}")
def admin_delete_server(sid: int, _: User = Depends(admin_user), db: Session = Depends(get_db)):
    s = db.get(Server, sid)
    if not s:
        raise HTTPException(404)
    db.delete(s)
    db.commit()
    return {"ok": True}


SETTING_KEYS = (
    "lab_name", "welcome_title", "welcome_markdown", "welcome_links", "welcome_chat_ids",
    "max_reservation_days", "max_advance_days", "max_gpu_sharers", "reminder_hour",
    "auto_renew_before_hours", "auto_renew_days", "migration_requires_approval", "require_reservation_to_login",
    "refresh_interval_seconds", "restrict_to_admin_tenant", "process_kill_grace_hours",
)
INT_SETTINGS = {
    "max_reservation_days": (1, 365), "max_advance_days": (0, 365), "max_gpu_sharers": (1, 10), "reminder_hour": (0, 23),
    "auto_renew_before_hours": (1, 24 * 14), "auto_renew_days": (1, 60),
    "migration_requires_approval": (0, 1), "require_reservation_to_login": (0, 1), "restrict_to_admin_tenant": (0, 1),
    "refresh_interval_seconds": (3, 600), "process_kill_grace_hours": (0, 24 * 30),
}


@router.get("/admin/settings")
def admin_get_settings(_: User = Depends(admin_user), db: Session = Depends(get_db)):
    return {k: services.get_setting(db, k) for k in SETTING_KEYS}


@router.put("/admin/settings")
def admin_put_settings(body: dict[str, str], _: User = Depends(admin_user), db: Session = Depends(get_db)):
    for k, v in body.items():
        if k in INT_SETTINGS:
            lo, hi = INT_SETTINGS[k]
            if not str(v).strip().isdigit() or not lo <= int(v) <= hi:
                raise HTTPException(400, f"{k} 需为 {lo}-{hi} 之间的整数")
            v = str(int(v))
        if k == "lab_name":
            v = str(v).strip()
            if not v or len(v) > 40:
                raise HTTPException(400, "平台名称不能为空，最长 40 个字")
        if k == "welcome_chat_ids":  # 逗号 / 空白 / 换行分隔都接受，统一存成逗号分隔
            v = ",".join(c for c in re.split(r"[,\s，]+", str(v)) if c)
        if k in SETTING_KEYS:
            services.set_setting(db, k, v)
    db.commit()
    services.sync_runtime(db)
    return {k: services.get_setting(db, k) for k in SETTING_KEYS}


@router.post("/admin/welcome/test")
def admin_test_welcome(me: User = Depends(admin_user), db: Session = Depends(get_db)):
    if not settings.feishu_enabled:
        raise HTTPException(400, "服务端未配置飞书应用")
    try:
        services.send_welcome(db, me.open_id, force=True, raise_errors=True)
    except feishu.FeishuError as e:
        raise HTTPException(400, f"飞书返回错误：{e}")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"发送失败：{e}")
    return {"ok": True}


# ---------------- 文件迁移 ----------------


# “进行中”的迁移已经没人在做：比如完成时正好连不上平台、结果丢了，或 Agent 停了
# 新版 Agent 每次同步都报告自己正在执行哪个迁移，据此判断（留几分钟给 Agent 重启后接着做）；
# 旧版不报告、或 Agent 长时间没同步时，只能按多久没收到进度来猜（conda 自检等步骤中间不上报，所以留得宽一些）
MIGRATION_STALL = timedelta(minutes=30)
MIGRATION_ORPHAN = timedelta(minutes=5)
STALLED_PHASE = "失去联系"


def _stalled(t: MigrationTask) -> bool:
    if t.status != "running" or t.updated_at is None:
        return False
    idle = now() - t.updated_at
    srv = t.server
    agent_fresh = srv.last_seen_at is not None and now() - srv.last_seen_at < MIGRATION_ORPHAN
    if srv.current_migration is None or not agent_fresh:
        return idle > MIGRATION_STALL
    return srv.current_migration != t.id and idle > MIGRATION_ORPHAN


def task_out(t: MigrationTask) -> dict:
    return {
        "id": t.id,
        "server_id": t.server_id,
        "server_name": t.server.name,
        "user_id": t.user_id,
        "user_name": t.user.name,
        "user_avatar": t.user.avatar_url,
        "kind": t.kind,
        "summary": t.summary,
        "source": t.source,
        "mode": t.mode,
        "dest_name": t.dest_name,
        "dest_root": t.dest_root or "",
        # 完成前显示的目标位置（完成后看 dest_path：重名时实际位置会加后缀）
        "dest_hint": f"{t.dest_root.rstrip('/')}/{t.user.linux_username}/{t.dest_name}" if t.dest_root else f"~/{t.dest_name}",
        "dest_path": t.dest_path,
        "status": t.status,
        "phase": t.phase,
        "bytes_done": t.bytes_done,
        "bytes_total": t.bytes_total,
        "files_done": t.files_done,
        "files_total": t.files_total,
        "message": t.message,
        "created_at": t.created_at.isoformat(),
        "finished_at": t.finished_at.isoformat() if t.finished_at else None,
        "stalled": _stalled(t),
    }


@router.get("/me/migrations")
def my_migrations(user: User = Depends(current_user), db: Session = Depends(get_db)):
    tasks = db.scalars(
        select(MigrationTask).where(MigrationTask.user_id == user.id).order_by(MigrationTask.created_at.desc(), MigrationTask.id.desc())
    )
    return [task_out(t) for t in tasks]


def _related(a: str, b: str) -> bool:
    """两个路径相同，或者一个在另一个之下。"""
    a, b = a.rstrip("/"), b.rstrip("/")
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


DEST_NAME_RE = re.compile(r"^[\w.-][\w .+@-]{0,126}$")


def _check_dir_claim(db: Session, server_id: int, source: str, exclude: int | None = None):
    """移动 / 原地保留会把文件从共享位置拿走：同一个目录只能有一个人这样做，已被别人申请移走的也不能再复制。
    路径有包含关系（上级或下级已被别人移走）同样算冲突。"""
    claimed = db.scalars(
        select(MigrationTask).where(
            MigrationTask.server_id == server_id,
            MigrationTask.kind == "dir",
            MigrationTask.mode.in_(("move", "link")),
            MigrationTask.status.in_(MigrationTask.ACTIVE + ("done",)),
            MigrationTask.id != (exclude or 0),
        )
    ).all()
    clash = next((t for t in claimed if _related(t.source, source)), None)
    if clash:
        raise HTTPException(409, f"该目录已被 {clash.user.name} 申请迁移")


def _check_env_claim(db: Session, server_id: int, options: dict, exclude: int | None = None):
    """conda 环境是“移动”（迁完删掉共享账号里的原环境）：同一个环境同时只能有一个人在迁。"""
    legacy = options.get("legacy_user")
    want = {(c.get("name"), e) for c in options.get("conda") or [] for e in c.get("envs") or []}
    if not want:
        return
    active = db.scalars(
        select(MigrationTask).where(
            MigrationTask.server_id == server_id,
            MigrationTask.kind == "profile",
            MigrationTask.status.in_(MigrationTask.ACTIVE),
            MigrationTask.id != (exclude or 0),
        )
    ).all()
    for t in active:
        o = t.options
        if o.get("legacy_user") != legacy:
            continue
        both = want & {(c.get("name"), e) for c in o.get("conda") or [] for e in c.get("envs") or []}
        if both:
            raise HTTPException(409, f"conda 环境 {'、'.join(sorted(e for _n, e in both))} 已被 {t.user.name} 申请迁移")


class MigrationIn(BaseModel):
    server_id: int
    source: str = Field(max_length=512)
    mode: str = Field(pattern="^(move|copy|link)$")
    dest_name: str = Field(max_length=128)
    dest_root: str = Field(default="", max_length=256)  # 空 = 家目录；数据盘挂载点 = 放到 <挂载点>/<用户名>/


@router.post("/migrations")
def create_migration(body: MigrationIn, bg: BackgroundTasks, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if not user.linux_username:
        raise HTTPException(400, "请先设置 Linux 用户名")
    server = db.get(Server, body.server_id)
    if not server:
        raise HTTPException(404, "服务器不存在")
    # 只能迁移旧账号家目录 / 迁移目录之下的内容（Agent 端还会再按自己的白名单校验一遍）
    source = posixpath.normpath(body.source)
    roots = server.browse_roots
    cand = next((c for c in server.candidates if c.get("path") == source), None)
    under_root = any(source.startswith(r.rstrip("/") + "/") for r in roots)
    if not source.startswith("/") or not (cand or under_root) or posixpath.basename(source).startswith(".lab-"):
        raise HTTPException(400, "只能迁移旧账号家目录或迁移目录里的文件和目录")
    legacy_homes = [l.get("home", "").rstrip("/") for l in server.legacy if l.get("home")]
    in_home = (cand or {}).get("in_home") or any(source.startswith(h + "/") for h in legacy_homes)
    if in_home and body.mode == "link":
        raise HTTPException(400, "旧账号家目录里的内容不能“原地保留”，请选择移动或复制")
    dest = body.dest_name.strip()
    if not DEST_NAME_RE.match(dest) or dest in (".", ".."):
        raise HTTPException(400, "目标文件夹名只能包含字母、数字、中文和 . _ - 等字符")
    dest_root = body.dest_root.strip()
    if dest_root:
        if body.mode == "link":
            raise HTTPException(400, "“原地保留”不移动数据，不能选择放到数据盘")
        if dest_root not in [d["mount"] for d in server.data_disks]:
            raise HTTPException(400, f"{dest_root} 不是这台服务器上的数据盘")
        if not server.supports_data_disk:
            raise HTTPException(400, "这台服务器的 Agent 版本太旧，更新到 0.9.5 以上才能迁移到数据盘，请联系管理员")
    _check_dir_claim(db, server.id, source)
    need_approval = (
        body.mode != "copy" and services.get_setting(db, "migration_requires_approval") == "1" and not user.is_admin
    )
    t = MigrationTask(
        user_id=user.id, server_id=server.id, kind="dir", source=source, mode=body.mode, dest_name=dest, dest_root=dest_root,
        status="pending_approval" if need_approval else "queued",
        phase="等待管理员确认" if need_approval else "排队中",
    )
    db.add(t)
    db.commit()
    bg.add_task(_bg, services.notify_admins_migration, t.id)
    return task_out(t)


class CondaPick(BaseModel):
    name: str = Field(max_length=128)
    base: bool = False
    envs: list[str] = Field(default=[], max_length=100)


class ProfileIn(BaseModel):
    server_id: int
    legacy_user: str = Field(max_length=64)
    configs: list[str] = Field(default=[], max_length=50)
    conda: list[CondaPick] = Field(default=[], max_length=10)


@router.post("/migrations/profile")
def create_profile_migration(
    body: ProfileIn, bg: BackgroundTasks, user: User = Depends(current_user), db: Session = Depends(get_db)
):
    """从旧共享账号复制配置文件、用户级软件和选中的 conda 环境（原账号不受影响）。"""
    if not user.linux_username:
        raise HTTPException(400, "请先设置 Linux 用户名")
    server = db.get(Server, body.server_id)
    if not server:
        raise HTTPException(404, "服务器不存在")
    legacy = next((l for l in server.legacy if l.get("user") == body.legacy_user), None)
    if not legacy:
        raise HTTPException(400, "该服务器上没有这个旧共享账号")
    known_cfg = {c["key"] for c in legacy.get("configs") or []}
    if not set(body.configs) <= known_cfg:
        raise HTTPException(400, "选择了不存在的配置项")
    conda_map = {c["name"]: c for c in legacy.get("conda") or []}
    picks = []
    for c in body.conda:
        info = conda_map.get(c.name)
        if not info:
            raise HTTPException(400, f"conda 目录 {c.name} 不存在")
        envs = sorted(set(c.envs))
        if not set(envs) <= {e["name"] for e in info.get("envs") or []}:
            raise HTTPException(400, "选择了不存在的 conda 环境")
        if envs or c.base:
            picks.append({"name": c.name, "base": c.base, "envs": envs})
    if not body.configs and not picks:
        raise HTTPException(400, "请至少选择一项")
    options = {"legacy_user": body.legacy_user, "configs": sorted(set(body.configs)), "conda": picks}
    _check_env_claim(db, server.id, options)
    t = MigrationTask(
        user_id=user.id, server_id=server.id, kind="profile", source=legacy.get("home", ""), mode="copy",
        dest_name="", options_json=json.dumps(options, ensure_ascii=False), status="queued", phase="排队中",
    )
    db.add(t)
    db.commit()
    return task_out(t)


def _own_task(db: Session, tid: int, user: User) -> MigrationTask:
    t = db.get(MigrationTask, tid)
    if not t or (t.user_id != user.id and not user.is_admin):
        raise HTTPException(404)
    return t


@router.delete("/migrations/{tid}")
def cancel_migration(tid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    t = _own_task(db, tid, user)
    if t.status not in ("pending_approval", "queued"):
        raise HTTPException(400, "迁移已开始或已结束，不能取消")
    t.status, t.phase = "cancelled", "已取消"
    db.commit()
    return task_out(t)


@router.post("/migrations/{tid}/retry")
def retry_migration(tid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    t = _own_task(db, tid, user)
    if t.status != "failed":
        raise HTTPException(400, "只有失败的迁移可以重试")
    # 失败期间别人可能已经申请了同一个目录 / 环境
    if t.kind == "dir":
        _check_dir_claim(db, t.server_id, t.source, exclude=t.id)
    if t.kind == "profile":
        _check_env_claim(db, t.server_id, t.options, exclude=t.id)
    t.status, t.phase, t.message = "queued", "排队中", ""
    t.finished_at = None
    t.attempt = (t.attempt or 0) + 1
    db.commit()
    return task_out(t)


@router.post("/migrations/{tid}/abandon")
def abandon_migration(tid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """长时间没有进度的迁移标记为失败，之后可以重试。Agent 晚些送到的真实结果仍会被接受（见 agent_migration_progress）。"""
    t = _own_task(db, tid, user)
    if not _stalled(t):
        raise HTTPException(400, "服务器还在执行这个迁移，不能标记为失败")
    t.status, t.phase, t.finished_at = "failed", STALLED_PHASE, now()
    t.message = "服务器已不在执行这个迁移（或长时间没有回应），已标记为失败，可以点“重试”。如果迁移其实已在服务器上完成，重试会直接确认完成，不会重复复制"
    db.commit()
    return task_out(t)


@router.get("/admin/migrations")
def admin_migrations(_: User = Depends(admin_user), db: Session = Depends(get_db)):
    tasks = db.scalars(select(MigrationTask).order_by(MigrationTask.id.desc()).limit(200))
    return [task_out(t) for t in tasks]


@router.post("/admin/migrations/{tid}/{action}")
def admin_review_migration(tid: int, action: str, _: User = Depends(admin_user), db: Session = Depends(get_db)):
    t = db.get(MigrationTask, tid)
    if not t or t.status != "pending_approval" or action not in ("approve", "reject"):
        raise HTTPException(400, "该申请不在待确认状态")
    if action == "approve":
        t.status, t.phase = "queued", "排队中"
    else:
        t.status, t.phase = "rejected", "管理员已拒绝"
    db.commit()
    return task_out(t)


# ---------------- GPU 服务器上的 Agent ----------------


class ProcessIn(BaseModel):
    pid: int | None = None  # nvidia-smi 在容器 / vGPU 等环境里可能给出 [N/A]
    user: str = ""
    used_memory: int | None = None
    path: str = ""


class GPUIn(BaseModel):
    index: int
    uuid: str = ""
    name: str = ""
    memory_total: int | None = None
    memory_used: int | None = None
    utilization: int | None = None
    processes: list[ProcessIn] = []


class AccountIn(BaseModel):
    id: int
    username: str = ""  # 本机实际的用户名（改名未生效时是旧名字）
    state: str = "ok"  # ok / pending / error
    message: str = ""


class CandidateIn(BaseModel):
    path: str
    size: int | None = None
    mtime: str = ""
    owner: str = ""
    same_fs: bool = True
    in_home: bool = False  # 在旧共享账号的家目录里
    is_dir: bool = True


class KilledIn(BaseModel):
    gpu: int
    pid: int
    user: str = ""


class AgentReport(BaseModel):
    hostname: str = ""
    agent_version: str = ""
    gpus: list[GPUIn] = []
    accounts: list[AccountIn] | None = None
    candidates: list[CandidateIn] | None = None
    browse_roots: list[str] | None = None
    legacy: list[dict] | None = None
    home_free: int | None = None
    system: dict | None = None
    killed: list[KilledIn] | None = None  # 上一轮按平台清单终止的进程
    link: str = ""          # "direct" / "tunnel"：这次是怎么连上平台的
    current_migration: int | None = None  # 正在执行的迁移 id，0 = 没有；旧版 Agent 不报（None）
    lan_ip: str | None = None  # 直连为主的机器上报的当前内网 IP（IP 不固定时，备用隧道跟着换地址）


def _is_lan_ip(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False
    return ip.version == 4 and ip.is_private and not (ip.is_loopback or ip.is_link_local)


def _agent_server(db: Session, authorization: str) -> Server:
    token = authorization.removeprefix("Bearer ").strip()
    server = db.scalar(select(Server).where(Server.token_hash == services.hash_token(token))) if token else None
    if not server:
        raise HTTPException(401, "invalid agent token")
    return server


@router.get("/agent/bootstrap-config")
def agent_bootstrap_config(authorization: str = Header(default=""), db: Session = Depends(get_db)):
    """install.sh --platform-url / tools/add-gpu.sh 用服务器 token 取回首次部署所需的参数（不含飞书等无关配置）。
    token 只有管理员在平台上添加服务器时看到一次，相当于该服务器的安装口令。"""
    server = _agent_server(db, authorization)
    return {
        "name": server.name,
        "host": server.host,
        "chisel_port": settings.chisel_port,
        "chisel_secret": settings.chisel_secret,
        "chisel_allow": settings.chisel_allow,
        # 启用了校园网隧道：GPU 经隧道在本机 127.0.0.1 上访问平台；否则直连平台公网地址
        "server_url": f"http://127.0.0.1:{settings.remote_platform_port}" if settings.chisel_secret else settings.base_url,
    }


def _disk_usage(rows: list) -> list[dict]:
    """数据盘上各一级目录的占用（MiB），Agent 0.9.5 起上报。"""
    return [
        {"path": str(r.get("path", ""))[:128], "user": str(r.get("user", ""))[:64], "size": int(r["size"])}
        for r in rows[:100] if isinstance(r, dict) and isinstance(r.get("size"), (int, float))
    ]


@router.post("/agent/sync")
def agent_sync(
    body: AgentReport, bg: BackgroundTasks, authorization: str = Header(default=""), db: Session = Depends(get_db)
):
    """Agent 每分钟调用一次：上报显卡与账号状态，取回这台机器“应该”开放给谁、要执行哪些迁移。"""
    server = _agent_server(db, authorization)
    server.last_seen_at = now()
    server.hostname = body.hostname[:128]
    server.agent_version = body.agent_version[:32]
    if body.link in ("direct", "tunnel"):
        server.link = body.link
    server.current_migration = body.current_migration
    if body.lan_ip and body.lan_ip != server.host and _is_lan_ip(body.lan_ip):
        log.info("服务器 %s 的内网地址变为 %s（原 %s），备用隧道改连新地址", server.name, body.lan_ip, server.host or "空")
        server.host = body.lan_ip
    existing = {g.index: g for g in server.gpus}
    for gi in body.gpus:
        g = existing.get(gi.index)
        if not g:
            g = GPU(server_id=server.id, index=gi.index)
            db.add(g)
            server.gpus.append(g)
            existing[gi.index] = g
        g.uuid, g.name = gi.uuid[:64], gi.name[:64]
        g.memory_total, g.memory_used, g.utilization = gi.memory_total, gi.memory_used, gi.utilization
        g.processes_json = json.dumps([p.model_dump() for p in gi.processes])
        g.updated_at = server.last_seen_at
    if body.accounts is not None:
        server.accounts_json = json.dumps({str(a.id): {**a.model_dump(), "at": now().isoformat()} for a in body.accounts})
    if body.candidates is not None:
        server.candidates_json = json.dumps([c.model_dump() for c in body.candidates][:500])
    if body.browse_roots is not None:
        server.browse_roots_json = json.dumps([r for r in body.browse_roots if isinstance(r, str)][:50], ensure_ascii=False)
    if body.legacy is not None:
        server.legacy_json = json.dumps(body.legacy[:5], ensure_ascii=False)
    if body.home_free is not None:
        server.home_free = body.home_free
    if body.system is not None:
        keys = ("cpu", "mem_used", "mem_total", "disk_used", "disk_total")
        info = {k: body.system[k] for k in keys if isinstance(body.system.get(k), (int, float))}
        users = body.system.get("users")
        if isinstance(users, list):  # 各用户家目录占用（MiB）
            info["users"] = [
                {"user": str(u.get("user", ""))[:64], "size": int(u["size"])}
                for u in users[:100] if isinstance(u, dict) and isinstance(u.get("size"), (int, float))
            ]
        disks = body.system.get("disks")
        if isinstance(disks, list):  # 每块硬盘（MiB），家目录所在的排第一
            info["disks"] = [
                {"mount": str(d.get("mount", ""))[:128], "total": int(d["total"]), "used": int(d["used"]), "home": bool(d.get("home")),
                 **({"data": bool(d["data"])} if "data" in d else {}),
                 **({"usage": _disk_usage(d["usage"])} if isinstance(d.get("usage"), list) else {})}
                for d in disks[:20]
                if isinstance(d, dict) and isinstance(d.get("total"), (int, float)) and isinstance(d.get("used"), (int, float))
            ]
        server.sysinfo_json = json.dumps(info)
    db.commit()

    users = services.desired_state(db, server)
    d = today()
    holders = services.today_holders(db, server_id=server.id)
    kill = services.track_violations(db, server, holders)
    for g in server.gpus:
        for name in services.gpu_alerts(g, holders.get(g.id, set())):
            bg.add_task(_bg, services.notify_violation, server.id, g.index, name)
    done: dict[tuple[int, str], list[int]] = {}
    for k in body.killed or []:
        done.setdefault((k.gpu, k.user), []).append(k.pid)
    for (gpu_index, username), pids in done.items():
        bg.add_task(_bg, services.notify_killed, server.id, gpu_index, username, pids)
    migrations, attempts = services.pending_migrations(db, server)

    return {
        "server": server.name,
        "date": d.isoformat(),
        "users": users,
        # 开启时：当天没有预约这台服务器显卡的成员不能 SSH 登录；关闭时所有成员都能登录
        "require_reservation_to_login": services.get_setting(db, "require_reservation_to_login") == "1",
        # Agent 按这个间隔（秒）同步 / 上报，和网页刷新间隔一致
        "interval": services.get_int_setting(db, "refresh_interval_seconds"),
        "migrations": migrations,
        "migration_attempts": attempts,
        # 在没预约的卡上超过缓冲期的进程：Agent 核对后终止，下一轮同步时上报结果
        "kill": kill,
        # 平台的时间（Unix 秒）：内网 GPU 连不上 NTP，Agent 据此校准本机时钟
        "server_time": time.time(),
    }


class MigrationProgress(BaseModel):
    # 文字字段不限长度、在下面截断：超长就拒收的话，最终结果永远送不到，任务一直卡在“进行中”
    status: str = Field(pattern="^(running|done|failed)$")
    phase: str = ""
    bytes_done: int = 0
    bytes_total: int = 0
    files_done: int = 0
    files_total: int = 0
    message: str = ""
    dest_path: str = ""


class BrowseIn(BaseModel):
    op: str = Field(default="list", pattern="^(list|size)$")
    path: str = Field(default="", max_length=1024)


@router.post("/servers/{sid}/browse")
def browse_server(sid: int, body: BrowseIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """成员选择要迁移的文件 / 目录时，请这台服务器上的 Agent 列出目录（op=list）或统计大小（op=size）。"""
    server = db.get(Server, sid)
    if not server:
        raise HTTPException(404, "服务器不存在")
    if not server.online:
        raise HTTPException(503, "服务器当前离线")
    sid_, name = server.id, server.name
    db.close()  # 等待 Agent 回复期间不占用数据库连接
    res = browse.broker.ask(sid_, body.op, posixpath.normpath(body.path) if body.path else "", timeout=60 if body.op == "size" else 25)
    if res is None:
        raise HTTPException(504, f"{name} 没有及时响应。需要在这台服务器上把 Agent 更新到 0.7.0 或更高版本")
    if not res.get("ok"):
        raise HTTPException(400, res.get("error") or "读取失败")
    if body.op == "size":
        return {"size": res.get("size")}
    entries = res.get("entries") or []
    # 标出已经被别人移走的（含上 / 下级路径有包含关系的）
    db2 = SessionLocal()
    try:
        claims = db2.scalars(
            select(MigrationTask).where(
                MigrationTask.server_id == sid_, MigrationTask.kind == "dir",
                MigrationTask.mode.in_(("move", "link")), MigrationTask.status.in_(MigrationTask.ACTIVE + ("done",)),
            ).options(joinedload(MigrationTask.user))
        ).all()
        for e in entries:
            t = next((t for t in claims if _related(t.source, e.get("path", ""))), None)
            e["claimed_by"] = t.user.name if t else None
    finally:
        db2.close()
    return {"entries": entries}


@router.post("/agent/browse/poll")
def agent_browse_poll(authorization: str = Header(default="")):
    """Agent 长轮询：有成员在浏览文件时立刻返回请求，否则最多挂 20 秒。"""
    with SessionLocal() as db:
        sid = _agent_server(db, authorization).id
    return {"requests": browse.broker.poll(sid, timeout=20)}


class BrowseResult(BaseModel):
    ok: bool = True
    error: str = ""
    entries: list[dict] | None = None
    size: int | None = None


@router.post("/agent/browse/{req_id}")
def agent_browse_answer(req_id: str, body: BrowseResult, authorization: str = Header(default="")):
    with SessionLocal() as db:
        sid = _agent_server(db, authorization).id
    if not browse.broker.answer(req_id, sid, body.model_dump()):
        raise HTTPException(404, "请求已过期")
    return {"ok": True}


@router.post("/agent/migrations/{tid}")
def agent_migration_progress(
    tid: int, body: MigrationProgress, bg: BackgroundTasks,
    authorization: str = Header(default=""), db: Session = Depends(get_db),
):
    """Agent 执行迁移时每隔几秒上报一次进度。"""
    server = _agent_server(db, authorization)
    t = db.get(MigrationTask, tid)
    if not t or t.server_id != server.id:
        raise HTTPException(404)
    # 因“失去联系”被标记为失败的，Agent 晚些送到的最终结果照样接受
    late = t.status == "failed" and t.phase == STALLED_PHASE and body.status in ("done", "failed")
    if t.status not in ("queued", "running") and not late:
        return {"ok": False, "status": t.status}
    t.status = body.status
    t.phase, t.message, t.dest_path = body.phase[:32], body.message[:4000], body.dest_path[:512] or t.dest_path
    t.bytes_done, t.bytes_total = body.bytes_done, body.bytes_total
    t.files_done, t.files_total = body.files_done, body.files_total
    t.updated_at = now()  # 进度没变也算一次心跳（值都没变时 onupdate 不会触发）
    if body.status == "running" and not t.started_at:
        t.started_at = now()
    if body.status in ("done", "failed"):
        t.finished_at = now()
        bg.add_task(_bg, services.notify_migration, t.id)
    db.commit()
    return {"ok": True}
