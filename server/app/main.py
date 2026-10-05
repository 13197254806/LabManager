import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import api, auth, jobs, services
from .config import settings
from .db import SessionLocal, init_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

STATIC = Path(__file__).parent / "static"
# GPU 服务器上安装 Agent 时从这里下载脚本（都是公开内容，不含密钥）
AGENT_DIR = Path(os.environ.get("AGENT_DIR", Path(__file__).resolve().parents[2] / "agent"))
AGENT_FILES = {"install.sh", "lab_agent.py", "lab-gpus", "lab-gpu.sh", "lab_migrate.py", "chisel"}


@asynccontextmanager
async def lifespan(_app):
    init_db()
    # 把历史上遗留的、首尾相接的同一人同一张卡的预约合并成一条（已合并的数据不受影响，可重复执行）
    with SessionLocal() as db, services.booking_lock:
        merged = services.merge_reservations(db)
        db.commit()
    if merged:
        logging.getLogger("startup").info("已合并 %d 条首尾相接的预约", len(merged))
    with SessionLocal() as db:
        services.sync_runtime(db)
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


class RevalidatingStaticFiles(StaticFiles):
    """静态文件每次都向服务器确认是否有更新（没改动时只返回 304）。
    否则浏览器会按 Last-Modified 启发式缓存 js / css，部署后还在用旧版本。"""

    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


app.mount("/static", RevalidatingStaticFiles(directory=STATIC), name="static")


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/")
def index():
    # 给 js / css 的地址带上文件修改时间：部署后地址变了，浏览器一定会取新文件（包括已缓存了旧版本的）
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    for name in ("style.css", "app.js"):
        html = html.replace(f"/static/{name}\"", f"/static/{name}?v={int((STATIC / name).stat().st_mtime)}\"")
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


@app.get("/agent/{name}")
def agent_file(name: str):
    if name not in AGENT_FILES or not (AGENT_DIR / name).is_file():
        raise HTTPException(404)
    media = "application/octet-stream" if name == "chisel" else "text/plain; charset=utf-8"
    return FileResponse(AGENT_DIR / name, media_type=media)
