#!/usr/bin/env python3
"""把 .env 里的 WEBVPN_PROXY_NODES（逗号分隔的 ss:// 链接）转成 mihomo 配置。

学校 WebVPN 封了云服务器 IP，隧道要经家宽代理出口访问。这里用 mihomo 把 ss 家宽节点
变成本机一个 HTTP/SOCKS 代理（mixed-port 7891），隧道的 WebVPN 流量都走它。
多个节点做成 fallback：第一个（建议填家宽）不通自动切下一个。健康检查打的是
generate_204，不碰 WebVPN。

用法：WEBVPN_PROXY_NODES='ss://...,ss://...' python3 gen-proxy-config.py > config.yaml
    或  python3 gen-proxy-config.py <.env 路径> <输出路径>
只用标准库。
"""
import base64
import os
import sys
import urllib.parse


def _b64(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def parse_ss(url, idx):
    """解析 SIP002 的 ss:// 链接 → mihomo proxy dict。"""
    assert url.startswith("ss://"), "只支持 ss:// 链接"
    body = url[5:]
    frag = ""
    if "#" in body:
        body, frag = body.split("#", 1)
    name = urllib.parse.unquote(frag) or f"node{idx}"
    query = ""
    if "?" in body:
        body, query = body.split("?", 1)
    # body = base64(method:password)@host:port  （也兼容整体 base64）
    if "@" not in body:
        body = _b64(body).decode()
    userinfo, hostport = body.rsplit("@", 1)
    if ":" not in userinfo:
        userinfo = _b64(userinfo).decode()
    method, password = userinfo.split(":", 1)
    host, port = hostport.rsplit(":", 1)
    proxy = {"name": name, "type": "ss", "server": host, "port": int(port),
             "cipher": method, "password": password, "udp": True}
    opts = dict(p.split("=", 1) for p in query.split(";") if "=" in p) if query else {}
    plugin = opts.get("plugin", "")
    if plugin.startswith("obfs") or opts.get("obfs"):
        proxy["plugin"] = "obfs"
        proxy["plugin-opts"] = {"mode": opts.get("obfs", "http"),
                                "host": opts.get("obfs-host", "")}
    return proxy, name


def yaml_dump(obj, indent=0):
    """极简 YAML 输出（够用：dict / list / 标量），避免引入 PyYAML 依赖。"""
    pad = "  " * indent
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                out.append(f"{pad}{k}:")
                out.append(yaml_dump(v, indent + 1))
            else:
                out.append(f"{pad}{k}: {_scalar(v)}")
    elif isinstance(obj, list):
        for v in obj:
            if isinstance(v, dict):
                inner = yaml_dump(v, indent + 1).lstrip()
                out.append(f"{pad}- {inner}")
            else:
                out.append(f"{pad}- {_scalar(v)}")
    return "\n".join(out)


def _scalar(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    if s == "" or any(c in s for c in ":#{}[],&*?|<>=!%@`\"'") or s != s.strip():
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return s


def build_config(nodes_str):
    urls = [u.strip() for u in nodes_str.split(",") if u.strip()]
    if not urls:
        sys.exit("WEBVPN_PROXY_NODES 为空：请在 .env 里填至少一个 ss:// 家宽节点")
    proxies, names = [], []
    for i, u in enumerate(urls, 1):
        p, n = parse_ss(u, i)
        # 名字去重
        base = n
        k = 2
        while n in names:
            n = f"{base}-{k}"
            k += 1
        p["name"] = n
        proxies.append(p)
        names.append(n)
    cfg = {
        "mixed-port": 7891,
        "allow-lan": True,
        "bind-address": "*",
        "mode": "rule",
        "log-level": "warning",
        "dns": {"enable": True, "nameserver": ["223.5.5.5", "1.1.1.1"]},
        "proxies": proxies,
        "proxy-groups": [{
            "name": "PROXY", "type": "fallback", "proxies": names,
            "url": "http://www.gstatic.com/generate_204", "interval": 300,
        }],
        "rules": ["MATCH,PROXY"],
    }
    return yaml_dump(cfg) + "\n"


def read_env_var(env_path, key):
    try:
        for line in open(env_path):
            if line.startswith(key + "="):
                v = line.split("=", 1)[1].strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
                    v = v[1:-1]
                return v
    except OSError:
        pass
    return ""


def main():
    if len(sys.argv) >= 2:
        nodes = read_env_var(sys.argv[1], "WEBVPN_PROXY_NODES")
        out = build_config(nodes)
        if len(sys.argv) >= 3:
            os.makedirs(os.path.dirname(sys.argv[2]) or ".", exist_ok=True)
            with open(sys.argv[2], "w") as f:
                f.write(out)
            os.chmod(sys.argv[2], 0o600)
        else:
            sys.stdout.write(out)
    else:
        sys.stdout.write(build_config(os.environ.get("WEBVPN_PROXY_NODES", "")))


if __name__ == "__main__":
    main()
