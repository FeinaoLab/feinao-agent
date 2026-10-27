# -*- coding: utf-8 -*-
"""卡死上限（任务 6）测试：墙钟、三连相同调用、连续畸形输出。

三条都走 react_run 的真实路径，用可控桩 llm 造出"模型卡住"的现场。
断言三件事：① 及时中断；② 任务状态落库为 aborted；③ 用户看到的是人话，不是堆栈。
"""
import json
import os
import sys
import time

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("FEINAO_DB", os.path.join(BASE_DIR, "tests", "test_limits.db"))
for _k in ("FEINAO_QWEN_KEY", "FEINAO_DEEPSEEK_KEY", "FEINAO_HUNYUAN_KEY"):
    os.environ[_k] = ""

sys.path.insert(0, os.path.join(BASE_DIR, "backend"))
import ai  # noqa: E402

ai.init_db()

# 这些用例会替换 ai.llm_chat 造现场：用完必须还原，否则后面的模块（如 test_provider）
# 会拿着桩跑真链路，失败得莫名其妙。测试之间的隔离和代码本身一样重要。
import pytest  # noqa: E402

_ORIG_LLM = ai.llm_chat


@pytest.fixture(autouse=True)
def _restore_llm():
    yield
    ai.llm_chat = _ORIG_LLM


def _last_evidence():
    conn = ai.db()
    row = conn.execute("SELECT * FROM evidence ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()
    return dict(row) if row else {}


def test_repeated_identical_call_aborts():
    """同一个工具同样的参数连着来：第 3 次（same_call_repeat_limit）直接停，第 3 次不执行。"""
    def fake_llm(messages, user="system"):
        return json.dumps({"tool": "search_docs", "args": {"q": "不存在的单号"}}), "stub"
    ai.llm_chat = fake_llm
    r = ai.react_run("反复查一个不存在的单号", user="admin")
    assert r.get("status") == "aborted", "应中断：%s" % r
    assert r.get("reason") == "repeated_identical_call"
    assert "参数完全相同" in r["answer"] and "换个查法" in r["answer"], "要说人话：%s" % r["answer"]
    assert "Traceback" not in r["answer"]
    executed = [s for s in r["steps"] if s.get("obs")]
    assert len(executed) == 2, "第 3 次应在执行前被拦住，实际执行 %d 次" % len(executed)
    t = ai.task_get(r["task_id"])
    assert t["status"] == "aborted", "任务状态应落库为 aborted"
    assert any(s["kind"] == "aborted" for s in t["steps"]), "中断原因应落库"
    ev = _last_evidence()
    assert "aborted" in (ev.get("trace") or ""), "证据链应记录中断：%s" % ev.get("trace")


def test_wall_clock_abort():
    """卡在慢接口上：墙钟到点就停，不等步数耗尽。"""
    old = ai.SPEC["limits"]["max_wall_clock_seconds"]

    def slow_llm(messages, user="system"):
        time.sleep(0.12)
        return json.dumps({"tool": "search_docs", "args": {"q": "慢查询"}}), "stub"
    ai.llm_chat = slow_llm
    ai.SPEC["limits"]["max_wall_clock_seconds"] = 0.05
    try:
        r = ai.react_run("慢任务压测", user="admin")
    finally:
        ai.SPEC["limits"]["max_wall_clock_seconds"] = old
    assert r.get("reason") == "wall_clock_timeout", "应墙钟中断：%s" % r
    assert "超过" in r["answer"] and "上限" in r["answer"], "要说清超了多久：%s" % r["answer"]
    assert ai.task_get(r["task_id"])["status"] == "aborted"


def test_malformed_json_degrades_to_text():
    """连续吐坏格式：第二次就降级为纯文本回答，不无限重试。"""
    def broken_llm(messages, user="system"):
        return '{"tool": "query_system", "args": {"system": "kms",}}', "stub"
    ai.llm_chat = broken_llm
    r = ai.react_run("格式错乱压测", user="admin")
    assert "已降级为纯文本回答" in r["answer"], "应降级而不是崩：%s" % r["answer"]
    assert not [s for s in r["steps"] if s.get("obs")], "坏格式不应执行任何工具"
    degraded = [s for s in r["steps"] if s.get("degraded")]
    assert degraded and degraded[0]["degraded"] == "malformed_json"
    assert ai.task_get(r["task_id"])["status"] == "done", "降级回答算完成，不是中断"


def test_normal_run_not_affected():
    """正常任务不受三条上限影响：直接给答案就是 done。"""
    ai.llm_chat = lambda messages, user="system": ("这是答案（依据：DOC1）", "stub")
    r = ai.react_run("简单问题", user="admin")
    assert r.get("answer") == "这是答案（依据：DOC1）"
    assert ai.task_get(r["task_id"])["status"] == "done"
