# -*- coding: utf-8 -*-
"""State 层 + 动作级权限/预算 + 证据链 的单元级测试。

不经过 HTTP：直接 import ai，monkeypatch llm_chat 为可控桩，
精确触发「预算耗尽 / 角色拒绝 / 需人工确认 / 断点续跑」四条路径。
"""
import io
import json
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEST_DB = os.path.join(BASE_DIR, "tests", "test_state.db")
if os.path.exists(TEST_DB):
    os.remove(TEST_DB)
os.environ["FEINAO_DB"] = TEST_DB
for _k in ("FEINAO_QWEN_KEY", "FEINAO_DEEPSEEK_KEY", "FEINAO_HUNYUAN_KEY"):
    os.environ[_k] = ""

sys.path.insert(0, os.path.join(BASE_DIR, "backend"))
import ai  # noqa: E402

ai.init_db()


def test_budget_denial():
    """search_docs 预算 3 次/任务：模型连续要调第 4 次必须被拒（本地工具，不依赖上游）。"""
    def fake_llm(messages, user="system"):
        return json.dumps({"tool": "search_docs", "args": {"q": "部署"}}), "stub"
    ai.llm_chat = fake_llm
    r = ai.react_run("连续查库压测", user="admin")
    assert r.get("task_id"), "应生成任务"
    denied = [s for s in r["steps"] if s.get("denied")]
    assert denied and "预算上限" in denied[0]["denied"], "应有预算拒绝：%s" % r["steps"]
    executed = [s for s in r["steps"] if s.get("obs")]
    assert len(executed) == 3, "预算内应恰好执行 3 次，实际 %d" % len(executed)
    assert len(denied) == 3, "步数 6 = 执行 3 + 拒绝 3，实际拒绝 %d" % len(denied)
    t = ai.task_get(r["task_id"])
    assert t["status"] == "active", "步数耗尽任务应保持 active 可续跑"
    kinds = [s["kind"] for s in t["steps"]]
    assert "denied" in kinds and "tool_call" in kinds, "步骤应落库（State 层）"


def test_role_denial():
    """query_system 只允许 admin/ops：viewer 必须被拒且一次都不执行。"""
    def fake_llm(messages, user="system"):
        return json.dumps({"tool": "query_system", "args": {"system": "kms", "entity": "docs"}}), "stub"
    ai.llm_chat = fake_llm
    r = ai.react_run("viewer 越权压测", user="view01")
    assert r["steps"] and all("无权" in s.get("denied", "") for s in r["steps"]), "viewer 应全部被拒"
    assert not any(s.get("obs") for s in r["steps"]), "viewer 不应有任何工具结果"


def test_requires_confirm():
    """requires_confirm=True：工具不执行，落 pending 步骤。"""
    ai.SPEC["tool_policies"]["search_docs"]["requires_confirm"] = True
    try:
        def fake_llm(messages, user="system"):
            return json.dumps({"tool": "search_docs", "args": {"q": "部署"}}), "stub"
        ai.llm_chat = fake_llm
        r = ai.react_run("敏感操作压测", user="admin")
        pend = [s for s in r["steps"] if s.get("status")]
        assert pend and "人工确认" in pend[0]["status"], "应挂起待确认"
        assert not any(s.get("obs") for s in r["steps"]), "待确认不应执行"
        t = ai.task_get(r["task_id"])
        assert any(s["kind"] == "pending" for s in t["steps"]), "pending 应落库"
    finally:
        ai.SPEC["tool_policies"]["search_docs"]["requires_confirm"] = False


def test_resume_and_evidence():
    """断点续跑：active 任务带历史步骤续跑成功后置 done，证据落库可查。"""
    def fake_llm_tool(messages, user="system"):
        return json.dumps({"tool": "search_docs", "args": {"q": "部署"}}), "stub"
    ai.llm_chat = fake_llm_tool
    r = ai.react_run("先查一步然后停", user="admin", max_steps=1)
    tid = r["task_id"]
    t = ai.task_get(tid)
    assert t["status"] == "active" and t["steps"], "第一步后应可续跑"

    def fake_llm_answer(messages, user="system"):
        return "结论：部署用 systemd（依据：知识库文档）。", "stub"
    ai.llm_chat = fake_llm_answer
    r2 = ai.react_run("", user="admin", task_id=tid, max_steps=2)
    assert r2["task_id"] == tid and "systemd" in r2["answer"], "应续跑出答案"
    assert ai.task_get(tid)["status"] == "done", "续跑完成应置 done"
    # 旧任务里的 tool_call 步骤应计入续跑轮的预算
    assert r2["evidence_id"], "react 证据应落库"
    ev = ai.evidence_list(user="admin", conv_id=None, limit=100)
    assert any(e["id"] == r2["evidence_id"] and e["channel"] == "react" for e in ev), "证据可查"
    # 非本人/不存在任务拒绝续跑
    assert "error" in ai.react_run("", user="ops01", task_id=tid), "他人任务不可续跑"
    assert "error" in ai.react_run("", user="admin", task_id=tid), "done 任务不可续跑"


if __name__ == "__main__":
    test_budget_denial()
    test_role_denial()
    test_requires_confirm()
    test_resume_and_evidence()
    print("test_state: 4/4 passed")
