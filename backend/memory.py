# -*- coding: utf-8 -*-
"""飞忆 · 跨会话长期记忆（Harness 第 1 层最后一块）。

和 LCM 的分工：
  - LCM 管「一个会话内不丢细节」（原文全留 + 摘要上卷 + 关键词回溯）；
  - 飞忆管「换个会话还记得你」——只存提炼过的条目，不存聊天原文。

三条铁律（防"错记被固化"）：
  1. 只读检索：记忆只作为参考注入，不改写本轮事实，与本轮说法冲突以本轮为准；
  2. 默认待确认：抽取出来的条目先挂 pending，人点头才 active（宁可少记，不要错记）；
  3. 全程可溯源：每条带来源会话/轮次/时间/置信度，可停用、可删除。

零依赖：仅用 ai.db() / ai.llm_chat() / ai._keywords()，不引向量库。
"""
import json
import re
import time

import ai

KINDS = ("fact", "decision", "preference", "artifact")

# 过程性噪声：这些是"对话发生了什么"的元信息，不是值得长期记的事实
NOISE = ("已记录", "已同步", "已确认", "已完成", "会话上下文", "历史摘要", "本次对话",
         "以上", "PILOT", "收到", "好的", "明白")


# ---------------- 配置 ----------------

def policy():
    p = ai.SPEC.get("memory") or {}
    return {
        "name": p.get("name", "飞忆"),
        "inject_topk": int(p.get("inject_topk", 3)),
        "min_score": int(p.get("min_score", 1)),
        # 默认 1.0 = 永不自动生效，一切都等人点头；调低（如 0.9）才放开高置信度自动入长期区
        "auto_activate_min": float(p.get("auto_activate_min", 1.0)),
        "max_text": int(p.get("max_text", 140)),
    }


# ---------------- 建表 ----------------

def init_memory_db():
    ai.init_db()          # 幂等，保证基础表在
    conn = ai.db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS memory(
        id INTEGER PRIMARY KEY AUTOINCREMENT, user TEXT NOT NULL,
        kind TEXT DEFAULT 'fact', text TEXT NOT NULL,
        src_conv INT, src_seq INT, src_time TEXT,
        confidence REAL DEFAULT 0.6,
        status TEXT DEFAULT 'pending',
        created TEXT, decided TEXT, decided_by TEXT,
        hits INT DEFAULT 0, last_used TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_memory_user ON memory(user, status);
    """)
    conn.commit()
    conn.close()


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


# ---------------- 归一化与去重 ----------------

def _norm(t):
    return re.sub(r"[\s，。；、,.!?！？：:（）()【】\[\]\"'“”]+", "", (t or "")).lower()


def _nums(t):
    return set(re.findall(r"\d+", t or ""))


def _overlap(a, b, limit=12):
    ka, kb = set(ai._keywords(a, limit)), set(ai._keywords(b, limit))
    if not ka or not kb:
        return 0.0
    return len(ka & kb) / float(min(len(ka), len(kb)))


def _similar(a, b):
    """关键词重叠率（复用 ai 的 bigram 分词），≥0.6 视为同一条记忆。

    但具体数值不同时不算重复——"端口 8060" 和 "端口 8100" 是两个事实，
    后者应该取代前者（见 _same_slot），而不是被当成重复丢掉。
    """
    na, nb = _nums(a), _nums(b)
    if na and nb and na != nb:
        return False
    return _overlap(a, b) >= 0.6


def _same_slot(a, b):
    """同一件事的同一位置、只有数值不同 → 新值是更新，应取代旧值。
    比对时把数字抹成 #，只看"周围的话"是否同一句。"""
    na, nb = _nums(a), _nums(b)
    if not (na and nb and na != nb):
        return False
    sa, sb = re.sub(r"\d+", "#", a), re.sub(r"\d+", "#", b)
    return _overlap(sa, sb, 14) >= 0.8


def _kws(q, limit=16):
    """记忆检索用的关键词：中文二元组 + 长度≥3 的英文词 + 完整数字。

    丢掉 2 字宽的 ASCII 片段（"ro"、"ad" 这种）——它们会误命中 role/admin，
    把毫不相干的记忆注入进来。
    """
    out = []
    for k in ai._keywords(q or "", limit * 2):
        if re.fullmatch(r"[\u4e00-\u9fff]+", k) or len(k) >= 3:
            out.append(k)
    out += re.findall(r"\d{2,}", q or "")
    seen, res = set(), []
    for k in out:
        if k not in seen:
            seen.add(k)
            res.append(k)
    return res[:limit]


def _grounded(text, src):
    """反虚构：条目里的数字必须原文出现过，关键词至少六成能在原文里找到。"""
    nums = _nums(text)
    if nums and not nums.issubset(_nums(src)):
        return False
    kws = _kws(text, 12)
    if not kws:
        return True
    hit = sum(1 for k in kws if k in src)
    return hit / float(len(kws)) >= 0.6


def _supersede(user, text, kind):
    """同位置出现新值 → 把旧条目停用（标 auto:superseded），避免两条互相矛盾。"""
    conn = ai.db()
    rows = [dict(r) for r in conn.execute(
        "SELECT id,text,kind FROM memory WHERE user=? AND status!='disabled'", (user,))]
    killed = []
    for r in rows:
        if r["kind"] == kind and _same_slot(r["text"], text):
            conn.execute("UPDATE memory SET status='disabled', decided=?, decided_by=? WHERE id=?",
                         (_now(), "auto:superseded", r["id"]))
            killed.append(r["id"])
    conn.commit()
    conn.close()
    return killed


def _exists(user, text):
    conn = ai.db()
    rows = [r["text"] for r in conn.execute(
        "SELECT text FROM memory WHERE user=? AND status!='disabled'", (user,))]
    conn.close()
    n = _norm(text)
    for t in rows:
        if _norm(t) == n or _similar(t, text):
            return True
    return False


# ---------------- 写入 ----------------

def add(user, text, kind="fact", confidence=0.6, src_conv=None, src_seq=None,
        status=None, by="system"):
    """落一条记忆。默认进待确认区；高置信度（≥auto_activate_min）才直接生效。"""
    text = re.sub(r"\s+", " ", (text or "")).strip()[:policy()["max_text"]]
    if len(text) < 4:
        return None
    if kind not in KINDS:
        kind = "fact"
    if status is None:
        status = "active" if confidence >= policy()["auto_activate_min"] else "pending"
    killed = _supersede(user, text, kind)      # 同位置的新值取代旧值，避免两种说法并存
    now = _now()
    conn = ai.db()
    cur = conn.execute(
        "INSERT INTO memory(user,kind,text,src_conv,src_seq,src_time,confidence,status,created) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (user, kind, text, src_conv, src_seq, now, float(confidence), status, now))
    conn.commit()
    mid = cur.lastrowid
    conn.close()
    if killed:
        ai.audit_log(user, "memory_supersede", {"new": mid, "old": killed}, text[:80], True)
    return mid


# ---------------- 抽取：从某一会话里提炼值得长期记的条目 ----------------

def _conv_msgs(conv_id, limit=16):
    conn = ai.db()
    rows = [dict(r) for r in conn.execute(
        "SELECT seq,role,content FROM lcm_msg WHERE conv_id=? ORDER BY seq DESC LIMIT ?",
        (conv_id, limit))]
    conn.close()
    rows.reverse()
    return rows


def latest_conv(user):
    conn = ai.db()
    row = conn.execute("SELECT id FROM lcm_conv WHERE user=? ORDER BY id DESC LIMIT 1",
                       (user,)).fetchone()
    conn.close()
    return row["id"] if row else None


def _heuristic(msgs):
    """零依赖兜底：只从用户发言里挑有"具体值/明确决定"的句子。"""
    trig = ("定", "决定", "确定", "约定", "要求", "必须", "只允许", "不允许", "不要",
            "以后", "记住", "命名", "叫", "规范", "端口", "域名", "路径", "版本", "统一")
    out = []
    for m in msgs:
        if m["role"] != "user":
            continue
        t = re.sub(r"\s+", " ", m["content"]).strip()
        if len(t) < 8 or len(t) > 200:
            continue
        if not any(k in t for k in trig):
            continue
        kind = "decision" if any(k in t for k in ("定", "决定", "约定", "只允许", "规范", "统一")) else "fact"
        out.append({"kind": kind, "text": t[:120], "confidence": 0.55})
        if len(out) >= 3:
            break
    return out


def _llm_extract(msgs):
    body = "\n".join("%s: %s" % (m["role"], re.sub(r"\[PILOT:[a-z]+\]", "", m["content"]))
                     for m in msgs)
    prompt = (
        "你是记忆整理员。从下面的对话里抽取「值得跨会话长期记住」的条目。\n"
        "只抽四类：fact（稳定事实：数字、地址、端口、账号名、字段名）、"
        "decision（已定的方案/规范/审批规则）、preference（用户偏好）、"
        "artifact（产出物及其位置）。\n"
        "不要抽：寒暄、一次性提问、模型自己的解释、推测和过程描述。\n"
        "严禁补全原文没有出现的内容：不要编文件路径、配置项、命令、代码、账号；"
        "只保留对话里明确出现过的事实。如果确实没有值得长期记的，返回 []。\n"
        "每条 text 不超过 60 字，必须带具体值；只有用户明确陈述才给 confidence>0.8。\n"
        "只输出 JSON 数组，不要任何其它文字："
        '[{"kind":"fact","text":"部署端口 8060","confidence":0.9}]'
    )
    out, provider = ai.llm_chat([{"role": "system", "content": prompt},
                                 {"role": "user", "content": body[:3000]}], "system")
    if not out or provider == "mock":
        return None
    m = re.search(r"\[.*\]", out, re.S)
    if not m:
        return None
    try:
        arr = json.loads(m.group(0))
    except Exception:
        return None
    items = []
    for x in arr if isinstance(arr, list) else []:
        if not isinstance(x, dict):
            continue
        t = (x.get("text") or "").strip()
        if len(t) < 4:
            continue
        try:
            c = float(x.get("confidence", 0.6))
        except Exception:
            c = 0.6
        items.append({"kind": x.get("kind") or "fact", "text": t[:120],
                      "confidence": max(0.0, min(1.0, c))})
    return items or None


def extract(user, conv_id=None, use_llm=True, limit=16):
    """从某会话抽取候选记忆 → 落 pending（高置信度直接生效）。"""
    init_memory_db()
    conv_id = conv_id or latest_conv(user)
    if not conv_id:
        return {"error": "该用户还没有会话可抽取", "created": 0, "items": []}
    msgs = _conv_msgs(int(conv_id), limit=limit)
    if not msgs:
        return {"error": "会话不存在或没有消息", "created": 0, "items": []}

    items, method = None, "llm"
    if use_llm:
        try:
            items = _llm_extract(msgs)
        except Exception:
            items = None
    if not items:
        items, method = _heuristic(msgs), "heuristic"

    created, skipped, dropped = [], 0, 0
    src_text = re.sub(r"\[PILOT:[a-z]+\]", "",
                      "\n".join(m["content"] for m in msgs))
    for it in items:
        if any(n in it["text"] for n in NOISE):
            dropped += 1                      # 过程性元信息不是记忆
            continue
        if not _grounded(it["text"], src_text):
            dropped += 1                      # 模型补全出来的内容一律丢掉
            continue
        if _exists(user, it["text"]):
            skipped += 1
            continue
        mid = add(user, it["text"], it["kind"], it["confidence"],
                  src_conv=conv_id, src_seq=(msgs[-1]["seq"] if msgs else None))
        if mid:
            created.append({"id": mid, "kind": it["kind"], "text": it["text"][:120],
                            "confidence": round(it["confidence"], 2)})
    ai.audit_log(user, "memory_extract",
                 {"conv": conv_id, "method": method},
                 "created=%d skipped=%d dropped=%d" % (len(created), skipped, dropped), True)
    return {"conv_id": int(conv_id), "method": method,
            "created": len(created), "skipped": skipped, "dropped": dropped,
            "items": created}


# ---------------- 读取：关键词检索（不引向量库） ----------------

def _row(r):
    return {"id": r["id"], "user": r["user"], "kind": r["kind"], "text": r["text"],
            "src_conv": r["src_conv"], "src_time": r["src_time"],
            "confidence": r["confidence"], "status": r["status"],
            "created": r["created"], "hits": r["hits"], "last_used": r["last_used"]}


def search(user, q, topk=None, min_score=None, touch=True):
    """只在 active 记忆里按关键词打分。命中不足就不返回（宁可空白，不要瞎回忆）。"""
    p = policy()
    topk = topk or p["inject_topk"]
    min_score = p["min_score"] if min_score is None else min_score
    kws = [k for k in _kws(q, 16) if k]
    if not kws:
        return []
    conn = ai.db()
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM memory WHERE user=? AND status='active' ORDER BY id DESC LIMIT 200", (user,))]
    scored = []
    for r in rows:
        hit = sum(1 for k in kws if k in r["text"])
        if hit >= min_score:
            scored.append((hit, r))
    scored.sort(key=lambda x: (-x[0], -x[1]["id"]))
    picked = [r for _, r in scored[:topk]]
    if picked and touch:
        now = _now()
        for r in picked:
            conn.execute("UPDATE memory SET hits=hits+1, last_used=? WHERE id=?", (now, r["id"]))
        conn.commit()
    conn.close()
    return [_row(r) for r in picked]


def build_context(user, q, conv_id=None):
    """给模型用的记忆段落。返回 (text, ids)。命中不到就返回空串（不占预算、不诱导编造）。"""
    try:
        items = search(user, q)
    except Exception:
        return "", []
    if not items:
        return "", []
    lines = ["【跨会话长期记忆·仅供参考】（来自该用户此前的会话，可能已过期）"]
    for it in items:
        src = "M#%d" % it["id"]
        when = (it["src_time"] or it["created"] or "")[:10]
        conv = ("·会话#%d" % it["src_conv"]) if it["src_conv"] else ""
        lines.append("%s [%s %s%s] %s" % (src, when, it["kind"], conv, it["text"]))
    lines.append("使用要求：与本轮用户明示冲突时以本轮为准；引用记忆须标编号（如 M#12）；"
                 "记忆里没有的信息必须说不知道，不得编造。")
    return "\n".join(lines), [it["id"] for it in items]


# ---------------- 管理：列表 / 决定 / 统计 ----------------

def list_items(user, status="pending", limit=50, all_users=False):
    init_memory_db()
    sql = "SELECT * FROM memory"
    args, where = [], []
    if not all_users:
        where.append("user=?")
        args.append(user)
    if status and status != "all":
        where.append("status=?")
        args.append(status)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    conn = ai.db()
    rows = [_row(r) for r in conn.execute(sql, args)]
    conn.close()
    return rows


def counts(user, all_users=False):
    conn = ai.db()
    if all_users:
        rows = conn.execute("SELECT status, COUNT(*) c FROM memory GROUP BY status").fetchall()
    else:
        rows = conn.execute("SELECT status, COUNT(*) c FROM memory WHERE user=? GROUP BY status",
                            (user,)).fetchall()
    conn.close()
    out = {"pending": 0, "active": 0, "disabled": 0}
    for r in rows:
        out[r["status"]] = r["c"]
    out["total"] = sum(out.values())
    return out


def get(mid):
    conn = ai.db()
    r = conn.execute("SELECT * FROM memory WHERE id=?", (mid,)).fetchone()
    conn.close()
    return _row(r) if r else None


def decide(mid, action, by):
    """action: activate（记住）/ disable（停用）/ delete（删除）。只能动自己的记忆。"""
    it = get(mid)
    if not it:
        return {"error": "记忆 #%d 不存在" % mid}
    if it["user"] != by and ai.user_role(by) != "admin":
        return {"error": "无权处理别人的记忆"}
    conn = ai.db()
    if action == "delete":
        conn.execute("DELETE FROM memory WHERE id=?", (mid,))
        conn.commit()
        conn.close()
        ai.audit_log(by, "memory_delete", {"id": mid}, it["text"][:80], True)
        return {"ok": True, "id": mid, "action": "delete", "deleted": True}
    if action not in ("activate", "disable"):
        conn.close()
        return {"error": "action 须为 activate / disable / delete"}
    status = "active" if action == "activate" else "disabled"
    conn.execute("UPDATE memory SET status=?, decided=?, decided_by=? WHERE id=?",
                 (status, _now(), by, mid))
    conn.commit()
    conn.close()
    ai.audit_log(by, "memory_" + action, {"id": mid}, it["text"][:80], True)
    return {"ok": True, "id": mid, "action": action, "status": status}
