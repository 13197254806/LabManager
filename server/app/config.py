import os
from datetime import date, datetime
from zoneinfo import ZoneInfo


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _list(name: str) -> list[str]:
    return [x.strip() for x in _env(name).split(",") if x.strip()]


class Settings:
    lab_name = _env("LAB_NAME", "实验室")
    base_url = _env("BASE_URL", "http://localhost:8000").rstrip("/")
    secret_key = _env("SECRET_KEY", "dev-secret-change-me")
    database_url = _env("DATABASE_URL", "sqlite:///./data/lab.db")
    tz = ZoneInfo(_env("TZ_NAME", "Asia/Shanghai"))

    # 飞书自建应用
    feishu_app_id = _env("FEISHU_APP_ID")
    feishu_app_secret = _env("FEISHU_APP_SECRET")
    feishu_open_base = _env("FEISHU_OPEN_BASE", "https://open.feishu.cn")
    feishu_accounts_base = _env("FEISHU_ACCOUNTS_BASE", "https://accounts.feishu.cn")
    # 扫码登录的授权入口（二维码组件只认这个域名下的 /suite/passport/oauth/authorize，用 accounts 的地址会报 4401）
    feishu_passport_base = _env("FEISHU_PASSPORT_BASE", "https://passport.feishu.cn")
    # 只允许该企业的成员登录（可选，留空不校验）
    feishu_tenant_key = _env("FEISHU_TENANT_KEY")
    # 只对这些群的新成员发欢迎卡片（留空则机器人所在的所有群都发）
    welcome_chat_ids = _list("WELCOME_CHAT_IDS")

    # 本地开发用：免飞书登录。线上务必为 0
    dev_login = _env("DEV_LOGIN", "0") == "1"

    # 预约规则（管理员不受限）
    max_reservation_days = int(_env("MAX_RESERVATION_DAYS", "14"))
    max_advance_days = int(_env("MAX_ADVANCE_DAYS", "30"))
    # 同一张 GPU 同一天最多几个人共用
    max_gpu_sharers = max(1, int(_env("MAX_GPU_SHARERS", "3")))
    # 每天几点提醒“今天到期”
    reminder_hour = int(_env("REMINDER_HOUR", "20"))

    # Linux UID = UID_BASE + 用户 id，保证多台服务器上 UID 一致
    uid_base = int(_env("UID_BASE", "10000"))
    # Agent 超过这么多秒没上报就视为离线
    agent_offline_seconds = int(_env("AGENT_OFFLINE_SECONDS", "180"))

    # 校园网隧道（tunnel 服务，WebVPN + chisel）：配置了 TUNNEL_TARGETS 时，GPU 服务器经隧道访问平台
    tunnel_targets = [t.split("=", 1)[0].strip() for t in _list("TUNNEL_TARGETS") if "=" in t]
    tunnel_status_file = _env("TUNNEL_DIR", "/data/tunnel") + "/status.json"
    # 首次部署 GPU 服务器（Mac 上的 bootstrap 脚本经 /agent/bootstrap-config 读取这些值）
    chisel_port = int(_env("CHISEL_PORT", "9000"))
    chisel_secret = _env("CHISEL_SECRET")
    chisel_allow = _env("CHISEL_ALLOW", "59.69.127.0/24")
    remote_platform_port = int(_env("REMOTE_PLATFORM_PORT", "18765"))

    @property
    def feishu_enabled(self) -> bool:
        return bool(self.feishu_app_id and self.feishu_app_secret)


settings = Settings()


def now() -> datetime:
    """本地时区的 naive datetime（整个实验室只用一个时区，存库时不带时区）。"""
    return datetime.now(settings.tz).replace(tzinfo=None, microsecond=0)


def today() -> date:
    return now().date()
