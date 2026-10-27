# -*- coding: utf-8 -*-
"""飞脑 AI 层：LLM 多供应商网关（降级链）+ RAG(FTS5) + 工具 + 三试点。
零依赖：仅 Python 标准库。"""
import io
import json
import os
import re
import sqlite3
import time
import urllib.request
import urllib.error

BASE = os.path.dirname(os.path.abspath(__file__))
SPEC = json.load(io.open(os.path.join(BASE, "spec.json"), encoding="utf-8"))
DB_PATH = os.environ.get("FEINAO_DB", os.path.join(BASE, "feinao.db"))
_lock_lock = True

# 验证型收尾要求：所有面向用户的回答都必须满足。
# 来源：Harness 循环设计的核心原则——完成必须是可验证产物，不能是模型自述。
VERIFY_TAIL = ("回答收尾要求：① 结论必须附依据（来自哪条上下文/文档编号/接口返回）；"
               "② 涉及产出物时写明保存在哪、如何核对；"
               "③ 无法验证的部分明确标注「未验证」，不得声称已完成。")


def _load_env_file():
    """从 .env 文件读供应商 Key（优先级低于真实环境变量，不改动 systemd 单元）。

    查找顺序：FEINAO_ENV_FILE 指定 → backend/.env → 项目根/.env → /opt/feinao-agent/.env
    文件格式：每行 KEY=VALUE，# 开头为注释。
    """
    cands = [os.environ.get("FEINAO_ENV_FILE", ""),
             os.path.join(BASE, ".env"),
             os.path.join(os.path.dirname(BASE), ".env"),
             "/opt/feinao-agent/.env"]
    for p in cands:
        if not p or not os.path.isfile(p):
            continue
        try:
            for line in io.open(p, encoding="utf-8"):
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
        except Exception:
            pass


_load_env_file()


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS users(username TEXT PRIMARY KEY, password TEXT, role TEXT);
    CREATE TABLE IF NOT EXISTS conversations(id INTEGER PRIMARY KEY AUTOINCREMENT, user TEXT, title TEXT, created TEXT);
    CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY AUTOINCREMENT, conv_id INT, role TEXT, content TEXT, created TEXT);
    CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, user TEXT, tool TEXT, args TEXT, result TEXT, ok INT);
    CREATE TABLE IF NOT EXISTS usage(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, provider TEXT, model TEXT, ok INT, latency_ms INT, pt INT, ct INT);
    CREATE TABLE IF NOT EXISTS docs(id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, body TEXT, source TEXT, created TEXT);
    CREATE TABLE IF NOT EXISTS evidence(
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, conv_id INT, user TEXT,
        channel TEXT, q TEXT, answer TEXT, provider TEXT,
        sources TEXT, trace TEXT, context_tokens INT, unverified INT);
    CREATE TABLE IF NOT EXISTS tasks(
        id INTEGER PRIMARY KEY AUTOINCREMENT, user TEXT, goal TEXT,
        status TEXT DEFAULT 'active', created TEXT, updated TEXT);
    CREATE TABLE IF NOT EXISTS task_steps(
        id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INT, idx INT,
        kind TEXT, content TEXT, created TEXT);
    CREATE TABLE IF NOT EXISTS approvals(
        id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INT, step_idx INT, user TEXT,
        tool TEXT, args TEXT, reason TEXT, status TEXT DEFAULT 'pending', consumed INT DEFAULT 0,
        created TEXT, decided TEXT, decided_by TEXT, note TEXT, result TEXT);
    """)
    for u, p in SPEC["users"].items():
        conn.execute("INSERT OR IGNORE INTO users VALUES(?,?,?)",
                     (u, p, "admin" if u == "admin" else ("ops" if u.startswith("ops") else "viewer")))
    conn.commit()
    conn.close()
    _ensure_fts()


def _ensure_fts():
    conn = db()
    try:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(title, body, content='docs', content_rowid='id')")
        conn.commit()
        globals()["FTS_OK"] = True
    except Exception:
        globals()["FTS_OK"] = False
    conn.close()


def audit_log(user, tool, args, result, ok):
    conn = db()
    conn.execute("INSERT INTO audit(ts,user,tool,args,result,ok) VALUES(?,?,?,?,?,?)",
                 (time.strftime("%Y-%m-%dT%H:%M:%S"), user, tool,
                  json.dumps(args, ensure_ascii=False)[:500],
                  str(result)[:2000], 1 if ok else 0))
    conn.commit()
    conn.close()


# ---------------- 证据链（Harness 第 5 层：验证必须是环境的，不是模型自述） ----------------

def evidence_log(user, channel, conv_id, q, answer, provider, sources=None,
                 trace=None, context_tokens=0):
    """把一次回答的环境级证据落库：用了哪些来源、走了哪些动作、上下文多大。

    环境级 = 由代码记录"实际发生了什么"（检索命中/工具调用/回溯轮次），
    与模型嘴上说的"我完成了"互相独立，可事后审计比对。
    返回 evidence_id。
    """
    unverified = 1 if "未验证" in (answer or "") else 0
    conn = db()
    cur = conn.execute(
        "INSERT INTO evidence(ts,conv_id,user,channel,q,answer,provider,sources,trace,context_tokens,unverified) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (time.strftime("%Y-%m-%dT%H:%M:%S"), conv_id, user, channel,
         (q or "")[:500], (answer or "")[:2000], provider,
         json.dumps(sources or [], ensure_ascii=False)[:2000],
         json.dumps(trace or [], ensure_ascii=False)[:4000],
         int(context_tokens or 0), unverified))
    conn.commit()
    eid = cur.lastrowid
    conn.close()
    return eid


def evidence_list(user=None, conv_id=None, limit=50):
    conn = db()
    sql = "SELECT id,ts,conv_id,user,channel,q,answer,provider,sources,trace,context_tokens,unverified FROM evidence"
    where, args = [], []
    if user:
        where.append("user=?")
        args.append(user)
    if conv_id:
        where.append("conv_id=?")
        args.append(int(conv_id))
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(int(limit))
    rows = []
    for r in conn.execute(sql, args):
        d = dict(r)
        try:
            d["sources"] = json.loads(d["sources"] or "[]")
        except Exception:
            d["sources"] = []
        try:
            d["trace"] = json.loads(d["trace"] or "[]")
        except Exception:
            d["trace"] = []
        rows.append(d)
    conn.close()
    return rows


def user_role(user):
    conn = db()
    row = conn.execute("SELECT role FROM users WHERE username=?", (user,)).fetchone()
    conn.close()
    return row["role"] if row else "viewer"


# ---------------- 动作级权限/预算（Harness 第 4 层） ----------------

def tool_policy(name):
    """spec.json tool_policies 声明的动作级策略；未声明的工具走保守默认。"""
    pol = (SPEC.get("tool_policies") or {}).get(name) or {}
    return {
        "max_calls_per_run": int(pol.get("max_calls_per_run", 2)),
        "roles": pol.get("roles") or ["admin", "ops", "viewer"],
        "requires_confirm": bool(pol.get("requires_confirm", False)),
    }


def policy_role_ok(name, user):
    pol = tool_policy(name)
    role = user_role(user)
    if role not in pol["roles"]:
        return False, "角色 %s 无权调用 %s（允许：%s）" % (role, name, ",".join(pol["roles"]))
    return True, ""


def policy_budget_ok(name, used_counts):
    pol = tool_policy(name)
    if used_counts.get(name, 0) >= pol["max_calls_per_run"]:
        return False, "%s 已达单次任务预算上限 %d 次" % (name, pol["max_calls_per_run"])
    return True, ""


def policy_check(name, user, used_counts):
    """返回 (ok, reason)。ok=False 时工具不执行。"""
    ok, reason = policy_role_ok(name, user)
    if not ok:
        return False, reason
    return policy_budget_ok(name, used_counts)


# ---------------- 审批工作台（Permission 层的真闸门） ----------------
#
# 原则：放行的是"挂起的那一次具体动作"，不是给模型一张空白通行证。
# 审批单落库可查，放行后由系统真的执行该动作，再把任务交回模型继续。

def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def approval_policy():
    p = SPEC.get("approval") or {}
    return {
        "name": p.get("name") or "飞审审批工作台",
        "approvers": p.get("approvers") or ["admin"],
        "allow_self": bool(p.get("allow_self", True)),
        "ttl_minutes": int(p.get("ttl_minutes", 1440)),
    }


def approval_request(user, task_id, step_idx, tool, args, reason=""):
    """挂起一次需确认的动作（不执行），返回审批单 id。"""
    conn = db()
    cur = conn.execute(
        "INSERT INTO approvals(task_id,step_idx,user,tool,args,reason,status,consumed,created) "
        "VALUES(?,?,?,?,?,?,'pending',0,?)",
        (task_id, step_idx, user, tool, json.dumps(args or {}, ensure_ascii=False),
         (reason or "")[:300], _now()))
    conn.commit()
    aid = cur.lastrowid
    conn.close()
    audit_log(user, "approval_request", {"tool": tool, "task_id": task_id},
              "approval id=%d" % aid, True)
    return aid


def _approval_row(r):
    d = dict(r)
    try:
        d["args"] = json.loads(d.get("args") or "{}")
    except Exception:
        d["args"] = {}
    return d


def approval_get(aid):
    conn = db()
    row = conn.execute("SELECT * FROM approvals WHERE id=?", (aid,)).fetchone()
    conn.close()
    return _approval_row(row) if row else None


def _expire_stale():
    """超时的待审批单自动置 expired——挂着不处理也是一种决定。"""
    ttl = approval_policy()["ttl_minutes"]
    cutoff = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - ttl * 60))
    conn = db()
    n = conn.execute("UPDATE approvals SET status='expired' WHERE status='pending' AND created<?",
                     (cutoff,)).rowcount
    conn.commit()
    conn.close()
    return n


def approval_list(status="pending", user=None, only_mine=False, limit=50):
    """审批列表：有审批权的人看全部待办，其他人只看自己发起的。"""
    _expire_stale()
    sql = "SELECT * FROM approvals"
    where, args = [], []
    if status and status != "all":
        where.append("status=?")
        args.append(status)
    if only_mine and user:
        where.append("user=?")
        args.append(user)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    conn = db()
    rows = [_approval_row(r) for r in conn.execute(sql, args)]
    conn.close()
    for r in rows:  # 带上任务目标，工作台上看得懂在批什么
        t = task_get(r["task_id"]) if r.get("task_id") else None
        r["goal"] = (t or {}).get("goal", "")
    return rows


def approval_decide(aid, decision, by, note=""):
    """人工决策：approved / rejected。返回 (ok, msg, row)。"""
    if decision not in ("approved", "rejected"):
        return False, "decision 须为 approved / rejected", None
    row = approval_get(aid)
    if not row:
        return False, "审批单不存在", None
    pol = approval_policy()
    role = user_role(by)
    if role not in pol["approvers"]:
        return False, "角色 %s 无审批权（审批人：%s）" % (role, ",".join(pol["approvers"])), row
    if not pol["allow_self"] and row["user"] == by:
        return False, "不允许审批自己发起的动作", row
    conn = db()
    n = conn.execute(
        "UPDATE approvals SET status=?,decided=?,decided_by=?,note=? WHERE id=? AND status='pending'",
        (decision, _now(), by, (note or "")[:300], aid)).rowcount
    conn.commit()
    conn.close()
    if not n:
        return False, "审批单状态为 %s，不可重复处理" % (approval_get(aid) or {}).get("status"), row
    audit_log(by, "approval_" + decision, {"id": aid, "tool": row["tool"], "requester": row["user"]},
              (note or "")[:200], decision == "approved")
    return True, ("已放行" if decision == "approved" else "已拒绝"), approval_get(aid)


def _task_used_counts(task_id):
    conn = db()
    rows = conn.execute("SELECT content FROM task_steps WHERE task_id=? AND kind='tool_call'",
                        (task_id,)).fetchall()
    conn.close()
    out = {}
    for r in rows:
        try:
            out[json.loads(r["content"])["tool"]] = out.get(json.loads(r["content"])["tool"], 0) + 1
        except Exception:
            pass
    return out


def _task_next_idx(task_id):
    conn = db()
    row = conn.execute("SELECT MAX(idx) m FROM task_steps WHERE task_id=?", (task_id,)).fetchone()
    conn.close()
    return ((row["m"] + 1) if row and row["m"] is not None else 0)


def _prev_result(task_id, tool, args):
    """同一任务里是否已经执行过完全相同的动作（tool+args）。

    用于写操作幂等：模型在放行后重复要同一动作，直接复用结果，不重复落库、不占预算。
    """
    want = json.dumps(args or {}, ensure_ascii=False, sort_keys=True)
    conn = db()
    rows = conn.execute(
        "SELECT idx,kind,content FROM task_steps WHERE task_id=? AND kind IN ('tool_call','tool_result') ORDER BY idx",
        (task_id,)).fetchall()
    conn.close()
    seq = [dict(r) for r in rows]
    for i, s in enumerate(seq):
        if s["kind"] != "tool_call":
            continue
        try:
            c = json.loads(s["content"])
        except Exception:
            continue
        if c.get("tool") != tool:
            continue
        if json.dumps(c.get("args") or {}, ensure_ascii=False, sort_keys=True) != want:
            continue
        for nxt in seq[i + 1:]:
            if nxt["kind"] == "tool_result":
                return nxt["content"]
            if nxt["kind"] == "error":
                return None
    return None


def _rejected_before(task_id, tool, args):
    """同一任务里是否有过被拒绝的完全相同的动作 → 返回该审批单（避免反复打扰审批人）。"""
    if not task_id:
        return None
    want = json.dumps(args or {}, ensure_ascii=False, sort_keys=True)
    conn = db()
    rows = conn.execute(
        "SELECT * FROM approvals WHERE task_id=? AND tool=? AND status='rejected' ORDER BY id DESC",
        (task_id, tool)).fetchall()
    conn.close()
    for r in rows:
        try:
            if json.dumps(json.loads(r["args"] or "{}"), ensure_ascii=False, sort_keys=True) == want:
                return _approval_row(r)
        except Exception:
            continue
    return None


def approval_handle(aid, decision, by, note="", resume=True):
    """审批工作台唯一入口：决策 → 放行则真执行该动作 → 把任务交回模型续跑。"""
    ok, msg, row = approval_decide(aid, decision, by, note)
    if not ok:
        return {"error": msg, "approval": row}
    task_id = row.get("task_id")
    out = {"approval": approval_get(aid), "approved": decision == "approved", "decision": decision}
    idx = _task_next_idx(task_id) if task_id else 0

    if decision == "rejected":
        entry = {"tool": row["tool"], "args": row["args"],
                 "denied": "审批被拒绝：%s" % (note or "无理由")}
        if task_id:
            task_step(task_id, idx, "denied", json.dumps(entry, ensure_ascii=False))
    else:
        tool = TOOLS.get(row["tool"])
        if not tool:
            return {"error": "工具 %s 已不存在，无法执行" % row["tool"],
                    "approval": approval_get(aid)}
        if task_id:
            ok2, reason = policy_check(row["tool"], row["user"], _task_used_counts(task_id))
            if not ok2:
                # 放行也不能越过角色/预算：审批单退回 pending，等人改主意
                conn = db()
                conn.execute("UPDATE approvals SET status='pending',decided=NULL,decided_by=NULL,note=? WHERE id=?",
                             ("放行受阻：" + reason, aid))
                conn.commit()
                conn.close()
                return {"error": "放行失败：" + reason, "approval": approval_get(aid)}
            task_step(task_id, idx, "tool_call", json.dumps(
                {"tool": row["tool"], "args": row["args"], "approved_by": by}, ensure_ascii=False))
            idx += 1
        try:
            obs = tool["fn"](row["args"])
            if task_id:
                task_step(task_id, idx, "tool_result", json.dumps(obs, ensure_ascii=False)[:2000])
            _set_approval_result(aid, 1, json.dumps(obs, ensure_ascii=False)[:1000])
            out["result"] = obs
        except Exception as e:
            err = {"error": str(e)[:200]}
            if task_id:
                task_step(task_id, idx, "error", json.dumps(err, ensure_ascii=False))
            _set_approval_result(aid, 1, "error: " + str(e)[:200])
            out["result"] = err

    if task_id:
        task_finish(task_id, "active")  # 挂起时是 waiting_approval，先放回可续跑
        if resume:
            t = task_get(task_id)
            out["run"] = react_run((t or {}).get("goal") or "（续跑）", row["user"], task_id=task_id)
    return out


def _set_approval_result(aid, consumed, result):
    conn = db()
    conn.execute("UPDATE approvals SET consumed=?, result=? WHERE id=?", (consumed, result, aid))
    conn.commit()
    conn.close()


# ---------------- LLM 网关（多供应商 + combo 降级链） ----------------

def _call_openai(p, messages, timeout=30):
    key = os.environ.get(p.get("key_env", ""), "")
    if not key:
        raise RuntimeError("no key for " + p["name"])
    body = json.dumps({"model": p["model"], "messages": messages, "temperature": 0.4}).encode()
    req = urllib.request.Request(p["base"], data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Bearer " + key)
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode())
    lat = int((time.time() - t0) * 1000)
    text = data["choices"][0]["message"]["content"]
    u = data.get("usage", {})
    _usage(p["name"], p["model"], True, lat, u.get("prompt_tokens", 0), u.get("completion_tokens", 0))
    return text


def _call_mock(messages, **kw):
    sys_txt = "\n".join(m.get("content", "") for m in messages if m.get("role") == "system")
    user_txt = "\n".join(m.get("content", "") for m in messages if m.get("role") == "user")
    if "[PILOT:lcm]" in sys_txt:
        # 模拟"只能看见给定上下文"的模型：上下文里有就答，没有就说不知道。
        # 用它来做 LCM / 截断窗口的对照实验，结论不掺运气。
        turns = [m.get("content", "") for m in messages if m.get("role") == "user"]
        question = turns[-1] if turns else ""      # 只用最后一句提问，别把历史当问题
        kws = [k for k in _keywords(question, 8) if len(k) >= 2]
        asks_num = any(t in question for t in ("多少", "几", "多大", "比例", "金额", "时间", "日期"))
        scored = []
        for line in sys_txt.splitlines():
            if "[PILOT:" in line or line.startswith("回答收尾要求"):
                continue
            hit = sum(1 for k in kws if k in line)
            if not hit:
                continue
            score = hit + (1 if (asks_num and re.search(r"[0-9]", line)) else 0)
            scored.append((score, line.strip()))
        if scored:
            scored.sort(key=lambda x: -x[0])
            top = [t for sc, t in scored[:2]]
            return "（依据上下文）" + " ｜ ".join(x[:110] for x in top)
        return "（上下文中没有相关信息，无法回答）"
    if "[PILOT:sum]" in sys_txt:
        # 速记员 mock：抽取每轮开头，保留数字型细节
        parts = [l.strip() for l in user_txt.split("\n") if l.strip()]
        parts = [re.sub(r"^-+\s*", "", re.sub(r"^(user|assistant|summary):\s*", "", p)) for p in parts]
        keep = []
        for p in parts[:12]:
            nums = re.findall(r"[0-9]+(?:\.[0-9]+)?", p)
            keep.append("- " + p[:60] + (("（含：" + "、".join(nums[:2]) + "）") if nums else ""))
        return "\n".join(keep)[:700]
    if "[PILOT:nl2api]" in sys_txt:
        # 返回结构化意图（JSON）
        kw_hit = [k for k in ("订单", "商品", "客户", "销售", "工单") if k in user_txt]
        ent = {"订单": "orders", "商品": "products", "客户": "customers", "销售": "sales", "工单": "tickets"}.get(
            kw_hit[0] if kw_hit else "", "orders")
        return json.dumps({"entity": ent, "q": "", "limit": 20}, ensure_ascii=False)
    if "[PILOT:rag]" in sys_txt:
        return "根据知识库资料：%s（mock 摘要回答，已检索 %s 条相关文档）" % (user_txt[:60], sys_txt.count("[DOC]"))
    if "[PILOT:script]" in sys_txt:
        return ("【话术-开场】您好，欢迎光临，我是您的专属顾问。\n"
                "【话术-推荐】根据您的需求，为您推荐以下方案：\n"
                "【话术-促单】现在下单可享专属优惠，需要为您锁定库存吗？\n"
                "（mock 生成，场景：%s）" % user_txt[:40])
    return "（飞脑·mock）已收到：%s" % user_txt[:80]


def _usage(provider, model, ok, lat, pt, ct):
    conn = db()
    conn.execute("INSERT INTO usage(ts,provider,model,ok,latency_ms,pt,ct) VALUES(?,?,?,?,?,?,?)",
                 (time.strftime("%Y-%m-%dT%H:%M:%S"), provider, model, 1 if ok else 0, lat, pt, ct))
    conn.commit()
    conn.close()


def llm_chat(messages, user="system"):
    """按 providers 顺序降级重试，全部失败抛异常。"""
    last_err = None
    for p in SPEC["providers"]:
        t0 = time.time()
        try:
            if p["type"] == "mock":
                out = _call_mock(messages)
                _usage("mock", "mock-1", True, int((time.time() - t0) * 1000), 0, 0)
                return out, p["name"]
            out = _call_openai(p, messages)
            return out, p["name"]
        except Exception as e:
            last_err = str(e)[:200]
            _usage(p["name"], p.get("model", "-"), False, int((time.time() - t0) * 1000), 0, 0)
    raise RuntimeError("all providers failed: %s" % last_err)


def mask_key(val):
    """Key 只回指纹：够短的不显示，够长的只留末四位。
    任何对外响应都不许出现完整 Key —— 见 tests/test_provider.py 的硬断言。"""
    if not val:
        return None
    val = val.strip()
    if len(val) < 12:
        return "…（已配置）"
    return "…" + val[-4:]


def _scrub(text, *vals):
    """把可能混进错误信息的 Key 值抹掉。"""
    s = str(text)
    for v in vals:
        if v and len(v) >= 6:
            s = s.replace(v, mask_key(v) or "***")
    return s


def providers_health(ping=False):
    """供应商体检：Key 是否就位 / 指纹；ping=True 时真发一次最小请求验证 Key 有效。"""
    out = []
    for p in SPEC["providers"]:
        item = {"name": p["name"], "type": p["type"], "model": p.get("model", "-")}
        if p["type"] == "mock":
            item.update(ok=True, key=False, hasKey=False, fingerprint=None,
                        note="内置兜底，无需 Key")
            out.append(item)
            continue

        env_name = p.get("key_env", "")
        val = os.environ.get(env_name, "").strip()
        has = bool(val)
        item.update(ok=has, key=has, hasKey=has, key_env=env_name,
                    fingerprint=mask_key(val), key_len=len(val))
        if not has:
            item["note"] = "未配置 Key（降级到下一家）"
        elif ping:
            t0 = time.time()
            try:
                reply = _call_openai(p, [{"role": "user", "content": "回复两个字：在线"}], timeout=20)
                item.update(ok=True, latency_ms=int((time.time() - t0) * 1000),
                            reply=_scrub((reply or "")[:30], val))
            except Exception as e:
                item.update(ok=False, error=_scrub(str(e)[:150], val))
        else:
            item["note"] = "Key 已配置（未实测，加 ?ping=1 实测）"
        out.append(item)
    return {"providers": out, "chain": [p["name"] for p in SPEC["providers"]],
            "pinged": bool(ping)}


# ---------------- 上游系统客户端（spec 驱动） ----------------

_tok_cache = {}


def _upstream_get(name, path):
    up = SPEC["upstreams"][name]
    tok = _tok_cache.get(name)
    if not tok:
        body = json.dumps({"username": up["user"], "password": up["pass"]}).encode()
        req = urllib.request.Request(up["base"] + "api/login", data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=10) as r:
            tok = json.loads(r.read().decode())["token"]
        _tok_cache[name] = tok
    req = urllib.request.Request(up["base"] + path)
    req.add_header("Authorization", "Bearer " + tok)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 401:
            _tok_cache.pop(name, None)
        raise


def upstream_spec(name):
    return _upstream_get(name, "api/spec")


def upstream_rows(name, entity, q="", limit=20):
    path = "api/%s?limit=%d" % (entity, int(limit))
    if q:
        path += "&q=" + urllib.request.quote(q)
    d = _upstream_get(name, path)
    return d.get("rows") or d.get("items") or (d if isinstance(d, list) else [])


# ---------------- 试点1：RAG 知识问答（SQLite FTS5，LIKE 兜底） ----------------

def rag_ingest(title, body, source="manual"):
    conn = db()
    conn.execute("INSERT INTO docs(title,body,source,created) VALUES(?,?,?,?)",
                 (title, body, source, time.strftime("%Y-%m-%dT%H:%M:%S")))
    conn.commit()
    rid = conn.execute("SELECT last_insert_rowid() id").fetchone()["id"]
    conn.close()
    if globals().get("FTS_OK"):
        conn = db()
        conn.execute("INSERT INTO docs_fts(rowid,title,body) VALUES(?,?,?)", (rid, title, body))
        conn.commit()
        conn.close()
    return rid


def _keywords(q, limit=8):
    """中文二元分词（bigram）：相邻两字滑窗，保证短词可命中。"""
    segs = [t for t in re.split(r"[\s,，。?？!！、;；:：]+", q) if t]
    kws = []
    for seg in segs:
        if len(seg) <= 2:
            kws.append(seg)
            continue
        for i in range(len(seg) - 1):
            kws.append(seg[i:i + 2])
    seen, out = set(), []
    for k in kws:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out[:limit]


def rag_search(q, topk=None):
    topk = topk or SPEC["pilots"]["rag_topk"]
    kws = _keywords(q)
    if not kws:
        kws = [q]
    hits = []
    conn = db()
    if globals().get("FTS_OK"):
        try:
            match = " OR ".join('"%s"' % k.replace('"', "") for k in kws)
            for r in conn.execute("SELECT rowid, title, body FROM docs_fts WHERE docs_fts MATCH ? ORDER BY rank LIMIT ?",
                                  (match, topk)):
                hits.append(dict(r))
        except Exception:
            hits = []
    if not hits:
        seen = set()
        for kw in kws:
            for r in conn.execute("SELECT id, title, body FROM docs WHERE title LIKE ? OR body LIKE ? LIMIT ?",
                                  ("%" + kw + "%", "%" + kw + "%", topk)):
                if r["id"] not in seen:
                    seen.add(r["id"])
                    hits.append({"rowid": r["id"], "title": r["title"], "body": r["body"][:800]})
            if len(hits) >= topk:
                break
    conn.close()
    return hits[:topk]


def pilot_ask(q, user="admin"):
    hits = rag_search(q)
    ctx = "\n".join("[DOC%d] %s\n%s" % (i + 1, h["title"], (h["body"] or "")[:600]) for i, h in enumerate(hits))
    messages = [
        {"role": "system", "content": "你是飞系知识助手。仅依据以下知识库文档回答，并标注引用编号。" + VERIFY_TAIL + " [PILOT:rag]\n" + ctx},
        {"role": "user", "content": q},
    ]
    ans, provider = llm_chat(messages, user)
    audit_log(user, "pilot_ask", {"q": q, "hits": len(hits)}, ans[:200], True)
    eid = evidence_log(user, "rag", None, q, ans, provider,
                       sources=[{"id": h["rowid"], "title": h["title"]} for h in hits])
    return {"answer": ans, "provider": provider,
            "sources": [{"id": h["rowid"], "title": h["title"]} for h in hits],
            "evidence_id": eid}


# ---------------- 试点2：BI 自然语言查数（NL2API，非裸SQL，安全） ----------------

def _match_entity(spec, q):
    """关键词匹配实体（spec 驱动，兼容二批 entities 与电商 tables 两种形态）。"""
    ents = spec.get("entities") or spec.get("tables") or []
    if isinstance(ents, dict):
        ents = [dict(v, key=k) if isinstance(v, dict) else {"key": k, "label": str(v)} for k, v in ents.items()]
    out = []
    ql = q.lower()
    for e in ents:
        key = e.get("key") or e.get("name") or ""
        label = e.get("label") or ""
        score = 0
        if key and key.lower() in ql: score += 3
        if label and label in q: score += 3
        for ch in (label or ""):
            if ch and ch in q: score += 0.2
        if score > 0:
            out.append((score, key, label))
    out.sort(reverse=True)
    return out


def pilot_nl2api(q, system="bi", user="admin"):
    try:
        spec = upstream_spec(system)
    except Exception as e:
        result = {"error": "上游系统不可达：%s" % str(e)[:120]}
        audit_log(user, "pilot_nl2api", {"q": q, "system": system}, result, False)
        return result
    cands = _match_entity(spec, q)
    intent_txt = None
    if cands:
        intent = {"entity": cands[0][1], "q": "", "limit": SPEC["pilots"]["nl2api_max_rows"]}
        ent_key = intent["entity"]
    else:
        raw, provider = llm_chat([
            {"role": "system", "content": "从用户问题中提取查询意图，输出JSON：{\"entity\":\"实体key\",\"q\":\"搜索词\",\"limit\":20}。可用实体：%s [PILOT:nl2api]" %
             json.dumps([e.get("key") or e.get("name") for e in (spec.get("entities") or spec.get("tables") or [])], ensure_ascii=False)},
            {"role": "user", "content": q}], user)
        try:
            m = re.search(r"\{.*\}", raw, re.S)
            intent = json.loads(m.group(0))
            provider = provider
        except Exception:
            intent = {"entity": "", "q": q, "limit": 20}
            provider = "parse-fail"
        ent_key = intent.get("entity", "")
    if not ent_key:
        result = {"error": "未能识别目标数据实体", "entities": [e.get("key") or e.get("name") for e in (spec.get("entities") or spec.get("tables") or [])][:20]}
        audit_log(user, "pilot_nl2api", {"q": q, "system": system}, result, False)
        return result
    try:
        rows = upstream_rows(system, ent_key, intent.get("q", ""), intent.get("limit", 20))
    except Exception as e:
        result = {"error": "查询失败：%s" % str(e)[:120]}
        audit_log(user, "pilot_nl2api", {"q": q, "system": system}, result, False)
        return result
    summary, provider = llm_chat([
        {"role": "system", "content": "你是数据分析助手。用中文简要总结以下数据（条数/关键发现），不超过150字。"},
        {"role": "user", "content": "问题：%s\n实体：%s\n数据：%s" % (q, ent_key, json.dumps(rows[:20], ensure_ascii=False)[:3000])},
    ], user)
    result = {"system": system, "entity": ent_key, "rows": len(rows),
              "sample": rows[:5], "summary": summary, "provider": provider}
    audit_log(user, "pilot_nl2api", {"q": q, "system": system, "entity": ent_key}, summary[:200], True)
    return result


# ---------------- 试点3：客服话术生成 ----------------

def pilot_script(scenario, tone="专业友好", user="admin"):
    hits = rag_search(scenario, 3)
    ctx = "\n".join("[DOC] %s: %s" % (h["title"], (h["body"] or "")[:300]) for h in hits)
    ans, provider = llm_chat([
        {"role": "system", "content": "你是资深客服培训师。按【开场】【承接】【方案】【促单】【收尾】五段生成话术，语气：%s。可参考知识库：\n%s [PILOT:script]" % (tone, ctx)},
        {"role": "user", "content": scenario},
    ], user)
    audit_log(user, "pilot_script", {"scenario": scenario, "tone": tone}, ans[:200], True)
    return {"script": ans, "provider": provider, "refs": [h["title"] for h in hits]}


# ---------------- L2：轻量 ReAct（工具注册表） ----------------

TOOLS = {
    "search_docs": {"desc": "搜索飞系知识库文档", "args": {"q": "str"},
                    "fn": lambda a: rag_search(a.get("q", ""))},
    "query_system": {"desc": "查询飞系系统数据（system: kms/bi/cc…, entity: 实体key）", "args": {"system": "str", "entity": "str", "q": "str"},
                     "fn": lambda a: upstream_rows(a["system"], a["entity"], a.get("q", ""), 10)},
    # 写操作：会真的落库，所以 requires_confirm=true，必须人工放行（审批工作台）
    "ingest_doc": {"desc": "把一段内容写入飞脑知识库（写操作，需人工审批）", "args": {"title": "str", "body": "str"},
                   "fn": lambda a: {"doc_id": rag_ingest((a.get("title") or "未命名")[:120],
                                                        (a.get("body") or "")[:4000], "agent")}},
}


# ---------------- State 层：任务进度落库（Harness 第 2 层） ----------------

def task_create(user, goal):
    conn = db()
    cur = conn.execute("INSERT INTO tasks(user,goal,status,created,updated) VALUES(?,?, 'active',?,?)",
                       (user, goal[:300], time.strftime("%Y-%m-%dT%H:%M:%S"),
                        time.strftime("%Y-%m-%dT%H:%M:%S")))
    conn.commit()
    tid = cur.lastrowid
    conn.close()
    return tid


def task_step(task_id, idx, kind, content):
    conn = db()
    conn.execute("INSERT INTO task_steps(task_id,idx,kind,content,created) VALUES(?,?,?,?,?)",
                 (task_id, idx, kind, str(content)[:2000], time.strftime("%Y-%m-%dT%H:%M:%S")))
    conn.execute("UPDATE tasks SET updated=? WHERE id=?",
                 (time.strftime("%Y-%m-%dT%H:%M:%S"), task_id))
    conn.commit()
    conn.close()


def task_finish(task_id, status):
    conn = db()
    conn.execute("UPDATE tasks SET status=?, updated=? WHERE id=?",
                 (status, time.strftime("%Y-%m-%dT%H:%M:%S"), task_id))
    conn.commit()
    conn.close()


def task_get(task_id, user=None):
    conn = db()
    row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    steps = [dict(r) for r in conn.execute(
        "SELECT idx,kind,content,created FROM task_steps WHERE task_id=? ORDER BY idx", (task_id,))]
    conn.close()
    if not row:
        return None
    d = dict(row)
    if user and d["user"] != user:
        return None
    d["steps"] = steps
    return d


def task_list(user=None, limit=30):
    conn = db()
    if user:
        rows = [dict(r) for r in conn.execute(
            "SELECT id,goal,status,created,updated FROM tasks WHERE user=? ORDER BY id DESC LIMIT ?",
            (user, limit))]
    else:
        rows = [dict(r) for r in conn.execute(
            "SELECT id,user,goal,status,created,updated FROM tasks ORDER BY id DESC LIMIT ?", (limit,))]
    conn.close()
    return rows


def _react_system_prompt(trace):
    return ("你是飞脑Agent。可用工具：%s。如需工具，输出JSON：{\"tool\":\"名称\",\"args\":{...}}；"
            "若可回答，直接输出答案并遵守：%s [PILOT:react]"
            % (json.dumps({k: v["desc"] for k, v in TOOLS.items()}, ensure_ascii=False), VERIFY_TAIL))


def react_run(task, user="admin", task_id=None, max_steps=None):
    """ReAct 多步任务：每一步落 task_steps（State 层），工具调用过动作级权限/预算
    （Permission 层），结束后把证据落 evidence 表（Verification 层）。

    task_id 传入且该任务属于此用户且状态 active 时，从上次的步骤继续跑（断点续跑）。
    """
    max_steps = max_steps or SPEC["pilots"]["react_max_steps"]
    trace = []
    used_counts = {}
    if task_id:
        t = task_get(task_id, user)
        if not t:
            return {"error": "任务不存在或不属于当前用户"}
        if t["status"] != "active":
            return {"error": "任务状态为 %s，仅 active 任务可续跑" % t["status"]}
        for s in t["steps"]:
            if s["kind"] in ("tool_call", "tool_result", "error", "denied", "replay"):
                trace.append(json.loads(s["content"]))
        idx = (t["steps"][-1]["idx"] + 1) if t["steps"] else 0
        for s in t["steps"]:
            if s["kind"] == "tool_call":
                try:
                    c = json.loads(s["content"])
                    used_counts[c["tool"]] = used_counts.get(c["tool"], 0) + 1
                except Exception:
                    pass
    else:
        task_id = task_create(user, task)
        idx = 0

    provider = "-"
    answer = None
    for step in range(max_steps):
        raw, provider = llm_chat([
            {"role": "system", "content": _react_system_prompt(trace)},
            {"role": "user", "content": "任务：%s\n已执行：%s" % (task, json.dumps(trace, ensure_ascii=False)[:1500])},
        ], user)
        m = re.search(r"\{.*\}", raw, re.S)
        if not (m and '"tool"' in raw):
            answer = raw
            task_step(task_id, idx, "answer", raw[:2000])
            idx += 1
            break
        try:
            call = json.loads(m.group(0))
            tool = TOOLS.get(call["tool"])
            if not tool:
                raise ValueError("unknown tool " + call["tool"])
            ok, reason = policy_role_ok(call["tool"], user)
            if not ok:
                # 角色越权：不执行，把原因喂回模型，让它换路或直接回答
                entry = {"tool": call["tool"], "denied": reason}
                trace.append(entry)
                task_step(task_id, idx, "denied", json.dumps(entry, ensure_ascii=False))
                idx += 1
                continue
            pol = tool_policy(call["tool"])
            if pol["requires_confirm"]:
                # 同一动作此前已被审批人拒绝 → 直接拒，不再重复挂单（不打扰人）
                rej = _rejected_before(task_id, call["tool"], call.get("args", {}))
                if rej:
                    entry = {"tool": call["tool"], "args": call.get("args", {}),
                             "denied": "该动作已被审批人拒绝（审批单 #%d）：%s；请勿重复提交"
                                       % (rej["id"], rej.get("note") or "无理由")}
                    trace.append(entry)
                    task_step(task_id, idx, "denied", json.dumps(entry, ensure_ascii=False))
                    idx += 1
                    continue
                # 写操作：先看同一动作是否已放行执行过（幂等回放，不算新调用），否则挂起等人工审批
                prev = _prev_result(task_id, call["tool"], call.get("args", {}))
                if prev is not None:
                    entry = {"tool": call["tool"], "args": call.get("args", {}),
                             "obs": prev, "replay": True,
                             "note": "同一动作已在本任务放行执行过，复用结果（不重复执行、不占预算）"}
                    trace.append(entry)
                    task_step(task_id, idx, "replay", json.dumps(entry, ensure_ascii=False)[:2000])
                    idx += 1
                    continue
                aid = approval_request(user, task_id, idx, call["tool"], call.get("args", {}),
                                       reason="动作级策略要求人工确认")
                entry = {"tool": call["tool"], "args": call.get("args", {}),
                         "status": "需要人工确认，已挂起等待审批", "approval_id": aid}
                trace.append(entry)
                task_step(task_id, idx, "pending", json.dumps(entry, ensure_ascii=False))
                idx += 1
                task_finish(task_id, "waiting_approval")
                return {"answer": ("动作 %s 需要人工审批后才能执行：已生成审批单 #%d，任务 #%d 已挂起。"
                                   "审批人在「审批」页放行后任务会自动继续；拒绝则该动作不执行。"
                                   % (call["tool"], aid, task_id)),
                        "status": "waiting_approval", "approval_id": aid,
                        "task_id": task_id, "steps": trace, "provider": provider}
            ok, reason = policy_budget_ok(call["tool"], used_counts)
            if not ok:
                entry = {"tool": call["tool"], "denied": reason}
                trace.append(entry)
                task_step(task_id, idx, "denied", json.dumps(entry, ensure_ascii=False))
                idx += 1
                continue
            used_counts[call["tool"]] = used_counts.get(call["tool"], 0) + 1
            task_step(task_id, idx, "tool_call", json.dumps(
                {"tool": call["tool"], "args": call.get("args", {})}, ensure_ascii=False))
            idx += 1
            obs = tool["fn"](call.get("args", {}))
            task_step(task_id, idx, "tool_result", json.dumps(obs, ensure_ascii=False)[:2000])
            idx += 1
            trace.append({"tool": call["tool"], "args": call.get("args", {}),
                          "obs": json.dumps(obs, ensure_ascii=False)[:600]})
        except Exception as e:
            entry = {"error": str(e)[:200]}
            trace.append(entry)
            task_step(task_id, idx, "error", json.dumps(entry, ensure_ascii=False))
            idx += 1

    if answer is None:
        answer = "已达最大步数上限，部分结果如下（任务进度已落库，可用 task_id=%d 续跑）" % task_id
        task_finish(task_id, "active")
    else:
        task_finish(task_id, "done")
    eid = evidence_log(user, "react", None, task, answer, provider,
                       sources=[s.get("tool") for s in trace if isinstance(s, dict) and s.get("tool")],
                       trace=trace, context_tokens=sum(len(json.dumps(s, ensure_ascii=False)) for s in trace))
    audit_log(user, "react", {"task": task, "task_id": task_id}, answer[:200], True)
    return {"answer": answer, "steps": trace, "provider": provider,
            "task_id": task_id, "evidence_id": eid}
