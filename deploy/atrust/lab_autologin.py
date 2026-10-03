"""aTrust 自动登录入口：从环境变量读取配置，调用 aTrustLogin 的 main()（不经过 fire 的参数解析）。"""
import os
import sys

sys.path.insert(0, "/opt/atrust-autologin")
from main import main  # noqa: E402

env = os.environ.get
for k in ("ATRUST_PORTAL", "ATRUST_USERNAME", "ATRUST_PASSWORD"):
    if not env(k):
        sys.exit(f"缺少环境变量 {k}，请在 deploy/.env 里填写")

main(
    portal_address=env("ATRUST_PORTAL"),
    username=env("ATRUST_USERNAME"),
    password=env("ATRUST_PASSWORD"),
    totp_key=env("ATRUST_TOTP_KEY") or None,
    cookie_tid=env("ATRUST_COOKIE_TID") or None,
    cookie_sig=env("ATRUST_COOKIE_SIG") or None,
    keepalive=int(env("ATRUST_KEEPALIVE") or 200),
    data_dir=os.path.expanduser("~/.atrust-data"),
    driver_type="chrome",
    driver_path="/usr/bin/chromedriver",
    browser_path="/usr/bin/chromium",
    interactive=True,  # 遇到图形验证码时等待：VNC 里输入后，docker attach lab-atrust 按回车
    wait_atrust=True,
    container_mode=True,
)
