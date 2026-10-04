# 实验室平台

GPU 预约与服务器账号管理，带飞书机器人。成员用飞书登录，在网页上预约显卡、设置自己的 Linux 账号；GPU 服务器上的 Agent 把预约、账号、密码、公钥同步到本机。

主要功能：

- **GPU 日历**：按月显示「显卡 × 日期」，点格子预约；每张卡每天最多 N 人共用；显示实时利用率、占用进程、CPU / 内存 / 磁盘和各用户磁盘占用；没预约却在用卡会告警
- **临时使用 / 长期使用**：长期使用不填日期，从今天起预约一个周期，到期前自动延长，并发飞书通知
- **服务器账号**：每人一个 Linux 账号（密码 / 公钥由平台统一下发），用户名可随时改，各服务器自动改名；是否需要预约才能登录由管理员开关控制
- **迁移共享账号**：从旧共享账号复制配置、conda，并通过文件浏览器选择个人文件移动 / 复制到自己的家目录，后台执行
- **校园网接入**：公网服务器经家宽代理登录学校 WebVPN，用 chisel 反向隧道连到内网 GPU
- **飞书机器人**：新人欢迎卡片，预约 / 到期 / 迁移通知，私聊查询

## 代码结构

```
.
├── server/                      平台（FastAPI + 原生 JS 前端 + SQLite）
│   ├── Dockerfile               web / bot / tunnel 共用同一个镜像
│   ├── requirements.txt
│   ├── app/
│   │   ├── main.py              Web 入口：挂载接口和静态文件，提供 /agent/* 下载，启动时合并存量预约、启动定时任务
│   │   ├── api.py               全部 HTTP 接口：预约、账号、迁移、管理页、Agent 同步、文件浏览转发
│   │   ├── services.py          业务规则：预约容量 / 合并 / 长期使用 / 自动延长、账号期望状态、飞书通知文案、系统设置
│   │   ├── models.py            数据表：User、SSHKey、Server、GPU、Reservation、MigrationTask、Setting、Notice
│   │   ├── db.py                SQLite 连接、建表、给已有表补字段（_EXTRA_COLUMNS）
│   │   ├── config.py            环境变量配置、时区、当前日期
│   │   ├── auth.py              飞书 OAuth 登录、飞书组织校验、会话
│   │   ├── feishu.py            飞书开放平台封装：令牌、发消息 / 卡片、错误码提示
│   │   ├── bot.py               飞书长连接机器人（独立进程）：欢迎卡片、私聊命令
│   │   ├── jobs.py              定时线程（web 进程内）：长期使用自动延长、到期提醒
│   │   ├── browse.py            网页文件浏览 ↔ Agent 的请求转发（长轮询，内存队列）
│   │   ├── tunnel.py            校园网隧道（独立进程）：登录 WebVPN，经家宽代理用 chisel 连每台 GPU
│   │   ├── webvpn.py            WebVPN 登录客户端：密码加密、滑块验证码识别（纯标准库）
│   │   └── static/
│   │       ├── index.html       单页应用骨架：日历、我的账号、管理页、各弹窗
│   │       ├── app.js           前端逻辑
│   │       ├── style.css        样式（含深色模式）
│   │       └── logo.png
│   └── tests/                   pytest：预约规则、Agent 接口、文件浏览、登录限制、Agent 同步循环
│       ├── conftest.py
│       ├── test_reservations.py
│       ├── test_agent_api.py
│       └── test_agent_once.py
│
├── agent/                       部署在每台 GPU 服务器上（只依赖 Python 3.6+ 标准库）；也由平台经 /agent/* 提供下载
│   ├── lab_agent.py             Agent 本体：上报显卡 / 系统信息，对齐账号、密码、公钥、登录组、显卡分配，执行迁移，响应文件浏览
│   ├── install.sh               安装 / 升级 Agent、chisel 服务端，收紧 sshd；也负责卸载
│   ├── lab-gpus                 成员在服务器上查看 / 刷新自己预约的显卡
│   ├── lab-gpu.sh               登录时按预约设置 CUDA_VISIBLE_DEVICES（装到 /etc/profile.d）
│   └── lab_migrate.py           管理员批量迁移存量数据的命令行工具（成员自助迁移在网页上完成）
│
├── deploy/                      公网服务器部署（Docker Compose）
│   ├── docker-compose.yml       服务：web、bot、tunnel、proxy、caddy
│   ├── .env.example             全部配置项（复制为 .env 填写）
│   ├── setup-server.sh          首次部署：装 Docker、生成密钥、注册 .env 自动生效的 systemd 服务
│   ├── apply-config.sh          .env 保存后自动校验并只重建有变化的容器
│   ├── update.sh                用当前代码重新构建并重启（自动部署调用）
│   ├── gen-proxy-config.py      把 WEBVPN_PROXY_NODES 的 ss:// 节点转成 mihomo 配置
│   └── Caddyfile                反向代理和 HTTPS
│
├── tools/
│   ├── add-gpu.sh               备用：在管理员电脑上把 Agent 推到一台 GPU 并安装
│   └── webvpn_login_test.sh     在本机测试 WebVPN 自动登录
│
├── docs/
│   ├── 管理员手册.md             架构、部署、校园网接入、接入 GPU、迁移、运维、开发
│   └── 用户指南.md               给同学：登录、预约、使用显卡、迁移文件、常见问题
│
└── .github/workflows/deploy.yml push 到 master：语法检查 + 单元测试 + 冒烟测试，通过后 rsync 到服务器并重启
```

运行时生成、不进 git 的内容：`deploy/.env`（全部配置和密钥）、`deploy/data/`（`lab.db`、隧道状态、代理配置、`config-apply.log`）、`agent/chisel`（隧道二进制，构建镜像时下载）。

## 数据流

```
成员浏览器 ──▶ caddy ──▶ web ◀── bot（飞书事件）
                          ▲
GPU 上 lab-agent ─ 127.0.0.1:18765 ─ chisel 反向隧道 ─ WebVPN ─ 家宽代理 ─ tunnel（公网服务器）
```

Agent 主动连平台（GPU 在内网，公网访问不到）：上报状态、取回「本机此刻应该是什么样」并对齐；成员在网页上选文件时，平台通过 Agent 的长轮询请求实时列目录。

## 快速开始

本地开发：

```bash
cd server && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
DEV_LOGIN=1 .venv/bin/uvicorn app.main:app --reload    # 免飞书登录，输入任意名字（线上必须关闭）
.venv/bin/python -m pytest tests -q                    # 单元测试
```

部署和运维见 [docs/管理员手册.md](docs/管理员手册.md)，成员使用说明见 [docs/用户指南.md](docs/用户指南.md)。
