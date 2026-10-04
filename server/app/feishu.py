"""飞书开放平台的最小封装：tenant token、发消息、网页 OAuth 登录。

文档：
- 网页登录 https://open.feishu.cn/document/common-capabilities/sso/web-application-sso/web-app-overview
- 发送消息 https://open.feishu.cn/document/server-docs/im-v1/message/create
"""

import json
import logging
import threading
import time
from urllib.parse import urlencode

import httpx

from .config import settings

log = logging.getLogger("feishu")

_lock = threading.Lock()
_token = ""
_token_expire = 0.0


class FeishuError(Exception):
    pass


def _check(resp: httpx.Response) -> dict:
    try:
        data = resp.json()
    except ValueError:
        raise FeishuError(f"HTTP {resp.status_code}: {resp.text[:200]}")
    if data.get("code", 0) != 0:
        raise FeishuError(f"{data.get('code')}: {data.get('msg') or data.get('error_description')}")
    return data


def tenant_access_token() -> str:
    global _token, _token_expire
    with _lock:
        if _token and time.time() < _token_expire - 300:
            return _token
        resp = httpx.post(
            f"{settings.feishu_open_base}/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": settings.feishu_app_id, "app_secret": settings.feishu_app_secret},
            timeout=10,
        )
        data = _check(resp)
        _token = data["tenant_access_token"]
        _token_expire = time.time() + int(data.get("expire", 7200))
        return _token


def send_message(open_id: str, msg_type: str, content: dict, raise_errors: bool = False) -> bool:
    """以机器人身份私聊某个用户。默认失败只记日志、返回 False；raise_errors=True 时抛出（供测试按钮显示具体原因）。"""
    if not settings.feishu_enabled:
        log.info("[飞书未配置，跳过发送] to=%s %s", open_id, json.dumps(content, ensure_ascii=False)[:300])
        if raise_errors:
            raise FeishuError("服务端未配置飞书应用")
        return False
    try:
        resp = httpx.post(
            f"{settings.feishu_open_base}/open-apis/im/v1/messages",
            params={"receive_id_type": "open_id"},
            headers={"Authorization": f"Bearer {tenant_access_token()}"},
            json={"receive_id": open_id, "msg_type": msg_type, "content": json.dumps(content, ensure_ascii=False)},
            timeout=10,
        )
        _check(resp)
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("发送飞书消息失败 to=%s: %s", open_id, e)
        if raise_errors:
            raise
        return False


def send_text(open_id: str, text: str) -> bool:
    return send_message(open_id, "text", {"text": text})


def send_card(open_id: str, card: dict, raise_errors: bool = False) -> bool:
    return send_message(open_id, "interactive", card, raise_errors=raise_errors)


def build_card(title: str, markdown: str, buttons: list[tuple[str, str]] | None = None, template: str = "blue") -> dict:
    """生成一张简单的消息卡片：标题 + markdown 正文 + 若干链接按钮。"""
    elements: list[dict] = [{"tag": "div", "text": {"tag": "lark_md", "content": markdown}}]
    if buttons:
        elements.append(
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": text},
                        "type": "primary" if i == 0 else "default",
                        "url": url,
                    }
                    for i, (text, url) in enumerate(buttons)
                ],
            }
        )
    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": title}, "template": template},
        "elements": elements,
    }


# ---------------- 网页 OAuth 登录 ----------------


def authorize_url(redirect_uri: str, state: str) -> str:
    query = urlencode(
        {
            "client_id": settings.feishu_app_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "state": state,
        }
    )
    return f"{settings.feishu_accounts_base}/open-apis/authen/v1/authorize?{query}"


def exchange_code(code: str, redirect_uri: str) -> dict:
    """用授权码换 user_access_token，再取用户信息。返回 open_id/union_id/name/avatar_url/tenant_key。"""
    resp = httpx.post(
        f"{settings.feishu_open_base}/open-apis/authen/v2/oauth/token",
        json={
            "grant_type": "authorization_code",
            "client_id": settings.feishu_app_id,
            "client_secret": settings.feishu_app_secret,
            "code": code,
            "redirect_uri": redirect_uri,
        },
        timeout=10,
    )
    token = _check(resp)["access_token"]
    resp = httpx.get(
        f"{settings.feishu_open_base}/open-apis/authen/v1/user_info",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )
    return _check(resp)["data"]
