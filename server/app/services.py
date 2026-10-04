import base64
import hashlib
import re
import secrets
import struct
import threading
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from . import feishu
from .config import now, settings, today
from .models import GPU, MigrationTask, Notice, Reservation, Server, Setting, User

# 预约的“检查冲突 + 写入”必须串行（网页请求和自动续约共用），避免两个人抢到同一张卡。
# 依赖 web 以单进程运行（docker-compose 里就是单 worker）。
booking_lock = threading.Lock()

# ---------------- 校验 ----------------

USERNAME_RE = re.compile(r"^[a-z][a-z0-9_-]{1,30}$")
RESERVED_USERNAMES = {
    "root", "admin", "administrator", "daemon", "bin", "sys", "sync", "games", "man", "lp", "mail",
    "news", "uucp", "proxy", "www-data", "backup", "list", "irc", "gnats", "nobody", "syslog",
    "messagebus", "sshd", "ubuntu", "centos", "debian", "docker", "nvidia", "guest", "test",
    "operator", "postfix", "ntp", "polkitd", "landscape", "pollinate", "lxd", "dnsmasq",
}


def validate_linux_username(name: str) -> str:
    name = (name or "").strip()
    if not USERNAME_RE.match(name):
        raise ValueError("用户名需以小写字母开头，只含小写字母、数字、- 和 _，长度 2-31")
    if name in RESERVED_USERNAMES or name.startswith(("lab-", "systemd", "_")):
        raise ValueError("该用户名为系统保留名，请换一个")
    return name


SSH_KEY_TYPES = {
    "ssh-ed25519", "ssh-rsa", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521",
    "sk-ssh-ed25519@openssh.com", "sk-ecdsa-sha2-nistp256@openssh.com",
}


def parse_ssh_key(text: str) -> tuple[str, str]:
    """校验 OpenSSH 公钥，返回 (规范化后的单行公钥, SHA256 指纹)。"""
    text = (text or "").strip()
    if not text or "\n" in text or "\r" in text or len(text) > 16384:
        raise ValueError("请粘贴单行公钥（通常是 ~/.ssh/id_ed25519.pub 的内容）")
    parts = text.split(None, 2)
    if len(parts) < 2 or parts[0] not in SSH_KEY_TYPES:
        raise ValueError("无法识别的公钥格式，应以 ssh-ed25519 / ssh-rsa / ecdsa-... 开头。注意不要粘贴私钥！")
    try:
        blob = base64.b64decode(parts[1], validate=True)
        (n,) = struct.unpack(">I", blob[:4])
        if blob[4 : 4 + n].decode() != parts[0]:
            raise ValueError
    except Exception:
        raise ValueError("公钥内容损坏，请完整复制 .pub 文件内容")
    comment = re.sub(r"[^\w.@+=:/-]", "_", parts[2])[:64] if len(parts) > 2 else ""
    fp = "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
    normalized = f"{parts[0]} {parts[1]}" + (f" {comment}" if comment else "")
    return normalized, fp


def hash_password(password: str, username: str | None) -> str:
    """校验强度并生成 Linux 可用的 sha512-crypt 哈希（/etc/shadow 格式）。"""
    from passlib.hash import sha512_crypt

    # 只做最低限度的校验：长度（太短的密码同步到各服务器 SSH 上有被爆破的风险）和字符安全
    # （换行/空字符会破坏 /etc/shadow）。不再强制复杂度、不再禁止包含用户名。
    if len(password) < 8 or len(password) > 128:
        raise ValueError("密码长度需为 8-128 位")
    if any(c in password for c in "\n\r\x00"):
        raise ValueError("密码不能包含换行等特殊字符")
    return sha512_crypt.using(rounds=100000).hash(password)


def new_agent_token() -> tuple[str, str]:
    token = "lab_" + secrets.token_urlsafe(32)
    return token, hash_token(token)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ---------------- 预约查询 ----------------


def overlapping(db: Session, start: date, end: date, gpu_ids=None, server_id=None):
    q = (
        select(Reservation)
        .join(GPU)
        .where(Reservation.status == "active", Reservation.start_date <= end, Reservation.end_date >= start)
        .options(joinedload(Reservation.user), joinedload(Reservation.gpu).joinedload(GPU.server))
    )
    if gpu_ids is not None:
        q = q.where(Reservation.gpu_id.in_(list(gpu_ids)))
    if server_id is not None:
        q = q.where(GPU.server_id == server_id)
    return list(db.scalars(q).unique())


def gpu_alerts(gpu: GPU, holders: set[str]) -> list[str]:
    """GPU 上有进程、但进程属主不是今天预约这张卡的人 → 返回这些用户名。"""
    users = {p.get("user") or "?" for p in gpu.processes}
    return sorted(u for u in users if u not in holders)


def desired_state(db: Session, server: Server) -> list[dict]:
    """某台服务器“此刻应该是什么样”：所有已绑定账号的成员 + 今天谁有预约、能用哪些卡。
    Agent 用 uid 识别账号，所以平台上改了用户名，Agent 会把同一个账号改名，而不是新建。"""
    d = today()
    active: dict[int, set[int]] = {}
    for r in overlapping(db, d, d, server_id=server.id):
        active.setdefault(r.user_id, set()).add(r.gpu.index)
    users = db.scalars(
        select(User).where(User.enabled.is_(True), User.linux_username.is_not(None)).options(joinedload(User.ssh_keys))
    ).unique()
    return [
        {
            "id": u.id,
            "username": u.linux_username,
            "uid": u.uid,
            "name": u.name,
            "password_hash": u.password_hash or "",
            "pubkeys": [k.public_key for k in u.ssh_keys],
            "active": u.id in active,
            "gpus": sorted(active.get(u.id, [])),
        }
        for u in users
    ]


def pending_migrations(db: Session, server: Server) -> list[dict]:
    tasks = db.scalars(
        select(MigrationTask)
        .where(MigrationTask.server_id == server.id, MigrationTask.status.in_(("queued", "running")))
        .order_by(MigrationTask.id)
    )
    return [
        {
            "id": t.id,
            "kind": t.kind,
            "user_id": t.user_id,
            "uid": t.user.uid,
            "source": t.source,
            "mode": t.mode,
            "dest_name": t.dest_name,
            "options": t.options,
        }
        for t in tasks
        if t.user.enabled and t.user.linux_username
    ]


# ---------------- 可配置项 ----------------

DEFAULT_SETTINGS = {
    "welcome_title": "欢迎加入实验室 👋",
    "welcome_markdown": (
        "你好，欢迎加入实验室！下面是新人必读材料，请在第一周内看完：\n"
        "- **新人指南**：实验室日常、组会安排、常用联系人\n"
        "- **GPU 使用说明**：如何预约显卡、登录服务器\n\n"
        "使用 GPU 前，请先打开实验室平台，按引导设置 Linux 用户名和登录密码；以前在共享账号（如 ubuntu）里的配置、conda 环境和项目文件，可以在平台上迁移到自己的账号下。"
    ),
    # 每行一个按钮：按钮文字|链接。{base_url} 会被替换为平台地址
    "welcome_links": "打开实验室平台|{base_url}\n新人指南|https://example.feishu.cn/wiki/xxxx",
    # 自动续约：到期前多少小时触发，每次延长多少天
    "auto_renew_before_hours": "24",
    "auto_renew_days": "7",
    # 成员发起的文件迁移是否需要管理员确认（1/0）
    "migration_requires_approval": "0",
}


def get_int_setting(db: Session, key: str) -> int:
    try:
        return int(get_setting(db, key))
    except ValueError:
        return int(DEFAULT_SETTINGS[key])


def get_setting(db: Session, key: str) -> str:
    row = db.get(Setting, key)
    return row.value if row else DEFAULT_SETTINGS.get(key, "")


def set_setting(db: Session, key: str, value: str):
    row = db.get(Setting, key)
    if row:
        row.value = value
    else:
        db.add(Setting(key=key, value=value))


# ---------------- 通知 ----------------


def claim_notice(db: Session, key: str) -> bool:
    """去重：同一个 key 只发送一次。返回 True 表示本次应该发送。"""
    db.add(Notice(key=key))
    try:
        db.commit()
        return True
    except IntegrityError:
        db.rollback()
        return False


def _fmt_range(r: Reservation) -> str:
    if r.start_date == r.end_date:
        return r.start_date.isoformat()
    return f"{r.start_date.isoformat()} ~ {r.end_date.isoformat()}"


def describe(rs: list[Reservation]) -> str:
    lines = []
    for r in sorted(rs, key=lambda r: (r.gpu.server.name, r.gpu.index, r.start_date)):
        lines.append(f"- **{r.gpu.server.name}** GPU{r.gpu.index}（{r.gpu.name or 'GPU'}）：{_fmt_range(r)}")
    return "\n".join(lines)


def welcome_card(db: Session) -> dict:
    buttons = []
    for line in get_setting(db, "welcome_links").splitlines():
        if "|" in line:
            text, url = line.split("|", 1)
            url = url.strip().replace("{base_url}", settings.base_url)
            if text.strip() and url.startswith("http"):
                buttons.append((text.strip(), url))
    return feishu.build_card(get_setting(db, "welcome_title"), get_setting(db, "welcome_markdown"), buttons)


def send_welcome(db: Session, open_id: str, force: bool = False, raise_errors: bool = False) -> bool:
    if not force and not claim_notice(db, f"welcome:{open_id}"):
        return False
    return feishu.send_card(open_id, welcome_card(db), raise_errors=raise_errors)


def notify_booked(db: Session, user_id: int, res_ids: list[int], action: str):
    user = db.get(User, user_id)
    rs = [db.get(Reservation, i) for i in res_ids]
    md = describe(rs) + "\n\n预约当天 00:00 起，登录对应服务器后会自动只看到你预约的显卡；到期后次日 00:00 这些卡不再分配给你。"
    if any(r.auto_renew for r in rs):
        md += "\n已开启**自动续约**：到期前会自动延长，并通知你。"
    feishu.send_card(
        user.open_id,
        feishu.build_card(f"GPU {action}", md, [("查看日历", settings.base_url)], template="green"),
    )


def notify_violation(db: Session, server_id: int, gpu_index: int, username: str):
    user = db.scalar(select(User).where(User.linux_username == username))
    server = db.get(Server, server_id)
    if not user or not server:
        return
    gpu = next(g for g in server.gpus if g.index == gpu_index)
    if not claim_notice(db, f"violation:{today()}:{server.id}:{gpu.index}:{username}"):
        return
    md = (
        f"检测到你在 **{server.name}** 的 **GPU{gpu.index}** 上运行程序，但今天这张卡不在你的预约里。\n"
        "请尽快结束该进程，或在平台上补一个预约（如果这张卡空闲）。\n"
        "提示：登录后执行 `lab-gpus` 可以查看你当前可用的卡。"
    )
    feishu.send_card(user.open_id, feishu.build_card("⚠️ 未预约使用 GPU", md, [("去预约", settings.base_url)], template="orange"))


def send_expiry_reminders(db: Session) -> int:
    """给“今天到期”的预约发提醒，每人每天最多一条。"""
    d = today()
    # 开了自动续约的不提醒（续约失败时会单独通知）
    rs = [r for r in overlapping(db, d, d) if r.end_date == d and not r.auto_renew]
    by_user: dict[int, list[Reservation]] = {}
    for r in rs:
        by_user.setdefault(r.user_id, []).append(r)
    sent = 0
    for _uid, items in by_user.items():
        user = items[0].user
        if not claim_notice(db, f"expire:{d}:{user.id}"):
            continue
        md = describe(items) + "\n\n以上预约将在 **今晚 24:00** 到期，之后这些卡不再分配给你（登录和文件不受影响，已在运行的程序也不会被强制结束）。\n需要继续使用请在平台上延长预约。"
        feishu.send_card(user.open_id, feishu.build_card("⏰ GPU 预约今天到期", md, [("去续约", settings.base_url)], template="orange"))
        sent += 1
    return sent


def run_auto_renew(db: Session) -> int:
    """到期前 N 小时自动延长开启了自动续约的预约；与别人的预约冲突时尽量延长到冲突前一天。"""
    before = timedelta(hours=get_int_setting(db, "auto_renew_before_hours"))
    days = max(1, get_int_setting(db, "auto_renew_days"))
    t = now()
    d = today()
    renewed: dict[int, list[tuple[Reservation, date, date]]] = {}
    failed: dict[int, list[tuple[Reservation, str]]] = {}
    step = timedelta(days=days)
    with booking_lock:
        candidates = [r for r in overlapping(db, d, d + timedelta(days=3650)) if r.auto_renew and r.user.enabled]
        for r in candidates:
            old_end = r.end_date
            deadline = datetime.combine(old_end + timedelta(days=1), datetime.min.time())
            if t < deadline - before:
                continue
            # 一次续到“离到期时间已经早于触发窗口”为止：提前小时数大于每次延长天数时需要连续延长几次，
            # 否则会每分钟续一次、反复发通知
            wanted = old_end + step * ((t + before - deadline) // step + 1)
            new_end = wanted
            reason = ""
            clash = [c for c in overlapping(db, old_end + timedelta(days=1), new_end, gpu_ids=[r.gpu_id]) if c.id != r.id]
            if clash:
                first = min(clash, key=lambda c: c.start_date)
                new_end = first.start_date - timedelta(days=1)
                reason = f"{first.user.name} 已预约了 {first.start_date} 起的这张卡"
            if new_end > old_end:
                r.end_date = new_end
                r.renew_count += 1
                renewed.setdefault(r.user_id, []).append((r, old_end, wanted))
            else:
                # 一天都续不了：关闭自动续约，避免每分钟重试，并通知本人
                r.auto_renew = False
                failed.setdefault(r.user_id, []).append((r, reason))
        db.commit()

    for items in renewed.values():
        user = items[0][0].user
        lines = [f"- **{r.gpu.server.name}** GPU{r.gpu.index}：{old} → **{r.end_date}**" for r, old, _w in items]
        partial = any(r.end_date < wanted for r, _old, wanted in items)
        md = "以下预约已自动续约：\n" + "\n".join(lines)
        if partial:
            md += "\n\n部分预约因后面已有他人预约，只延长到了冲突前一天。"
        md += "\n\n不再需要时请及时释放，或在平台上关闭自动续约。"
        feishu.send_card(user.open_id, feishu.build_card("🔁 GPU 已自动续约", md, [("管理预约", settings.base_url)], template="green"))
    for items in failed.values():
        user = items[0][0].user
        lines = [f"- **{r.gpu.server.name}** GPU{r.gpu.index}（{r.end_date} 到期）：{why}" for r, why in items]
        md = "以下预约**自动续约失败**，已为你关闭自动续约：\n" + "\n".join(lines) + "\n\n请在到期前处理好实验，或换一张卡预约。"
        feishu.send_card(user.open_id, feishu.build_card("⚠️ GPU 自动续约失败", md, [("去预约", settings.base_url)], template="orange"))
    return sum(len(v) for v in renewed.values())


def human_size(n: int | None) -> str:
    if n is None:
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"


def notify_migration(db: Session, task_id: int):
    t = db.get(MigrationTask, task_id)
    if not t or t.status not in ("done", "failed"):
        return
    if t.status == "done":
        if t.kind == "profile":
            md = f"**{t.server.name}**：{t.summary}，已完成（{human_size(t.bytes_total)}，{t.files_total} 个文件）。"
        else:
            how = {"move": "已移动到", "copy": "已复制到", "link": "已归你所有，并在家目录建立快捷方式"}.get(t.mode, "已迁移到")
            md = f"**{t.server.name}** 上的 `{t.source}` {how} `{t.dest_path}`（{human_size(t.bytes_total)}，{t.files_total} 个文件）。"
        if t.message:
            md += f"\n\n{t.message}"
        card = feishu.build_card("📦 文件迁移完成", md, [("查看详情", settings.base_url)], template="green")
    else:
        md = f"**{t.server.name}**：{t.summary} 迁移失败：{t.message}\n\n可以在平台上重试，或联系管理员。"
        card = feishu.build_card("⚠️ 文件迁移失败", md, [("查看详情", settings.base_url)], template="red")
    feishu.send_card(t.user.open_id, card)


def notify_admins_migration(db: Session, task_id: int):
    t = db.get(MigrationTask, task_id)
    if not t:
        return
    need = t.status == "pending_approval"
    md = f"{t.user.name}（{t.user.linux_username}）申请迁移 **{t.server.name}**：{t.summary}。"
    if need:
        md += "\n\n需要管理员在平台「管理 → 文件迁移」中确认。"
    for admin in db.scalars(select(User).where(User.is_admin.is_(True), User.enabled.is_(True))):
        feishu.send_card(admin.open_id, feishu.build_card("📦 文件迁移申请" if need else "📦 文件迁移记录", md, [("打开平台", settings.base_url)]))


def free_gpus_text(db: Session) -> str:
    d = today()
    busy = {r.gpu_id: r.user.name for r in overlapping(db, d, d)}
    lines = [f"📅 {d.isoformat()} GPU 占用情况："]
    for s in db.scalars(select(Server).order_by(Server.name)):
        cells = []
        for g in s.gpus:
            cells.append(f"GPU{g.index}:{busy[g.id] if g.id in busy else '空闲'}")
        status = "" if s.online else "（离线）"
        lines.append(f"• {s.name}{status}  " + "  ".join(cells))
    return "\n".join(lines)


def my_reservations_text(db: Session, user: User) -> str:
    d = today()
    rs = [r for r in overlapping(db, d, d + timedelta(days=365)) if r.user_id == user.id]
    if not rs:
        return "你目前没有进行中或即将开始的 GPU 预约。"
    lines = ["你的 GPU 预约："]
    for r in sorted(rs, key=lambda r: (r.start_date, r.gpu.server.name, r.gpu.index)):
        lines.append(f"• {r.gpu.server.name} GPU{r.gpu.index}  {_fmt_range(r)}")
    return "\n".join(lines)


def my_migrations_text(db: Session, user: User) -> str:
    tasks = db.scalars(select(MigrationTask).where(MigrationTask.user_id == user.id).order_by(MigrationTask.id.desc()).limit(10)).all()
    if not tasks:
        return "你没有文件迁移记录。"
    names = {"pending_approval": "待确认", "queued": "排队中", "running": "进行中", "done": "已完成",
             "failed": "失败", "cancelled": "已取消", "rejected": "已拒绝"}
    lines = ["你的文件迁移："]
    for t in tasks:
        pct = f" {t.bytes_done * 100 // t.bytes_total}%" if t.status == "running" and t.bytes_total else ""
        lines.append(f"• {t.server.name} {t.summary}：{names.get(t.status, t.status)}{pct} {t.phase if t.status == 'running' else ''}")
    return "\n".join(lines)
