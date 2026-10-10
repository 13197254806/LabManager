"""家宽代理配置（deploy/gen-proxy-config.py）：按顺序回退，健康检查打 WebVPN 本身。"""
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("gen_proxy_config", Path(__file__).resolve().parents[2] / "deploy" / "gen-proxy-config.py")
gen = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gen)

NODE = "ss://YWVzLTEyOC1nY206cHc@{host}:{port}?plugin=obfs-local;obfs=http;obfs-host=a.example.com;obfs-uri=/#{name}"


def test_nodes_fall_back_in_order_and_health_check_hits_webvpn():
    nodes = ",".join(NODE.format(host=f"n{i}.example.com", port=1000 + i, name=n) for i, n in enumerate(["CN", "HK", "TW"]))
    out = gen.build_config(nodes, "https://webvpn.example.edu.cn/")
    group = out[out.index("proxy-groups:"):]
    assert group.index("- CN") < group.index("- HK") < group.index("- TW")
    assert 'url: "https://webvpn.example.edu.cn/login"' in group  # 到不了学校的节点也算不通
    assert "expected-status: 200" in group                       # 出口 IP 被 WebVPN 封（403）也算不通
    assert "plugin: obfs" in out and "host: a.example.com" in out
