import logging
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import feishu, services
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
    user.tenant_key = info.get("tenant_key") or user.tenant_key
    db.commit()
    if not user.enabled:
        raise HTTPException(403, "你的账号已被管理员停用")
    request.session.clear()
    request.session["uid"] = user.id
    return user


def _tenant_allowed(db: Session, info: dict) -> bool:
    """只允许实验室自己的飞书组织登录。
    优先用环境变量 FEISHU_TENANT_KEY；没配置时，以管理员登录时记下的组织为准（「管理 → 规则」里可关闭）。
    还不知道管理员的组织时，只放行已经有账号的人（他们登录后会记下组织），陌生人一律拒绝。"""
    tenant = info.get("tenant_key")
    if settings.feishu_tenant_key:
        return tenant == settings.feishu_tenant_key
    if services.get_setting(db, "restrict_to_admin_tenant") != "1":
        return True
    allowed = {
        t for t in db.scalars(select(User.tenant_key).where(User.is_admin.is_(True), User.enabled.is_(True))) if t
    }
    if allowed:
        return tenant in allowed
    if db.scalar(select(User.id).limit(1)) is None:  # 还没有任何人：第一个登录的人成为管理员
        return True
    return db.scalar(select(User.id).where(User.open_id == info.get("open_id"))) is not None


def _redirect_uri() -> str:
    return f"{settings.base_url}/auth/callback"


@router.get("/auth/login")
def login(request: Request):
    if not settings.feishu_enabled:
        raise HTTPException(500, "服务端未配置 FEISHU_APP_ID / FEISHU_APP_SECRET")
    state = secrets.token_urlsafe(16)
    request.session["oauth_state"] = state
    return RedirectResponse(feishu.authorize_url(_redirect_uri(), state))


@router.get("/auth/qr")
def qr_login(request: Request):
    """扫码登录：页面里的二维码用这个地址（授权页地址，带本次登录的 state），扫码确认后飞书跳回 /auth/callback。"""
    if not settings.feishu_enabled:
        raise HTTPException(500, "服务端未配置 FEISHU_APP_ID / FEISHU_APP_SECRET")
    state = request.session.get("oauth_state") or secrets.token_urlsafe(16)
    request.session["oauth_state"] = state
    return {"goto": feishu.authorize_url(_redirect_uri(), state)}


@router.get("/auth/callback")
def callback(request: Request, code: str = "", state: str = "", db: Session = Depends(get_db)):
    if not code or not state or state != request.session.pop("oauth_state", None):
        return HTMLResponse('登录状态已失效，<a href="/auth/login">请重新登录</a>', status_code=400)
    try:
        info = feishu.exchange_code(code, _redirect_uri())
    except feishu.FeishuError as e:
        log.warning("飞书登录失败: %s", e)
        return HTMLResponse(f'飞书登录失败：{e}。<a href="/auth/login">重试</a>', status_code=400)
    if not _tenant_allowed(db, info):
        log.warning("拒绝登录：%s 来自其他飞书组织（tenant_key=%s）", info.get("name"), info.get("tenant_key"))
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
