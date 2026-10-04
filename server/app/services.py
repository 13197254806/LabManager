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


def merge_reservations(db: Session, user_id: int | None = None, gpu_id: int | None = None) -> dict[int, int]:
    """把同一个人在同一张卡上首尾相接（或重叠）的有效预约合并成一条，不提交事务。
    保留最早的那条，开始日期取最早、结束日期取最晚；用途备注和长期使用标记以这段连续预约里
    第一条还没结束的预约为准（都已结束则沿用最早那条）。返回 {被合并删除的预约 id: 保留的预约 id}。"""
    q = select(Reservation).where(Reservation.status == "active").order_by(
        Reservation.user_id, Reservation.gpu_id, Reservation.start_date, Reservation.id
    )
    if user_id is not None:
        q = q.where(Reservation.user_id == user_id)
    if gpu_id is not None:
        q = q.where(Reservation.gpu_id == gpu_id)
    d = today()
    chains: list[list[Reservation]] = []
    last_end: date | None = None
    for r in db.scalars(q):
        prev = chains[-1][0] if chains else None
        if prev and (prev.user_id, prev.gpu_id) == (r.user_id, r.gpu_id) and r.start_date <= last_end + timedelta(days=1):
            chains[-1].append(r)
            last_end = max(last_end, r.end_date)
        else:
            chains.append([r])
            last_end = r.end_date
    mapping: dict[int, int] = {}
    for chain in chains:
        if len(chain) < 2:
            continue
        keep = chain[0]
        head = next((r for r in chain if r.end_date >= d), keep)
        keep.end_date = max(r.end_date for r in chain)
        keep.note, keep.auto_renew = head.note, head.auto_renew
        keep.renew_count = sum(r.renew_count for r in chain)
        for r in chain[1:]:
            mapping[r.id] = keep.id
            db.delete(r)
    return mapping


def first_blocked_day(others: list[Reservation], user_id: int, start: date, end: date):
    """在 start~end 里找第一个不能再加这位用户预约的日子。others 是这张卡上别的预约。
    返回 (日期, 原因, 当天已有预约的人数) 或 None。原因：mine=这个人在这张卡上已有预约，full=共用人数已满。"""
    d = start
    while d <= end:
        day = [r for r in others if r.start_date <= d <= r.end_date]
        if any(r.user_id == user_id for r in day):
            return d, "mine", len(day)
        if len(day) >= settings.max_gpu_sharers:
            return d, "full", len(day)
        d += timedelta(days=1)
    return None


def check_capacity(db: Session, gpu_ids: list[int], start: date, end: date, user_id: int, exclude_id: int | None = None):
    """同一张卡同一天最多 max_gpu_sharers 人共用，且同一个人不能在同一张卡同一天重复预约。冲突时抛 ValueError。"""
    rs = [r for r in overlapping(db, start, end, gpu_ids=gpu_ids) if r.id != exclude_id]
    problems = []
    for gid in gpu_ids:
        mine = [r for r in rs if r.gpu_id == gid]
        hit = first_blocked_day(mine, user_id, start, end)
        if not hit:
            continue
        day, why, n = hit
        g = mine[0].gpu if mine else None
        label = f"{g.server.name} GPU{g.index}" if g else f"GPU #{gid}"
        if why == "mine":
            problems.append(f"{label} 在 {day} 你已经有一条预约了（可以直接修改它的日期）")
        else:
            names = "、".join(sorted({r.user.name for r in mine if r.start_date <= day <= r.end_date}))
            problems.append(f"{label} 在 {day} 已有 {n} 人预约（{names}），最多 {settings.max_gpu_sharers} 人共用")
    if problems:
        raise ValueError("时间冲突：" + "；".join(problems))


def book_long_term(db: Session, owner: User, gpus: list[GPU], note: str = "") -> list[int]:
    """长期使用：从今天起预约一个周期（auto_renew_days 天），之后到期前自动延长。
    这张卡上自己已有的临时预约被覆盖：今天起的部分取消，已经开始的预约只保留今天之前的历史；
    自己已有的长期使用预约保持不变，只补上没有预约的日子，最后合并成一条。
    某张卡今天就满员（或被自己占着以外的原因无法预约）时抛 ValueError，整个操作不生效。返回预约 id。"""
    d = today()
    cycle = min(max(1, get_int_setting(db, "auto_renew_days")), settings.max_reservation_days)
    window_end = d + timedelta(days=cycle - 1)
    ids: list[int] = []
    problems = []
    for g in gpus:
        for r in overlapping(db, d, d + timedelta(days=3650), gpu_ids=[g.id]):
            if r.user_id == owner.id and not r.auto_renew:
                if r.start_date >= d:
                    r.status = "cancelled"
                else:
                    r.end_date = d - timedelta(days=1)
        db.flush()
        rs = overlapping(db, d, d + timedelta(days=3650), gpu_ids=[g.id])
        mine = [r for r in rs if r.user_id == owner.id]
        others = [r for r in rs if r.user_id != owner.id]

        def covered(day, mine=mine):
            return any(r.start_date <= day <= r.end_date for r in mine)

        def full(day, others=others):
            return sum(1 for r in others if r.start_date <= day <= r.end_date) >= settings.max_gpu_sharers

        label = f"{g.server.name} GPU{g.index}"
        if not covered(d) and full(d):
            problems.append(f"{label} 今天已有 {settings.max_gpu_sharers} 人预约，最多 {settings.max_gpu_sharers} 人共用")
            continue
        # 补上 [今天, 周期末] 里自己还没预约的日子；中途遇到别人已满的日子就停在前一天
        day, reach = d, d
        run_start = None
        while day <= window_end:
            if covered(day):
                if run_start:
                    db.add(Reservation(user_id=owner.id, gpu_id=g.id, start_date=run_start, end_date=day - timedelta(days=1), note=note))
                    run_start = None
            elif full(day):
                break
            elif run_start is None:
                run_start = day
            reach = day
            day += timedelta(days=1)
        if run_start:
            db.add(Reservation(user_id=owner.id, gpu_id=g.id, start_date=run_start, end_date=reach, note=note))
        db.flush()
        # 从今天起连在一起的这段预约都标记为长期使用，再合并成一条
        for r in overlapping(db, d, reach + timedelta(days=1), gpu_ids=[g.id]):
            if r.user_id == owner.id:
                r.auto_renew = True
        db.flush()
        merge_reservations(db, owner.id, g.id)
        keep = db.scalar(
            select(Reservation).where(Reservation.status == "active", Reservation.user_id == owner.id,
                                      Reservation.gpu_id == g.id, Reservation.start_date <= d, Reservation.end_date >= d)
        ) or db.scalar(
            select(Reservation).where(Reservation.status == "active", Reservation.user_id == owner.id,
                                      Reservation.gpu_id == g.id, Reservation.end_date >= d).order_by(Reservation.start_date)
        )
        if note and keep and not keep.note:
            keep.note = note
        if keep:
            ids.append(keep.id)
    if problems:
        raise ValueError("无法设为长期使用：" + "；".join(problems))
    return ids


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
    # 长期使用的自动延长：到期前多少小时触发，每次延长多少天
    # 默认：预约结束当天 20:00（距到期 4 小时）自动延长 7 天
    "auto_renew_before_hours": "4",
    "auto_renew_days": "7",
    # 成员发起的文件迁移是否需要管理员确认（1/0）
    "migration_requires_approval": "0",
    # 1：没有预约某台服务器显卡的成员，当天不能 SSH 登录这台服务器；0：所有成员都能登录，预约只决定能用哪些卡
    "require_reservation_to_login": "0",
    # 网页自动刷新（GPU 日历、管理页）的间隔；Agent 也按这个间隔上报，让页面上的数据足够新（秒）
    "refresh_interval_seconds": "10",
    # 1：只允许和管理员同一个飞书组织的成员登录（FEISHU_TENANT_KEY 配置了就以它为准）
    "restrict_to_admin_tenant": "1",
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
    if get_setting(db, "require_reservation_to_login") == "1":
        md = describe(rs) + "\n\n预约当天 00:00 起可以 SSH 登录对应服务器并使用所选显卡，到期后次日 00:00 关闭登录权限。"
    else:
        md = describe(rs) + "\n\n预约当天 00:00 起，登录对应服务器后会自动只看到你预约的显卡；到期后次日 00:00 这些卡不再分配给你。"
    if any(r.auto_renew for r in rs):
        md += "\n这是**长期使用**：到期前会自动延长，并通知你。"
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
    # 长期使用（自动延长）的不提醒（续约失败时会单独通知）
    rs = [r for r in overlapping(db, d, d) if r.end_date == d and not r.auto_renew]
    by_user: dict[int, list[Reservation]] = {}
    for r in rs:
        by_user.setdefault(r.user_id, []).append(r)
    sent = 0
    for _uid, items in by_user.items():
        user = items[0].user
        if not claim_notice(db, f"expire:{d}:{user.id}"):
            continue
        after = ("之后无法再 SSH 登录这些服务器" if get_setting(db, "require_reservation_to_login") == "1"
                 else "之后这些卡不再分配给你（登录和文件不受影响）")
        md = describe(items) + f"\n\n以上预约将在 **今晚 24:00** 到期，{after}，已在运行的程序不会被强制结束。\n需要继续使用请在平台上延长预约。"
        feishu.send_card(user.open_id, feishu.build_card("⏰ GPU 预约今天到期", md, [("去续约", settings.base_url)], template="orange"))
        sent += 1
    return sent


def run_auto_renew(db: Session) -> int:
    """到期前 N 小时自动延长长期使用的预约；与别人的预约冲突时尽量延长到冲突前一天。"""
    before = timedelta(hours=get_int_setting(db, "auto_renew_before_hours"))
    days = max(1, get_int_setting(db, "auto_renew_days"))
    t = now()
    d = today()
    renewed: dict[int, list[tuple[Reservation, date, date]]] = {}
    failed: dict[int, list[tuple[Reservation, str]]] = {}
    step = timedelta(days=days)
    touched: set[tuple[int, int]] = set()
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
            others = [c for c in overlapping(db, old_end + timedelta(days=1), new_end, gpu_ids=[r.gpu_id]) if c.id != r.id]
            hit = first_blocked_day(others, r.user_id, old_end + timedelta(days=1), new_end)
            if hit:
                day, why, n = hit
                new_end = day - timedelta(days=1)
                reason = (f"{day} 起你在这张卡上已有另一条预约" if why == "mine"
                          else f"{day} 起这张卡已有 {n} 人预约（最多 {settings.max_gpu_sharers} 人共用）")
            if new_end > old_end:
                r.end_date = new_end
                r.renew_count += 1
                renewed.setdefault(r.user_id, []).append((r, old_end, wanted))
                touched.add((r.user_id, r.gpu_id))
            else:
                # 一天都续不了：转为临时使用，避免每分钟重试，并通知本人
                r.auto_renew = False
                failed.setdefault(r.user_id, []).append((r, reason))
        db.flush()
        for uid, gid in touched:  # 续到与自己的另一条预约首尾相接时，合并成一条
            merge_reservations(db, uid, gid)
        db.commit()

    for items in renewed.values():
        user = items[0][0].user
        lines = [f"- **{r.gpu.server.name}** GPU{r.gpu.index}：{old} → **{r.end_date}**" for r, old, _w in items]
        partial = any(r.end_date < wanted for r, _old, wanted in items)
        md = "以下长期使用的预约已自动延长：\n" + "\n".join(lines)
        if partial:
            md += "\n\n部分预约因后面这张卡的共用人数已满，只延长到了冲突前一天。"
        md += "\n\n不再需要时请及时释放，或在平台上改为临时使用。"
        feishu.send_card(user.open_id, feishu.build_card("GPU 长期使用已自动延长", md, [("管理预约", settings.base_url)], template="green"))
    for items in failed.values():
        user = items[0][0].user
        lines = [f"- **{r.gpu.server.name}** GPU{r.gpu.index}（{r.end_date} 到期）：{why}" for r, why in items]
        md = "以下长期使用的预约**自动延长失败**，已改为临时使用：\n" + "\n".join(lines) + "\n\n请在到期前处理好实验，或换一张卡预约。"
        feishu.send_card(user.open_id, feishu.build_card("⚠️ GPU 长期使用延长失败", md, [("去预约", settings.base_url)], template="orange"))
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
    busy: dict[int, list[str]] = {}
    for r in overlapping(db, d, d):
        busy.setdefault(r.gpu_id, []).append(r.user.name)
    lines = [f"📅 {d.isoformat()} GPU 占用情况："]
    for s in db.scalars(select(Server).order_by(Server.name)):
        cells = []
        for g in s.gpus:
            cells.append(f"GPU{g.index}:{'、'.join(busy[g.id]) if g.id in busy else '空闲'}")
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
