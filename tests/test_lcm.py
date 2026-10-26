# -*- coding: utf-8 -*-
"""LCM 无损上下文 · 单元 + 接口测试。

覆盖：原文全留、压缩建节点、上下文预算受控、早期事实可召回、回溯排除当前提问、HTTP 接口。
"""
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "backend"))

# 独立库：必须先设环境变量再 import ai
os.environ["FEINAO_DB"] = os.path.join(tempfile.gettempdir(), "feinao_lcm_test_%d.db" % os.getpid())

import ai    # noqa: E402
import lcm   # noqa: E402

FACTS = [
    "客户是恒启制造，项目代号 HX-2026。",
    "项目总预算 380 万，付款比例按 4:4:2 走。",
    "对接人是王振华（王工），手机尾号 8821。",
    "上线时间定在 11 月 20 日。",
    "机房在深圳坪山，走客户自有机房。",
    "验收线定在 85% 准确率，评测集 200 题。",
]
FILLER = "日常推进：跟客户同步了联调进度，没有新问题，明天继续。"


def _conv(n_filler=30):
    o = lcm.LCM(user="admin", title="lcm-test")
    for f in FACTS:
        o.append("user", f)
    for _ in range(n_filler):
        o.append("user", FILLER)
    return o


def test_raw_messages_never_lost():
    o = _conv(10)
    o.compress(keep_recent=4)
    raw = o.messages()
    assert len(raw) == len(FACTS) + 10, "原文必须一条不丢"
    assert any("380" in m["content"] for m in raw)


def test_compress_creates_dag_nodes():
    o = _conv(30)
    created = o.compress(keep_recent=4, chunk=5)
    assert created, "应生成摘要节点"
    nodes = o.nodes()
    assert nodes and all(n["level"] >= 1 for n in nodes)
    # 节点覆盖范围不应超出原文
    assert max(n["to_seq"] for n in nodes) <= len(o.messages())


def test_context_respects_budget():
    o = _conv(60)
    ctx = o.build_context(keep_recent=6, budget=600)
    assert lcm.msgs_tokens(ctx) <= 600, "上下文不得超过预算"
    assert ctx, "上下文不能为空"


def test_context_recalls_early_fact():
    o = _conv(40)
    ctx = o.build_context(keep_recent=6, budget=1500)
    joined = "\n".join(m["content"] for m in ctx)
    # 早期事实要么在摘要里，要么能回溯到
    hit = ("380" in joined) or any("380" in d["content"] for d in o.retrieve("总预算多少？"))
    assert hit, "早期事实必须能找回来"


def test_retrieve_excludes_current_question():
    o = _conv(5)
    seq = o.turn("机房在哪个城市？")
    hits = o.retrieve("机房在哪个城市？", 3, exclude_seq=seq)
    assert all(h["seq"] != seq for h in hits), "不能把自己检索出来"
    assert hits and "坪山" in hits[0]["content"]


def test_ask_end_to_end_answers_old_fact():
    o = _conv(40)
    out = o.ask("机房在哪个城市？", keep_recent=6, budget=1500)
    assert "坪山" in out["answer"], "长会话下仍应答出早期事实：%s" % out["answer"]
    assert out["detail_back"], "应触发原文回溯"
    assert out["context_tokens"] > 0


def test_baseline_truncation_would_miss():
    """对照：只留最近 6 轮，注定答不出早期事实。"""
    o = _conv(40)
    recent = o.messages()[-6:]
    ctx = [{"role": "system", "content": "[PILOT:lcm]\n" + "\n".join(m["content"] for m in recent)}]
    ctx.append({"role": "user", "content": "机房在哪个城市？"})
    ans, _ = ai.llm_chat(ctx, "admin")
    assert "坪山" not in ans, "截断基线不应答出"


# ---------------- HTTP 接口 ----------------
PORT = 8097
BASE = "http://127.0.0.1:%d" % PORT


def _req(path, method="GET", body=None, token=None):
    r = urllib.request.Request(BASE + path, method=method)
    if body is not None:
        r.data = json.dumps(body).encode()
        r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(r, timeout=30) as x:
            return x.status, json.loads(x.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8", "ignore") or "{}")


def test_http_lcm_routes():
    env = dict(os.environ, FEINAO_PORT=str(PORT),
               FEINAO_DB=os.path.join(tempfile.gettempdir(), "feinao_lcm_http_%d.db" % os.getpid()))
    proc = subprocess.Popen([sys.executable, "server.py"],
                            cwd=os.path.join(BASE_DIR, "backend"), env=env)
    try:
        st = None
        for _ in range(20):
            time.sleep(0.5)
            try:
                st, _j = _req("/api/health")
                if st == 200:
                    break
            except Exception:
                continue
        assert st == 200, "服务未起来"

        _st, j = _req("/api/login", "POST", {"username": "admin", "password": "admin123"})
        tok = j["token"]

        # 未登录拒绝
        st, _j = _req("/api/ai/lcm", "POST", {"text": "你好"})
        assert st == 401

        # 建会话并对话
        st, j1 = _req("/api/ai/lcm", "POST", {"text": "项目预算 380 万"}, tok)
        assert st == 200 and j1["conv_id"] and "stats" in j1
        cid = j1["conv_id"]
        for t in ["对接人王工", "机房在深圳坪山", FILLER, FILLER, FILLER, FILLER, FILLER]:
            st, _ = _req("/api/ai/lcm", "POST", {"text": t, "conv_id": cid}, tok)
            assert st == 200

        st, j2 = _req("/api/ai/lcm", "POST", {"text": "机房在哪个城市？", "conv_id": cid}, tok)
        assert st == 200 and j2["conv_id"] == cid
        assert "坪山" in j2["answer"], "HTTP 链路也要能召回：%s" % j2["answer"]

        # 库结构视图
        st, j3 = _req("/api/lcm?conv_id=%d" % cid, token=tok)
        assert st == 200 and j3["stats"]["turns"] >= 8
        assert "nodes" in j3

        # 回溯接口
        st, j4 = _req("/api/ai/lcm_detail", "POST", {"conv_id": cid, "q": "预算多少"}, tok)
        assert st == 200 and j4["hits"] and "380" in j4["hits"][0]["content"]
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
