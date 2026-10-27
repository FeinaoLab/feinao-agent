# -*- coding: utf-8 -*-
"""飞脑AI Agent网关 · HTTP 引擎（零依赖 ThreadingHTTPServer）
路由：静态(/) + /api/{health,login,dashboard,ai/{ask,nl2api,script},chat,react,audit,rag/ingest,providers,conversations}"""
import hashlib
import io
import json
import mimetypes
import os
import secrets
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))
FRONTEND = os.path.abspath(os.path.join(BASE, "..", "frontend"))
MOBILE = os.path.abspath(os.path.join(BASE, "..", "mobile"))

import ai  # noqa: E402
import lcm  # noqa: E402
import memory  # noqa: E402
import json as _json

SPEC = ai.SPEC
TOKENS = {}
server_version = "FeiNao/1.0"


def _auth(request, roles=None):
    tok = request.headers.get("Authorization", "").replace("Bearer ", "")
    info = TOKENS.get(tok)
    if not info:
        return None
    if roles and info["role"] not in roles:
        return False
    return info


class Handler(BaseHTTPRequestHandler):
    server_version = server_version

    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def _static(self, path, root):
        if path in ("/", "/index.html"):
            fp = os.path.join(root, "index.html")
        elif path == "/app.js":
            fp = os.path.join(root, "app.js")
        elif path == "/style.css":
            fp = os.path.join(root, "style.css")
        elif path == "/icon.svg":
            fp = os.path.join(root, "icon.svg")
        elif path == "/config.js":
            fp = os.path.join(root, "config.js")
        elif path == "/manifest.webmanifest":
            fp = os.path.join(root, "manifest.webmanifest")
        elif path == "/sw.js":
            fp = os.path.join(root, "sw.js")
        else:
            self.send_response(404)
            self.end_headers()
            return
        if not os.path.exists(fp):
            self.send_response(404)
            self.end_headers()
            return
        ctype = mimetypes.guess_type(fp)[0] or "application/octet-stream"
        if fp.endswith(".js"):
            ctype = "application/javascript"
        with open(fp, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ---------- GET ----------
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        parts = [p for p in u.path.split("/") if p]
        qs = urllib.parse.parse_qs(u.query)
        # mobile 静态
        if parts[:1] == ["m"]:
            if len(parts) == 1:
                return self._static("/index.html", MOBILE)
            rest = "/" + "/".join(parts[1:])
            return self._static(rest, MOBILE)
        if parts[:1] != ["api"]:
            return self._static(u.path or "/", FRONTEND)

        try:
            if len(parts) == 2 and parts[1] == "health":
                return self._json(200, {"status": "ok", "service": SPEC["brand"]["full"],
                                        "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                        "providers": [p["name"] for p in SPEC["providers"]]})
            if len(parts) == 2 and parts[1] == "providers":
                return self._json(200, {"providers": [{"name": p["name"], "type": p["type"]} for p in SPEC["providers"]],
                                        "upstreams": {k: v["label"] for k, v in SPEC["upstreams"].items()}})
            if parts[1:3] == ["providers", "health"]:
                # GET /api/providers/health?ping=1 —— Key 是否就位 / 真发请求验证
                me = _auth(self)
                if not me:
                    return self._json(401, {"error": "请先登录"})
                ping = (qs.get("ping", ["0"])[0] in ("1", "true", "yes"))
                return self._json(200, ai.providers_health(ping=ping))
            if len(parts) == 2 and parts[1] == "dashboard":
                me = _auth(self)
                if not me:
                    return self._json(401, {"error": "请先登录"})
                conn = ai.db()
                counts = {
                    "会话": conn.execute("SELECT COUNT(*) c FROM conversations").fetchone()["c"],
                    "消息": conn.execute("SELECT COUNT(*) c FROM messages").fetchone()["c"],
                    "审计": conn.execute("SELECT COUNT(*) c FROM audit").fetchone()["c"],
                    "知识文档": conn.execute("SELECT COUNT(*) c FROM docs").fetchone()["c"],
                    "任务": conn.execute("SELECT COUNT(*) c FROM tasks").fetchone()["c"],
                    "证据": conn.execute("SELECT COUNT(*) c FROM evidence").fetchone()["c"],
                    "记忆": conn.execute("SELECT COUNT(*) c FROM memory WHERE status='active'").fetchone()["c"],
                    "待确认记忆": conn.execute("SELECT COUNT(*) c FROM memory WHERE status='pending'").fetchone()["c"],
                    "待审批": conn.execute(
                        "SELECT COUNT(*) c FROM approvals WHERE status='pending'").fetchone()["c"],
                }
                ok_calls = conn.execute("SELECT COUNT(*) c FROM usage WHERE ok=1").fetchone()["c"]
                fail_calls = conn.execute("SELECT COUNT(*) c FROM usage WHERE ok=0").fetchone()["c"]
                conn.close()
                return self._json(200, {"service": SPEC["brand"]["full"], "counts": counts, "role": me["role"],
                                        "llm_ok": ok_calls, "llm_fail": fail_calls})
            if len(parts) == 2 and parts[1] == "audit":
                me = _auth(self, roles=("admin", "ops"))
                if not me:
                    return self._json(403 if me is False else 401, {"error": "请先登录或无权限"})
                conn = ai.db()
                rows = [dict(r) for r in conn.execute(
                    "SELECT id,ts,user,tool,args,result,ok FROM audit ORDER BY id DESC LIMIT 50")]
                conn.close()
                return self._json(200, {"rows": rows})
            if len(parts) == 2 and parts[1] == "conversations":
                me = _auth(self)
                if not me:
                    return self._json(401, {"error": "请先登录"})
                conn = ai.db()
                rows = [dict(r) for r in conn.execute(
                    "SELECT id,title,created FROM conversations WHERE user=? ORDER BY id DESC LIMIT 30", (me["user"],))]
                conn.close()
                return self._json(200, {"rows": rows})
            if len(parts) == 2 and parts[1] == "lcm_convs":
                # GET /api/lcm_convs —— LCM 会话列表（带轮次与最后一句）
                me = _auth(self)
                if not me:
                    return self._json(401, {"error": "请先登录"})
                conn = ai.db()
                rows = [dict(r) for r in conn.execute(
                    "SELECT c.id, c.title, c.created, "
                    "(SELECT COUNT(*) FROM lcm_msg m WHERE m.conv_id=c.id) turns "
                    "FROM lcm_conv c WHERE c.user=? ORDER BY c.id DESC LIMIT 30", (me["user"],))]
                for r in rows:
                    last = conn.execute(
                        "SELECT content FROM lcm_msg WHERE conv_id=? ORDER BY seq DESC LIMIT 1",
                        (r["id"],)).fetchone()
                    r["last"] = (last["content"][:40] if last else "")
                conn.close()
                return self._json(200, {"rows": rows})
            if len(parts) == 2 and parts[1] == "tasks":
                # GET /api/tasks —— 当前用户的任务列表（State 层）
                me = _auth(self)
                if not me:
                    return self._json(401, {"error": "请先登录"})
                return self._json(200, {"rows": ai.task_list(user=me["user"])})
            if parts[1:3] == ["tasks", "detail"]:
                # GET /api/tasks/detail?id=N —— 任务详情 + 全部步骤
                me = _auth(self)
                if not me:
                    return self._json(401, {"error": "请先登录"})
                tid = int((qs.get("id") or ["0"])[0])
                if not tid:
                    return self._json(422, {"error": "id 必填"})
                t = ai.task_get(tid, user=me["user"])
                if not t:
                    return self._json(404, {"error": "任务不存在或不属于当前用户"})
                return self._json(200, t)
            if len(parts) == 2 and parts[1] == "approvals":
                # GET /api/approvals?status=pending|approved|rejected|expired|all
                # 有审批权的人看全部待办，其他人只看自己发起的
                me = _auth(self)
                if not me:
                    return self._json(401, {"error": "请先登录"})
                pol = ai.approval_policy()
                role = ai.user_role(me["user"])
                can = role in pol["approvers"]
                status = (qs.get("status") or ["pending"])[0]
                rows = ai.approval_list(status=status, user=me["user"], only_mine=not can)
                return self._json(200, {
                    "rows": rows, "can_approve": can, "my_role": role,
                    "brand": pol["name"],
                    "approvers": pol["approvers"],
                    "pending": len([r for r in rows if r["status"] == "pending"])})
            if len(parts) == 2 and parts[1] == "evidence":
                # GET /api/evidence?conv_id=N —— 证据链查询（Harness 第 5 层）
                me = _auth(self)
                if not me:
                    return self._json(401, {"error": "请先登录"})
                cid = int((qs.get("conv_id") or ["0"])[0]) or None
                return self._json(200, {"rows": ai.evidence_list(user=me["user"], conv_id=cid)})
            if len(parts) == 2 and parts[1] == "memory":
                # GET /api/memory?status=pending|active|disabled|all —— 飞忆（跨会话长期记忆）
                me = _auth(self)
                if not me:
                    return self._json(401, {"error": "请先登录"})
                status = (qs.get("status") or ["pending"])[0]
                rows = memory.list_items(me["user"], status=status)
                pol = memory.policy()
                return self._json(200, {"rows": rows, "brand": pol["name"],
                                        "counts": memory.counts(me["user"]),
                                        "my_role": ai.user_role(me["user"]),
                                        "inject_topk": pol["inject_topk"]})
            if len(parts) == 2 and parts[1] == "lcm":
                # GET /api/lcm?conv_id=N —— 会话的库结构统计与摘要节点（DAG 视图）
                me = _auth(self)
                if not me:
                    return self._json(401, {"error": "请先登录"})
                cid = int((qs.get("conv_id") or ["0"])[0])
                if not cid:
                    return self._json(422, {"error": "conv_id 必填"})
                o = lcm.LCM(conv_id=cid, user=me["user"])
                return self._json(200, {"stats": o.stats(), "nodes": o.nodes()})
            return self._json(404, {"error": "not found"})
        except Exception as e:
            return self._json(500, {"error": str(e)[:200]})

    # ---------- POST ----------
    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        parts = [p for p in u.path.split("/") if p]
        if parts[:1] != ["api"]:
            return self._json(404, {"error": "not found"})
        body = self._body()
        try:
            if parts[1:2] == ["login"]:
                username = body.get("username", "")
                password = body.get("password", "")
                conn = ai.db()
                row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
                conn.close()
                if not row or row["password"] != password:
                    return self._json(401, {"error": "账号或密码错误"})
                tok = secrets.token_hex(16)
                TOKENS[tok] = {"user": username, "role": row["role"]}
                return self._json(200, {"token": tok, "user": username, "role": row["role"]})

            me = _auth(self)
            if me is None:
                return self._json(401, {"error": "请先登录"})
            user = me["user"]

            if parts[1:3] == ["ai", "ask"]:
                q = (body.get("q") or "").strip()
                if not q:
                    return self._json(422, {"error": "q 必填"})
                return self._json(200, ai.pilot_ask(q, user))

            if parts[1:3] == ["ai", "nl2api"]:
                q = (body.get("q") or "").strip()
                system = body.get("system", "bi")
                if system not in SPEC["upstreams"]:
                    return self._json(422, {"error": "system 须为 " + ",".join(SPEC["upstreams"])})
                if not q:
                    return self._json(422, {"error": "q 必填"})
                return self._json(200, ai.pilot_nl2api(q, system, user))

            if parts[1:3] == ["ai", "script"]:
                scenario = (body.get("scenario") or "").strip()
                if not scenario:
                    return self._json(422, {"error": "scenario 必填"})
                return self._json(200, ai.pilot_script(scenario, body.get("tone", "专业友好"), user))

            if parts[1:3] == ["ai", "react"]:
                task = (body.get("task") or "").strip()
                if not task and not body.get("task_id"):
                    return self._json(422, {"error": "task 必填（续跑时 task 可空）"})
                return self._json(200, ai.react_run(task or "（续跑）", user,
                                                    task_id=body.get("task_id")))

            if parts[1:3] == ["approvals", "decide"]:
                # POST /api/approvals/decide {id, decision: approved|rejected, note?}
                # 放行 = 真执行挂起的那一次动作，然后任务自动续跑
                aid = int(body.get("id") or 0)
                decision = body.get("decision")
                if not aid or decision not in ("approved", "rejected"):
                    return self._json(422, {"error": "id / decision(approved|rejected) 必填"})
                r = ai.approval_handle(aid, decision, user, body.get("note", ""))
                if r.get("error"):
                    msg = r["error"]
                    if "无审批权" in msg or "无权" in msg or "不允许" in msg:
                        code = 403
                    elif "不存在" in msg:
                        code = 404
                    else:
                        code = 409
                    return self._json(code, r)
                return self._json(200, r)

            if parts[1:3] == ["memory", "decide"]:
                # POST /api/memory/decide {id, action: activate|disable|delete}
                mid = int(body.get("id") or 0)
                action = body.get("action")
                if not mid or action not in ("activate", "disable", "delete"):
                    return self._json(422, {"error": "id / action(activate|disable|delete) 必填"})
                r = memory.decide(mid, action, user)
                if r.get("error"):
                    code = 404 if "不存在" in r["error"] else 403
                    return self._json(code, r)
                return self._json(200, r)

            if parts[1:3] == ["memory", "extract"]:
                # POST /api/memory/extract {conv_id?} —— 从最近会话抽取候选记忆（默认进待确认区）
                cid = body.get("conv_id")
                r = memory.extract(user, conv_id=int(cid) if cid else None,
                                   use_llm=bool(body.get("use_llm", True)))
                if r.get("error"):
                    return self._json(422, r)
                return self._json(200, r)

            if parts[1:3] == ["chat"]:
                text = (body.get("text") or "").strip()
                if text:
                    # 在线链路走 LCM：会话记忆存在服务端，前端只发本轮这句话。
                    # 历史摘要 + 最近原文 + 关键词回溯，一起组成本轮上下文。
                    cid = body.get("conv_id") or None
                    keep_recent = int(body.get("keep_recent") or 8)
                    budget = int(body.get("budget") or 1200)
                    o = lcm.LCM(conv_id=int(cid) if cid else None, user=user, title=text)
                    out = o.ask(text, keep_recent=keep_recent, budget=budget)
                    out["stats"] = o.stats(keep_recent=keep_recent, budget=budget)
                    return self._json(200, out)
                msgs = body.get("messages") or []
                if not msgs:
                    return self._json(422, {"error": "messages 必填"})
                # 兼容链路：客户端没带 system 时补上人设 + 验证型收尾要求
                if not any(m.get("role") == "system" for m in msgs):
                    msgs = [{"role": "system", "content": "你是飞脑，飞系 52 个项目的 AI 助手。\n" + ai.VERIFY_TAIL}] + msgs
                conn = ai.db()
                cur = conn.execute("INSERT INTO conversations(user,title,created) VALUES(?,?,?)",
                                   (user, (msgs[-1].get("content") or "")[:40],
                                    time.strftime("%Y-%m-%dT%H:%M:%S")))
                conv = cur.lastrowid
                for m in msgs:
                    conn.execute("INSERT INTO messages(conv_id,role,content,created) VALUES(?,?,?,?)",
                                 (conv, m.get("role"), (m.get("content") or "")[:4000],
                                  time.strftime("%Y-%m-%dT%H:%M:%S")))
                conn.commit()
                conn.close()
                ans, provider = ai.llm_chat(msgs, user)
                conn = ai.db()
                conn.execute("INSERT INTO messages(conv_id,role,content,created) VALUES(?,?,?,?)",
                             (conv, "assistant", ans[:4000], time.strftime("%Y-%m-%dT%H:%M:%S")))
                conn.commit()
                conn.close()
                ai.audit_log(user, "chat", {"turns": len(msgs)}, ans[:200], True)
                return self._json(200, {"answer": ans, "provider": provider, "conv_id": conv})

            if parts[1:3] == ["rag", "ingest"]:
                if me["role"] != "admin":
                    return self._json(403, {"error": "仅管理员可入库"})
                title = (body.get("title") or "").strip()
                text = (body.get("body") or "").strip()
                if not title or not text:
                    return self._json(422, {"error": "title/body 必填"})
                rid = ai.rag_ingest(title, text, body.get("source", "manual"))
                ai.audit_log(user, "rag_ingest", {"title": title}, "doc id=%d" % rid, True)
                return self._json(200, {"id": rid})

            if parts[1:3] == ["rag", "search"]:
                q = (body.get("q") or "").strip()
                if not q:
                    return self._json(422, {"error": "q 必填"})
                return self._json(200, {"hits": ai.rag_search(q)})

            if parts[1:3] == ["ai", "lcm"]:
                # 无损上下文会话：POST /api/ai/lcm {text, conv_id?, keep_recent?, budget?}
                text = (body.get("text") or "").strip()
                if not text:
                    return self._json(422, {"error": "text 必填"})
                o = lcm.LCM(conv_id=body.get("conv_id") or None, user=user, title=text)
                out = o.ask(text,
                            keep_recent=int(body.get("keep_recent") or 8),
                            budget=int(body.get("budget") or 1200))
                out["stats"] = o.stats(keep_recent=int(body.get("keep_recent") or 8),
                                       budget=int(body.get("budget") or 1200))
                return self._json(200, out)

            if parts[1:3] == ["ai", "lcm_detail"]:
                # 回溯：POST /api/ai/lcm_detail {conv_id, q} —— 从原文里捞细节
                cid = body.get("conv_id")
                q = (body.get("q") or "").strip()
                if not cid or not q:
                    return self._json(422, {"error": "conv_id/q 必填"})
                o = lcm.LCM(conv_id=int(cid), user=user)
                return self._json(200, {"hits": o.retrieve(q)})

            return self._json(404, {"error": "not found"})
        except Exception as e:
            return self._json(500, {"error": str(e)[:200]})


def run():
    ai.init_db()
    lcm.init_lcm_db()
    memory.init_memory_db()
    port = int(os.environ.get("FEINAO_PORT", SPEC["brand"]["port"]))
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print("FeiNao %s on :%d" % (SPEC["brand"]["full"], port))
    srv.serve_forever()


if __name__ == "__main__":
    run()
