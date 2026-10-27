# -*- coding: utf-8 -*-
"""对照式判定（P0-3）的测试：只用构造出来的 bench_run 行，不真跑任务。

为什么不真跑：这一层要验证的是"差异如何被归纳"，而真跑会掺进模型与网络的随机性。
把判据泡在噪声里，测到的就不是判据。真跑的端到端在 test_bench.py 里。

覆盖的六种结论：
  ① 全都一样 + 代价也没动   → 测不出差别，别编故事
  ② 稳定变坏（3/3 → 0/3）  → 先别上线，并点名任务与新的失败原因
  ③ 只差一次（3/3 → 2/3）  → 判不了，必须自己声明样本不足
  ④ 稳定变好 + 成本大涨     → 可以上线，但要把账算给你看
  ⑤ 没变好但更贵 / 更便宜   → 只买到成本 / 白捡的省
  ⑥ 一好一坏               → 有得有失，先加样本
另外验证：跨轮对照、打转与出错进结论、坏参数不崩。
"""
import json
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEST_DB = os.path.join(BASE_DIR, "tests", "test_compare.db")

for _p in (TEST_DB, TEST_DB + "-wal", TEST_DB + "-shm"):
    if os.path.exists(_p):
        os.remove(_p)

os.environ["FEINAO_DB"] = TEST_DB
for _k in ("FEINAO_QWEN_KEY", "FEINAO_DEEPSEEK_KEY", "FEINAO_HUNYUAN_KEY"):
    os.environ[_k] = ""

sys.path.insert(0, os.path.join(BASE_DIR, "backend"))
import ai  # noqa: E402
import bench  # noqa: E402

ai.DB_PATH = TEST_DB
ai.init_db()
bench.init_db()

try:
    import pytest

    @pytest.fixture(autouse=True)
    def _pin_db():
        """别的用例会改 ai.DB_PATH（全局状态共享），每条用例前把库钉回来。"""
        orig = ai.DB_PATH
        ai.DB_PATH = TEST_DB
        ai.init_db()
        bench.init_db()
        yield
        ai.DB_PATH = orig
except ImportError:  # 直接 python 运行本文件时没有 pytest
    pass

DEFAULTS = {"steps": 3, "tokens": 100, "evidence": 1, "denied": 0,
            "dups": 0, "errors": 0, "approvals": 0}


def _row(job_id, stage, tid, kind, rep, ok, fail="", **kw):
    m = dict(DEFAULTS)
    m.update(kw)
    conn = ai.db()
    conn.execute(
        "INSERT INTO bench_run(job_id,stage,rev_id,rev_sha,task_id,kind,rep,ok,fail,"
        "llm_calls,steps,tool_calls,denied,errors,dups,approvals,tokens,chars,"
        "evidence,ms,provider,created) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (job_id, stage, 1, "sha-a", tid, kind, rep, ok, fail, 1, m["steps"], 2,
         m["denied"], m["errors"], m["dups"], m["approvals"], m["tokens"], 200,
         m["evidence"], 10, "mock", "2026-10-27T22:00:00"))
    conn.commit()
    conn.close()


def _job(tasks, repeats=3):
    """tasks: [{id, kind, a:[0/1...], b:[0/1...], a_kw:{}, b_kw:{}}]"""
    conn = ai.db()
    cur = conn.execute(
        "INSERT INTO bench_job(base_rev,cand_rev,base_sha,cand_sha,task_ids,repeats,note,by,"
        "created,status) VALUES(1,2,'sha-a','sha-b',?,?,'测试','t','2026-10-27T22:00:00','done')",
        (json.dumps([t["id"] for t in tasks]), repeats))
    jid = cur.lastrowid
    conn.commit()
    conn.close()
    for t in tasks:
        for rep, ok in enumerate(t["a"]):
            _row(jid, "base", t["id"], t["kind"], rep, ok, **(t.get("a_kw") or {}))
        for rep, ok in enumerate(t["b"]):
            _row(jid, "cand", t["id"], t["kind"], rep, ok, **(t.get("b_kw") or {}))
    return jid


def _flat(n=6):
    """n 个任务，两版全过、代价一致：这是"什么都没发生"的对照组。"""
    return [{"id": "rag-%02d" % i, "kind": "rag", "a": [1, 1, 1], "b": [1, 1, 1]}
            for i in range(n)]


# ---------------------------------------------------------------- ① 诚实

def test_no_difference_is_reported_as_no_difference():
    """全都一样时不许编出"略有提升"这种话：结论必须是"测不出差别"。"""
    jid = _job(_flat())
    out = bench.compare(jid)
    v = out["verdict"]
    assert v["tone"] == "flat", v
    assert "测不出差别" in v["headline"], v["headline"]
    assert "任务集没覆盖" in v["suggest"], v["suggest"]
    assert v["decisive"] == {"better": 0, "worse": 0, "unclear": 0, "same": 6}
    # 没有总分这种东西：输出里不许出现 score / grade 字段
    assert "score" not in json.dumps(out, ensure_ascii=False).lower()


# ---------------------------------------------------------------- ② 变坏

def test_stable_regression_is_decisive():
    """3/3 → 0/3 是稳定变坏：要给"先别上线"，并点名任务与新的失败原因。"""
    tasks = _flat(5)
    tasks[2]["b"] = [0, 0, 0]
    tasks[2]["b_kw"] = {"fail": "没有执行预期动作 search_docs（实际：无）", "denied": 2}
    jid = _job(tasks)
    v = bench.compare(jid)["verdict"]
    assert v["tone"] == "worse", v
    assert v["suggest"] == "先别上线", v
    assert "rag-02" in v["headline"], v["headline"]
    assert "search_docs" in v["headline"], "新的失败原因要进结论：%s" % v["headline"]
    assert v["decisive"]["worse"] == 1 and v["decisive"]["better"] == 0
    assert any("被策略拒" in line for line in v["lines"]), "denied 涨了必须出现在过程行：%s" % v["lines"]


# ---------------------------------------------------------------- ③ 判不了

def test_single_flip_is_not_a_verdict():
    """只差一次执行（3/3 → 2/3）不许判胜负，必须标成判不了并声明样本不足。"""
    tasks = _flat(5)
    tasks[1]["b"] = [1, 1, 0]
    jid = _job(tasks)
    v = bench.compare(jid)["verdict"]
    assert v["decisive"]["unclear"] == 1, v
    assert v["decisive"]["better"] == 0 and v["decisive"]["worse"] == 0
    assert "加样本" in v["headline"], v["headline"]
    assert any("抖动" in c for c in v["caveats"]), v["caveats"]


def test_one_run_per_cell_cannot_conclude():
    """每格只跑 1 次时，1/1 → 0/1 也只是"看起来掉了"，不能当头号结论。"""
    tasks = _flat(4)
    tasks[0]["a"], tasks[0]["b"] = [1], [0]
    jid = _job(tasks, repeats=1)
    v = bench.compare(jid)["verdict"]
    assert v["tone"] == "flat", "单次执行不许判变坏：%s" % v
    assert v["decisive"]["unclear"] == 1, v
    assert any("每格只跑了 1 次" in c for c in v["caveats"]), v["caveats"]


# ---------------------------------------------------------------- ④⑤ 值不值

def test_better_but_costlier_is_a_trade_not_a_victory():
    """通过了但贵 60%：结论要同时给出"可以上线"和这个价，不能只说好。"""
    tasks = _flat(6)
    for t in tasks[:2]:
        t["a"] = [0, 0, 0]
    for t in tasks:
        t["b_kw"] = {"tokens": 160, "steps": 4}
    jid = _job(tasks)
    v = bench.compare(jid)["verdict"]
    assert v["tone"] == "better", v
    assert "可以上线" in v["headline"], v["headline"]
    assert "+60%" in v["headline"], "代价必须写进结论行：%s" % v["headline"]
    assert v["suggest"] in ("可以上线", "能上线，先算成本"), v["suggest"]


def test_same_result_but_more_expensive_is_only_cost():
    """通过率一点没变、token 涨 60%：这叫只买到成本。"""
    tasks = _flat(6)
    for t in tasks:
        t["b_kw"] = {"tokens": 160, "steps": 4}
    v = bench.compare(_job(tasks))["verdict"]
    assert v["tone"] == "wash", v
    assert "只买到成本" in v["headline"], v["headline"]
    assert "别上" in v["suggest"], v["suggest"]


def test_same_result_but_cheaper_is_free_lunch():
    """同样的通过率、token 砍半：这是白捡的省，要明确说可以上。"""
    tasks = _flat(6)
    for t in tasks:
        t["b_kw"] = {"tokens": 50, "steps": 2}
    v = bench.compare(_job(tasks))["verdict"]
    assert v["tone"] == "cheaper", v
    assert "省成本" in v["suggest"], v["suggest"]


def test_mixed_is_not_an_improvement():
    """一好一坏：净额可能为 0，但结论要说清是换了个坑。"""
    tasks = _flat(6)
    tasks[0]["b"] = [0, 0, 0]
    tasks[0]["b_kw"] = {"fail": "回答里没有预期内容"}
    tasks[1]["a"] = [0, 0, 0]
    jid = _job(tasks)
    v = bench.compare(jid)["verdict"]
    assert v["tone"] == "mixed", v
    assert "换了个坑" in v["headline"], v["headline"]
    assert v["suggest"] == "加样本再判", v["suggest"]
    assert v["decisive"]["better"] == 1 and v["decisive"]["worse"] == 1


# ---------------------------------------------------------------- ⑥ 过程与跨轮

def test_spinning_and_errors_show_up_even_when_ok_rate_is_flat():
    """通过率没变，但更爱打转/更容易出错，也必须被看见 —— 这是绝对打分看不见的退化。"""
    tasks = _flat(6)
    for t in tasks:
        t["b_kw"] = {"dups": 2, "errors": 1}
    v = bench.compare(_job(tasks))["verdict"]
    proc = [line for line in v["lines"] if line.startswith("过程：")]
    assert proc, "过程行缺失：%s" % v["lines"]
    assert "重复动作 6 个任务上升（0 → 2）" in proc[0], proc[0]
    assert "执行出错 6 个任务上升（0 → 1）" in proc[0], proc[0]


def test_cross_job_compares_two_candidates():
    """跨轮对照：第二轮候选 vs 第一轮候选，默认比两轮的 cand 阶段。"""
    j1 = _job(_flat(4))
    tasks = _flat(4)
    tasks[0]["b"] = [0, 0, 0]
    tasks[0]["b_kw"] = {"fail": "执行报错：boom"}
    j2 = _job(tasks)
    out = bench.compare(j1, j2)
    assert out["a"]["job"] == j1 and out["a"]["stage"] == "cand"
    assert out["b"]["job"] == j2 and out["b"]["stage"] == "cand"
    assert out["verdict"]["tone"] == "worse", out["verdict"]


def test_bad_input_does_not_explode():
    """不存在的作业、空阶段：给一句能看懂的话，不要抛栈。"""
    assert "不存在" in bench.compare(99999)["error"]
    empty = _job([{"id": "rag-01", "kind": "rag", "a": [1, 1, 1], "b": []}])
    out = bench.compare(empty)
    assert "没有可比数据" in out["error"], out


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
