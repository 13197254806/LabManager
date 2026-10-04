import os
import sys
import tempfile
from pathlib import Path

# 必须在 import app 之前设置环境变量（配置在 import 时读取）
_tmp = tempfile.mkdtemp(prefix="lab-test-")
os.environ.update(
    DATABASE_URL=f"sqlite:///{_tmp}/test.db",
    DEV_LOGIN="1",
    SECRET_KEY="test-secret",
    BASE_URL="http://testserver",
    TUNNEL_DIR=f"{_tmp}/tunnel",
    MAX_RESERVATION_DAYS="14",
    MAX_ADVANCE_DAYS="30",
    MAX_GPU_SHARERS="1",  # 默认测试按“一张卡同一天只属于一个人”；多人共用的用例单独调整
)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.db import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(autouse=True)
def clean_db():
    from app import models  # noqa: F401

    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield


@pytest.fixture
def db():
    with SessionLocal() as s:
        yield s


def make_client(name: str) -> TestClient:
    c = TestClient(app)
    r = c.post("/auth/dev", json={"name": name})
    assert r.status_code == 200, r.text
    return c


@pytest.fixture
def admin():
    """第一个登录的人是管理员。"""
    c = make_client("admin")
    assert c.put("/api/me/linux-username", json={"username": "adminuser"}).status_code == 200
    return c


@pytest.fixture
def alice(admin):
    c = make_client("alice")
    assert c.put("/api/me/linux-username", json={"username": "alice"}).status_code == 200
    return c


@pytest.fixture
def server(admin):
    """一台在线、带 2 张卡的服务器，返回 (server_json, token)。"""
    r = admin.post("/api/admin/servers", json={"name": "gpu_01", "host": "192.168.1.10"})
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    agent = TestClient(app)
    body = {"hostname": "h", "agent_version": "t", "gpus": [
        {"index": 0, "uuid": "u0", "name": "A100", "memory_total": 100, "memory_used": 1, "utilization": 0},
        {"index": 1, "uuid": "u1", "name": "A100", "memory_total": 100, "memory_used": 1, "utilization": 0},
    ]}
    r = agent.post("/api/agent/sync", json=body, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    return r.json(), token


def agent_headers(token):
    return {"Authorization": f"Bearer {token}"}
