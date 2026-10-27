# -*- coding: utf-8 -*-
"""基准任务集 + 双跑对照（P0-2）的测试。

四层，从"标尺准不准"到"真能抓出退步"：
  ① 任务集本身：数量、覆盖、id 唯一、字段齐；标尺坏了后面全是错数
  ② 双跑：同一批任务在 base 与候选上各跑 N 次，逐条指标落库，配置跑完必须还原
  ③ 退步可见：把 search_docs 预算改成 0，候选 ok_rate 必须掉下来，并说清为什么
  ④ 权限与守卫：ops 不能跑（会临时切配置）、锁占用时拒绝并行、写错键名直接报错

全部用例只在临时 spec 副本与临时库上操作，不碰仓库里的 backend/spec.json。
"""
import io
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEST_DB = os.path.join(BASE_DIR, "tests", "test_bench.db")
TEST_SPEC = os.path.join(BASE_DIR, "tests", "test_bench_spec.json")
ORIG_SPEC = os.path.join(BASE_DIR, "backend", "spec.json")

for _p in (TEST_DB, TEST_DB + "-wal", TEST_DB + "-shm"):
    if os.path.exists(_p):
        os.remove(_p)
shutil.copyfile(ORIG_SPEC, TEST_SPEC)
ORIGINAL_TEXT = io.open(ORIG_SPEC, encoding="utf-8").read()

os.environ["FEINAO_DB"] = TEST_DB
os.environ["FEINAO_SPEC"] = TEST_SPEC
for _k in ("FEINAO_QWEN_KEY", "FEINAO_DEEPSEEK_KEY", "FEINAO_HUNYUAN_KEY"):
    os.environ[_k] = ""

sys.path.insert(0, os.path.join(BASE_DIR, "backend"))
import ai  # noqa: E402
import bench  # noqa: E402
import demo  # noqa: E402

# 双跑要真跑任务，得确认拿到的是真的 llm_chat（别的用例会把它换成桩且不还原），
# 以及自己的库和配置副本（别的用例可能先 import 过 ai，全局状态是共享的）。
_REAL_LLM = ai.llm_chat
DEMO_PORT = int(os.environ.get("FEINAO_BENCH_DEMO_PORT") or 8095)
UPSTREAM_ENVS = ("FEINAO_UPSTREAM_KMS", "FEINAO_UPSTREAM_BI", "FEINAO_UPSTREAM_CC")


def _restore_spec():
    """磁盘副本 + 内存配置 + active 指针，一起回到原始版。"""
    with io.open(TEST_SPEC, "w", encoding="utf-8", newline="\n") as f:
        f.write(ORIGINAL_TEXT)
    ai.reload_spec()


def _pin_active_base():
    _, base = ai.harness_snapshot(note="原始配置")
    conn = ai.db()
    conn.execute("UPDATE harness_rev SET active=0")
    conn.execute("UPDATE harness_rev SET active=1 WHERE id=?", (base["id"],))
    conn.commit()
    conn.close()


def _setup():
    demo.start_upstream(DEMO_PORT)
    for s in ("KMS", "BI", "CC"):
        os.environ["FEINAO_UPSTREAM_" + s] = "http://127.0.0.1:%d/%s" % (DEMO_PORT, s.lower())
    _restore_spec()
    ai.init_db()
    bench.init_db()
    demo.seed_docs()   # 幂等：种子文档已存在就跳过
    _pin_active_base()


def _teardown(db_path, spec_path, llm, env_orig):
    ai.DB_PATH, ai.SPEC_PATH, ai.llm_chat = db_path, spec_path, llm
    for k in UPSTREAM_ENVS:
        if env_orig.get(k) is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = env_orig[k]
    _restore_spec()


try:
    import pytest

    @pytest.fixture(autouse=True)
    def _isolate():
        """每条用例独占库与配置副本：双跑会写 15 个任务的执行记录，不能进别人的库。"""
        orig = {"db": ai.DB_PATH, "spec": ai.SPEC_PATH, "llm": ai.llm_chat}
        orig.update({k: os.environ.get(k) for k in UPSTREAM_ENVS})
        ai.DB_PATH, ai.SPEC_PATH, ai.llm_chat = TEST_DB, TEST_SPEC, _REAL_LLM
        _setup()
        yield
        _teardown(orig["db"], orig["spec"], orig["llm"], orig)
except ImportError:  # 直接 python 运行本文件时没有 pytest
    pass


# ---------------------------------------------------------------- ① 任务集

def test_task_set_is_a_usable_ruler():
    """任务集是标尺：数量够、四类能力全覆盖、id 不重、字段齐全。"""
    tasks = bench.load_tasks()
    assert 12 <= len(tasks) <= 20, "任务数应在 12-20 之间，实际 %d" % len(tasks)
    kinds = {t["kind"] for t in tasks}
    assert kinds == set(bench.KINDS), "五类任务要全覆盖，实际 %s" % ",".join(sorted(kinds))
    ids = [t["id"] for t in tasks]
    assert len(ids) == len(set(ids)), "任务 id 不能重复"
    for t in tasks:
        assert t.get("title"), "任务 %s 缺少人话标题" % t["id"]
        assert t.get("expect"), "任务 %s 没有可判定的验收条件" % t["id"]


def test_task_set_rejects_broken_input(tmp_path):
    """坏任务集必须当场报错，而不是跑出一堆假指标。"""
    p = tmp_path / "bad.json"
    p.write_text(json.dumps({"tasks": [{"id": "x", "kind": "rag"}]}), encoding="utf-8")
    try:
        bench.load_tasks(str(p))
    except ValueError as e:
        assert "rag-0" not in str(e) and ("缺少字段" in str(e) or "q" in str(e))
    else:
        raise AssertionError("缺 q 的 rag 任务应报错")

    p.write_text(json.dumps({"tasks": [
        {"id": "a", "kind": "rag", "q": "x"},
        {"id": "a", "kind": "react", "goal": "y"},
    ]}), encoding="utf-8")
    try:
        bench.load_tasks(str(p))
    except ValueError as e:
        assert "重复" in str(e)
    else:
        raise AssertionError("重复 id 应报错")


# ---------------------------------------------------------------- ② 双跑

def _sha_disk():
    return ai.spec_sha(io.open(TEST_SPEC, encoding="utf-8").read())


def test_dual_run_records_metrics_and_restores_spec():
    """base 与候选各跑一遍：指标逐条落库，跑完配置必须回到磁盘原文。"""
    before = _sha_disk()
    r = bench.run_job(patch=[{"path": "pilots.rag_topk", "value": 3}],
                      task_ids=["rag-01", "react-02", "approval-01"],
                      repeats=2, note="双跑落库")
    assert r.get("ok") and not r.get("error"), r
    assert r["base_rev"] != r["cand_rev"], "候选应是新落的一条修订"
    assert r["restored"] is True, "跑完内存配置必须还原"
    assert _sha_disk() == before, "双跑不许动磁盘上的 spec.json"

    rows = bench.job_detail(r["job_id"])["runs"]
    assert len(rows) == 3 * 2 * 2, "3 个任务 × 2 次重复 × 2 个版本 = 12 条，实际 %d" % len(rows)
    assert {x["stage"] for x in rows} == {"base", "cand"}
    assert all(x["provider"] for x in rows), "provider 要记下来，否则没法解释差异"
    for x in rows:
        for k in ("ok", "llm_calls", "steps", "tool_calls", "denied", "approvals",
                  "tokens", "chars", "evidence", "ms"):
            assert x[k] is not None, "指标 %s 不能是 None" % k

    s = r["summary"]
    assert s["base"]["ok_rate"] == 1.0, "基线任务应全部通过：%s" % s["base"]["per_task"]
    assert s["base"]["runs"] == 6 and s["cand"]["runs"] == 6
    # approval 任务只演练交接：审批单被自动拒绝，知识库不该多出一篇
    appr = [x for x in s["base"]["per_task"] if x["task_id"] == "approval-01"][0]
    assert appr["approvals"] == 1, "审批交接应记 1 次人工介入"
    conn = ai.db()
    n = conn.execute("SELECT COUNT(*) c FROM docs WHERE source='agent'").fetchone()["c"]
    conn.close()
    assert n == 0, "bench 自动拒绝后不该写库，实际写入 %d 篇" % n

    jobs = bench.list_jobs()
    assert jobs and jobs[0]["id"] == r["job_id"] and jobs[0]["status"] == "done"
    assert jobs[0]["ok_rate_base"] == 1.0


def test_candidate_uses_existing_rev():
    """候选也可以是已有修订：不写 patch 时直接比两年版。"""
    spec = json.loads(ORIGINAL_TEXT)
    spec["pilots"]["rag_topk"] = 4
    with io.open(TEST_SPEC, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(spec, ensure_ascii=False, indent=2))
    ai.reload_spec()
    _, cand = ai.harness_snapshot(note="改 topk", by="admin")
    base = [x for x in ai.harness_list()["rows"] if x["sha"] == ai.spec_sha(ORIGINAL_TEXT)][0]
    r = bench.run_job(base_rev=base["id"], cand_rev=cand["id"], task_ids=["rag-01"], repeats=1)
    assert r.get("ok"), r
    assert {x["rev_id"] for x in bench.job_detail(r["job_id"])["runs"]} == {base["id"], cand["id"]}


# ---------------------------------------------------------------- ③ 退步可见

def test_regression_is_visible():
    """把 search_docs 预算改成 0，候选必须掉分，而且理由是代码给的、不是模型编的。"""
    r = bench.run_job(patch=[{"path": "tool_policies.search_docs.max_calls_per_run", "value": 0}],
                      task_ids=["react-01"], repeats=2, note="预算清零")
    assert r.get("ok"), r
    s = r["summary"]
    assert s["base"]["ok_rate"] == 1.0, s["base"]
    assert s["cand"]["ok_rate"] == 0.0, "改坏配置后候选不该还满分：%s" % s["cand"]
    assert s["delta"]["ok_rate"] == -1.0
    bad = s["cand"]["per_task"][0]
    assert bad["denied"] >= 1, "被预算挡住了，denied 应大于 0"
    assert "search_docs" in bad["fails"][0], "失败原因要指名是哪个动作没跑成：%s" % bad["fails"]


# ---------------------------------------------------------------- ④ 守卫

def test_guardrails():
    """并行、错键名、未知任务、参数互斥——都在跑之前拦住。"""
    assert bench._LOCK.acquire(False)
    try:
        r = bench.run_job(patch=[{"path": "pilots.rag_topk", "value": 3}], repeats=1)
        assert "正在进行" in r["error"], "并行双跑必须拒绝：%s" % r
    finally:
        bench._LOCK.release()

    r = bench.run_job(patch=[{"path": "pilots.rag_topkk", "value": 3}], repeats=1)
    assert "没有 pilots.rag_topkk" in r["error"], "写错键名要当场报错（否则双跑会假报零差异）：%s" % r

    r = bench.run_job(patch=[{"path": "pilots.rag_topk", "value": 3}],
                      task_ids=["ghost-99"], repeats=1)
    assert "不认识的任务" in r["error"]

    r = bench.run_job(cand_rev=1, patch=[{"path": "pilots.rag_topk", "value": 3}], repeats=1)
    assert "二选一" in r["error"]

    r = bench.run_job(repeats=1)
    assert "cand_rev" in r["error"] and "patch" in r["error"]

    # 重复次数是配给，不是许愿池：0 按 1 跑，超过 5 按 5 跑
    r = bench.run_job(patch=[{"path": "pilots.rag_topk", "value": 3}],
                      task_ids=["rag-01"], repeats=0)
    assert r["repeats"] == 1
    assert bench._LOCK.acquire(False), "跑完必须释放锁"
    bench._LOCK.release()


# ---------------------------------------------------------------- ⑤ 起真服务

API_PORT = 8063
API = "http://127.0.0.1:%d" % API_PORT


def _req(path, method="GET", body=None, token=None):
    r = urllib.request.Request(API + path, method=method)
    if body is not None:
        r.data = json.dumps(body).encode()
        r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(r, timeout=60) as x:
            return x.status, json.loads(x.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8", "ignore") or "{}")


def test_api_requires_admin():
    """ops 看不了也跑不了（双跑会临时切配置）；admin 跑得动，结果带两列指标。"""
    env = dict(os.environ,
               FEINAO_DB=os.path.join(BASE_DIR, "tests", "test_bench_api.db"),
               FEINAO_SPEC=TEST_SPEC, FEINAO_PORT=str(API_PORT), FEINAO_DEMO="0")
    for p in (env["FEINAO_DB"], env["FEINAO_DB"] + "-wal", env["FEINAO_DB"] + "-shm"):
        if os.path.exists(p):
            os.remove(p)
    # 服务进程用独立的库：先把知识库灌好，否则 RAG 类任务会因为"没有文档"而假失败
    import sqlite3
    conn = sqlite3.connect(env["FEINAO_DB"])
    conn.execute("CREATE TABLE IF NOT EXISTS docs(id INTEGER PRIMARY KEY AUTOINCREMENT, "
                 "title TEXT, body TEXT, source TEXT, created TEXT)")
    for title, body in demo.SEED_DOCS:
        conn.execute("INSERT INTO docs(title,body,source,created) VALUES(?,?,'demo','2026-10-27T00:00:00')",
                     (title, body))
    conn.commit()
    conn.close()
    proc = subprocess.Popen([sys.executable, "server.py"], cwd=os.path.join(BASE_DIR, "backend"), env=env)
    try:
        for _ in range(30):
            time.sleep(0.4)
            try:
                if _req("/api/health")[0] == 200:
                    break
            except Exception:
                continue
        else:
            raise AssertionError("服务没起来")

        _, j = _req("/api/login", "POST", {"username": "admin", "password": "admin123"})
        admin = j["token"]
        _, j = _req("/api/login", "POST", {"username": "ops01", "password": "ops123456"})
        ops = j["token"]

        assert _req("/api/bench", token=ops)[0] == 403, "ops 不该看到任务集与双跑记录"
        st, _ = _req("/api/bench/run", "POST", {"patch": [{"path": "pilots.rag_topk", "value": 3}]}, token=ops)
        assert st == 403, "ops 不该能触发双跑，实际 %d" % st

        st, j = _req("/api/bench", token=admin)
        assert st == 200 and len(j["tasks"]) >= 12 and j["active"], j

        st, j = _req("/api/bench/run", "POST",
                     {"patch": [{"path": "pilots.rag_topk", "value": 3}],
                      "tasks": ["rag-01"], "repeats": 1, "note": "API 双跑"}, token=admin)
        assert st == 200 and j.get("ok"), j
        s = j["summary"]
        assert s["base"]["ok_rate"] == 1.0 and s["cand"]["ok_rate"] == 1.0

        st, j = _req("/api/bench/detail?id=%d" % j["job_id"], token=admin)
        assert st == 200 and len(j["runs"]) == 2, "1 任务 × 1 次 × 2 版本 = 2 条"

        st, _ = _req("/api/bench/run", "POST", {"patch": [{"path": "pilots.rag_topk", "value": 3}],
                                                "repeats": 9}, token=admin)
        assert st == 422, "repeats 超上限应 422"
        st, _ = _req("/api/bench/detail?id=99999", token=admin)
        assert st == 404
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    _orig = {"db": ai.DB_PATH, "spec": ai.SPEC_PATH, "llm": ai.llm_chat}
    _orig.update({k: os.environ.get(k) for k in UPSTREAM_ENVS})
    ai.DB_PATH, ai.SPEC_PATH, ai.llm_chat = TEST_DB, TEST_SPEC, _REAL_LLM
    _setup()
    try:
        test_task_set_is_a_usable_ruler()
        test_dual_run_records_metrics_and_restores_spec()
        test_candidate_uses_existing_rev()
        test_regression_is_visible()
        test_guardrails()
    finally:
        _teardown(_orig["db"], _orig["spec"], _orig["llm"], _orig)
    print("test_bench: 5/5 passed（坏任务集与 API 权限用例用 pytest 跑）")
