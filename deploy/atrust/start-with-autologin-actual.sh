#!/bin/bash
# 替换 kenvix/docker-atrust-autologin 自带的同名脚本：
# 原脚本把 $ATRUST_OPTS 不加引号展开，纯数字账号会被当成整数、密码里有空格等字符会出错。
# 这里改为由 lab_autologin.py 直接从环境变量读取账号密码。
export DISPLAY=":1"
while ! xset q >/dev/null 2>&1; do
    echo "Waiting for X11 to start..."
    sleep 3
done
while true; do
    python3 /opt/lab_autologin.py && break
    echo "Autologin crashed. Restarting in 10s..."
    sleep 10
done
