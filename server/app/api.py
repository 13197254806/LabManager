import json
import posixpath
import re
from datetime import date, timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from . import feishu, services
from .auth import admin_user, current_user
from .config import now, settings, today
from .db import SessionLocal, get_db
from .models import GPU, MigrationTask, Reservation, Server, SSHKey, User

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
        "description": s.description,
        "online": s.online,
        "last_seen_at": s.last_seen_at.isoformat() if s.last_seen_at else None,
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
        d.update(hostname=s.hostname, agent_version=s.agent_version, token=s.token)
    return d


# ---------------- 公共 / 个人 ----------------


@router.get("/config")
def public_config(db: Session = Depends(get_db)):
    return {
        "auto_renew_before_hours": services.get_int_setting(db, "auto_renew_before_hours"),
        "auto_renew_days": services.get_int_setting(db, "auto_renew_days"),
        "lab_name": settings.lab_name,
        "dev_login": settings.dev_login,
        "feishu_enabled": settings.feishu_enabled,
        "max_reservation_days": settings.max_reservation_days,
        "max_advance_days": settings.max_advance_days,
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
                "online": s.online,
                "home_free": s.home_free,
                "account": acct,
                "candidates": cands,
                "legacy": s.legacy,
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
    days = max(1, min(days, 62))
    start = start or today()
    end = start + timedelta(days=days - 1)
    servers = db.scalars(select(Server).options(joinedload(Server.gpus)).order_by(Server.name)).unique().all()
    d = today()
    holders: dict[int, set[str]] = {}
    for r in services.overlapping(db, d, d):
        if r.user.linux_username:
            holders.setdefault(r.gpu_id, set()).add(r.user.linux_username)
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


def _check_rules(user: User, start: date, end: date, is_new_start: bool):
    if end < start:
        raise HTTPException(400, "结束日期不能早于开始日期")
    if end < today():
        raise HTTPException(400, "结束日期不能早于今天")
    if user.is_admin:
        return
    if is_new_start and start < today():
        raise HTTPException(400, "不能预约过去的日期")
    if (end - start).days + 1 > settings.max_reservation_days:
        raise HTTPException(400, f"单次预约最长 {settings.max_reservation_days} 天，到期前可以续约")
    if start > today() + timedelta(days=settings.max_advance_days):
        raise HTTPException(400, f"最多提前 {settings.max_advance_days} 天预约")


def _check_conflicts(db: Session, gpu_ids: list[int], start: date, end: date, exclude_id: int | None = None):
    clash = [r for r in services.overlapping(db, start, end, gpu_ids=gpu_ids) if r.id != exclude_id]
    if clash:
        msg = "；".join(
            f"{r.gpu.server.name} GPU{r.gpu.index} 已被 {r.user.name} 预约（{r.start_date}~{r.end_date}）" for r in clash
        )
        raise HTTPException(409, f"时间冲突：{msg}")


class ReservationIn(BaseModel):
    gpu_ids: list[int] = Field(min_length=1, max_length=16)
    start_date: date
    end_date: date
    note: str = Field(default="", max_length=256)
    auto_renew: bool = False
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
    _check_rules(user, body.start_date, body.end_date, is_new_start=True)
    with _booking_lock:
        _check_conflicts(db, body.gpu_ids, body.start_date, body.end_date)
        rs = [
            Reservation(
                user_id=owner.id, gpu_id=g.id, start_date=body.start_date, end_date=body.end_date,
                note=body.note.strip(), auto_renew=body.auto_renew,
            )
            for g in gpus
        ]
        db.add_all(rs)
        db.commit()
    for r in rs:
        db.refresh(r)
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
def update_reservation(
    rid: int, body: ReservationPatch, bg: BackgroundTasks, user: User = Depends(current_user), db: Session = Depends(get_db)
):
    with _booking_lock:
        r = _own_reservation(db, rid, user)
        if r.end_date < today():
            raise HTTPException(400, "已结束的预约不能修改")
        start = body.start_date or r.start_date
        end = body.end_date or r.end_date
        if start != r.start_date and r.start_date <= today() and not user.is_admin:
            raise HTTPException(400, "预约已经开始，不能修改开始日期")
        _check_rules(user, start, end, is_new_start=start != r.start_date)
        _check_conflicts(db, [r.gpu_id], start, end, exclude_id=r.id)
        changed = (start, end) != (r.start_date, r.end_date)
        r.start_date, r.end_date = start, end
        if body.note is not None:
            r.note = body.note.strip()
        if body.auto_renew is not None:
            r.auto_renew = body.auto_renew
        db.commit()
    if changed:
        bg.add_task(_bg, services.notify_booked, r.user_id, [r.id], "预约已修改")
    return res_out(r, user)


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
    description: str = Field(default="", max_length=256)


@router.post("/admin/servers")
def admin_create_server(body: ServerIn, _: User = Depends(admin_user), db: Session = Depends(get_db)):
    if db.scalar(select(Server).where(Server.name == body.name.strip())):
        raise HTTPException(400, "服务器名称已存在")
    token, token_hash = services.new_agent_token()
    s = Server(name=body.name.strip(), host=body.host.strip(), description=body.description.strip(), token=token, token_hash=token_hash)
    db.add(s)
    db.commit()
    return {"server": server_out(s, admin=True), "token": token}


@router.patch("/admin/servers/{sid}")
def admin_update_server(sid: int, body: ServerIn, _: User = Depends(admin_user), db: Session = Depends(get_db)):
    s = db.get(Server, sid)
    if not s:
        raise HTTPException(404)
    s.name, s.host, s.description = body.name.strip(), body.host.strip(), body.description.strip()
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
    "welcome_title", "welcome_markdown", "welcome_links",
    "auto_renew_before_hours", "auto_renew_days", "migration_requires_approval",
)
INT_SETTINGS = {"auto_renew_before_hours": (1, 24 * 14), "auto_renew_days": (1, 60), "migration_requires_approval": (0, 1)}


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
        if k in SETTING_KEYS:
            services.set_setting(db, k, v)
    db.commit()
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


def task_out(t: MigrationTask) -> dict:
    return {
        "id": t.id,
        "server_id": t.server_id,
        "server_name": t.server.name,
        "user_id": t.user_id,
        "user_name": t.user.name,
        "kind": t.kind,
        "summary": t.summary,
        "source": t.source,
        "mode": t.mode,
        "dest_name": t.dest_name,
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
    }


@router.get("/me/migrations")
def my_migrations(user: User = Depends(current_user), db: Session = Depends(get_db)):
    tasks = db.scalars(select(MigrationTask).where(MigrationTask.user_id == user.id).order_by(MigrationTask.id.desc()))
    return [task_out(t) for t in tasks]


DEST_NAME_RE = re.compile(r"^[\w.-][\w .+@-]{0,126}$")


class MigrationIn(BaseModel):
    server_id: int
    source: str = Field(max_length=512)
    mode: str = Field(pattern="^(move|copy|link)$")
    dest_name: str = Field(max_length=128)


@router.post("/migrations")
def create_migration(body: MigrationIn, bg: BackgroundTasks, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if not user.linux_username:
        raise HTTPException(400, "请先设置 Linux 用户名")
    server = db.get(Server, body.server_id)
    if not server:
        raise HTTPException(404, "服务器不存在")
    # 只能选 Agent 上报的候选目录（Agent 端还会再按自己的白名单校验一遍）
    source = posixpath.normpath(body.source)
    cand = next((c for c in server.candidates if c.get("path") == source), None)
    if not cand:
        raise HTTPException(400, "该目录不在可迁移列表中（列表由服务器每 10 分钟刷新一次）")
    if cand.get("in_home") and body.mode == "link":
        raise HTTPException(400, "旧账号家目录里的内容不能“原地保留”，请选择移动或复制")
    dest = body.dest_name.strip()
    if not DEST_NAME_RE.match(dest) or dest in (".", ".."):
        raise HTTPException(400, "目标文件夹名只能包含字母、数字、中文和 . _ - 等字符")
    # 移动 / 原地保留会把文件从共享位置拿走，同一个目录只能有一个人这样做；复制不受限制
    clash = db.scalar(
        select(MigrationTask).where(
            MigrationTask.server_id == server.id,
            MigrationTask.kind == "dir",
            MigrationTask.source == source,
            MigrationTask.mode.in_(("move", "link")),
            MigrationTask.status.in_(MigrationTask.ACTIVE + ("done",)),
        )
    )
    if clash:
        raise HTTPException(409, f"该目录已被 {clash.user.name} 申请迁移")
    need_approval = (
        body.mode != "copy" and services.get_setting(db, "migration_requires_approval") == "1" and not user.is_admin
    )
    t = MigrationTask(
        user_id=user.id, server_id=server.id, kind="dir", source=source, mode=body.mode, dest_name=dest,
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
    t.status, t.phase, t.message = "queued", "排队中", ""
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
    pid: int
    user: str = ""
    used_memory: int | None = None


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


class AgentReport(BaseModel):
    hostname: str = ""
    agent_version: str = ""
    gpus: list[GPUIn] = []
    accounts: list[AccountIn] | None = None
    candidates: list[CandidateIn] | None = None
    legacy: list[dict] | None = None
    home_free: int | None = None


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


@router.post("/agent/sync")
def agent_sync(
    body: AgentReport, bg: BackgroundTasks, authorization: str = Header(default=""), db: Session = Depends(get_db)
):
    """Agent 每分钟调用一次：上报显卡与账号状态，取回这台机器“应该”开放给谁、要执行哪些迁移。"""
    server = _agent_server(db, authorization)
    server.last_seen_at = now()
    server.hostname = body.hostname[:128]
    server.agent_version = body.agent_version[:32]
    existing = {g.index: g for g in server.gpus}
    for gi in body.gpus:
        g = existing.get(gi.index)
        if not g:
            g = GPU(server_id=server.id, index=gi.index)
            db.add(g)
            server.gpus.append(g)
        g.uuid, g.name = gi.uuid[:64], gi.name[:64]
        g.memory_total, g.memory_used, g.utilization = gi.memory_total, gi.memory_used, gi.utilization
        g.processes_json = json.dumps([p.model_dump() for p in gi.processes])
        g.updated_at = server.last_seen_at
    if body.accounts is not None:
        server.accounts_json = json.dumps({str(a.id): {**a.model_dump(), "at": now().isoformat()} for a in body.accounts})
    if body.candidates is not None:
        server.candidates_json = json.dumps([c.model_dump() for c in body.candidates][:500])
    if body.legacy is not None:
        server.legacy_json = json.dumps(body.legacy[:5], ensure_ascii=False)
    if body.home_free is not None:
        server.home_free = body.home_free
    db.commit()

    users = services.desired_state(db, server)
    holders: dict[int, set[str]] = {}
    d = today()
    for r in services.overlapping(db, d, d, server_id=server.id):
        holders.setdefault(r.gpu_id, set()).add(r.user.linux_username)
    for g in server.gpus:
        for name in services.gpu_alerts(g, holders.get(g.id, set())):
            bg.add_task(_bg, services.notify_violation, server.id, g.index, name)

    return {
        "server": server.name,
        "date": d.isoformat(),
        "users": users,
        "migrations": services.pending_migrations(db, server),
    }


class MigrationProgress(BaseModel):
    status: str = Field(pattern="^(running|done|failed)$")
    phase: str = Field(default="", max_length=32)
    bytes_done: int = 0
    bytes_total: int = 0
    files_done: int = 0
    files_total: int = 0
    message: str = Field(default="", max_length=2000)
    dest_path: str = Field(default="", max_length=512)


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
    if t.status not in ("queued", "running"):
        return {"ok": False, "status": t.status}
    t.status = body.status
    t.phase, t.message, t.dest_path = body.phase, body.message, body.dest_path or t.dest_path
    t.bytes_done, t.bytes_total = body.bytes_done, body.bytes_total
    t.files_done, t.files_total = body.files_done, body.files_total
    if body.status == "running" and not t.started_at:
        t.started_at = now()
    if body.status in ("done", "failed"):
        t.finished_at = now()
        bg.add_task(_bg, services.notify_migration, t.id)
    db.commit()
    return {"ok": True}
