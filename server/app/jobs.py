"""定时任务：在 web 进程里运行，和网页预约共用同一把预约锁。"""

import logging
import threading
import time

from . import services
from .config import now
from .db import SessionLocal

log = logging.getLogger("jobs")
_started = False


def _loop():
    while True:
        try:
            with SessionLocal() as db:
                n = services.run_auto_renew(db)
                if n:
                    log.info("自动续约 %d 条预约", n)
                if now().hour >= services.get_int_setting(db, "reminder_hour"):
                    n = services.send_expiry_reminders(db)
                    if n:
                        log.info("已发送 %d 条到期提醒", n)
        except Exception:  # noqa: BLE001
            log.exception("定时任务出错")
        time.sleep(60)


def start():
    global _started
    if not _started:
        _started = True
        threading.Thread(target=_loop, daemon=True, name="jobs").start()
