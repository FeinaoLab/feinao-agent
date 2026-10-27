# -*- coding: utf-8 -*-
"""基准任务集 + 双跑对照（P0-2）。

为什么要有这一层：没有固定的任务集，"这次改配置到底有没有变好"就只能靠感觉；
有了它，改配置变成一次可复现的对照实验：

    bench/tasks.json（15 个任务，覆盖 RAG / NL2API / 话术 / ReAct / 审批）
      → 同一批任务在 base 修订与候选修订上各跑 N 次
      → 每次执行只记录本来就存在的事实（步数、工具调用、被拒、审批、token、证据）
      → 汇总成 base / cand 两列与一列 delta

三条自我约束：
  1. 不引入新的采集：所有数字都能从 task_steps / approvals / usage / evidence 里复查。
  2. 不做模型打分：ok 由任务自带的 expect 判定（代码写的判据），
     模型不稳定带来的噪声靠"每格跑 N 次取均值"压下去，而不是靠更聪明的评判者。
  3. 双跑只在内存里切配置（reload_spec），不写盘、不动 active 标记；
     跑完（包括异常）一定把内存恢复到磁盘原文，并用 sha 自检。

并发注意：双跑期间内存里的 SPEC 会在两个修订之间来回切，其他请求看到的配置会跟着变。
这是"在安静时段做实验"的操作，不是线上常态，所以接口只给 admin，且有全局锁串行化。
"""
import json
import os
import re
import threading
import time

import ai

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TASKS_PATH = os.environ.get("FEINAO_BENCH_TASKS") or os.path.abspath(
    os.path.join(BASE_DIR, "..", "bench", "tasks.json"))

KINDS = ("rag", "nl2api", "script", "react", "approval")
# 每种任务必须带的字段（缺了跑不了，加载时就报错，别等到跑一半）
NEEDS = {"rag": ("q",), "nl2api": ("q",), "script": ("scenario",),
         "react": ("goal",), "approval": ("goal",)}
AVG_KEYS = ("llm_calls", "steps", "tool_calls", "denied", "approvals",
            "tokens", "chars", "evidence", "ms")

_LOCK = threading.Lock()


# ---------------------------------------------------------------- 任务集

def load_tasks(path=None):
    """读基准任务集并做结构校验。坏任务集比没有任务集更糟，所以一律抛错。"""
    p = path or TASKS_PATH
    with open(p, encoding="utf-8") as f:
        data = json.load(f)
    tasks = data.get("tasks") if isinstance(data, dict) else data
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("%s 里没有 tasks 列表" % p)
    seen = set()
    for t in tasks:
        if not isinstance(t, dict) or not t.get("id"):
            raise ValueError("任务缺少 id：%r" % (t,))
        tid = t["id"]
        if tid in seen:
            raise ValueError("任务 id 重复：%s" % tid)
        seen.add(tid)
        kind = t.get("kind")
        if kind not in KINDS:
            raise ValueError("任务 %s 的 kind 只能是 %s，实际 %r" % (tid, "/".join(KINDS), kind))
        for field in NEEDS[kind]:
            if not t.get(field):
                raise ValueError("任务 %s（%s）缺少字段 %s" % (tid, kind, field))
        if not isinstance(t.get("expect") or {}, dict):
            raise ValueError("任务 %s 的 expect 必须是对象" % tid)
    return tasks


# ---------------------------------------------------------------- 落库

def init_db():
    conn = ai.db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS bench_job(
        id INTEGER PRIMARY KEY AUTOINCREMENT, base_rev INT, cand_rev INT,
        base_sha TEXT, cand_sha TEXT, task_ids TEXT, repeats INT, note TEXT, by TEXT,
        created TEXT, status TEXT DEFAULT 'running', summary TEXT, error TEXT);
    CREATE TABLE IF NOT EXISTS bench_run(
        id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INT, stage TEXT, rev_id INT, rev_sha TEXT,
        task_id TEXT, kind TEXT, rep INT, ok INT, fail TEXT, llm_calls INT, steps INT,
        tool_calls INT, denied INT, approvals INT, tokens INT, chars INT,
        evidence INT, ms INT, provider TEXT, created TEXT);
    """)
    conn.commit()
    conn.close()


def _rows(job_id):
    conn = ai.db()
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM bench_run WHERE job_id=? ORDER BY id", (int(job_id),))]
    conn.close()
    return rows


def _job_row(job_id):
    conn = ai.db()
    r = conn.execute("SELECT * FROM bench_job WHERE id=?", (int(job_id),)).fetchone()
    conn.close()
    if not r:
        return None
    d = dict(r)
    try:
        d["summary"] = json.loads(d.get("summary") or "null")
    except ValueError:
        d["summary"] = None
    try:
        d["task_ids"] = json.loads(d.get("task_ids") or "[]")
    except ValueError:
        d["task_ids"] = []
    return d


def list_jobs(limit=20):
    conn = ai.db()
    rows = [dict(r) for r in conn.execute(
        "SELECT id,base_rev,cand_rev,base_sha,cand_sha,task_ids,repeats,note,by,created,status,summary "
        "FROM bench_job ORDER BY id DESC LIMIT ?", (int(limit),))]
    conn.close()
    for d in rows:
        try:
            s = json.loads(d.get("summary") or "null") or {}
            d["ok_rate_base"] = (s.get("base") or {}).get("ok_rate")
            d["ok_rate_cand"] = (s.get("cand") or {}).get("ok_rate")
            d["task_count"] = len(json.loads(d.get("task_ids") or "[]"))
        except ValueError:
            pass
        d.pop("summary", None)
        d.pop("task_ids", None)
    return rows


def job_detail(job_id):
    d = _job_row(job_id)
    if not d:
        return {"error": "双跑作业 #%s 不存在" % job_id}
    d["runs"] = _rows(job_id)
    return d


def overview():
    try:
        tasks = load_tasks()
        err = None
    except Exception as e:  # 任务集坏了也要能打开页面看到原因
        tasks, err = [], str(e)[:200]
    return {"tasks": tasks, "task_error": err, "jobs": list_jobs(),
            "active": ai.harness_list()["active"], "repeats_default": 3,
            "kinds": list(KINDS),
            "note": "同一批任务在 base 与候选修订上各跑 N 次；ok 由任务自带 expect 判定，不由模型打总分"}


# ---------------------------------------------------------------- 修订与候选

def _rev(rid):
    row = ai.harness_get(int(rid))
    if not row:
        raise ValueError("修订 #%s 不存在" % rid)
    return row


def active_rev_id():
    conn = ai.db()
    r = conn.execute("SELECT id FROM harness_rev WHERE active=1 ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()
    return r["id"] if r else None


def _set_path(obj, path, value):
    """按 a.b[0].c 写入一个键。只用于"改一个参数试一下"，不支持结构性重排。"""
    parts = re.findall(r"[^.\[\]]+|\[\d+\]", str(path or ""))
    if not parts:
        raise ValueError("path 为空")
    cur = obj
    for i, p in enumerate(parts[:-1]):
        nxt = parts[i + 1]
        if p.startswith("["):
            idx = int(p[1:-1])
            if not isinstance(cur, list):
                raise ValueError("%s 的父级不是列表" % path)
            while len(cur) <= idx:
                cur.append({} if not nxt.startswith("[") else [])
            cur = cur[idx]
        else:
            if not isinstance(cur, dict):
                raise ValueError("%s 的父级不是对象" % path)
            default = [] if nxt.startswith("[") else {}
            cur = cur.setdefault(p, default)
    last = parts[-1]
    if last.startswith("["):
        idx = int(last[1:-1])
        if not isinstance(cur, list):
            raise ValueError("%s 的父级不是列表" % path)
        while len(cur) <= idx:
            cur.append(None)
        cur[idx] = value
    else:
        if not isinstance(cur, dict):
            raise ValueError("%s 的父级不是对象" % path)
        cur[last] = value
    return obj


def _rev_from_patch(base_text, patches, note, by):
    """在 base 之上改若干键，落成一条"候选修订"（不进 active，也不写盘）。

    路径必须已存在：写错一个字就悄悄多一个没人读的键，双跑会显示"零差异"，
    那比报错更误导人。
    """
    obj = json.loads(base_text)
    flat = ai._spec_flat(obj)
    for p in patches or []:
        if not isinstance(p, dict) or not p.get("path"):
            raise ValueError("patch 每项都要有 path")
        path = p["path"]
        if path not in flat:
            raise ValueError("配置里没有 %s 这个键（先 GET /api/ai/policy 看可用路径）" % path)
        _set_path(obj, path, p.get("value"))
    text = json.dumps(obj, ensure_ascii=False, indent=2) + "\n"
    ai.load_spec_text(text, "候选配置")  # 坏配置不许进库，跟飞构同一条规矩
    sha = ai.spec_sha(text)
    conn = ai.db()
    try:
        row = conn.execute("SELECT * FROM harness_rev WHERE sha=?", (sha,)).fetchone()
        if row:
            rid = row["id"]
        else:
            cur = conn.execute(
                "INSERT INTO harness_rev(sha,content,note,by,created,active) VALUES(?,?,?,?,?,0)",
                (sha, text, "候选：" + (note or "bench 试改"), by, ai._now()))
            rid = cur.lastrowid
            conn.commit()
    finally:
        conn.close()
    return rid, sha


# ---------------------------------------------------------------- 跑一条任务

def _usage_mark():
    conn = ai.db()
    r = conn.execute("SELECT COALESCE(MAX(id),0) m FROM usage").fetchone()
    conn.close()
    return int(r["m"])


def _usage_tokens(mark):
    conn = ai.db()
    r = conn.execute("SELECT COALESCE(SUM(pt+ct),0) t FROM usage WHERE id>?", (int(mark),)).fetchone()
    conn.close()
    return int(r["t"] or 0)


def _evidence_ok(eid):
    if not eid:
        return 0
    conn = ai.db()
    r = conn.execute("SELECT sources, trace FROM evidence WHERE id=?", (int(eid),)).fetchone()
    conn.close()
    if not r:
        return 0
    has_src = (r["sources"] or "[]") not in ("[]", "")
    has_trace = (r["trace"] or "[]") not in ("[]", "")
    return 1 if (has_src or has_trace) else 0


def _approvals_of(task_id):
    if not task_id:
        return 0
    conn = ai.db()
    r = conn.execute("SELECT COUNT(*) c FROM approvals WHERE task_id=?", (int(task_id),)).fetchone()
    conn.close()
    return int(r["c"])


def _dispatch(task, user):
    kind = task.get("kind")
    if kind == "rag":
        return ai.pilot_ask(task["q"], user)
    if kind == "nl2api":
        return ai.pilot_nl2api(task["q"], task.get("system") or "bi", user)
    if kind == "script":
        return ai.pilot_script(task["scenario"], task.get("tone") or "专业友好", user)
    if kind == "react":
        return ai.react_run(task["goal"], user)
    # approval：跑到"需要人工确认"就算完成交接，然后自动拒绝——只演练闸门，不产生数据变更
    res = ai.react_run(task["goal"], user)
    aid = res.get("approval_id")
    if aid and res.get("status") == "waiting_approval":
        out = ai.approval_handle(aid, "rejected", user,
                                 note="bench 自动拒绝：只演练审批交接，不改知识库", resume=True)
        res["_bench_approval"] = {"approval_id": aid, "decision": "rejected",
                                  "resumed": bool((out or {}).get("run"))}
    return res


def judge(task, res):
    """按任务自带的 expect 判定 ok。返回 (0/1, 失败原因)。"""
    exp = task.get("expect") or {}
    res = res or {}
    if res.get("status") == "aborted" and "aborted" not in (exp.get("status_any") or []):
        return 0, "任务被中断：%s" % (res.get("reason") or "unknown")
    if exp.get("no_error", True) and res.get("error"):
        return 0, "执行报错：%s" % str(res["error"])[:80]
    if exp.get("answer_any"):
        ans = " ".join(str(res.get(k) or "") for k in ("answer", "summary", "script"))
        if not any(w in ans for w in exp["answer_any"]):
            return 0, "回答里没有预期内容 %s" % "/".join(exp["answer_any"][:3])
    if exp.get("tool_any"):
        used = [s.get("tool") for s in (res.get("steps") or []) if isinstance(s, dict) and s.get("obs")]
        if not any(t in used for t in exp["tool_any"]):
            return 0, "没有执行预期动作 %s（实际：%s）" % (
                "/".join(exp["tool_any"]), "/".join([u for u in used if u]) or "无")
    if exp.get("status_any") and res.get("status") not in exp["status_any"]:
        return 0, "状态是 %s，预期 %s" % (res.get("status"), "/".join(exp["status_any"]))
    if exp.get("min_rows") is not None and int(res.get("rows") or 0) < int(exp["min_rows"]):
        return 0, "只查到 %s 条，少于预期 %s" % (res.get("rows"), exp["min_rows"])
    if exp.get("max_steps") is not None and len(res.get("steps") or []) > int(exp["max_steps"]):
        return 0, "步数 %d 超过预期 %d" % (len(res.get("steps") or []), exp["max_steps"])
    return 1, ""


def _execute(task, user):
    """跑一次任务，返回只含事实的指标（不做判定）。"""
    counter = {"calls": 0, "chars": 0}
    orig = ai.llm_chat

    def wrapped(messages, user_="system"):
        out, provider = orig(messages, user_)
        counter["calls"] += 1
        counter["chars"] += sum(len(str(m.get("content") or "")) for m in messages) + len(out or "")
        return out, provider

    ai.llm_chat = wrapped
    mark = _usage_mark()
    t0 = time.time()
    try:
        res = _dispatch(task, user)
    except Exception as e:
        # 单条任务炸了不能带走整轮实验：记成"执行报错"，交给判据去扣分
        res = {"error": "执行异常：%s" % str(e)[:160]}
    finally:
        ai.llm_chat = orig
        ms = int((time.time() - t0) * 1000)
    tokens = _usage_tokens(mark)
    trace = [s for s in (res.get("steps") or []) if isinstance(s, dict)]
    ok, fail = judge(task, res)
    return {
        "ok": ok, "fail": fail,
        "llm_calls": counter["calls"],
        "steps": len(trace),
        "tool_calls": len([s for s in trace if s.get("obs") and not s.get("replay")]),
        "denied": len([s for s in trace if s.get("denied")]),
        "approvals": _approvals_of(res.get("task_id")),
        "tokens": tokens,
        "chars": counter["chars"],
        "evidence": _evidence_ok(res.get("evidence_id")),
        "ms": ms,
        "provider": res.get("provider") or "-",
        "status": res.get("status") or "",
        "task_id": res.get("task_id"),
    }


# ---------------------------------------------------------------- 汇总

def summarize(job_id):
    """按 stage 汇总：每个任务先在自己的 N 次重复上取均值，再对任务取宏平均。

    先均后比（而不是把所有执行混在一起），避免某个任务重复次数多就把整体拽歪。
    """
    rows = _rows(job_id)
    out = {}
    for stage in ("base", "cand"):
        srows = [r for r in rows if r["stage"] == stage]
        per_task, acc = {}, {}
        for r in srows:
            d = per_task.setdefault(r["task_id"], {
                "task_id": r["task_id"], "kind": r["kind"], "runs": 0, "ok": 0,
                "fails": [], **{k: 0 for k in AVG_KEYS}})
            d["runs"] += 1
            d["ok"] += 1 if r["ok"] else 0
            for k in AVG_KEYS:
                d[k] += r[k] or 0
            if not r["ok"] and r["fail"] and r["fail"] not in d["fails"]:
                d["fails"].append(r["fail"])
        per = []
        for d in per_task.values():
            n = max(1, d["runs"])
            item = {"task_id": d["task_id"], "kind": d["kind"], "runs": d["runs"],
                    "ok_rate": round(d["ok"] / n, 3),
                    "evidence_rate": round(d["evidence"] / n, 3),
                    "fails": d["fails"][:2]}
            for k in ("llm_calls", "steps", "tool_calls", "denied", "approvals", "tokens", "chars", "ms"):
                item[k] = round(d[k] / n, 2 if k != "tokens" else 1)
            per.append(item)
        per.sort(key=lambda x: x["task_id"])
        acc["tasks"] = len(per)
        acc["runs"] = len(srows)
        if per:
            acc["ok_rate"] = round(sum(p["ok_rate"] for p in per) / len(per), 3)
            acc["evidence_rate"] = round(sum(p["evidence_rate"] for p in per) / len(per), 3)
            for k in ("llm_calls", "steps", "tool_calls", "denied", "approvals", "chars", "ms"):
                acc[k] = round(sum(p[k] for p in per) / len(per), 2)
            acc["tokens"] = round(sum(p["tokens"] for p in per) / len(per), 1)
        else:
            for k in ("ok_rate", "evidence_rate", "llm_calls", "steps", "tool_calls",
                      "denied", "approvals", "chars", "ms", "tokens"):
                acc[k] = None
        acc["per_task"] = per
        out[stage] = acc
    delta = {}
    for k in ("ok_rate", "evidence_rate", "llm_calls", "steps", "tool_calls",
              "denied", "approvals", "chars", "ms", "tokens"):
        a, b = out["base"].get(k), out["cand"].get(k)
        delta[k] = None if a is None or b is None else round(b - a, 3)
    out["delta"] = delta
    return out


# ---------------------------------------------------------------- 跑一轮

def run_job(base_rev=None, cand_rev=None, patch=None, task_ids=None, repeats=3,
            note="", by="admin", user=None):
    """跑一轮双跑对照。返回 {job_id, summary, ...}，失败返回 {"error": ...}。

    结构上只有一个入口，所以用一把全局锁串行化：两轮同时跑会互相切配置，结果全废。
    """
    if not _LOCK.acquire(False):
        return {"error": "另一轮双跑正在进行，等它跑完再来（配置会被来回切，不能并行）"}
    try:
        try:
            all_tasks = load_tasks()
        except Exception as e:
            return {"error": "基准任务集不可用：%s" % str(e)[:160]}
        if not cand_rev and not patch:
            return {"error": "要么给 cand_rev（已有修订），要么给 patch（在 base 上改几个键）"}
        if cand_rev and patch:
            return {"error": "cand_rev 与 patch 只能二选一"}

        want = [str(x) for x in (task_ids or [])]
        if want:
            known = {t["id"] for t in all_tasks}
            unknown = [w for w in want if w not in known]
            if unknown:
                return {"error": "不认识的任务 id：%s" % ",".join(unknown[:5])}
            tasks = [t for t in all_tasks if t["id"] in want]
        else:
            tasks = all_tasks
        repeats = max(1, min(int(repeats) if repeats is not None else 3, 5))
        # 执行身份与被操作人分开：任务要以某个真实账号跑（默认 admin），
        # 否则拿操作者名字当身份会因为没有对应账号而全部被判越权，双跑全是 0 分。
        user = user or ("admin" if "admin" in (ai.SPEC.get("users") or {}) else by)
        if user not in (ai.SPEC.get("users") or {}):
            return {"error": "执行身份 %s 不存在，用 --user / run_as 指定一个真实账号" % user}

        try:
            base_id = int(base_rev) if base_rev else active_rev_id()
            if not base_id:
                return {"error": "没有生效修订可作为 base，先给配置留一版快照"}
            base = _rev(base_id)
            if patch:
                cand_id, cand_sha = _rev_from_patch(base["content"], patch, note, by)
                cand = _rev(cand_id)
            else:
                cand = _rev(int(cand_rev))
                cand_id, cand_sha = cand["id"], cand["sha"]
        except ValueError as e:
            return {"error": str(e)[:200]}

        orig_text = ai._spec_text()
        orig_sha = ai.spec_sha(orig_text)
        now = ai._now()
        conn = ai.db()
        cur = conn.execute(
            "INSERT INTO bench_job(base_rev,cand_rev,base_sha,cand_sha,task_ids,repeats,note,by,created,status) "
            "VALUES(?,?,?,?,?,?,?,?,?,'running')",
            (base["id"], cand["id"], base["sha"], cand["sha"],
             json.dumps([t["id"] for t in tasks], ensure_ascii=False), repeats, note or "", by, now))
        job_id = cur.lastrowid
        conn.commit()
        conn.close()

        restored = False
        err = None
        try:
            for stage, rev in (("base", base), ("cand", cand)):
                ai.reload_spec(rev["content"])
                for rep in range(repeats):
                    for t in tasks:
                        m = _execute(t, user)
                        conn = ai.db()
                        conn.execute(
                            "INSERT INTO bench_run(job_id,stage,rev_id,rev_sha,task_id,kind,rep,ok,fail,"
                            "llm_calls,steps,tool_calls,denied,approvals,tokens,chars,evidence,ms,provider,created) "
                            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (job_id, stage, rev["id"], rev["sha"], t["id"], t["kind"], rep,
                             m["ok"], m["fail"], m["llm_calls"], m["steps"], m["tool_calls"],
                             m["denied"], m["approvals"], m["tokens"], m["chars"], m["evidence"],
                             m["ms"], m["provider"], ai._now()))
                        conn.commit()
                        conn.close()
        except Exception as e:
            err = "双跑中断：%s" % str(e)[:180]
        finally:
            ai.reload_spec(orig_text)  # 一定恢复：实验期间改的是内存，收摊必须还原
            restored = ai.spec_sha() == orig_sha
            if not restored:  # 极不可能，但恢复不了就必须大声说出来
                conn = ai.db()
                conn.execute("UPDATE bench_job SET error=? WHERE id=?",
                             ("配置恢复失败：当前 sha %s，期望 %s" % (ai.spec_sha(), orig_sha), job_id))
                conn.commit()
                conn.close()

        summary = summarize(job_id)
        conn = ai.db()
        conn.execute("UPDATE bench_job SET status=?, summary=?, error=? WHERE id=?",
                     ("done" if (restored and not err) else "error",
                      json.dumps(summary, ensure_ascii=False), err, job_id))
        conn.commit()
        conn.close()
        ai.audit_log(by, "bench_run",
                     {"job": job_id, "base": base["id"], "cand": cand["id"],
                      "tasks": len(tasks), "repeats": repeats},
                     "ok_rate %s → %s" % (summary["base"]["ok_rate"], summary["cand"]["ok_rate"]),
                     bool(restored and not err))
        out = {"ok": bool(restored and not err), "job_id": job_id, "base_rev": base["id"],
               "cand_rev": cand["id"], "base_sha": base["sha"], "cand_sha": cand["sha"],
               "tasks": len(tasks), "repeats": repeats, "restored": restored,
               "summary": summary}
        if err:
            out["error"] = err
        return out
    finally:
        _LOCK.release()
