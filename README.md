# 实验室平台

实验室 GPU 预约与服务器账号管理，以及飞书机器人。

- **GPU 日历**：飞书登录后，按「显卡 × 日期」查看和预约，点了就生效，显示实时利用率和未预约使用告警
- **自动续约**：到期前 N 小时自动延长 M 天，与他人预约冲突时只延长到冲突前一天，并发飞书卡片通知
- **服务器账号**：每人一个 Linux 账号，可以用平台上设置的密码或登记的公钥登录，只能在有预约的当天登录；用户名可以随时修改，各服务器自动改名；以前的共享账号（ubuntu / ubuntu602）保留给管理员
- **从共享账号迁移**：首次使用时，从以前的共享账号复制 .bashrc / git 等配置和 conda 本体（可选复制旧环境，自动修正路径并自检），并移动或复制自己的项目目录；后台执行，进度实时可见，迁移后文件完全归本人所有
- **校园网接入**：公网服务器自动登录学校 WebVPN（支持 WebSocket），经家宽代理 + chisel 反向隧道，让内网 GPU 服务器的 Agent 回连平台做管理（云服务器不接管 GPU 外网）。学校封云 IP，故经家宽节点出口；不占用 aTrust 名额，和你电脑上的 aTrust 互不冲突
- **飞书机器人**：新人入职 / 进群时发欢迎卡片，发送预约、续约、迁移通知，支持私聊查询

```
server/          平台（FastAPI + 原生 JS 前端 + SQLite）
  app/main.py      Web 入口
  app/api.py       预约、账号、管理、Agent 同步接口
  app/bot.py       飞书长连接机器人（独立进程）
  app/jobs.py      自动续约、到期提醒（在 web 进程内运行）
  app/tunnel.py    校园网隧道：经家宽代理登录 WebVPN，用 chisel 连 GPU 建反向隧道（独立进程）
  app/webvpn.py    WebVPN 登录客户端：密码加密、滑块验证码自动识别（纯标准库）
  app/static/      前端页面
agent/           部署在每台 GPU 服务器上（只依赖 Python 3.6+ 标准库）
  lab_agent.py     每分钟同步：上报 nvidia-smi，对齐账号、密码、公钥、登录组和显卡分配；执行文件迁移
  install.sh       安装 Agent，并收紧 sshd 配置
  lab_migrate.py   管理员批量迁移存量文件的命令行工具（成员自助迁移在平台上完成）
  lab-gpus         成员在服务器上查看自己预约的卡
deploy/          docker compose（web / bot / tunnel / caddy）+ setup-server.sh 首次部署 + update.sh 更新
tools/           add-gpu.sh：在管理员电脑上首次部署一台 GPU 服务器；webvpn_login_test.sh：本地测 WebVPN 登录
.github/          push 到 master 时自动检查并部署（GitHub Actions）
docs/            用户指南（给同学）、管理员手册（部署与切换流程）
```

部署和 GPU 服务器的切换步骤见 [docs/管理员手册.md](docs/管理员手册.md)，给同学看的使用说明见 [docs/用户指南.md](docs/用户指南.md)。
