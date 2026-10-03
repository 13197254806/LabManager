# 实验室平台

实验室 GPU 预约与服务器账号管理，以及飞书机器人。

- **GPU 日历**：飞书登录后，按「显卡 × 日期」查看和预约，点了就生效，显示实时利用率和未预约使用告警
- **自动续约**：到期前 N 小时自动延长 M 天，与他人预约冲突时只延长到冲突前一天，并发飞书卡片通知
- **服务器账号**：每人一个 Linux 账号，可以用平台上设置的密码或登记的公钥登录，只能在有预约的当天登录；用户名可以随时修改，各服务器自动改名；以前的共享账号（ubuntu / ubuntu602）保留给管理员
- **从共享账号迁移**：首次使用时，从以前的共享账号复制 .bashrc / git 等配置和 conda 本体（可选复制旧环境，自动修正路径并自检），并移动或复制自己的项目目录；后台执行，进度实时可见，迁移后文件完全归本人所有
- **校园网接入**：公网服务器自动登录 aTrust 校园网 VPN，经 SSH 反向隧道让内网 GPU 服务器访问平台，并经公网服务器上外网（pip / conda / git）
- **飞书机器人**：新人入职 / 进群时发欢迎卡片，发送预约、续约、迁移通知，支持私聊查询

```
server/          平台（FastAPI + 原生 JS 前端 + SQLite）
  app/main.py      Web 入口
  app/api.py       预约、账号、管理、Agent 同步接口
  app/bot.py       飞书长连接机器人（独立进程）
  app/jobs.py      自动续约、到期提醒（在 web 进程内运行）
  app/tunnel.py    校园网隧道：经 aTrust 的 SOCKS5 SSH 到 GPU 服务器建反向隧道，附带上外网代理（独立进程）
  app/static/      前端页面
agent/           部署在每台 GPU 服务器上（只依赖 Python 3.6+ 标准库）
  lab_agent.py     每分钟同步：上报 nvidia-smi，对齐账号、密码、公钥、登录组和显卡分配；执行文件迁移
  install.sh       安装 Agent，并收紧 sshd 配置
  lab_migrate.py   管理员批量迁移存量文件的命令行工具（成员自助迁移在平台上完成）
  lab-gpus         成员在服务器上查看自己预约的卡
deploy/          docker compose（web / bot / atrust / tunnel / caddy）+ setup-server.sh 一键部署
  atrust/          aTrust 自动登录的启动入口（替换镜像自带的，账号密码从 atrust.env 读）
docs/            用户指南（给同学）、管理员手册（部署与切换流程）
```

部署和 GPU 服务器的切换步骤见 [docs/管理员手册.md](docs/管理员手册.md)，给同学看的使用说明见 [docs/用户指南.md](docs/用户指南.md)。
