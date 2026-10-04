#!/usr/bin/env bash
# 在自己的 Mac 上测试 WebVPN 自动登录（含滑块验证码识别）。会用你的学号密码真实登录一次。
#
#   bash tools/webvpn_login_test.sh <学号>
#
# 只用系统自带的 python3，不需要安装任何依赖。
# 密码在运行时输入、不回显，也不会发到校园网以外的地方。登录成功只打印 Cookie 的名称和长度，不打印值。
# 账号或密码错误时会立即停止（避免把账号试到锁定）。滑块失败只会换一张新图重试，不消耗密码尝试次数。
set -euo pipefail
cd "$(dirname "$0")/.."

USER_ARG="${1:-}"
[ -n "$USER_ARG" ] || { echo "用法：bash tools/webvpn_login_test.sh <学号>"; exit 1; }
PY=/usr/bin/python3
[ -x "$PY" ] || PY=python3

echo "==> 先做离线 AES 自检"
( cd server && "$PY" -m app.webvpn --self-test )

echo "==> 开始登录测试（直连校园网，走你本机的 aTrust）"
cd server && exec "$PY" -m app.webvpn --portal https://webvpn.whut.edu.cn --user "$USER_ARG"
