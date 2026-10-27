# -*- coding: utf-8 -*-
"""演示自足化的守护测试：clone 之后不改配置就能跑通三个试点。

覆盖三件事：
  1. 内置假上游讲的是和生产一致的 api/login + api/spec + api/<entity> 契约
  2. 种子文档幂等（重复调用不重复灌）
  3. 上游基地址可被环境变量覆盖（演示 → 生产的切换路径）
"""
import json
import os
import sys
import urllib.parse
import urllib.request

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEST_DB = os.path.join(BASE_DIR, "tests", "test_demo.db")
if os.path.exists(TEST_DB):
    os.remove(TEST_DB)
os.environ["FEINAO_DB"] = TEST_DB
for _k in ("FEINAO_QWEN_KEY", "FEINAO_DEEPSEEK_KEY", "FEINAO_HUNYUAN_KEY"):
    os.environ[_k] = ""

sys.path.insert(0, os.path.join(BASE_DIR, "backend"))
import ai  # noqa: E402
import demo  # noqa: E402

PORT = 8098
BASE = "http://127.0.0.1:%d" % PORT


def _req(path, method="GET", body=None, token=None):
    r = urllib.request.Request(BASE + path, method=method)
    if body is not None:
        r.data = json.dumps(body).encode()
        r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(r, timeout=10) as x:
            return x.status, json.loads(x.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8", "ignore") or "{}")


def test_upstream_contract():
    """假上游必须能被真实客户端 _upstream_get 直接消费。"""
    demo.start_upstream(PORT)
    st, j = _req("/bi/api/login", "POST", {"username": "admin", "password": "admin123"})
    assert st == 200 and j["token"]
    tok = j["token"]

    st, j = _req("/bi/api/spec", token=tok)
    assert st == 200
    keys = [e["key"] for e in j["entities"]]
    assert "order" in keys and "customer" in keys

    st, j = _req("/bi/api/order?limit=3", token=tok)
    assert st == 200 and len(j["rows"]) == 3 and j["total"] == len(demo.SYSTEMS["bi"]["rows"]["order"])

    # 关键词过滤：客户名出现在任意字段即命中
    q = urllib.parse.quote("西部能源")
    st, j = _req("/bi/api/order?q=%s" % q, token=tok)
    assert st == 200 and j["rows"] and all(r["customer"] == "西部能源" for r in j["rows"])

    # 无 token 必须 401，否则鉴权路径和真上游不一致
    st, _ = _req("/bi/api/spec")
    assert st == 401

    # 未知系统 / 未知实体
    assert _req("/nope/api/spec", token=tok)[0] == 404
    assert _req("/bi/api/ghost", token=tok)[0] == 404


def test_seed_docs_idempotent():
    """种子文档只灌一次，且能被 RAG 检索命中并带引用。"""
    ai.init_db()
    n1 = demo.seed_docs()
    assert n1 == len(demo.SEED_DOCS)
    assert demo.seed_docs() == 0, "重复调用不应重复灌入"

    hits = ai.rag_search("住宿报销标准", topk=3)
    assert hits, "种子文档必须支撑 RAG 演示"
    assert any("报销" in h["title"] for h in hits)


def test_base_override():
    """演示开关与上游地址覆盖：默认走 demo_base，FEINAO_UPSTREAM_* 优先。"""
    os.environ.pop("FEINAO_DEMO", None)
    assert ai.demo_enabled() is True
    assert ":8099/" in ai._upstream_base("bi")

    os.environ["FEINAO_DEMO"] = "0"
    assert ai.demo_enabled() is False
    assert "8041" in ai._upstream_base("bi"), "关闭演示后必须回到 spec.json 的真实 base"

    os.environ["FEINAO_UPSTREAM_BI"] = "http://10.0.0.9:9000/bi"
    assert ai._upstream_base("bi") == "http://10.0.0.9:9000/bi/", "环境变量优先级最高"

    os.environ.pop("FEINAO_UPSTREAM_BI")
    os.environ.pop("FEINAO_DEMO")
