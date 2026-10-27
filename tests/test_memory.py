# -*- coding: utf-8 -*-
"""飞忆 · 跨会话长期记忆测试。

覆盖五条铁律：
  1. 抽取先挂 pending（不自动生效）；
  2. 待确认的记忆不被注入；
  3. 生效后新会话能命中并带来源编号（这是"跨会话"的核心证据）；
  4. 命中不到就不注入（不许瞎回忆）；
  5. 按用户隔离 + 可停用/删除 + 去重。
"""
import json
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEST_DB = os.path.join(BASE_DIR, "tests", "test_memory.db")
if os.path.exists(TEST_DB):
    os.remove(TEST_DB)
os.environ["FEINAO_DB"] = TEST_DB
for _k in ("FEINAO_QWEN_KEY", "FEINAO_DEEPSEEK_KEY", "FEINAO_HUNYUAN_KEY"):
    os.environ[_k] = ""

sys.path.insert(0, os.path.join(BASE_DIR, "backend"))
import ai      # noqa: E402
import lcm     # noqa: E402
import memory  # noqa: E402

try:
    import pytest

    @pytest.fixture(autouse=True)
    def _restore_llm():
        orig = ai.llm_chat
        yield
        ai.llm_chat = orig
except ImportError:
    pass

ai.init_db()
lcm.init_lcm_db()
memory.init_memory_db()


def _clear(user="admin"):
    conn = ai.db()
    conn.execute("DELETE FROM memory WHERE user=?", (user,))
    conn.commit()
    conn.close()


def _make_conv(user="admin", turns=None):
    o = lcm.LCM(user=user, title="记忆源会话")
    for t in (turns or []):
        o.append("user", t)
        o.append("assistant", "收到。")
    return o


# ---------- 1. 抽取：默认 pending，且重复抽取不重复落 ----------

def test_extract_goes_pending_and_dedup():
    _clear()
    o = _make_conv(turns=["部署端口定为 8060，域名走 /nao/ 前缀",
                          "审批只允许 admin 放行，ops 只能发起"])
    calls = {"n": 0}

    def fake_llm(messages, user="system"):
        calls["n"] += 1
        return json.dumps([
            {"kind": "fact", "text": "部署端口 8060，域名前缀 /nao/", "confidence": 0.9},
            {"kind": "decision", "text": "审批只允许 admin 放行，ops 只能发起", "confidence": 0.9},
        ], ensure_ascii=False), "stub"

    ai.llm_chat = fake_llm
    r1 = memory.extract("admin", conv_id=o.conv_id)
    assert r1["created"] == 2 and r1["method"] == "llm", "应抽出 2 条：%s" % r1
    rows = memory.list_items("admin", status="pending")
    assert len(rows) == 2, "抽出来应先进待确认区，不能自动生效"
    assert all(r["status"] == "pending" for r in rows), "默认状态必须 pending"
    assert all(r["src_conv"] == o.conv_id for r in rows), "每条都要带来源会话"

    r2 = memory.extract("admin", conv_id=o.conv_id)
    assert r2["created"] == 0 and r2["skipped"] >= 2, "重复抽取应被去重挡掉：%s" % r2
    assert len(memory.list_items("admin", status="all")) == 2, "去重后总数不变"


# ---------- 2. 待确认不注入；生效才注入 ----------

def test_pending_not_injected_until_activated():
    _clear()
    mid = memory.add("admin", "部署端口 8060", "fact", 0.9, src_conv=1)
    assert memory.get(mid)["status"] == "pending"
    assert memory.search("admin", "部署端口是多少") == [], "待确认的记忆不许注入"
    assert memory.decide(mid, "activate", "admin")["status"] == "active"
    hits = memory.search("admin", "部署端口是多少")
    assert len(hits) == 1 and hits[0]["id"] == mid, "生效后应可命中"
    # 停用后立刻不可命中
    memory.decide(mid, "disable", "admin")
    assert memory.search("admin", "部署端口是多少") == [], "停用后不许再注入"


# ---------- 3. 跨会话：新会话凭记忆答出来 ----------

def test_cross_conversation_recall():
    _clear()
    old = _make_conv(turns=["我们把部署端口定为 8060，别再用 8080"])
    mid = memory.add("admin", "部署端口定为 8060（不是 8080）", "decision", 0.95,
                     src_conv=old.conv_id, status="active")

    seen = {}

    def fake_llm(messages, user="system"):
        seen["msgs"] = messages
        sys_txt = "\n".join(m["content"] for m in messages if m["role"] == "system")
        seen["sys"] = sys_txt
        if "8060" in sys_txt:
            return "部署端口是 8060（依据：跨会话记忆 M#%d）。" % mid, "stub"
        return "我不知道，本会话没有提到部署端口。", "stub"

    ai.llm_chat = fake_llm
    fresh = lcm.LCM(user="admin", title="全新会话")      # 新会话，没有历史
    r = fresh.ask("上次我们定的部署端口是多少？")
    assert "跨会话长期记忆" in seen["sys"], "新会话应注入记忆块"
    assert "M#%d" % mid in seen["sys"], "注入时要带记忆编号"
    assert str(old.conv_id) in seen["sys"], "注入时要带来源会话号"
    assert "8060" in r["answer"] and r["memory_ids"] == [mid], "应凭记忆答出并回报命中的记忆"
    ev = ai.evidence_list(user="admin", conv_id=fresh.conv_id)
    assert ev and ev[0]["trace"] and mid in (ev[0]["trace"].get("memory") or []), \
        "证据链应记录本轮用了哪条记忆"


# ---------- 4. 命中不到就不注入（不许瞎回忆） ----------

def test_no_hit_no_injection():
    _clear()
    memory.add("admin", "部署端口定为 8060", "decision", 0.95, src_conv=1, status="active")
    seen = {}

    def fake_llm(messages, user="system"):
        seen["sys"] = "\n".join(m["content"] for m in messages if m["role"] == "system")
        return "我不知道，没有相关记录。", "stub"

    ai.llm_chat = fake_llm
    o = lcm.LCM(user="admin", title="无关问题")
    r = o.ask("今天食堂几点开饭？")
    assert "跨会话长期记忆" not in seen["sys"], "无关问题不该注入记忆"
    assert r["memory_ids"] == [], "没命中就该是空"


# ---------- 5. 按用户隔离 + 权限 ----------

def test_user_isolation_and_permission():
    _clear("admin")
    _clear("ops01")
    mid = memory.add("admin", "部署端口定为 8060", "decision", 0.95, src_conv=1, status="active")
    assert memory.search("ops01", "部署端口是多少") == [], "别人的记忆不许命中"
    assert "无权" in memory.decide(mid, "disable", "ops01").get("error", ""), "不许动别人的记忆"
    assert memory.get(mid)["status"] == "active", "越权调用不能改状态"
    assert memory.list_items("ops01", status="all") == [], "列表只回自己的"


# ---------- 6. 自动生效阈值默认关闭，调低才放开 ----------

def test_auto_activate_knob():
    _clear()
    mid = memory.add("admin", "默认配置下再高置信度也要人点头", "fact", 0.99, src_conv=1)
    assert memory.get(mid)["status"] == "pending", "默认阈值 1.0：不自动生效"
    old = ai.SPEC["memory"]["auto_activate_min"]
    try:
        ai.SPEC["memory"]["auto_activate_min"] = 0.9
        mid2 = memory.add("admin", "阈值调低后高置信度可自动入长期区", "fact", 0.95, src_conv=1)
        assert memory.get(mid2)["status"] == "active", "调低阈值后应自动生效"
    finally:
        ai.SPEC["memory"]["auto_activate_min"] = old


# ---------- 7. 同位置的新值取代旧值（不许两种说法并存） ----------

def test_new_value_supersedes_old():
    _clear()
    old = memory.add("admin", "部署端口统一 8060", "decision", 0.9, src_conv=1, status="active")
    o = _make_conv(turns=["我们把部署端口统一 8100，按新的来"])
    ai.llm_chat = lambda messages, user="system": (
        json.dumps([{"kind": "decision", "text": "部署端口统一 8100", "confidence": 0.7}],
                   ensure_ascii=False), "stub")
    r = memory.extract("admin", conv_id=o.conv_id)
    assert r["created"] == 1, "新值不该被当成重复丢掉：%s" % r
    assert memory.get(old)["status"] == "disabled", "旧值应被自动停用"
    rows = memory.list_items("admin", status="all")
    assert any("8100" in x["text"] and x["status"] == "pending" for x in rows), "新值应待确认"
    assert len([x for x in rows if x["status"] == "active"]) == 0, "不该同时留两条生效"


# ---------- 8. 模型虚构的条目必须丢掉（反幻觉） ----------

def test_hallucinated_items_dropped():
    _clear()
    o = _make_conv(turns=["部署端口定为 8060"])
    ai.llm_chat = lambda messages, user="system": (json.dumps([
        {"kind": "fact", "text": "部署端口 8060", "confidence": 0.6},
        {"kind": "artifact", "text": "/config/deployment.yaml 中 port: 9000", "confidence": 0.6},
    ], ensure_ascii=False), "stub")
    r = memory.extract("admin", conv_id=o.conv_id)
    assert r["created"] == 1 and r["dropped"] == 1, "原文没有的条目必须丢掉：%s" % r
    rows = memory.list_items("admin", status="all")
    assert len(rows) == 1 and "8060" in rows[0]["text"], "只该留下原文说过的那条"


# ---------- 9. 过程性噪声（"已记录/已同步"）不许进记忆 ----------

def test_process_noise_dropped():
    _clear()
    o = _make_conv(turns=["部署端口定为 8060"])
    ai.llm_chat = lambda messages, user="system": (json.dumps([
        {"kind": "fact", "text": "部署端口 8060", "confidence": 0.6},
        {"kind": "artifact", "text": "配置已同步至飞脑会话上下文 [PILOT:lcm]", "confidence": 0.6},
    ], ensure_ascii=False), "stub")
    r = memory.extract("admin", conv_id=o.conv_id)
    assert r["created"] == 1 and r["dropped"] == 1, "过程性元信息不该成为记忆：%s" % r
    rows = memory.list_items("admin", status="all")
    assert len(rows) == 1 and "8060" in rows[0]["text"]


# ---------- 10. ASCII 二元组不许误命中（线上踩过：ro→role） ----------

def test_ascii_bigram_no_false_hit():
    _clear()
    memory.add("admin", "/auth/policy.yaml 中 role: admin 具有 approve 权限", "decision", 0.6,
               src_conv=1, status="active")
    assert memory.search("admin", "上次我们定的数据库 root 密码是什么？") == [], \
        "ro/ot 这类二元组不许误命中 role，否则会注入无关记忆"


# ---------- 10. 无 LLM 时走抽取式兜底 ----------

def test_heuristic_fallback():
    _clear()
    o = _make_conv(turns=["我们把部署端口定为 8060，统一走 nginx 反向代理",
                          "今天天气不错"])
    ai.llm_chat = lambda messages, user="system": ("mock 兜底回答", "mock")
    r = memory.extract("admin", conv_id=o.conv_id)
    assert r["method"] == "heuristic" and r["created"] >= 1, "无真实模型时应走抽取式：%s" % r
    rows = memory.list_items("admin", status="all")
    assert any("8060" in x["text"] for x in rows), "抽取式也要保住具体数字"
    assert not any("天气" in x["text"] for x in rows), "寒暄不该被记住"


if __name__ == "__main__":
    test_extract_goes_pending_and_dedup()
    test_pending_not_injected_until_activated()
    test_cross_conversation_recall()
    test_no_hit_no_injection()
    test_user_isolation_and_permission()
    test_auto_activate_knob()
    test_new_value_supersedes_old()
    test_hallucinated_items_dropped()
    test_process_noise_dropped()
    test_ascii_bigram_no_false_hit()
    test_heuristic_fallback()
    print("test_memory: 11/11 passed")
