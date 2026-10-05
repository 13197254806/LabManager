"""飞书机器人 + 定时任务进程：python -m app.bot

- 通过飞书「长连接」接收事件（不需要公网回调地址）
  * 新成员加入企业 / 加入机器人所在的群 → 私聊发送欢迎卡片
  * 用户私聊机器人 → 简单命令（后续可以在 handle_text 里接入自己的 Agent）
（自动续约、到期提醒等定时任务在 web 进程里，见 jobs.py）
"""

import json
import logging
import threading

from sqlalchemy import select

from . import feishu, services
from .config import settings
from .db import SessionLocal, init_db
from .models import User

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bot")

HELP = (
    "我是实验室助手，可以发送：\n"
    "• 状态 —— 查看今天各服务器 GPU 占用\n"
    "• 我的 —— 查看我的 GPU 预约\n"
    "• 迁移 —— 查看我的文件迁移进度\n"
    "• 欢迎 —— 重新获取新人指南\n"
    f"预约 / 续约 / 释放请打开平台：{settings.base_url}"
)


def handle_text(open_id: str, text: str) -> str:
    """私聊消息入口。以后接入自己的 Agent 服务，就在这里转发 text 并返回回复。"""
    text = text.strip()
    with SessionLocal() as db:
        if text in ("状态", "空闲", "status"):
            return services.free_gpus_text(db)
        if text in ("我的", "我的预约", "mine"):
            user = db.scalar(select(User).where(User.open_id == open_id))
            if not user:
                return f"你还没有登录过实验室平台，请先打开 {settings.base_url} 登录。"
            return services.my_reservations_text(db, user)
        if text in ("迁移", "迁移进度", "migration"):
            user = db.scalar(select(User).where(User.open_id == open_id))
            return services.my_migrations_text(db, user) if user else f"请先打开 {settings.base_url} 登录。"
        if text in ("欢迎", "新人指南", "welcome"):
            services.send_welcome(db, open_id, force=True)
            return ""
    return HELP


# ---------------- 事件处理 ----------------


def _as_dict(data) -> dict:
    import lark_oapi as lark

    return json.loads(lark.JSON.marshal(data))


def on_user_created(data):
    ev = _as_dict(data).get("event", {})
    open_id = (ev.get("object") or {}).get("open_id")
    log.info("新成员加入企业: %s", open_id)
    if open_id:
        with SessionLocal() as db:
            services.send_welcome(db, open_id)


def on_chat_member_added(data):
    ev = _as_dict(data).get("event", {})
    chat_id = ev.get("chat_id")
    with SessionLocal() as db:
        allowed = services.welcome_chat_ids(db)  # 管理页「新人欢迎卡片」里设置；留空 = 机器人所在的所有群
    if allowed and chat_id not in allowed:
        return
    for u in ev.get("users") or []:
        open_id = (u.get("user_id") or {}).get("open_id")
        log.info("新成员进群 %s: %s", chat_id, open_id)
        if open_id:
            with SessionLocal() as db:
                services.send_welcome(db, open_id)


def on_message(data):
    ev = _as_dict(data).get("event", {})
    msg = ev.get("message") or {}
    if msg.get("chat_type") != "p2p" or msg.get("message_type") != "text":
        return
    open_id = ((ev.get("sender") or {}).get("sender_id") or {}).get("open_id")
    try:
        text = json.loads(msg.get("content") or "{}").get("text", "")
    except ValueError:
        text = ""
    if not open_id:
        return
    reply = handle_text(open_id, text)
    if reply:
        feishu.send_text(open_id, reply)


def _noop(_data):
    pass


def run_ws():
    import lark_oapi as lark

    handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_contact_user_created_v3(on_user_created)
        .register_p2_im_chat_member_user_added_v1(on_chat_member_added)
        .register_p2_im_message_receive_v1(on_message)
        .register_p2_im_message_message_read_v1(_noop)
        .build()
    )
    client = lark.ws.Client(settings.feishu_app_id, settings.feishu_app_secret, event_handler=handler, log_level=lark.LogLevel.INFO)
    client.start()  # 阻塞，SDK 内部自动重连


def main():
    init_db()
    if settings.feishu_enabled:
        log.info("飞书长连接启动")
        run_ws()
    else:
        log.warning("未配置飞书应用，机器人进程空转")
        threading.Event().wait()


if __name__ == "__main__":
    main()
