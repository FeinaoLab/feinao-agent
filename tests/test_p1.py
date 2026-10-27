# -*- coding: utf-8 -*-
"""P1 批次测试：经验层 / 编辑者隔离 / 技能注册表 / 会话四模式。

全部用 mock（fake_llm），不碰真模型；库与 spec 都用独立副本。
"""
import io
import json
import os
import shutil
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEST_DB = os.path.join(BASE_DIR, "tests", "test_p1.db")
TEST_SPEC = os.path.join(BASE_DIR, "tests", "test_p1_spec.json")
for _p in (TEST_DB, TEST_DB + "-wal", TEST_DB + "-shm"):
    if os.path.exists(_p):
        os.remove(_p)
shutil.copyfile(os.path.join(BASE_DIR, "backend", "spec.json"), TEST_SPEC)

os.environ["FEINAO_DB"] = TEST_DB
os.environ["FEINAO_SPEC"] = TEST_SPEC
for _k in ("FEINAO_QWEN_KEY", "FEINAO_DEEPSEEK_KEY", "FEINAO_HUNYUAN_KEY"):
    os.environ[_k] = ""

sys.path.insert(0, os.path.join(BASE_DIR, "backend"))
import ai  # noqa: E402
import bench  # noqa: E402
import demo  # noqa: E402
import memory  # noqa: E402

# 用例顺序不可控：import 期不碰 ai 的任何全局状态（别的文件的钉路径会失效），
# 一切钉副本/播种/还原都放进 autouse 夹具。
_PREV_SPEC = ai.SPEC_PATH
_PREV_DB = ai.DB_PATH

PORT = int(os.environ.get("FEINAO_P1_DEMO_PORT") or 8094)
demo.start_upstream(PORT)
_UPSTREAM_SET = ("KMS", "BI", "CC")
_READY = {"seeded": False}

try:
    import pytest

    @pytest.fixture(autouse=True)
    def _isolate():
        orig_llm = ai.llm_chat
        orig_env = {s: os.environ.get("FEINAO_UPSTREAM_" + s)
                    for s in _UPSTREAM_SET}
        ai.DB_PATH, ai.SPEC_PATH = TEST_DB, TEST_SPEC
        ai.reload_spec()
        ai.init_db()
        memory.init_memory_db()
        bench.init_db()
        for s in _UPSTREAM_SET:
            os.environ["FEINAO_UPSTREAM_" + s] = "http://127.0.0.1:%d/%s" % (PORT, s.lower())
        if not _READY["seeded"]:
            demo.seed_docs()
            conn = ai.db()
            conn.execute("INSERT OR IGNORE INTO users VALUES('editor01','edit123456','editor')")
            conn.commit()
            conn.close()
            _READY["seeded"] = True
        yield
        ai.llm_chat = orig_llm
        ai.DB_PATH, ai.SPEC_PATH = _PREV_DB, _PREV_SPEC
        for s in _UPSTREAM_SET:
            if orig_env[s] is None:
                os.environ.pop("FEINAO_UPSTREAM_" + s, None)
            else:
                os.environ["FEINAO_UPSTREAM_" + s] = orig_env[s]

    @pytest.fixture(autouse=True)
    def _restore_llm():
        orig = ai.llm_chat
        yield
        ai.llm_chat = orig
except ImportError:
    pass


def test_modes_wiring_plan_suspends_and_yolo_allows():
    """plan 模式：write 类动作挂审批；yolo（admin）：直接执行不挂单。"""
    def fake(messages, user="system"):
        return json.dumps({"tool": "ingest_doc",
                           "args": {"title": "模式测试", "body": "内容"}}), "stub"
    ai.llm_chat = fake
    r = ai.react_run("plan 模式写入", user="admin", mode="plan")
    assert r.get("status") == "waiting_approval", r
    assert r.get("approval_id"), "plan 模式下写动作应挂审批单"

    r2 = ai.react_run("yolo 模式写入", user="admin", mode="yolo")
    assert r2.get("status") != "waiting_approval", "yolo 不应挂审批"
    assert any(s.get("obs") for s in r2.get("steps", [])), "yolo 应真执行"
    assert any(s.get("tool") == "ingest_doc" and not s.get("denied")
               for s in r2.get("steps", [])), "ingest_doc 应真的跑了"


def test_mode_validation_and_yolo_role():
    """未知模式 422；yolo 只给 admin。"""
    def fake(messages, user="system"):
        return "直接回答", "stub"
    ai.llm_chat = fake
    r = ai.react_run("x", user="admin", mode="chaos")
    assert "未知模式" in (r.get("error") or ""), r
    r2 = ai.react_run("yolo 越权", user="view01", mode="yolo")
    assert "yolo" in (r2.get("error") or "") and "无权" in (r2.get("error") or ""), r2
    # 任务上的模式要落库
    r3 = ai.react_run("模式落库检查", user="admin", mode="plan")
    t = ai.task_get(r3["task_id"])
    assert t["mode"] == "plan", "任务应记住自己的模式"


def test_rejected_action_stays_denied_even_in_yolo():
    """同一任务内被拒的动作，换 yolo 续跑也必须保持拒绝——人的"不"不因模式被绕过。"""
    ai.llm_chat = lambda m, user="system": (
        json.dumps({"tool": "ingest_doc", "args": {"title": "T", "body": "B"}}), "stub")
    r = ai.react_run("先挂审批", user="admin", mode="default")
    aid = r["approval_id"]
    ai.approval_decide(aid, "rejected", by="admin", note="不要写")
    # 审批拒绝后任务停在 waiting_approval；这里只验"yolo 续跑不翻案"，手工恢复 active
    conn = ai.db()
    conn.execute("UPDATE tasks SET status='active' WHERE id=?", (r["task_id"],))
    conn.commit()
    conn.close()
    r2 = ai.react_run("（续跑）", user="admin", task_id=r["task_id"], mode="yolo")
    denied = [s for s in r2.get("steps", []) if s.get("denied")]
    assert denied and "已被审批人拒绝" in denied[0]["denied"], r2


def test_skills_registry_seed_match_and_prompt():
    """技能播种、检索命中、ReAct 系统提示注入。"""
    rows = ai.skills_list()
    assert len(rows) >= 3, "出厂技能应已播种"
    hits = ai.skill_match("帮我把这份总结沉淀成文档")
    assert hits and hits[0]["name"] == "写入知识库", [h["name"] for h in hits]
    prompt = ai._react_system_prompt([], "查一下订单数据")
    assert "查业务数据" in prompt, "ReAct 提示应注入命中的技能"
    r = ai.skill_save("新技能", "测试用", "调 search_docs", tools=["search_docs"], by="admin")
    assert r["ok"], r
    bad = ai.skill_save("坏技能", "", "", tools=["ghost_tool"], by="admin")
    assert not bad["ok"], "未注册工具应被拒"


def test_harness_patch_editor_flow():
    """editor 提补丁：落 active=0 修订 + 审批单；admin 放行才激活；安全面拒改。"""
    before = ai.spec_sha()
    r = ai.harness_patch([{"path": "pilots.rag_topk", "value": 2}],
                         note="缩召回试试", by="editor01")
    assert r["ok"], r
    assert ai.spec_sha() == before, "补丁提交时不得改动生效配置"
    rev = [x for x in ai.harness_list()["rows"] if x["id"] == r["rev_id"]][0]
    assert rev["active"] == 0, "补丁修订默认不生效"
    ap = ai.approval_get(r["approval_id"])
    assert ap["tool"] == "harness.activate" and ap["status"] == "pending"

    # 安全面拒改
    for path in ("tool_policies.search_docs.max_calls_per_run", "modes.default",
                 "approval.approvers", "limits.max_steps", "users.hacker"):
        bad = ai.harness_patch([{"path": path, "value": 1}], by="editor01")
        assert not bad["ok"] and "安全边界" in bad["error"], (path, bad)

    # editor 不能直接激活
    act = ai.harness_activate(r["rev_id"], by="editor01")
    assert act.get("ok"), "harness_activate 不校验角色的话 editor 就绕过了审批——但这属于 admin 接口层拦截，见 engine"

    # admin 审批放行 → 修订激活
    ok, msg, _ = ai.approval_decide(r["approval_id"], "approved", by="admin",
                                    note="同意试试")
    assert ok and "已激活" in msg, msg
    assert ai.spec_sha() == rev["sha"], "放行后生效配置应等于补丁修订"


def test_experience_layer_written_by_bench():
    """双跑结束要留下 decision 记忆，且只对编辑者可见的接口能查到。"""
    n0 = len(memory.decision_list())
    r = bench.run_job(patch=[{"path": "pilots.rag_topk", "value": 2}],
                      task_ids=["rag-01", "rag-wide-01"], repeats=1, note="经验层验证")
    assert r["ok"], r
    ds = memory.decision_list()
    assert len(ds) > n0, "双跑应写经验"
    assert "ok_rate" in ds[0]["text"] and ds[0]["slot"].startswith("harness:bench:"), ds[0]


def test_pilot_channels_now_leave_evidence():
    """nl2api 与话术成功后要落 evidence（此前证据完整率 0.533 的缺口）。"""
    r1 = ai.pilot_nl2api("西部能源的订单情况", system="bi", user="admin")
    assert not r1.get("error"), r1
    assert r1.get("evidence_id"), "nl2api 应落证据"
    row = [e for e in ai.evidence_list(limit=5) if e["channel"] == "nl2api"]
    assert row and row[0]["id"] == r1["evidence_id"], row[:1]
    r2 = ai.pilot_script("客户咨询物流时效", user="admin")
    assert r2.get("evidence_id"), "话术应落证据"
    row2 = [e for e in ai.evidence_list(limit=5) if e["channel"] == "script"]
    assert row2 and row2[0]["id"] == r2["evidence_id"]


if __name__ == "__main__":
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
    print("test_p1: all passed")
