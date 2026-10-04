import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import api, auth, jobs
from .config import settings
from .db import init_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

STATIC = Path(__file__).parent / "static"
# GPU 服务器上安装 Agent 时从这里下载脚本（都是公开内容，不含密钥）
AGENT_DIR = Path(os.environ.get("AGENT_DIR", Path(__file__).resolve().parents[2] / "agent"))
AGENT_FILES = {"install.sh", "lab_agent.py", "lab-gpus", "lab-gpu.sh", "lab_migrate.py", "chisel"}


@asynccontextmanager
async def lifespan(_app):
    init_db()
    jobs.start()  # 自动续约、到期提醒
    yield


app = FastAPI(title=f"{settings.lab_name}平台", docs_url=None, redoc_url=None, lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    session_cookie="lab_session",
    max_age=30 * 24 * 3600,
    same_site="lax",
    https_only=settings.base_url.startswith("https://"),
)
app.include_router(auth.router)
app.include_router(api.router)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/agent/{name}")
def agent_file(name: str):
    if name not in AGENT_FILES or not (AGENT_DIR / name).is_file():
        raise HTTPException(404)
    media = "application/octet-stream" if name == "chisel" else "text/plain; charset=utf-8"
    return FileResponse(AGENT_DIR / name, media_type=media)
