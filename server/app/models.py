import json
from datetime import date, datetime

from sqlalchemy import BigInteger, Boolean, Date, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .config import now, settings
from .db import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    open_id: Mapped[str] = mapped_column(String(64), unique=True)
    union_id: Mapped[str | None] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(64))
    avatar_url: Mapped[str | None] = mapped_column(String(512))
    tenant_key: Mapped[str | None] = mapped_column(String(64))  # 飞书组织标识，登录时记录
    linux_username: Mapped[str | None] = mapped_column(String(32), unique=True)
    # 服务器登录密码的 sha512-crypt 哈希（明文从不落库）；为空表示只能用公钥登录
    password_hash: Mapped[str | None] = mapped_column(String(256))
    password_updated_at: Mapped[datetime | None] = mapped_column(DateTime)
    # 是否已完成（或跳过）首次使用引导
    onboarded: Mapped[bool] = mapped_column(Boolean, default=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    ssh_keys: Mapped[list["SSHKey"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", order_by="SSHKey.id"
    )

    @property
    def uid(self) -> int:
        return settings.uid_base + self.id


class SSHKey(Base):
    __tablename__ = "ssh_keys"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String(64))
    public_key: Mapped[str] = mapped_column(Text)
    fingerprint: Mapped[str] = mapped_column(String(128), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    user: Mapped[User] = relationship(back_populates="ssh_keys")


class Server(Base):
    __tablename__ = "servers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    host: Mapped[str] = mapped_column(String(128), default="")
    description: Mapped[str] = mapped_column(String(256), default="")
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    # 明文注册 token：只给管理员在平台上查看 / 复制安装命令用（DB 在私有服务器上）
    token: Mapped[str | None] = mapped_column(String(64))
    hostname: Mapped[str | None] = mapped_column(String(128))
    agent_version: Mapped[str | None] = mapped_column(String(32))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    # Agent 上报：各成员账号在本机的同步状态 {user_id: {...}}、可迁移目录、家目录所在磁盘剩余空间
    accounts_json: Mapped[str] = mapped_column(Text, default="{}")
    candidates_json: Mapped[str] = mapped_column(Text, default="[]")
    # 旧共享账号（如 ubuntu）里可复制给成员的配置、软件、conda 环境
    legacy_json: Mapped[str] = mapped_column(Text, default="[]")
    browse_roots_json: Mapped[str] = mapped_column(Text, default="[]")  # 可以浏览 / 迁移的位置（旧账号家目录、迁移目录）
    home_free: Mapped[int | None] = mapped_column(BigInteger)
    # Agent 上报的整机资源：{"cpu": 12.3, "mem_used": MiB, "mem_total": MiB, "disk_used": MiB, "disk_total": MiB}
    sysinfo_json: Mapped[str] = mapped_column(Text, default="{}")

    gpus: Mapped[list["GPU"]] = relationship(
        back_populates="server", cascade="all, delete-orphan", order_by="GPU.index"
    )

    @property
    def online(self) -> bool:
        if not self.last_seen_at:
            return False
        return (now() - self.last_seen_at).total_seconds() < settings.agent_offline_seconds

    @property
    def accounts(self) -> dict:
        return _loads(self.accounts_json, {})

    @property
    def candidates(self) -> list[dict]:
        return _loads(self.candidates_json, [])

    @property
    def legacy(self) -> list[dict]:
        return _loads(self.legacy_json, [])

    @property
    def browse_roots(self) -> list[str]:
        return _loads(self.browse_roots_json, [])

    @property
    def sysinfo(self) -> dict:
        return _loads(self.sysinfo_json, {})


def _loads(text, default):
    try:
        return json.loads(text or "null") or default
    except ValueError:
        return default


class GPU(Base):
    __tablename__ = "gpus"
    __table_args__ = (UniqueConstraint("server_id", "index"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    server_id: Mapped[int] = mapped_column(ForeignKey("servers.id", ondelete="CASCADE"))
    index: Mapped[int] = mapped_column(Integer)
    uuid: Mapped[str | None] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(64), default="")
    memory_total: Mapped[int | None] = mapped_column(Integer)  # MiB
    memory_used: Mapped[int | None] = mapped_column(Integer)  # MiB
    utilization: Mapped[int | None] = mapped_column(Integer)  # %
    processes_json: Mapped[str] = mapped_column(Text, default="[]")
    updated_at: Mapped[datetime | None] = mapped_column(DateTime)

    server: Mapped[Server] = relationship(back_populates="gpus")

    @property
    def processes(self) -> list[dict]:
        return _loads(self.processes_json, [])


class Reservation(Base):
    __tablename__ = "reservations"
    __table_args__ = (Index("ix_res_gpu_dates", "gpu_id", "start_date", "end_date"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    gpu_id: Mapped[int] = mapped_column(ForeignKey("gpus.id", ondelete="CASCADE"))
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)  # 含当天
    note: Mapped[str] = mapped_column(String(256), default="")
    status: Mapped[str] = mapped_column(String(16), default="active")  # active / cancelled
    auto_renew: Mapped[bool] = mapped_column(Boolean, default=False)
    renew_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)

    user: Mapped[User] = relationship()
    gpu: Mapped[GPU] = relationship()


class MigrationTask(Base):
    """把服务器上已有的目录迁移到成员家目录（由 Agent 执行）。"""

    __tablename__ = "migrations"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    server_id: Mapped[int] = mapped_column(ForeignKey("servers.id", ondelete="CASCADE"))
    # dir：迁移一个文件 / 目录；profile：从旧共享账号复制配置、软件和 conda 环境
    kind: Mapped[str] = mapped_column(String(8), default="dir")
    source: Mapped[str] = mapped_column(String(512))
    # move：移动到 ~/<dest_name>；copy：复制，原文件不动；link：原地保留并改归属，在 ~ 下建快捷方式
    mode: Mapped[str] = mapped_column(String(8), default="move")
    # profile 的选择：{"legacy_user", "configs": [...], "conda": [{"name", "base", "envs"}]}
    options_json: Mapped[str] = mapped_column(Text, default="{}")
    dest_name: Mapped[str] = mapped_column(String(128))
    # pending_approval / queued / running / done / failed / cancelled / rejected
    status: Mapped[str] = mapped_column(String(20), default="queued")
    phase: Mapped[str] = mapped_column(String(32), default="")
    bytes_done: Mapped[int] = mapped_column(BigInteger, default=0)
    bytes_total: Mapped[int] = mapped_column(BigInteger, default=0)
    files_done: Mapped[int] = mapped_column(Integer, default=0)
    files_total: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str] = mapped_column(Text, default="")
    dest_path: Mapped[str] = mapped_column(String(512), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)

    user: Mapped[User] = relationship()
    server: Mapped[Server] = relationship()

    ACTIVE = ("pending_approval", "queued", "running")

    @property
    def options(self) -> dict:
        return _loads(self.options_json, {})

    @property
    def summary(self) -> str:
        """给人看的一句话描述。"""
        if self.kind != "profile":
            how = {"move": "移动", "copy": "复制", "link": "原地保留"}.get(self.mode, self.mode)
            return f"{self.source} → ~/{self.dest_name}（{how}）"
        o = self.options
        parts = [f"{len(o.get('configs') or [])} 项配置"] if o.get("configs") else []
        for c in o.get("conda") or []:
            envs = c.get("envs") or []
            parts.append(f"conda {c.get('name')}" + (f"：{'、'.join(envs)}" if envs else " 本体"))
        return f"从 {o.get('legacy_user')} 复制：" + "；".join(parts)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class Notice(Base):
    """已发送通知的去重记录。"""

    __tablename__ = "notices"

    key: Mapped[str] = mapped_column(String(191), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
