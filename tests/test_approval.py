# -*- coding: utf-8 -*-
"""审批工作台测试（Permission 层的真闸门）。

不经过 HTTP：直接驱动 ai，用可控 LLM 桩触发
「挂起 → 放行执行 → 任务续跑 / 拒绝不执行 / 越权不能批 / 幂等回放」。
"""
import json
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEST_DB = os.path.join(BASE_DIR, "tests", "test_approval.db")
if os.path.exists(TEST_DB):
    os.remove(TEST_DB)
os.environ["FEINAO_DB"] = TEST_DB
for _k in ("FEINAO_QWEN_KEY", "FEINAO_DEEPSEEK_KEY", "FEINAO_HUNYUAN_KEY"):
    os.environ[_k] = ""

sys.path.insert(0, os.path.join(BASE_DIR, "backend"))
import ai  # noqa: E402

try:
    import pytest

    @pytest.fixture(autouse=True)
    def _restore_llm():
        """每条用例后还原 llm_chat：全局桩不许泄漏给其他测试文件。"""
        orig = ai.llm_chat
        yield
        ai.llm_chat = orig
except ImportError:  # 直接 python 运行本文件时没有 pytest
    pass

ai.init_db()

DOC_TITLE = "审批测试文档"


def _doc_count(title):
    conn = ai.db()
    n = conn.execute("SELECT COUNT(*) c FROM docs WHERE title=?", (title,)).fetchone()["c"]
    conn.close()
    return n


def _stub(messages, user="system"):
    """看到工具结果 / 拒绝理由就收尾，否则一直要写知识库。"""
    last = messages[-1]["content"]
    if '"obs"' in last or "拒绝" in last or "replay" in last:
        return "结论：已处理完成（依据：工具返回）。未验证：无。", "stub"
    return json.dumps({"tool": "ingest_doc",
                       "args": {"title": DOC_TITLE, "body": "飞系部署规范：systemd + nginx"}}), "stub"


def _always_tool(messages, user="system"):
    return json.dumps({"tool": "ingest_doc",
                       "args": {"title": DOC_TITLE, "body": "幂等测试"}}), "stub"


def _suspend(user="admin"):
    ai.llm_chat = _stub
    r = ai.react_run("把部署规范写进知识库", user=user)
    assert r.get("status") == "waiting_approval", "写操作应挂起：%s" % r
    assert r.get("approval_id"), "应生成审批单"
    return r


def test_suspend_creates_approval():
    """写操作挂起：生成审批单、任务置 waiting_approval、动作一次都没执行。"""
    before = _doc_count(DOC_TITLE)
    r = _suspend()
    a = ai.approval_get(r["approval_id"])
    assert a and a["status"] == "pending" and a["tool"] == "ingest_doc", "审批单不对：%s" % a
    assert a["args"]["title"] == DOC_TITLE, "审批单应带完整参数"
    t = ai.task_get(r["task_id"])
    assert t["status"] == "waiting_approval", "任务状态应挂起：%s" % t["status"]
    assert _doc_count(DOC_TITLE) == before, "未审批前绝不能落库"
    assert any(s["kind"] == "pending" for s in t["steps"]), "pending 步骤应落库"
    # 挂起中的任务不接受"续跑"绕过审批
    assert "error" in ai.react_run("", user="admin", task_id=r["task_id"]), "挂起任务不可直接续跑"


def test_approve_executes_and_resumes():
    """放行：真执行该动作 → 落 tool_result → 任务回到模型续跑 → done + 证据。"""
    r = _suspend()
    out = ai.approval_handle(r["approval_id"], "approved", "admin", "同意，内容无害")
    assert out.get("approved") and not out.get("error"), "放行应成功：%s" % out
    assert out.get("result", {}).get("doc_id"), "放行后应真执行并返回 doc_id"
    assert _doc_count(DOC_TITLE) == 1, "放行后应恰好写入 1 篇"
    a = ai.approval_get(r["approval_id"])
    assert a["status"] == "approved" and a["consumed"] == 1 and a["decided_by"] == "admin", "审批单应留痕"
    run = out.get("run") or {}
    assert run.get("answer") and run.get("evidence_id"), "任务应续跑出结论并落证据"
    t = ai.task_get(r["task_id"])
    assert t["status"] == "done", "续跑完成应 done"
    kinds = [s["kind"] for s in t["steps"]]
    for k in ("tool_call", "tool_result", "answer"):
        assert k in kinds, "步骤缺 %s：%s" % (k, kinds)
    # 二次决策应被拒
    again = ai.approval_handle(r["approval_id"], "approved", "admin")
    assert "error" in again and "不可重复处理" in again["error"], "重复审批应拒绝：%s" % again


def test_reject_blocks_execution():
    """拒绝：动作不执行，拒绝理由回给模型，任务继续。"""
    before = _doc_count(DOC_TITLE)
    r = _suspend()
    out = ai.approval_handle(r["approval_id"], "rejected", "admin", "不该写库")
    assert not out.get("result"), "拒绝绝不能执行动作"
    assert _doc_count(DOC_TITLE) == before, "拒绝后知识库不应变化"
    a = ai.approval_get(r["approval_id"])
    assert a["status"] == "rejected" and a["note"] == "不该写库", "拒绝理由应留痕"
    t = ai.task_get(r["task_id"])
    denied = [json.loads(s["content"]) for s in t["steps"] if s["kind"] == "denied"]
    assert denied and "审批被拒绝" in denied[-1]["denied"], "拒绝原因应落步骤"
    assert t["status"] == "done", "拒绝后任务应继续跑完"


def test_approver_permission():
    """ops 能发起但不能审批；viewer 连发起都不行（被动作级角色拦住）。"""
    r = _suspend(user="ops01")
    out = ai.approval_handle(r["approval_id"], "approved", "ops01")
    assert "error" in out and "无审批权" in out["error"], "ops 不应能自批：%s" % out
    assert ai.approval_get(r["approval_id"])["status"] == "pending", "越权审批不应改变状态"
    # viewer 走动作级权限直接拒绝（不进审批）
    ai.llm_chat = _stub
    rv = ai.react_run("写点东西", user="view01")
    assert rv.get("steps") and all("无权" in s.get("denied", "") for s in rv["steps"]), \
        "viewer 应被角色拦在审批之前：%s" % rv.get("steps")
    assert not rv.get("approval_id"), "viewer 不该产生审批单"


def test_replay_no_duplicate_side_effect():
    """放行后模型重复要同一动作：复用结果，不重复写库。"""
    before = _doc_count(DOC_TITLE)
    ai.llm_chat = _always_tool
    r = ai.react_run("反复要求写同一篇", user="admin")
    assert r.get("status") == "waiting_approval", "首次应挂起"
    out = ai.approval_handle(r["approval_id"], "approved", "admin")
    run = out.get("run") or {}
    assert run.get("task_id"), "放行后应续跑：%s" % out
    assert _doc_count(DOC_TITLE) == before + 1, "同一动作只应落库一次，实际 %d" % (_doc_count(DOC_TITLE) - before)
    t = ai.task_get(r["task_id"])
    assert any(s["kind"] == "replay" for s in t["steps"]), "应走幂等回放：%s" % [s["kind"] for s in t["steps"]]


def test_repeated_after_reject_not_resubmitted():
    """拒绝后模型再提同一动作：直接拒，不重复挂新单（不打扰审批人）。"""
    ai.llm_chat = _always_tool
    r = ai.react_run("反复要写同一篇（拒绝版）", user="admin")
    assert r.get("status") == "waiting_approval"
    out = ai.approval_handle(r["approval_id"], "rejected", "admin", "不许写")
    assert not out.get("result"), "拒绝不应执行"
    t = ai.task_get(r["task_id"])
    kinds = [s["kind"] for s in t["steps"]]
    assert "denied" in kinds and "tool_call" not in kinds, "应全部走拒绝：%s" % kinds
    conn = ai.db()
    n = conn.execute("SELECT COUNT(*) c FROM approvals WHERE task_id=?", (r["task_id"],)).fetchone()["c"]
    conn.close()
    assert n == 1, "同一动作不该重复挂单，实际 %d 张" % n


def test_list_and_expire():
    """列表：审批人看全部并有 can_approve；超时自动 expired。"""
    pending = ai.approval_list(status="pending", user="admin", only_mine=False)
    assert isinstance(pending, list) and pending, "应有待审批单"
    assert all(r["status"] == "pending" for r in pending)
    assert all("goal" in r for r in pending), "列表应带任务目标"
    # 造一条超时单：把 ttl 改成 0 分钟
    old_ttl = ai.SPEC.get("approval", {}).get("ttl_minutes", 1440)
    ai.SPEC.setdefault("approval", {})["ttl_minutes"] = 0
    try:
        ai.approval_request("admin", None, 0, "ingest_doc", {"title": "过期单"}, "压测超时")
        rows = ai.approval_list(status="expired", user="admin", only_mine=False)
        assert any(r["tool"] == "ingest_doc" and r["status"] == "expired" for r in rows), "超时应置 expired"
    finally:
        ai.SPEC["approval"]["ttl_minutes"] = old_ttl


if __name__ == "__main__":
    test_suspend_creates_approval()
    test_approve_executes_and_resumes()
    test_reject_blocks_execution()
    test_approver_permission()
    test_replay_no_duplicate_side_effect()
    test_repeated_after_reject_not_resubmitted()
    test_list_and_expire()
    print("test_approval: 7/7 passed")
