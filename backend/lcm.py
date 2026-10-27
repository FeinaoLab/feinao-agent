# -*- coding: utf-8 -*-
"""LCM：无损上下文管理（Lossless Context Management）最小原型。

思路（参考 hermes-lcm 的 DAG 思路，用标准库自己实现）：
  1. 原始消息全量落库，一条都不丢（lcm_msg）；
  2. 会话变长时，把较早的原始消息压成摘要节点（lcm_node），节点可再压成更高层节点，形成 DAG；
  3. 组装上下文 = 高层摘要（覆盖久远历史）+ 最近若干轮原文（热区）；
  4. 摘要把细节抹平了怎么办？retrieve() 按关键词在原始消息里回溯，把原话捞回来。

零依赖：仅 Python 标准库 + 复用 ai.py 的 LLM 网关与数据库连接。
"""
import json
import os
import re
import sqlite3
import time

import ai
import memory


# ---------------- token 估算（中文按字、英文按词，够用即可） ----------------

def est_tokens(text):
    if not text:
        return 0
    cn = len(re.findall(r"[\u4e00-\u9fff]", text))
    en = len(re.findall(r"[A-Za-z0-9_]+", text))
    return cn + en


def msgs_tokens(msgs):
    return sum(est_tokens(m.get("content", "")) for m in msgs)


# ---------------- 建表 ----------------

def init_lcm_db():
    ai.init_db()          # 复用基础表（users/usage/audit…），幂等
    conn = ai.db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS lcm_conv(
        id INTEGER PRIMARY KEY AUTOINCREMENT, user TEXT, title TEXT, created TEXT);
    CREATE TABLE IF NOT EXISTS lcm_msg(
        id INTEGER PRIMARY KEY AUTOINCREMENT, conv_id INT, seq INT, role TEXT,
        content TEXT, created TEXT);
    CREATE TABLE IF NOT EXISTS lcm_node(
        id INTEGER PRIMARY KEY AUTOINCREMENT, conv_id INT, level INT,
        from_seq INT, to_seq INT, summary TEXT, method TEXT, created TEXT);
    CREATE INDEX IF NOT EXISTS idx_lcm_msg_conv ON lcm_msg(conv_id, seq);
    CREATE INDEX IF NOT EXISTS idx_lcm_node_conv ON lcm_node(conv_id, level);
    """)
    conn.commit()
    conn.close()
    memory.init_memory_db()      # 飞忆：跨会话长期记忆（另一层，幂等建表）


# ---------------- 核心对象 ----------------

class LCM:
    """一个会话一个实例。所有原始消息永久保留。"""

    def __init__(self, conv_id=None, user="admin", title=""):
        init_lcm_db()
        conn = ai.db()
        if conv_id is None:
            cur = conn.execute("INSERT INTO lcm_conv(user,title,created) VALUES(?,?,?)",
                               (user, title[:60], time.strftime("%Y-%m-%dT%H:%M:%S")))
            conv_id = cur.lastrowid
            conn.commit()
            self._seq = 0
        else:
            row = conn.execute("SELECT MAX(seq) s FROM lcm_msg WHERE conv_id=?", (conv_id,)).fetchone()
            self._seq = (row["s"] or 0) if row else 0
        conn.close()
        self.conv_id = conv_id
        self.user = user

    # -------- 写入：原文永远先落库 --------

    def append(self, role, content):
        self._seq += 1
        conn = ai.db()
        conn.execute("INSERT INTO lcm_msg(conv_id,seq,role,content,created) VALUES(?,?,?,?,?)",
                     (self.conv_id, self._seq, role, (content or "")[:4000],
                      time.strftime("%Y-%m-%dT%H:%M:%S")))
        conn.commit()
        conn.close()
        return self._seq

    def turn(self, text):
        """一次用户发言（并返回 seq）。"""
        return self.append("user", text)

    # -------- 读取 --------

    def messages(self, a=None, b=None):
        conn = ai.db()
        sql = "SELECT seq,role,content FROM lcm_msg WHERE conv_id=?"
        args = [self.conv_id]
        if a is not None:
            sql += " AND seq>=?"
            args.append(a)
        if b is not None:
            sql += " AND seq<=?"
            args.append(b)
        rows = [dict(r) for r in conn.execute(sql + " ORDER BY seq", args)]
        conn.close()
        return rows

    def nodes(self, level=None):
        conn = ai.db()
        if level is None:
            rows = [dict(r) for r in conn.execute(
                "SELECT id,level,from_seq,to_seq,summary,method FROM lcm_node WHERE conv_id=? ORDER BY level,from_seq",
                (self.conv_id,))]
        else:
            rows = [dict(r) for r in conn.execute(
                "SELECT id,level,from_seq,to_seq,summary,method FROM lcm_node WHERE conv_id=? AND level=? ORDER BY from_seq",
                (self.conv_id, level))]
        conn.close()
        return rows

    # -------- 压缩：把未被覆盖的早期原文压成节点 --------

    def compress(self, keep_recent=8, chunk=6, use_llm=True):
        """把 tail=keep_recent 之前的、尚未被任何节点覆盖的原文，压成 level-1 节点。
        返回新建节点列表。"""
        raw = self.messages()
        if len(raw) <= keep_recent:
            return []
        cutoff = raw[-keep_recent]["seq"]          # 热区起点（保留原文）
        covered = set()
        for n in self.nodes():                     # 任意层级已覆盖的都不重复压
            covered.update(range(n["from_seq"], n["to_seq"] + 1))
        pending = [m for m in raw if m["seq"] < cutoff and m["seq"] not in covered]
        if not pending:
            return []

        created = []
        conn = ai.db()
        for i in range(0, len(pending), chunk):
            part = pending[i:i + chunk]
            a, b = part[0]["seq"], part[-1]["seq"]
            summary, method = _summarize(part, use_llm=use_llm)
            cur = conn.execute(
                "INSERT INTO lcm_node(conv_id,level,from_seq,to_seq,summary,method,created) VALUES(?,?,?,?,?,?,?)",
                (self.conv_id, 1, a, b, summary, method, time.strftime("%Y-%m-%dT%H:%M:%S")))
            conn.commit()
            created.append({"id": cur.lastrowid, "level": 1, "from_seq": a, "to_seq": b,
                            "summary": summary, "method": method})
        conn.close()
        return created

    def compress_up(self, max_nodes=4, use_llm=True):
        """节点太多时，把最老的 level-1 节点合并成 level-2（DAG 上一层）。"""
        n1 = self.nodes(1)
        if len(n1) <= max_nodes:
            return []
        olds = n1[:-max_nodes]
        part = [{"role": "summary", "content": n["summary"], "seq": n["from_seq"]} for n in olds]
        summary, method = _summarize(part, use_llm=use_llm)
        conn = ai.db()
        cur = conn.execute(
            "INSERT INTO lcm_node(conv_id,level,from_seq,to_seq,summary,method,created) VALUES(?,?,?,?,?,?,?)",
            (self.conv_id, 2, olds[0]["from_seq"], olds[-1]["to_seq"], summary, method,
             time.strftime("%Y-%m-%dT%H:%M:%S")))
        conn.commit()
        node_id = cur.lastrowid
        conn.close()
        return [{"id": node_id, "level": 2, "from_seq": olds[0]["from_seq"],
                 "to_seq": olds[-1]["to_seq"], "summary": summary, "method": method}]

    # -------- 组装上下文（这是"不丢细节"的关键） --------

    def build_context(self, keep_recent=8, budget=1200, system="", use_llm=True, mutate=True):
        """返回可直接喂给模型的 messages 列表。

        结构：system（历史摘要）+ 热区原文；摘要从最新往回装，装到预算用完为止。
        mutate=True 时允许现场压缩（会写节点）；False 则只用已有节点，纯读。
        """
        if mutate:
            self.compress(keep_recent=keep_recent, use_llm=use_llm)
            self.compress_up(use_llm=use_llm)

        raw = self.messages()
        recent = raw[-keep_recent:] if keep_recent else []
        recent_seqs = {m["seq"] for m in recent}
        recent_msgs = [{"role": m["role"], "content": m["content"]} for m in recent]
        base_tokens = msgs_tokens(recent_msgs) + est_tokens(system)

        # 选摘要：从最新往外挑，跳过与热区重叠的、已被更高层覆盖的
        avail = max(0, budget - base_tokens)
        picked, used = [], set()
        for n in sorted(self.nodes(), key=lambda x: (-x["to_seq"], -x["level"])):
            rng = set(range(n["from_seq"], n["to_seq"] + 1))
            if rng & recent_seqs or rng & used:
                continue
            t = est_tokens(n["summary"]) + 8
            if t > avail:
                continue
            picked.append(n)
            used |= rng
            avail -= t
        picked.sort(key=lambda x: x["from_seq"])
        lines = ["[历史摘要 L%d·第%d-%d轮] %s" % (n["level"], n["from_seq"], n["to_seq"], n["summary"])
                 for n in picked]

        msgs = []
        sys_txt = ((system + "\n") if system else "") + "\n".join(lines)
        if sys_txt.strip():
            msgs.append({"role": "system", "content": sys_txt})
        msgs += recent_msgs
        # 兜底：仍然超预算就先丢最老的原文轮次
        while msgs_tokens(msgs) > budget and len(msgs) > 1:
            msgs.pop(1 if msgs[0]["role"] == "system" else 0)
        return msgs

    # -------- 回溯：摘要把细节压掉了，从原文里捞回来 --------

    def retrieve(self, query, topk=3, exclude_seq=None):
        """按关键词在原始消息中回溯（bigram 分词，复用 ai._keywords）。
        exclude_seq 用于排除刚落库的当前提问，避免"自己检索到自己"。"""
        kws = [k for k in ai._keywords(query, 10) if k]
        if not kws:
            return []
        conn = ai.db()
        rows = [dict(r) for r in conn.execute(
            "SELECT seq,role,content FROM lcm_msg WHERE conv_id=? ORDER BY seq", (self.conv_id,))]
        conn.close()
        scored = []
        for r in rows:
            if exclude_seq is not None and r["seq"] == exclude_seq:
                continue
            hit = sum(1 for k in kws if k in r["content"])
            if hit:
                scored.append((hit, r))
        scored.sort(key=lambda x: (-x[0], -x[1]["seq"]))
        return [{"seq": r["seq"], "role": r["role"], "content": r["content"],
                 "score": s} for s, r in scored[:topk]]

    # -------- 统计（给对比测试用） --------

    def stats(self, keep_recent=8, budget=1200):
        raw = self.messages()
        full = [{"role": m["role"], "content": m["content"]} for m in raw]
        ctx = self.build_context(keep_recent=keep_recent, budget=budget, mutate=False)
        nodes = self.nodes()
        return {
            "conv_id": self.conv_id,
            "turns": len(raw),
            "raw_tokens": msgs_tokens(full),
            "context_tokens": msgs_tokens(ctx),
            "budget": budget,
            "nodes": len(nodes),
            "levels": sorted({n["level"] for n in nodes}) if nodes else [],
            "compression": round(1 - msgs_tokens(ctx) / max(1, msgs_tokens(full)), 3),
        }

    # -------- 一问一答（端到端） --------

    def ask(self, q, keep_recent=8, budget=1200, use_detail=True, use_memory=True):
        """用户提问 → 摘要+热区 → 回溯原文 → 跨会话记忆 → 调模型 → 落库回答。
        回溯是常态动作（摘要必然有损），不是补救措施。
        记忆只读注入：命中不到就不注入，不占预算、不诱导编造。"""
        self.turn(q)
        sys_prompt = ("你是飞脑长期会话助手。下面给你的是本会话的历史摘要、最近对话，"
                      "以及按问题回溯到的原文片段；请依据这些信息作答，"
                      "没有的信息明确说不知道，不要编。" + ai.VERIFY_TAIL + " [PILOT:lcm]")
        mem_block, mem_ids = ("", [])
        if use_memory:
            try:
                mem_block, mem_ids = memory.build_context(self.user, q, conv_id=self.conv_id)
            except Exception:
                mem_block, mem_ids = "", []
        if mem_block:
            sys_prompt += "\n" + mem_block
        ctx = self.build_context(keep_recent=keep_recent, budget=budget, system=sys_prompt)
        detail = self.retrieve(q, 3, exclude_seq=self._seq) if use_detail else []
        if detail:
            ctx.append({"role": "system", "content": "【回溯原文】\n" + "\n".join(
                "[#%d %s] %s" % (d["seq"], d["role"], d["content"]) for d in detail)})
        ctx.append({"role": "user", "content": q})
        ans, provider = ai.llm_chat(ctx, self.user)
        self.append("assistant", ans)
        ai.audit_log(self.user, "lcm_ask",
                     {"conv": self.conv_id, "q": q, "ctx_tokens": msgs_tokens(ctx),
                      "detail": [d["seq"] for d in detail], "memory": mem_ids}, ans[:200], True)
        eid = ai.evidence_log(self.user, "lcm", self.conv_id, q, ans, provider,
                              sources=[{"seq": d["seq"], "role": d["role"]} for d in detail],
                              trace={"summaries": ctx[0]["content"].count("[历史摘要") if ctx and ctx[0]["role"] == "system" else 0,
                                     "turns": self._seq, "memory": mem_ids},
                              context_tokens=msgs_tokens(ctx))
        return {"answer": ans, "provider": provider, "conv_id": self.conv_id,
                "context_tokens": msgs_tokens(ctx),
                "summaries": ctx[0]["content"].count("[历史摘要") if ctx and ctx[0]["role"] == "system" else 0,
                "detail_back": [d["seq"] for d in detail],
                "memory_ids": mem_ids,
                "evidence_id": eid}


# ---------------- 摘要生成（LLM 优先，抽取式兜底） ----------------

def _summarize(part, use_llm=True):
    """part: [{seq,role,content}]。返回 (summary, method)。"""
    body = "\n".join("%s: %s" % (m["role"], m["content"]) for m in part)
    if use_llm:
        try:
            out, _ = ai.llm_chat([
                {"role": "system", "content": "你是会议速记员。把下面的对话压成 3-5 条要点，"
                                              "必须保留具体数字、人名、日期、地点、编号，不要评价，不要编。 [PILOT:sum]"},
                {"role": "user", "content": body[:3000]}], "system")
            if out and "mock" not in str(out)[:20] and len(out) > 10:
                return out[:800], "llm"
        except Exception:
            pass
    return _extractive(part), "extractive"


def _extractive(part):
    """零依赖兜底：保留每轮的关键片段与其中的数字/编号。"""
    out = []
    for m in part:
        t = re.sub(r"\s+", " ", m["content"]).strip()
        nums = re.findall(r"[0-9]+(?:\.[0-9]+)?[^\s，。；,;]{0,6}", t)
        head = t[:40]
        tail = ("（" + "、".join(nums[:3]) + "）") if nums else ""
        out.append("- " + head + ("…" if len(t) > 40 else "") + tail)
    return "\n".join(out)[:800]


# ---------------- 便捷函数（供 HTTP 层调用） ----------------

def get_or_create(conv_id=None, user="admin", title=""):
    return LCM(conv_id=conv_id, user=user, title=title)
