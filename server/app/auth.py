import logging
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import feishu
from .config import settings
from .db import get_db
from .models import User

log = logging.getLogger("auth")
router = APIRouter()


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    uid = request.session.get("uid")
    user = db.get(User, uid) if uid else None
    if not user or not user.enabled:
        raise HTTPException(401, "请先登录")
    return user


def admin_user(user: User = Depends(current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(403, "需要管理员权限")
    return user


def _login(request: Request, db: Session, info: dict) -> User:
    user = db.scalar(select(User).where(User.open_id == info["open_id"]))
    if not user:
        user = User(open_id=info["open_id"], name=info.get("name") or "未命名")
        db.add(user)
        # 系统里还没有管理员时，第一个登录的人成为管理员
        if not db.scalar(select(User).where(User.is_admin.is_(True))):
            user.is_admin = True
    user.name = info.get("name") or user.name
    user.union_id = info.get("union_id") or user.union_id
    user.avatar_url = info.get("avatar_url") or user.avatar_url
    db.commit()
    if not user.enabled:
        raise HTTPException(403, "你的账号已被管理员停用")
    request.session.clear()
    request.session["uid"] = user.id
    return user


def _redirect_uri() -> str:
    return f"{settings.base_url}/auth/callback"


@router.get("/auth/login")
def login(request: Request):
    if not settings.feishu_enabled:
        raise HTTPException(500, "服务端未配置 FEISHU_APP_ID / FEISHU_APP_SECRET")
    state = secrets.token_urlsafe(16)
    request.session["oauth_state"] = state
    return RedirectResponse(feishu.authorize_url(_redirect_uri(), state))


@router.get("/auth/callback")
def callback(request: Request, code: str = "", state: str = "", db: Session = Depends(get_db)):
    if not code or not state or state != request.session.pop("oauth_state", None):
        return HTMLResponse('登录状态已失效，<a href="/auth/login">请重新登录</a>', status_code=400)
    try:
        info = feishu.exchange_code(code, _redirect_uri())
    except feishu.FeishuError as e:
        log.warning("飞书登录失败: %s", e)
        return HTMLResponse(f'飞书登录失败：{e}。<a href="/auth/login">重试</a>', status_code=400)
    if settings.feishu_tenant_key and info.get("tenant_key") != settings.feishu_tenant_key:
        return HTMLResponse("仅限本实验室飞书组织的成员使用", status_code=403)
    _login(request, db, info)
    return RedirectResponse("/")


@router.post("/auth/logout")
def logout(request: Request):
    request.session.clear()
    return {"ok": True}


@router.post("/auth/dev")
def dev_login(request: Request, payload: dict, db: Session = Depends(get_db)):
    """本地开发用的免密登录，仅在 DEV_LOGIN=1 时可用。"""
    if not settings.dev_login:
        raise HTTPException(404)
    name = str(payload.get("name") or "").strip()[:32]
    if not name:
        raise HTTPException(400, "name 不能为空")
    _login(request, db, {"open_id": f"dev_{name}", "name": name})
    return {"ok": True}
