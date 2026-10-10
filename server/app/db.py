import os

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings

_is_sqlite = settings.database_url.startswith("sqlite")
if _is_sqlite:
    path = settings.database_url.split("///", 1)[-1]
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)

engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False, "timeout": 30} if _is_sqlite else {},
)

if _is_sqlite:

    @event.listens_for(engine, "connect")
    def _sqlite_pragma(conn, _record):
        cur = conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()


SessionLocal = sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    from . import models  # noqa: F401  注册模型

    Base.metadata.create_all(engine)
    _ensure_columns()


# 轻量级“迁移”：给已存在的表补上新增的列（SQLite 的 create_all 不会改已有表）。
_EXTRA_COLUMNS = {
    "users": {"tenant_key": "VARCHAR(64)"},
    "servers": {"token": "VARCHAR(64)", "sysinfo_json": "TEXT DEFAULT '{}'",
                "browse_roots_json": "TEXT DEFAULT '[]'", "link": "VARCHAR(16)", "current_migration": "INTEGER",
                "ssh_host": "VARCHAR(128) DEFAULT ''"},
    "migrations": {"attempt": "INTEGER DEFAULT 0", "dest_root": "VARCHAR(256) DEFAULT ''"},
}


def _ensure_columns():
    if not _is_sqlite:
        return
    with engine.begin() as conn:
        for table, cols in _EXTRA_COLUMNS.items():
            existing = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")}
            for col, decl in cols.items():
                if col not in existing:
                    conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
