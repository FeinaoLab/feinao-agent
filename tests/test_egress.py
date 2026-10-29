# -*- coding: utf-8 -*-
"""出站管控（egress）测试：策略登记了必须真的接线，否则等于没策略。

要守住四件事：
① enabled=false 完全透传（老部署升级不改行为，一个字节都不多拦）；
② 元数据端点 / 非白名单协议 / 越界端口 / 内网地址，一律拒且理由说得清；
③ spec 里写下的上游 origin 可信——本机回环的上游不会被私网策略误杀；
④ 所有出站请求只能走 egress_open 这一个口：被拒要落审计，响应体要有长度上限。
"""
import json
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "backend"))

for _k in ("FEINAO_QWEN_KEY", "FEINAO_DEEPSEEK_KEY", "FEINAO_HUNYUAN_KEY"):
    os.environ[_k] = ""
os.environ.setdefault("FEINAO_DB", os.path.join(BASE_DIR, "tests", "test_egress.db"))

import ai  # noqa: E402

ai.init_db()

_ORIG_EGRESS = json.loads(json.dumps(ai.SPEC["egress"]))


def _on(**over):
    """把 egress 打开并覆盖若干字段；用例结束由 fixture 还原。
    域名白名单默认置空（空=不限域名）——仓库 spec 现在配了真实供应商域名，
    用例里的 example.com 之类的占位域不该被生产配置误伤；要测白名单的用例自己传。"""
    e = json.loads(json.dumps(_ORIG_EGRESS))
    e["enabled"] = True
    e["allow_domains"] = over.get("allow_domains", [])   # 显式置空，不继承生产白名单
    e.update(over)
    ai.SPEC["egress"] = e
    return e


def _off():
    """关掉出站管控。注意不能只还原文件原值——仓库里的 spec 现在是 enabled=true，
    测「关闭时透传」必须显式置 false，否则用例会随配置漂移。"""
    e = json.loads(json.dumps(_ORIG_EGRESS))
    e["enabled"] = False
    ai.SPEC["egress"] = e


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _restore_egress():
    ai._tok_cache.clear()
    yield
    ai.SPEC["egress"] = json.loads(json.dumps(_ORIG_EGRESS))
    ai._tok_cache.clear()


# ---------------- ① 关闭时透传 ----------------

def test_disabled_passes_everything():
    """enabled=false：连元数据地址都放行——开关没开就不许有行为变化。"""
    _off()
    for url in ("http://169.254.169.254/latest/meta-data/",
                "http://127.0.0.1:9/x",
                "file:///etc/passwd"):
        ok, why = ai.check_egress(url, "test")
        assert ok is True, (url, why)


# ---------------- ② 开关打开后的硬红线 ----------------

def test_metadata_endpoint_denied_even_if_whitelisted():
    """元数据端点是 SSRF 红线：白名单里写了也不认。"""
    _on(allow_domains=["169.254.169.254"], allow_ports=[80])
    ok, why = ai.check_egress("http://169.254.169.254/latest/meta-data/", "test")
    assert ok is False
    assert "元数据" in why


def test_scheme_and_port_rules():
    _on(allow_ports=[80, 443])
    ok, why = ai.check_egress("file:///etc/passwd", "test")
    assert ok is False and "协议" in why

    ok, why = ai.check_egress("http://example.com:8080/x", "test")
    assert ok is False and "端口" in why

    ok, why = ai.check_egress("https://example.com/x", "test", resolve=False)
    assert ok is True, why


def test_domain_whitelist_suffix_match():
    """白名单按后缀匹配：example.com 收 a.example.com，不收 evil-example.com。"""
    _on(allow_domains=["example.com"], allow_ports=[443])
    assert ai.check_egress("https://api.example.com/x", "t", resolve=False)[0] is True
    assert ai.check_egress("https://example.com/x", "t", resolve=False)[0] is True
    ok, why = ai.check_egress("https://evil-example.com/x", "t", resolve=False)
    assert ok is False and "白名单" in why

    _on(allow_domains=["*.example.com"], allow_ports=[443])
    assert ai.check_egress("https://api.example.com/x", "t", resolve=False)[0] is True


def test_private_network_denied():
    """没写进 spec 的内网地址不许出站——哪怕端口在白名单里。"""
    _on(allow_ports=[80, 443, 8060], allow_domains=[])
    ok, why = ai.check_egress("http://192.168.1.10:8060/x", "t")
    assert ok is False and "内网" in why

    ok, why = ai.check_egress("http://127.0.0.1:8060/x", "t")
    assert ok is False and "内网" in why


def test_private_network_check_can_be_relaxed_by_config():
    _on(allow_ports=[8060], deny_private_networks=False)
    ok, why = ai.check_egress("http://127.0.0.1:8060/x", "t")
    assert ok is True, why


# ---------------- ③ 配置里声明的上游可信 ----------------

def test_spec_upstream_origin_is_trusted():
    """kms 上游是 127.0.0.1:8050：配置写下即可信，不被私网/端口规则误杀。"""
    _on(allow_ports=[80, 443], deny_private_networks=True)
    name = next(iter(ai.SPEC["upstreams"]))
    base = ai.SPEC["upstreams"][name]["base"]
    ok, why = ai.check_egress(base + "api/spec", "upstream:%s" % name)
    assert ok is True, (base, why)
    assert "上游" in why


def test_trust_can_be_turned_off():
    _on(allow_ports=[80, 443], trust_upstream_origins=False)
    name = next(iter(ai.SPEC["upstreams"]))
    base = ai.SPEC["upstreams"][name]["base"]
    ok, why = ai.check_egress(base + "api/spec", "t")
    assert ok is False and "端口" in why


# ---------------- ④ 唯一出口：拒绝要留痕、响应要限长 ----------------

class _FakeReq(object):
    def __init__(self, url):
        self.full_url = url


class _FakeResp(object):
    def __init__(self, body):
        self._body = body
        self.read_bytes = []

    def read(self, n=None):
        self.read_bytes.append(n)
        return self._body if n is None else self._body[:n]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _audit_count(action):
    conn = ai.db()
    n = conn.execute("SELECT COUNT(*) c FROM audit WHERE tool=?", (action,)).fetchone()["c"]
    conn.close()
    return n


def test_egress_open_denies_and_audits():
    _on(allow_ports=[80, 443])
    before = _audit_count("egress_denied")
    with pytest.raises(ai.EgressDenied):
        ai.egress_open(_FakeReq("http://169.254.169.254/latest/meta-data/"), purpose="ssrf-test")
    assert _audit_count("egress_denied") == before + 1


def test_egress_open_clamps_timeout(monkeypatch):
    """超时取请求意图与策略上限的较小值——策略是天花板，不是建议。"""
    _on(request_timeout_seconds=5)
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["t"] = timeout
        return _FakeResp(b"{}")

    monkeypatch.setattr(ai.urllib.request, "urlopen", fake_urlopen)
    ai.egress_open(_FakeReq("https://example.com/x"), timeout=30, purpose="t")
    assert seen["t"] == 5

    ai.egress_open(_FakeReq("https://example.com/x"), timeout=2, purpose="t")
    assert seen["t"] == 2


def test_read_capped_limits_body():
    _on(max_response_bytes=10)
    with pytest.raises(ai.EgressDenied):
        ai.read_capped(_FakeResp(b"x" * 11))
    assert ai.read_capped(_FakeResp(b"x" * 10)) == b"x" * 10

    _off()
    assert ai.read_capped(_FakeResp(b"x" * 50)) == b"x" * 50


def test_selftest_is_readable():
    """排障接口要能说清「拦谁放谁」，而不是让人读代码。"""
    _on()
    r = ai.egress_selftest()
    assert r["enabled"] is True
    assert any(c["ok"] is False for c in r["cases"])
    assert any(c["ok"] is True for c in r["cases"])
    assert all("reason" in c for c in r["cases"])


# ---------------- ⑤ 同源不同路径：egress 管不到，必须另设形状校验 ----------------

def test_entity_path_injection_blocked():
    """实体名拼进 URL 路径：`../../admin/delete` 是同源越权，egress 管域名管不到它。"""
    for bad in ("../../admin/delete", "sources?x=1", "a b", "", "x" * 41):
        with pytest.raises(ValueError):
            ai.upstream_rows("bi", bad, "", 10)


def test_safe_url_strips_secrets():
    """审计里记 URL 不能把 query 里的令牌和 userinfo 一起记进去。"""
    assert ai._safe_url("https://a.com/x?token=SECRET") == "https://a.com/x"
    assert ai._safe_url("https://user:pw@a.com/x?t=1") == "https://a.com/x"
    assert ai._safe_url("http://127.0.0.1:8050/kms/api/spec") == "http://127.0.0.1:8050/kms/api/spec"


