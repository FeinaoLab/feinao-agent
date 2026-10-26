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
    sys_txt = " ".join(m.get("content", "") for m in messages if m.get("role") == "system")
    user_txt = " ".join(m.get("content", "") for m in messages if m.get("role") == "user")
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
        {"role": "system", "content": "你是飞系知识助手。仅依据以下知识库文档回答，并标注引用编号。[PILOT:rag]\n" + ctx},
        {"role": "user", "content": q},
    ]
    ans, provider = llm_chat(messages, user)
    audit_log(user, "pilot_ask", {"q": q, "hits": len(hits)}, ans[:200], True)
    return {"answer": ans, "provider": provider, "sources": [{"id": h["rowid"], "title": h["title"]} for h in hits]}


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
}


def react_run(task, user="admin"):
    max_steps = SPEC["pilots"]["react_max_steps"]
    trace = []
    for step in range(max_steps):
        raw, provider = llm_chat([
            {"role": "system", "content": "你是飞脑Agent。可用工具：%s。如需工具，输出JSON：{\"tool\":\"名称\",\"args\":{...}}；若可回答，直接输出答案。 [PILOT:react]" %
             json.dumps({k: v["desc"] for k, v in TOOLS.items()}, ensure_ascii=False)},
            {"role": "user", "content": "任务：%s\n已执行：%s" % (task, json.dumps(trace, ensure_ascii=False)[:1500])},
        ], user)
        m = re.search(r"\{.*\}", raw, re.S)
        if not (m and '"tool"' in raw):
            audit_log(user, "react", {"task": task}, raw[:200], True)
            return {"answer": raw, "steps": trace, "provider": provider}
        try:
            call = json.loads(m.group(0))
            tool = TOOLS.get(call["tool"])
            if not tool:
                raise ValueError("unknown tool " + call["tool"])
            obs = tool["fn"](call.get("args", {}))
            trace.append({"tool": call["tool"], "args": call.get("args", {}),
                          "obs": json.dumps(obs, ensure_ascii=False)[:600]})
        except Exception as e:
            trace.append({"error": str(e)[:200]})
    return {"answer": "已达最大步数上限，部分结果如下", "steps": trace, "provider": provider}
