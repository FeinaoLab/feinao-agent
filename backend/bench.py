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
AVG_KEYS = ("llm_calls", "steps", "tool_calls", "denied", "errors", "dups",
            "approvals", "tokens", "chars", "evidence", "ms")

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
        tool_calls INT, denied INT, errors INT, dups INT, approvals INT, tokens INT, chars INT,
        evidence INT, ms INT, provider TEXT, created TEXT);
    """)
    # 老库补列：errors / dups 是 P0-3 才记的两个事实（出错动作数、重复动作数），
    # 历史行留 NULL，比较时按 0 处理（不假装知道）。
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(bench_run)")}
    for col in ("errors", "dups"):
        if col not in cols:
            conn.execute("ALTER TABLE bench_run ADD COLUMN %s INT DEFAULT 0" % col)
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
    try:  # 结论行跟着详情一起给，省一次往返（前端 P2-2 用得上）
        d["verdict"] = compare(job_id).get("verdict")
    except Exception:
        d["verdict"] = None
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
            "note": "同一批任务在 base 与候选修订上各跑 N 次；ok 由任务自带 expect 判定，不由模型打总分",
            "compare_hint": "GET /api/bench/compare?a=<job> 出结论行；只差一次执行的任务会标成「判不了」"}


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
    if exp.get("min_sources") is not None:
        n = len(res.get("sources") or res.get("refs") or [])
        if n < int(exp["min_sources"]):
            return 0, "只召回 %s 个来源，少于预期 %s（检索深度不够或关键词没覆盖）" % (n, exp["min_sources"])
    if exp.get("min_rows") is not None and int(res.get("rows") or 0) < int(exp["min_rows"]):
        return 0, "只查到 %s 条，少于预期 %s" % (res.get("rows"), exp["min_rows"])
    if exp.get("max_steps") is not None and len(res.get("steps") or []) > int(exp["max_steps"]):
        return 0, "步数 %d 超过预期 %d" % (len(res.get("steps") or []), exp["max_steps"])
    return 1, ""


def _action_stats(trace):
    """从 trace（= task_steps 的镜像）里数两个"过程事实"：

    - dups：同一动作、同一参数被执行了不止一次的次数（幂等回放不算，那不是重复执行）
    - errors：执行出错或降级（畸形输出）的动作数

    这两样以前就在步里躺着，只是从没进过汇总——而"打转"和"报错"恰恰是
    看起来通过率没变、实际已经变差的两种典型形态。
    """
    seen = {}
    for s in trace:
        if s.get("obs") and not s.get("replay"):
            sig = (s.get("tool"),
                   json.dumps(s.get("args") or {}, sort_keys=True, ensure_ascii=False))
            seen[sig] = seen.get(sig, 0) + 1
    dups = sum(v - 1 for v in seen.values() if v > 1)
    errors = len([s for s in trace if s.get("error") or s.get("degraded")])
    return dups, errors


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
    dups, errors = _action_stats(trace)
    ok, fail = judge(task, res)
    return {
        "ok": ok, "fail": fail,
        "llm_calls": counter["calls"],
        "steps": len(trace),
        "tool_calls": len([s for s in trace if s.get("obs") and not s.get("replay")]),
        "denied": len([s for s in trace if s.get("denied")]),
        "errors": errors,
        "dups": dups,
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

def _delta(base, cand):
    out = {}
    for k in ("ok_rate", "evidence_rate", "llm_calls", "steps", "tool_calls", "denied",
              "errors", "dups", "approvals", "chars", "ms", "tokens"):
        a, b = base.get(k), cand.get(k)
        out[k] = None if a is None or b is None else round(b - a, 3)
    return out


def _stage_acc(rows):
    """把一个 stage 的所有执行记录汇总成一份口径固定的账。

    先均后比（而不是把所有执行混在一起），避免某个任务重复次数多就把整体拽歪。
    除 ok_rate / evidence_rate 外都在 per_task 里保留两位小数，方便"改了多少"看得见。
    """
    per_task = {}
    for r in rows:
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
        item = {"task_id": d["task_id"], "kind": d["kind"], "runs": d["runs"], "ok_n": d["ok"],
                "ok_rate": round(d["ok"] / n, 3),
                "evidence_rate": round(d["evidence"] / n, 3),
                "fails": d["fails"][:2]}
        for k in ("llm_calls", "steps", "tool_calls", "denied", "errors", "dups",
                  "approvals", "chars", "ms"):
            item[k] = round(d[k] / n, 2)
        item["tokens"] = round(d["tokens"] / n, 1)
        per.append(item)
    per.sort(key=lambda x: x["task_id"])
    acc = {"tasks": len(per), "runs": len(rows)}
    if per:
        acc["ok_rate"] = round(sum(p["ok_rate"] for p in per) / len(per), 3)
        acc["evidence_rate"] = round(sum(p["evidence_rate"] for p in per) / len(per), 3)
        for k in ("llm_calls", "steps", "tool_calls", "denied", "errors", "dups",
                  "approvals", "chars", "ms"):
            acc[k] = round(sum(p[k] for p in per) / len(per), 2)
        acc["tokens"] = round(sum(p["tokens"] for p in per) / len(per), 1)
    else:
        for k in ("ok_rate", "evidence_rate", "llm_calls", "steps", "tool_calls", "denied",
                  "errors", "dups", "approvals", "chars", "ms", "tokens"):
            acc[k] = None
    acc["per_task"] = per
    acc["providers"] = sorted({(r.get("provider") or "-") for r in rows})
    return acc


def summarize(job_id):
    rows = _rows(job_id)
    out = {"base": _stage_acc([r for r in rows if r["stage"] == "base"]),
           "cand": _stage_acc([r for r in rows if r["stage"] == "cand"])}
    out["delta"] = _delta(out["base"], out["cand"])
    return out


# ---------------------------------------------------------------- 对照式判定（P0-3）
#
# 这一层不产出任何"分数"。开放任务没有标准答案，给 87 分只是把不确定性藏进一个数字里；
# 能站得住的只有一件事：同一批任务、两次执行、差在哪里。所以这里的输出是
#   ① 逐任务判稳（变好 / 变坏 / 判不了 / 没变）
#   ② 代价与过程指标的方向
#   ③ 一句人话结论 + 建议动作（能上线 / 先别上 / 加样本再判 / 任务集没覆盖）
# 判稳规则故意保守：只差一次执行的，一律写"判不了"，不写"变好"。

METRICS = (
    # key, 中文名, 比较口径, 阈值（rel 用相对变化，abs 用绝对值）
    ("ok_rate", "通过率", "abs", 0.005),
    ("evidence_rate", "证据覆盖", "abs", 0.050),
    ("steps", "单任务步数", "rel", 0.100),
    ("tool_calls", "工具调用", "rel", 0.100),
    ("tokens", "token", "rel", 0.150),
    ("denied", "被策略拒", "abs", 0.500),
    ("errors", "执行出错", "abs", 0.500),
    ("dups", "重复动作", "abs", 0.500),
    ("approvals", "人工介入", "abs", 0.500),
)


def _num(x):
    """给人看的数字：整数不带小数点，小数最多两位。"""
    if x is None:
        return "—"
    if isinstance(x, float):
        if abs(x) >= 1000:
            return "%.0f" % x
        return ("%.2f" % x).rstrip("0").rstrip(".")
    return str(x)


def _pct_txt(a, b):
    if not a or b is None:
        return "—"
    return "%+.0f%%" % ((b - a) / abs(a) * 100)


def _metric_diff(key, name, how, eps, a, b):
    """一个指标的差：值、百分比、方向。方向只分 up / down / flat / unknown 四档。"""
    d = None if (a is None or b is None) else round(b - a, 3)
    pct = None if (not a or b is None) else round((b - a) / abs(a) * 100, 1)
    if d is None:
        direction = "unknown"
    elif how == "rel":
        if a and b is not None:
            direction = "flat" if abs(pct) < eps * 100 else ("up" if d > 0 else "down")
        else:  # 分母为 0：只看绝对量
            direction = "flat" if abs(d) < eps * 10 else ("up" if d > 0 else "down")
    else:
        direction = "flat" if abs(d) < eps else ("up" if d > 0 else "down")
    return {"key": key, "name": name, "a": a, "b": b, "diff": d, "pct": pct,
            "dir": direction,
            "text": "%s → %s（%s）" % (_num(a), _num(b),
                                      _pct_txt(a, b) if how == "rel" else ("%+.2f" % d if d is not None else "—"))}


def _task_verdict(pa, pb):
    """单任务判稳。返回 better / worse / unclear / same / new / gone。

    每格只跑 1 次时任何差异都可能是抖动，所以 runs < 2 一律 unclear；
    runs ≥ 2 时，要么差 ≥ 2 次执行，要么两侧都是"全中/全失"，才算数。
    """
    if not pa or not pb:
        return "new" if pb else "gone"
    diff = pb["ok_n"] - pa["ok_n"]
    if diff == 0:
        return "same"
    runs = min(pa["runs"], pb["runs"])
    if runs < 2:
        return "unclear"
    unanimous = pa["ok_n"] in (0, pa["runs"]) and pb["ok_n"] in (0, pb["runs"])
    if abs(diff) >= 2 or unanimous:
        return "better" if diff > 0 else "worse"
    return "unclear"


def _cost_txt(diff, prefix=True):
    tk, st = diff["tokens"], diff["steps"]
    parts = []
    for d in (tk, st):
        if d["dir"] != "flat" and d["pct"] is not None:
            parts.append("%s %s" % (d["name"], _pct_txt(d["a"], d["b"])))
    if not parts:
        return "代价基本没动（token、步数都在 ±15% 内）"
    return ("代价 " if prefix else "") + "、".join(parts)


def _proc_rollup(tasks):
    """过程指标按"多少个任务发生了变化"来数，而不是宏平均。

    宏平均会把"1 个任务被拦了 5 次"摊薄成 0.05，看起来像没发生；而权限收紧
    恰恰是这种形态——只影响少数任务。所以这里数任务数，并给一个最极端的例子。
    """
    out = []
    for key, name in (("denied", "被策略拒"), ("errors", "执行出错"),
                      ("dups", "重复动作"), ("approvals", "人工介入")):
        deltas = [(t.get(key + "_b") or 0) - (t.get(key + "_a") or 0) for t in tasks]
        up = sum(1 for d in deltas if d >= 0.5)
        down = sum(1 for d in deltas if d <= -0.5)
        if not up and not down:
            continue
        if up >= down:
            worst = max(tasks, key=lambda t: (t.get(key + "_b") or 0) - (t.get(key + "_a") or 0))
            out.append("%s %d 个任务上升（%s → %s）"
                       % (name, up, _num(worst.get(key + "_a")), _num(worst.get(key + "_b"))))
        else:
            best = min(tasks, key=lambda t: (t.get(key + "_b") or 0) - (t.get(key + "_a") or 0))
            out.append("%s %d 个任务下降（%s → %s）"
                       % (name, down, _num(best.get(key + "_a")), _num(best.get(key + "_b"))))
    return out


def _verdict(A, B, diff, buckets, kinds, tasks, repeats):
    """把差异归纳成人话。结论行必须能被逐条复核，所以每条都带数字。"""
    nb, nw, nu = len(buckets["better"]), len(buckets["worse"]), len(buckets["unclear"])
    ns = len(buckets["same"])
    ok_a, ok_b = A.get("ok_rate"), B.get("ok_rate")
    tk, st = diff["tokens"], diff["steps"]
    ev = diff["evidence_rate"]
    cost_up = tk["dir"] == "up" or st["dir"] == "up"
    cost_down = tk["dir"] == "down" or st["dir"] == "down"
    lines, caveats = [], []

    # ① 有效性：先看"能不能完成任务"，这是唯一的硬指标
    if nb or nw or nu:
        lines.append("有效性：通过率 %s → %s。%d 个任务稳定变好、%d 个稳定变坏、"
                     "%d 个只差一次判不了、%d 个逐条一致"
                     % (_num(ok_a), _num(ok_b), nb, nw, nu, ns))
    else:
        lines.append("有效性：通过率 %s → %s，%d 个任务的通过与否逐条一致"
                     % (_num(ok_a), _num(ok_b), A["tasks"]))
    # ② 代价
    lines.append(_cost_txt(diff))
    # ③ 过程：被拒 / 出错 / 打转 / 人工介入（按"多少个任务变了"数，不摊平）
    proc = _proc_rollup(tasks)
    if proc:
        lines.append("过程：" + "；".join(proc))
    # ④ 证据
    if ev["a"] is not None:
        lines.append("证据：覆盖 %s → %s" % (_num(ev["a"]), _num(ev["b"])))
    # ⑤ 按能力：哪类任务吃到了这轮改动
    kd = [k for k in kinds if kinds[k]["better"] or kinds[k]["worse"]]
    if kd:
        lines.append("按能力：" + "；".join(
            "%s %s→%s" % (kinds[k]["label"], _num(kinds[k]["ok_a"]), _num(kinds[k]["ok_b"]))
            for k in kd))

    # 结论行：先判"能不能上"，再判"值不值"
    if nw and not nb:
        tone = "worse"
        ids = "、".join(t["task_id"] for t in buckets["worse"][:3])
        why = next((f for t in buckets["worse"] for f in t["new_fails"]), "")
        headline = "先别上线：%d 个任务稳定变坏（%s）%s" % (
            nw, ids, "，新的失败原因是「%s」" % why if why else "")
        suggest = "先别上线"
    elif nb and not nw:
        tone = "better"
        head = "可以上线：%d 个任务稳定变好，没有任务变坏" % nb
        if cost_up and tk["pct"] is not None and tk["pct"] >= 50:
            head += "，但 token %s，这个价先算一下划不划算" % _pct_txt(tk["a"], tk["b"])
            suggest = "能上线，先算成本"
        else:
            head += "，" + _cost_txt(diff)
            suggest = "可以上线"
        headline = head
    elif nb and nw:
        tone = "mixed"
        ids = "、".join(t["task_id"] for t in buckets["worse"][:3])
        headline = ("有得有失：%d 好 / %d 坏（%s 变坏），通过率净额 %+.3f —— "
                    "这不是改善，是换了个坑，先看坏的那几个是不是同一类任务"
                    % (nb, nw, ids, (ok_b or 0) - (ok_a or 0)))
        suggest = "加样本再判"
    elif cost_up:
        tone = "wash"
        headline = "只买到成本：通过率没变（%s），%s" % (_num(ok_a), _cost_txt(diff))
        suggest = "别上（除非本来就想慢一点）"
    elif cost_down:
        tone = "cheaper"
        headline = "白捡的省：通过率没变（%s），%s" % (_num(ok_a), _cost_txt(diff))
        suggest = "可以上（省成本）"
    else:
        tone = "flat"
        headline = ("测不出差别：%d 个任务逐条一致，代价也没动 —— 要么这轮改动对这批任务"
                    "没有影响，要么任务集还没覆盖到它影响的地方" % A["tasks"])
        suggest = "任务集没覆盖，先补任务"
    if tone in ("wash", "cheaper", "flat") and nu:
        headline += "；另有 %d 个任务只差一次执行，加样本才判得了" % nu

    # 诚实话：这些会让结论失效的前提，必须跟着结论一起给
    if repeats < 2:
        caveats.append("每格只跑了 %d 次：通过率差异和随机波动分不开，要下结论请 --repeats 3" % repeats)
    if nu and repeats >= 2:
        caveats.append("%d 个任务只差 1 次执行（%s）：样本内可能只是抖动，别当结论"
                       % (nu, "、".join(t["task_id"] for t in buckets["unclear"][:3])))
    prov = [p for p in (A.get("providers") or []) + (B.get("providers") or [])
            if p and p != "-"]
    if prov and all("mock" in p.lower() for p in prov):
        caveats.append("这轮走的是 mock 上游（没有真实模型调用）：只说明链路与策略生效，不能当模型能力结论")
    if A["tasks"] < 10:
        caveats.append("只跑了 %d 个任务：宏平均容易被单个任务带偏" % A["tasks"])
    if ev["a"] == 0 and ev["b"] == 0:
        caveats.append("两版证据覆盖都是 0：这些通道本来就不落 evidence，不是退步")

    return {"tone": tone, "headline": headline, "suggest": suggest,
            "lines": lines, "caveats": caveats,
            "decisive": {"better": nb, "worse": nw, "unclear": nu, "same": ns}}


KIND_LABEL = {"rag": "RAG", "nl2api": "NL2API", "script": "话术",
              "react": "ReAct", "approval": "审批"}


def compare(job_a, job_b=None, stage_a=None, stage_b=None):
    """对照两次执行，产出结构化差异 + 一句人话结论。

    默认比同一轮双跑里的 base 与 cand；给 job_b 则跨轮比（默认两轮都取 cand 阶段，
    也就是"改完再改一版，第二版比第一版好不好"）。
    """
    ja = _job_row(job_a)
    if not ja:
        return {"error": "双跑作业 #%s 不存在" % job_a}
    if job_b:
        jb = _job_row(job_b)
        if not jb:
            return {"error": "双跑作业 #%s 不存在" % job_b}
        sa, sb = (stage_a or "cand"), (stage_b or "cand")
    else:
        jb, sa, sb = ja, (stage_a or "base"), (stage_b or "cand")

    rows_a = [r for r in _rows(ja["id"]) if r["stage"] == sa]
    rows_b = [r for r in _rows(jb["id"]) if r["stage"] == sb]
    if not rows_a or not rows_b:
        return {"error": "没有可比数据：作业 #%s 的 %s 阶段 %d 条，作业 #%s 的 %s 阶段 %d 条"
                         % (ja["id"], sa, len(rows_a), jb["id"], sb, len(rows_b))}
    A, B = _stage_acc(rows_a), _stage_acc(rows_b)

    # 两次跑的任务集不一样时不能直接比宏平均（分母都不是同一批任务），
    # 先取交集，只比两边都跑过的；缺的那些列出来但不参与结论。
    ta = {p["task_id"]: p for p in A["per_task"]}
    tb = {p["task_id"]: p for p in B["per_task"]}
    common = set(ta) & set(tb)
    if not common:
        return {"error": "两次执行没有共同任务，比不了"}
    only_a = sorted(set(ta) - common)
    only_b = sorted(set(tb) - common)
    if only_a or only_b:
        rows_a = [r for r in rows_a if r["task_id"] in common]
        rows_b = [r for r in rows_b if r["task_id"] in common]
        A, B = _stage_acc(rows_a), _stage_acc(rows_b)
        ta = {k: v for k, v in ta.items() if k in common}
        tb = {k: v for k, v in tb.items() if k in common}

    diff = {key: _metric_diff(key, name, how, eps, A.get(key), B.get(key))
            for key, name, how, eps in METRICS}

    buckets = {"better": [], "worse": [], "unclear": [], "same": []}
    tasks = []
    for tid in sorted(common):
        pa, pb = ta.get(tid), tb.get(tid)
        v = _task_verdict(pa, pb)
        ref = pb or pa
        row = {"task_id": tid, "kind": ref["kind"], "verdict": v,
               "runs_a": (pa or {}).get("runs", 0), "runs_b": (pb or {}).get("runs", 0),
               "ok_n_a": (pa or {}).get("ok_n"), "ok_n_b": (pb or {}).get("ok_n"),
               "ok_a": (pa or {}).get("ok_rate"), "ok_b": (pb or {}).get("ok_rate"),
               "steps_a": (pa or {}).get("steps"), "steps_b": (pb or {}).get("steps"),
               "tokens_a": (pa or {}).get("tokens"), "tokens_b": (pb or {}).get("tokens"),
               "fails_a": (pa or {}).get("fails") or [], "fails_b": (pb or {}).get("fails") or []}
        for k in ("denied", "errors", "dups", "approvals"):
            row[k + "_a"] = (pa or {}).get(k)
            row[k + "_b"] = (pb or {}).get(k)
        row["new_fails"] = [f for f in row["fails_b"] if f not in row["fails_a"]]
        tasks.append(row)
        if v in buckets:
            buckets[v].append(row)

    kinds = {}
    for t in tasks:
        k = kinds.setdefault(t["kind"], {"kind": t["kind"], "label": KIND_LABEL.get(t["kind"], t["kind"]),
                                         "tasks": 0, "better": 0, "worse": 0, "unclear": 0,
                                         "same": 0, "ok_a": 0.0, "ok_b": 0.0})
        k["tasks"] += 1
        if t["verdict"] in k:
            k[t["verdict"]] += 1
        k["ok_a"] += t["ok_a"] or 0
        k["ok_b"] += t["ok_b"] or 0
    for k in kinds.values():
        n = max(1, k["tasks"])
        k["ok_a"] = round(k["ok_a"] / n, 3)
        k["ok_b"] = round(k["ok_b"] / n, 3)

    verdict = _verdict(A, B, diff, buckets, kinds, tasks, int(ja.get("repeats") or 1))
    if only_a or only_b:
        verdict["caveats"].insert(
            0, "两次执行的任务集不同（A 独有 %d 个、B 独有 %d 个），只比了共同的 %d 个任务"
               % (len(only_a), len(only_b), len(common)))
    rev_a = ja["base_rev"] if sa == "base" else ja["cand_rev"]
    sha_a = ja["base_sha"] if sa == "base" else ja["cand_sha"]
    rev_b = jb["base_rev"] if sb == "base" else jb["cand_rev"]
    sha_b = jb["base_sha"] if sb == "base" else jb["cand_sha"]
    return {
        "a": {"job": ja["id"], "stage": sa, "rev": rev_a, "sha": sha_a,
              "ok_rate": A.get("ok_rate"), "tasks": A["tasks"], "runs": A["runs"],
              "providers": A.get("providers")},
        "b": {"job": jb["id"], "stage": sb, "rev": rev_b, "sha": sha_b,
              "ok_rate": B.get("ok_rate"), "tasks": B["tasks"], "runs": B["runs"],
              "providers": B.get("providers")},
        "verdict": verdict,
        "diff": {k: {kk: vv for kk, vv in v.items() if kk != "key"} for k, v in diff.items()},
        "by_task": tasks,
        "by_kind": [kinds[k] for k in sorted(kinds)],
        "scope": {"common": sorted(common), "only_a": only_a, "only_b": only_b},
        "note": "不给总分：只给同一批任务两次执行的差，以及一句能逐条复核的结论",
    }


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
                            "llm_calls,steps,tool_calls,denied,errors,dups,approvals,tokens,chars,"
                            "evidence,ms,provider,created) "
                            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (job_id, stage, rev["id"], rev["sha"], t["id"], t["kind"], rep,
                             m["ok"], m["fail"], m["llm_calls"], m["steps"], m["tool_calls"],
                             m["denied"], m["errors"], m["dups"], m["approvals"], m["tokens"],
                             m["chars"], m["evidence"], m["ms"], m["provider"], ai._now()))
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
        try:  # 结论行随跑随给；判不了也不该让整轮失败
            out["verdict"] = compare(job_id)["verdict"]
        except Exception as e:
            out["verdict"] = {"tone": "unknown", "headline": "结论生成失败：%s" % str(e)[:120]}
        # 经验层（P1-1）：把这一轮"什么改法、什么效果"写进 decision 记忆，注入给编辑者
        if restored and not err:
            try:
                import memory
                v = out.get("verdict") or {}
                paths = ",".join(str(p.get("path")) for p in (patch or [])) or \
                        ("修订#%s" % cand.get("id"))
                memory.decision_write(
                    "harness:bench:%s" % (",".join(sorted(set(t["kind"] for t in tasks))) or "mixed"),
                    "双跑#%d（%s）：%s 在 %d 个任务上 ok_rate %s→%s，结论「%s」"
                    % (job_id, paths, (note or "无备注"), len(tasks),
                       summary["base"]["ok_rate"], summary["cand"]["ok_rate"],
                       v.get("tone") or "unknown"), by=by or "system")
            except Exception:
                pass  # 经验写不进去不影响双跑本身
        if err:
            out["error"] = err
        return out
    finally:
        _LOCK.release()
