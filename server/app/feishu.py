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


# 常见的配置类错误：告诉管理员去飞书开放平台改什么
ERROR_HINTS = {
    230006: "应用没有开启机器人能力：到飞书开放平台 → 该应用 → 添加应用能力 → 机器人，开启后在「版本管理与发布」里创建新版本并发布，审核通过后才生效",
    230013: "该用户不在机器人的可用范围内：到飞书开放平台 → 该应用 → 版本管理与发布，把可用范围改成全部成员或包含此人",
    99991672: "应用缺少发消息权限：到飞书开放平台 → 权限管理，开通 im:message:send_as_bot 后发布新版本",
    230002: "机器人不在这个群里：打开飞书群设置 → 群机器人 → 添加机器人，把本应用的机器人加进群",
}


def _check(resp: httpx.Response, hints: dict | None = None) -> dict:
    try:
        data = resp.json()
    except ValueError:
        raise FeishuError(f"HTTP {resp.status_code}: {resp.text[:200]}")
    if data.get("code", 0) != 0:
        code = data.get("code")
        hint = (hints or {}).get(code) or ERROR_HINTS.get(code)
        raise FeishuError(f"{code}: {data.get('msg') or data.get('error_description')}" + (f"（{hint}）" if hint else ""))
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


def send_message(receive_id: str, msg_type: str, content: dict, raise_errors: bool = False, id_type: str = "open_id") -> bool:
    """以机器人身份发消息：默认私聊某个用户（open_id），id_type="chat_id" 时发到群里。
    默认失败只记日志、返回 False；raise_errors=True 时抛出（供测试按钮显示具体原因）。"""
    if not settings.feishu_enabled:
        log.info("[飞书未配置，跳过发送] to=%s %s", receive_id, json.dumps(content, ensure_ascii=False)[:300])
        if raise_errors:
            raise FeishuError("服务端未配置飞书应用")
        return False
    try:
        resp = httpx.post(
            f"{settings.feishu_open_base}/open-apis/im/v1/messages",
            params={"receive_id_type": id_type},
            headers={"Authorization": f"Bearer {tenant_access_token()}"},
            json={"receive_id": receive_id, "msg_type": msg_type, "content": json.dumps(content, ensure_ascii=False)},
            timeout=10,
        )
        _check(resp)
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("发送飞书消息失败 to=%s: %s", receive_id, e)
        if raise_errors:
            raise
        return False


def send_text(open_id: str, text: str) -> bool:
    return send_message(open_id, "text", {"text": text})


def send_card(open_id: str, card: dict, raise_errors: bool = False) -> bool:
    return send_message(open_id, "interactive", card, raise_errors=raise_errors)


def send_chat_card(chat_id: str, card: dict) -> bool:
    """往群里发卡片（机器人要在群里）。失败直接抛出，由调用方记录原因。"""
    return send_message(chat_id, "interactive", card, raise_errors=True, id_type="chat_id")


def list_bot_chats() -> list[dict]:
    """机器人所在的群：[{chat_id, name, avatar}]。需要应用开通「获取群组信息」权限。"""
    if not settings.feishu_enabled:
        raise FeishuError("服务端未配置飞书应用")
    hints = {99991672: "应用缺少读取群列表的权限：到飞书开放平台 → 权限管理，开通 im:chat:readonly（获取群组信息）后发布新版本；也可以直接填写群的 chat_id"}
    chats, page = [], ""
    for _ in range(20):
        resp = httpx.get(
            f"{settings.feishu_open_base}/open-apis/im/v1/chats",
            params={"page_size": 100, **({"page_token": page} if page else {})},
            headers={"Authorization": f"Bearer {tenant_access_token()}"},
            timeout=10,
        )
        data = _check(resp, hints).get("data") or {}
        chats += [{"chat_id": c["chat_id"], "name": c.get("name") or "", "avatar": c.get("avatar") or ""}
                  for c in data.get("items") or [] if c.get("chat_id")]
        page = data.get("page_token") or ""
        if not data.get("has_more") or not page:
            break
    return chats


def get_chat(chat_id: str) -> dict:
    """某个群的名称和头像（机器人要在群里）：{name, avatar}。"""
    resp = httpx.get(
        f"{settings.feishu_open_base}/open-apis/im/v1/chats/{chat_id}",
        headers={"Authorization": f"Bearer {tenant_access_token()}"},
        timeout=10,
    )
    data = _check(resp).get("data") or {}
    return {"name": data.get("name") or "", "avatar": data.get("avatar") or ""}


def list_chat_members(chat_id: str) -> list[dict]:
    """群成员（不含机器人）：[{open_id, name}]。需要「获取群组信息」权限，机器人要在群里。"""
    members, page = [], ""
    for _ in range(50):
        resp = httpx.get(
            f"{settings.feishu_open_base}/open-apis/im/v1/chats/{chat_id}/members",
            params={"member_id_type": "open_id", "page_size": 100, **({"page_token": page} if page else {})},
            headers={"Authorization": f"Bearer {tenant_access_token()}"},
            timeout=10,
        )
        data = _check(resp).get("data") or {}
        members += [{"open_id": m["member_id"], "name": m.get("name") or ""}
                    for m in data.get("items") or [] if m.get("member_id")]
        page = data.get("page_token") or ""
        if not data.get("has_more") or not page:
            break
    return members


def user_avatars(open_ids: list[str]) -> dict[str, str]:
    """成员头像 {open_id: 地址}。需要通讯录权限且成员在应用的通讯录范围内；拿不到的不返回。"""
    out: dict[str, str] = {}
    for i in range(0, len(open_ids), 50):
        resp = httpx.get(
            f"{settings.feishu_open_base}/open-apis/contact/v3/users/batch",
            params=[("user_id_type", "open_id")] + [("user_ids", x) for x in open_ids[i:i + 50]],
            headers={"Authorization": f"Bearer {tenant_access_token()}"},
            timeout=10,
        )
        for u in (_check(resp).get("data") or {}).get("items") or []:
            av = u.get("avatar") or {}
            url = av.get("avatar_240") or av.get("avatar_72") or ""
            if u.get("open_id") and url.startswith("https://"):
                out[u["open_id"]] = url
    return out


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


def qr_authorize_url(redirect_uri: str, state: str) -> str:
    """扫码登录用的授权地址（给飞书二维码组件当 goto）：扫码确认后带 tmp_code 访问它，再跳回 redirect_uri。"""
    query = urlencode(
        {
            "client_id": settings.feishu_app_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "state": state,
        }
    )
    return f"{settings.feishu_passport_base}/suite/passport/oauth/authorize?{query}"


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
