# -*- coding: utf-8 -*-
"""飞脑 AI 层：LLM 多供应商网关（降级链）+ RAG(FTS5) + 工具 + 三试点。
零依赖：仅 Python 标准库。"""
import difflib
import hashlib
import io
import ipaddress
import json
import os
import re
import socket
import sqlite3
import time
import urllib.parse
import urllib.request
import urllib.error

BASE = os.path.dirname(os.path.abspath(__file__))
SPEC_PATH = os.environ.get("FEINAO_SPEC") or os.path.join(BASE, "spec.json")


class SpecError(RuntimeError):
    """spec.json 不符合约定。启动阶段抛出 = 拒绝启动：配置坏了宁可不上线，也别带病跑。"""


# 启动必需段。缺了系统根本跑不起来，必须指名道姓报错，而不是跑到一半 KeyError。
SPEC_REQUIRED = {"brand": dict, "users": dict, "upstreams": dict,
                 "pilots": dict, "approval": dict, "tool_policies": dict}

DEFAULT_MODES = {
    "default": "default",
    "definitions": {
        "plan":    {"read": "allow", "write": "suspend", "dangerous": "deny"},
        "default": {"read": "allow", "write": "confirm", "dangerous": "confirm"},
        "auto":    {"read": "allow", "write": "confirm", "dangerous": "confirm"},
        "yolo":    {"read": "allow", "write": "allow", "dangerous": "allow"},
    },
    "yolo_allowed_roles": [],
}
DEFAULT_LIMITS = {"max_steps": 20, "max_wall_clock_seconds": 600,
                  "same_call_repeat_limit": 3, "malformed_json_limit": 2}
DEFAULT_EGRESS = {"enabled": False, "allow_schemes": ["http", "https"], "allow_domains": [],
                  "allow_ports": [80, 443], "trust_upstream_origins": True,
                  "deny_private_networks": True, "deny_metadata_endpoints": True,
                  "max_response_bytes": 5242880, "request_timeout_seconds": 15}
MODE_ACTIONS = ("allow", "confirm", "suspend", "deny")
TOOL_CLASSES = ("read", "write", "dangerous")


def _copy(o):
    return json.loads(json.dumps(o))


def _merge(base, over):
    """深合并：over 覆盖 base，dict 逐键递归，其余整体替换。"""
    if not isinstance(over, dict) or not isinstance(base, dict):
        return _copy(over)
    out = _copy(base)
    for k, v in over.items():
        out[k] = _merge(base.get(k), v) if isinstance(v, dict) and isinstance(base.get(k), dict) else _copy(v)
    return out


def validate_spec(spec, path=None):
    """结构校验：必需段、跨段引用、枚举取值。只查启动必需项，可选段由默认值兜底。"""
    where = path or SPEC_PATH
    if not isinstance(spec, dict):
        raise SpecError("%s 顶层必须是对象，实际是 %s" % (where, type(spec).__name__))
    for k, t in SPEC_REQUIRED.items():
        if k not in spec:
            raise SpecError("%s 缺少顶层段 %s" % (where, k))
        if not isinstance(spec[k], t):
            raise SpecError("%s 的 %s 应为 %s，实际是 %s" % (where, k, t.__name__, type(spec[k]).__name__))
    if not spec["users"]:
        raise SpecError("%s 的 users 为空：没有用户就没人能登录" % where)
    if not spec["upstreams"]:
        raise SpecError("%s 的 upstreams 为空：至少需要一个上游" % where)
    for name, up in spec["upstreams"].items():
        if not isinstance(up, dict) or not up.get("base"):
            raise SpecError("%s 的 upstreams.%s 缺少 base" % (where, name))
        if not str(up["base"]).startswith(("http://", "https://")):
            raise SpecError("%s 的 upstreams.%s.base 必须是 http(s) 地址：%s" % (where, name, up["base"]))
    for name, pol in (spec.get("tool_policies") or {}).items():
        if not isinstance(pol, dict):
            raise SpecError("%s 的 tool_policies.%s 必须是对象" % (where, name))
        cls = pol.get("class")
        if cls is not None and cls not in TOOL_CLASSES:
            raise SpecError("%s 的 tool_policies.%s.class 只能是 %s，实际 %r"
                            % (where, name, "/".join(TOOL_CLASSES), cls))
    for m, defs in ((spec.get("modes") or {}).get("definitions") or {}).items():
        if not isinstance(defs, dict):
            raise SpecError("%s 的 modes.definitions.%s 必须是对象" % (where, m))
        for cls in TOOL_CLASSES:
            if cls not in defs:
                raise SpecError("%s 的 modes.definitions.%s 缺 %s 动作" % (where, m, cls))
            if defs[cls] not in MODE_ACTIONS:
                raise SpecError("%s 的 modes.definitions.%s.%s 只能是 %s，实际 %r"
                                % (where, m, cls, "/".join(MODE_ACTIONS), defs[cls]))
    return True


def apply_spec_defaults(spec):
    """把可选段补齐成生效值。只补内存，不写磁盘——飞构的快照永远是文件原文。"""
    spec["modes"] = _merge(DEFAULT_MODES, spec.get("modes") or {})
    spec["limits"] = _merge(DEFAULT_LIMITS, spec.get("limits") or {})
    spec["egress"] = _merge(DEFAULT_EGRESS, spec.get("egress") or {})
    for name, pol in (spec.get("tool_policies") or {}).items():
        # 没写 class 的老配置：以 requires_confirm 反推（要人工确认的多半是写操作）
        pol.setdefault("class", "write" if pol.get("requires_confirm") else "read")
        pol.setdefault("requires_confirm", pol["class"] in ("write", "dangerous"))
        pol.setdefault("timeout_seconds", 15)
        pol.setdefault("max_calls_per_run", 2)
        pol.setdefault("roles", ["admin", "ops", "viewer"])
    return spec


def load_spec_text(text, path=None):
    """从文本加载一份配置：先解析、再校验、最后补默认值。坏配置在这里就断掉。"""
    try:
        data = json.loads(text)
    except ValueError as e:
        raise SpecError("%s 不是合法 JSON：%s" % (path or SPEC_PATH, e))
    validate_spec(data, path)
    return apply_spec_defaults(data)


SPEC = load_spec_text(io.open(SPEC_PATH, encoding="utf-8").read(), SPEC_PATH)
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

# 演示口令可用环境变量覆盖（FEINAO_PASS_ADMIN / FEINAO_PASS_OPS01 / ...），
# 免得把真口令写进 spec.json。仅对新建库生效，已有库请直接改 users 表。
for _u in list(SPEC["users"]):
    _v = os.environ.get("FEINAO_PASS_" + _u.upper())
    if _v:
        SPEC["users"][_u] = _v

# 出厂口令，只用于本地演示；对外暴露前必须替换。
DEFAULT_PASSWORDS = {"admin123", "ops123456", "view123456", "edit123456"}


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
    CREATE TABLE IF NOT EXISTS harness_rev(
        id INTEGER PRIMARY KEY AUTOINCREMENT, sha TEXT, content TEXT, note TEXT,
        by TEXT, created TEXT, active INT DEFAULT 0);
    CREATE TABLE IF NOT EXISTS skills(
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE, use_when TEXT, how TEXT,
        tools TEXT, roles TEXT DEFAULT 'admin,ops,viewer,editor',
        enabled INT DEFAULT 1, created TEXT);
    """)
    for u, p in SPEC["users"].items():
        if u == "admin":
            role = "admin"
        elif u.startswith("ops"):
            role = "ops"
        elif u.startswith("editor"):
            role = "editor"
        else:
            role = "viewer"
        conn.execute("INSERT OR IGNORE INTO users VALUES(?,?,?)", (u, p, role))
    for t, c, ddl in (("tasks", "mode", "TEXT"),):
        cols = [r[1] for r in conn.execute("PRAGMA table_info(%s)" % t)]
        if c not in cols:
            conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (t, c, ddl))
    conn.commit()
    conn.close()
    _ensure_fts()
    skill_seed()
    skills_import_from_dir(by="system")   # SKILL.md 目录幂等导入（只补缺不覆盖）
    harness_snapshot(note="启动快照")  # 每次起服记一版，内容没变则幂等跳过


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
    """spec.json tool_policies 声明的动作级策略；未声明的工具走保守默认。

    class 决定"模式闸门怎么放行"（read/write/dangerous），
    requires_confirm 决定"要不要人工审批"，timeout_seconds 决定单次调用等多久。
    """
    pol = (SPEC.get("tool_policies") or {}).get(name) or {}
    cls = pol.get("class") or ("write" if pol.get("requires_confirm") else "read")
    if cls not in TOOL_CLASSES:
        cls = "write"  # 写不清类别时按最保守的处理
    return {
        "max_calls_per_run": int(pol.get("max_calls_per_run", 2)),
        "roles": pol.get("roles") or ["admin", "ops", "viewer"],
        "requires_confirm": bool(pol.get("requires_confirm", cls != "read")),
        "class": cls,
        "timeout_seconds": int(pol.get("timeout_seconds", 15)),
    }


def modes_policy():
    m = SPEC.get("modes") or {}
    return {"default": m.get("default") or "default",
            "definitions": m.get("definitions") or _copy(DEFAULT_MODES["definitions"]),
            "yolo_allowed_roles": m.get("yolo_allowed_roles") or []}


def mode_action(cls, mode):
    """会话模式 × 工具等级 → 动作：allow / confirm / suspend / deny。

    未知模式或未知组合一律按最保守的 confirm 处理：宁可多问人，不可少问。"""
    return ((modes_policy()["definitions"].get(mode) or {}).get(cls)) or "confirm"


def limits_policy():
    l = SPEC.get("limits") or {}
    return {"max_steps": int(l.get("max_steps", DEFAULT_LIMITS["max_steps"])),
            "max_wall_clock_seconds": float(l.get("max_wall_clock_seconds",
                                                  DEFAULT_LIMITS["max_wall_clock_seconds"])),
            "same_call_repeat_limit": int(l.get("same_call_repeat_limit",
                                                DEFAULT_LIMITS["same_call_repeat_limit"])),
            "malformed_json_limit": int(l.get("malformed_json_limit",
                                              DEFAULT_LIMITS["malformed_json_limit"]))}


def egress_policy():
    e = SPEC.get("egress") or {}
    return {"enabled": bool(e.get("enabled", False)),
            "allow_schemes": e.get("allow_schemes") or ["http", "https"],
            "allow_domains": e.get("allow_domains") or [],
            "allow_ports": [int(p) for p in (e.get("allow_ports") or [80, 443])],
            "trust_upstream_origins": bool(e.get("trust_upstream_origins", True)),
            "deny_private_networks": bool(e.get("deny_private_networks", True)),
            "deny_metadata_endpoints": bool(e.get("deny_metadata_endpoints", True)),
            "max_response_bytes": int(e.get("max_response_bytes", 5242880)),
            "request_timeout_seconds": int(e.get("request_timeout_seconds", 15))}


# ---------------- 出站管控（egress）：策略 + 唯一出口 ----------------
#
# 设计原则：策略在配置里，判断在代码里，出口只有一个。
# 任何绕过 egress_open 直接 urllib.request.urlopen 的调用都算 bug——
# 策略登记了却不接线，等于没策略。

# 云元数据服务：SSRF 的经典跳板。无论白名单怎么写都必须拒。
METADATA_HOSTS = {"169.254.169.254", "100.100.100.200", "100.100.100.100",
                  "metadata.google.internal", "metadata.tencentyun.com",
                  "fd00:ec2::254", "instance-data"}


class EgressDenied(RuntimeError):
    """出站请求被策略拒绝。带原因，便于审计与排障。"""


def _origin(url):
    p = urllib.parse.urlsplit(url)
    port = p.port or (443 if (p.scheme or "").lower() == "https" else 80)
    return ((p.scheme or "").lower(), (p.hostname or "").lower(), int(port))


def _trusted_origins():
    """spec 里显式声明的上游与供应商 origin 视为可信。

    可信来自「配置写下的地址」，不来自代码默认值：本机回环的上游因此不会被私网策略误杀，
    而任意一个没写进 spec 的内网地址仍然进不来。"""
    out = set()
    for name, up in (SPEC.get("upstreams") or {}).items():
        for k in ("base", "demo_base"):
            if up.get(k):
                try:
                    out.add(_origin(up[k]))
                except Exception:
                    pass
        over = os.environ.get("FEINAO_UPSTREAM_" + name.upper())
        if over:
            try:
                out.add(_origin(over))
            except Exception:
                pass
    for p in SPEC.get("providers") or []:
        if p.get("base"):
            try:
                out.add(_origin(p["base"]))
            except Exception:
                pass
    return out


def _host_allowed(host, allow_domains):
    """域名白名单：支持 example.com 与 *.example.com 两种写法（都按后缀匹配）。"""
    for d in allow_domains:
        d = str(d).strip().lower()
        if d.startswith("*."):
            d = d[2:]
        d = d.lstrip(".")
        if not d:
            continue
        if host == d or host.endswith("." + d):
            return True
    return False


def _ip_blocked(ip):
    a = ipaddress.ip_address(ip)
    return bool(a.is_private or a.is_loopback or a.is_link_local or a.is_reserved
                or a.is_multicast or a.is_unspecified)


def check_egress(url, purpose="", user=None, resolve=True):
    """出站闸门：返回 (ok, reason)。

    `enabled=false` 时一律放行，行为与改造前完全一致（老部署升级不炸）。
    判定顺序：协议 → 元数据端点 → 可信 origin → 端口 → 域名白名单 → 私网解析。"""
    pol = egress_policy()
    if not pol["enabled"]:
        return True, ""
    try:
        p = urllib.parse.urlsplit(url)
    except Exception:
        return False, "URL 无法解析：%s" % str(url)[:120]
    scheme = (p.scheme or "").lower()
    if scheme not in [s.lower() for s in pol["allow_schemes"]]:
        return False, "协议 %s 不在允许列表 %s" % (scheme or "(空)", pol["allow_schemes"])
    host = (p.hostname or "").lower()
    if not host:
        return False, "URL 缺少主机名"
    if pol["deny_metadata_endpoints"] and host in METADATA_HOSTS:
        return False, "目标是云元数据端点 %s（SSRF 红线）" % host
    try:
        port = int(p.port or (443 if scheme == "https" else 80))
    except ValueError:
        return False, "端口非法"
    if pol["trust_upstream_origins"] and (scheme, host, port) in _trusted_origins():
        return True, "spec 声明的上游/供应商"
    if port not in pol["allow_ports"]:
        return False, "端口 %d 不在允许列表 %s" % (port, pol["allow_ports"])
    if pol["allow_domains"] and not _host_allowed(host, pol["allow_domains"]):
        return False, "域名 %s 不在白名单" % host
    if pol["deny_private_networks"]:
        literal = None
        try:
            ipaddress.ip_address(host)
            literal = host
        except ValueError:
            literal = None
        if literal:
            if _ip_blocked(literal):
                return False, "目标是内网/保留地址 %s" % literal
        elif resolve:
            try:
                infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
            except Exception as e:
                return False, "域名解析失败（%s）：%s" % (host, str(e)[:80])
            for info in infos:
                ip = info[4][0]
                if _ip_blocked(ip):
                    return False, "解析到内网/保留地址 %s（%s）" % (ip, host)
    return True, ""


def _safe_url(url):
    """日志里不留 query（可能带令牌）与账号密码；端口只在非默认时保留。"""
    try:
        p = urllib.parse.urlsplit(url)
        netloc = p.hostname or ""
        try:
            port = p.port
        except ValueError:
            port = None
        if port and not ((p.scheme == "http" and port == 80)
                         or (p.scheme == "https" and port == 443)):
            netloc += ":%d" % port
        path = p.path if len(p.path) <= 120 else p.path[:117] + "..."
        return "%s://%s%s" % (p.scheme, netloc, path)
    except Exception:
        return str(url)[:120]


def egress_open(req, timeout=None, purpose="", user=None):
    """所有出站 HTTP 的唯一入口：先过策略、再限时。返回响应对象（记得用 read_capped 读）。"""
    pol = egress_policy()
    ok, why = check_egress(req.full_url, purpose, user)
    if not ok:
        audit_log(user or "system", "egress_denied",
                  {"url": _safe_url(req.full_url), "purpose": purpose}, why, False)
        raise EgressDenied("出站被拒：%s（%s）" % (why, _safe_url(req.full_url)))
    tmo = float(timeout or pol["request_timeout_seconds"])
    if pol["enabled"]:
        tmo = min(tmo, float(pol["request_timeout_seconds"]))
    return urllib.request.urlopen(req, timeout=tmo)


def read_capped(r, limit=None):
    """读取响应体并限长：多读一个字节即判定超限，绝不把超大响应读进内存。"""
    pol = egress_policy()
    if not pol["enabled"]:
        return r.read()
    cap = int(limit or pol["max_response_bytes"])
    data = r.read(cap + 1)
    if len(data) > cap:
        raise EgressDenied("响应体超过上限 %d 字节" % cap)
    return data


def egress_selftest():
    """排障用：用策略自身跑几条代表性判定，返回结论而不是让人猜。"""
    pol = egress_policy()
    cases = [
        ("https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions", True),
        ("http://169.254.169.254/latest/meta-data/", False),
        ("http://127.0.0.1:8050/kms/api/spec", None),   # 取决于是否写进 spec / 是否禁私网
        ("file:///etc/passwd", False),
    ]
    out = []
    for url, _ in cases:
        ok, why = check_egress(url, "selftest", None, resolve=False)
        out.append({"url": _safe_url(url), "ok": ok, "reason": why or "放行"})
    return {"enabled": pol["enabled"], "allow_domains": pol["allow_domains"],
            "allow_ports": pol["allow_ports"], "cases": out}


def policy_effective():
    """对外可见的生效策略（排障用）。不含口令、Key、上游账号——只回结构与取值。"""
    tools = {}
    for name in TOOLS:
        pol = tool_policy(name)
        tools[name] = {"class": pol["class"], "roles": pol["roles"],
                       "requires_confirm": pol["requires_confirm"],
                       "max_calls_per_run": pol["max_calls_per_run"],
                       "timeout_seconds": pol["timeout_seconds"]}
    ap = approval_policy()
    return {
        "spec": {"file": os.path.basename(SPEC_PATH), "sha": spec_sha()},
        "modes": modes_policy(),
        "limits": limits_policy(),
        "egress": egress_policy(),
        "approval": {"name": ap["name"], "approvers": ap["approvers"],
                     "allow_self": ap["allow_self"], "ttl_minutes": ap["ttl_minutes"]},
        "tools": tools,
        "upstreams": {k: v.get("label") for k, v in SPEC["upstreams"].items()},
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


# ---------------- 技能注册表（P1-3：加能力不改代码） ----------------

SKILL_SEEDS = [
    ("检索知识库", "问题涉及制度、标准、流程、文档等知识性内容",
     "先调 search_docs 拿回相关文档再作答，回答里说明依据了哪些文档", ["search_docs"]),
    ("查业务数据", "问题涉及订单、库存、设备等业务系统里的数字",
     "用 query_system 指定 system 和 entity 查数，先报条数再报要点", ["query_system"]),
    ("写入知识库", "需要把结论或资料沉淀成文档",
     "调 ingest_doc（写操作，会挂审批单），标题写清主题再写正文", ["ingest_doc"]),
]


def skill_seed():
    """幂等播种：出厂技能只补缺，不覆盖人工改过的说明。"""
    conn = db()
    n = 0
    for name, use_when, how, tools in SKILL_SEEDS:
        cur = conn.execute(
            "INSERT OR IGNORE INTO skills(name,use_when,how,tools,created) VALUES(?,?,?,?,?)",
            (name, use_when, how, json.dumps(tools), _now()))
        n += cur.rowcount
    conn.commit()
    conn.close()
    return n


def skills_list(enabled_only=True):
    sql = "SELECT * FROM skills" + (" WHERE enabled=1" if enabled_only else "") + " ORDER BY id"
    conn = db()
    rows = []
    for r in conn.execute(sql):
        d = dict(r)
        try:
            d["tools"] = json.loads(d.get("tools") or "[]")
        except Exception:
            d["tools"] = []
        rows.append(d)
    conn.close()
    return rows


def skill_save(name, use_when, how, tools=None, roles=None, enabled=1, by="admin"):
    """新增或更新一条技能。工具名必须是注册表里真实存在的，否则拒绝。"""
    name = (name or "").strip()
    if not name:
        return {"ok": False, "error": "name 必填"}
    tools = [t for t in (tools or []) if t in TOOLS]
    if not tools:
        return {"ok": False,
                "error": "tools 至少要有一个已注册工具（可选：%s）" % ",".join(TOOLS)}
    conn = db()
    conn.execute(
        "INSERT INTO skills(name,use_when,how,tools,roles,enabled,created) VALUES(?,?,?,?,?,?,?) "
        "ON CONFLICT(name) DO UPDATE SET use_when=excluded.use_when, how=excluded.how, "
        "tools=excluded.tools, roles=excluded.roles, enabled=excluded.enabled",
        (name, (use_when or "")[:300], (how or "")[:600], json.dumps(tools),
         (roles or "admin,ops,viewer,editor")[:120], 1 if enabled else 0, _now()))
    conn.commit()
    conn.close()
    audit_log(by, "skill_save", {"name": name, "tools": tools}, "ok", True)
    return {"ok": True, "name": name, "tools": tools}


def skill_match(q, top=2):
    """按关键词重叠挑最相关的技能（复用 RAG 的分词），给 ReAct 的系统提示注入。"""
    kws = set(_keywords(q, 20))
    if not kws:
        return []
    scored = []
    for s in skills_list():
        text = "%s %s %s" % (s["name"], s["use_when"], s["how"])
        skws = set(_keywords(text, 40))
        hit = len(kws & skws)
        if hit:
            scored.append((hit, s))
    scored.sort(key=lambda x: -x[0])
    return [s for _, s in scored[:top]]


# ---------------- SKILL.md 目录（任务 4：技能目录化） ----------------
#
# 技能除了走 API 存库，还支持仓库里的 skills/<名>/SKILL.md 文件：
#   ---
#   name: 查销售数据
#   use_when: 需要查 BI 销售相关数据
#   tools: [query_system]
#   enabled: true
#   ---
#   正文 = 怎么用（how）
# 启动时幂等导入（只补缺不覆盖人工改动，与 skill_seed 同语义）；也可手动触发重导。

SKILLS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "skills")


def skills_scan_dir(base=None):
    """扫描 skills/ 目录下的 SKILL.md，解析 frontmatter。返回 [{name,use_when,how,tools,enabled,path}]。"""
    base = base or SKILLS_DIR
    out = []
    if not os.path.isdir(base):
        return out
    for root, dirs, files in os.walk(base):
        if "SKILL.md" not in files:
            continue
        path = os.path.join(root, "SKILL.md")
        try:
            raw = io.open(path, encoding="utf-8").read()
        except Exception:
            continue
        meta, how = {}, raw
        if raw.startswith("---"):
            parts = raw.split("---", 2)
            if len(parts) >= 3:
                for line in parts[1].splitlines():
                    if ":" not in line:
                        continue
                    k, v = line.split(":", 1)
                    meta[k.strip().lower()] = v.strip()
                how = parts[2].strip()
        name = (meta.get("name") or os.path.basename(root)).strip()
        if not name:
            continue
        tools = meta.get("tools", "")
        tools = [t.strip() for t in tools.strip("[]").split(",") if t.strip()] if tools else []
        out.append({
            "name": name,
            "use_when": meta.get("use_when", ""),
            "how": how[:600],
            "tools": tools,
            "enabled": 0 if str(meta.get("enabled", "true")).lower() in ("false", "0", "no") else 1,
            "path": os.path.relpath(path, base).replace("\\\\", "/"),
        })
    return out


def skills_import_from_dir(by="admin", base=None):
    """把 SKILL.md 目录导入注册表。只补缺（INSERT OR IGNORE），不覆盖库里已有的同名技能。
    返回 {imported: [...], skipped_invalid: [...]}——工具名不在注册表的会拒绝并说明原因。"""
    res = {"imported": [], "skipped_invalid": []}
    for s in skills_scan_dir(base):
        bad = [t for t in s["tools"] if t not in TOOLS]
        if not s["tools"] or bad:
            res["skipped_invalid"].append(
                {"name": s["name"], "reason": "工具未注册: %s" % bad if bad else "tools 为空"})
            continue
        conn = db()
        cur = conn.execute(
            "INSERT OR IGNORE INTO skills(name,use_when,how,tools,enabled,created) "
            "VALUES(?,?,?,?,?,?)",
            (s["name"], s["use_when"][:300], s["how"], json.dumps(s["tools"]),
             s["enabled"], _now()))
        conn.commit()
        conn.close()
        if cur.rowcount:
            res["imported"].append(s["name"])
            audit_log(by, "skill_import", {"name": s["name"], "path": s["path"]}, "ok", True)
    return res


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
    """超时的待审批单自动置 expired——挂着不处理也是一种决定。

    用 `created <= cutoff`：ttl_minutes=0 表示"立刻过期"，必须覆盖同一秒内
    创建的单（用严格小于时，快速机器上创建与判过期落在同一秒会漏判）。
    """
    ttl = approval_policy()["ttl_minutes"]
    cutoff = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - ttl * 60))
    conn = db()
    n = conn.execute("UPDATE approvals SET status='expired' WHERE status='pending' AND created<=?",
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
    extra = ""
    # 补丁类审批：放行即激活。编辑者只能产出修订，生效权始终在审批人手里。
    if decision == "approved" and row["tool"] == "harness.activate":
        try:
            rid = (row.get("args") or {}).get("rev_id")
            r = harness_activate(rid, by=by)
            extra = "；修订 #%s 已%s" % (rid, "激活" if r.get("ok") else "激活失败：%s" % r.get("error"))
        except Exception as e:
            extra = "；激活异常：%s" % str(e)[:120]
    return True, (("已放行" if decision == "approved" else "已拒绝") + extra), approval_get(aid)


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


# ---------------- Harness 版本化（spec.json 的快照 / 对比 / 回滚） ----------------
#
# 原则：凡是能改变 agent 行为的配置（工具策略、审批人、上游、试点参数）都必须有历史、
# 能对比、能回到上一版。没有这一层，任何"改进"都不可证伪——改坏了连退路都没有。

def _spec_text():
    with io.open(SPEC_PATH, encoding="utf-8") as f:
        return f.read()


def spec_sha(text=None):
    """配置文件指纹（前 16 位十六进制），用于判重与对外展示。"""
    t = _spec_text() if text is None else text
    return hashlib.sha256(t.encode("utf-8")).hexdigest()[:16]


def _harness_row(r, with_content=False):
    d = {"id": r["id"], "sha": r["sha"], "note": r["note"] or "", "by": r["by"] or "",
         "created": r["created"], "active": bool(r["active"]),
         "bytes": len(r["content"] or "")}
    if with_content:
        d["content"] = r["content"] or ""
    return d


def harness_snapshot(note="", by="system"):
    """把当前 spec.json 落成一条修订。内容与历史某版一致时幂等返回（created=False）。"""
    text = _spec_text()
    sha = spec_sha(text)
    conn = db()
    row = conn.execute("SELECT * FROM harness_rev WHERE sha=?", (sha,)).fetchone()
    if row:
        conn.close()
        return False, _harness_row(row)
    now = _now()
    conn.execute("UPDATE harness_rev SET active=0")
    cur = conn.execute(
        "INSERT INTO harness_rev(sha,content,note,by,created,active) VALUES(?,?,?,?,?,1)",
        (sha, text, note or "", by, now))
    rid = cur.lastrowid
    conn.commit()
    row = conn.execute("SELECT * FROM harness_rev WHERE id=?", (rid,)).fetchone()
    conn.close()
    return True, _harness_row(row)


def harness_list(limit=20):
    conn = db()
    rows = [dict(r) for r in conn.execute(
        "SELECT id,sha,note,by,created,active,length(content) bytes FROM harness_rev "
        "ORDER BY id DESC LIMIT ?", (limit,))]
    act = conn.execute("SELECT id FROM harness_rev WHERE active=1 LIMIT 1").fetchone()
    conn.close()
    for r in rows:
        r["active"] = bool(r["active"])
    return {"rows": rows, "active": (act["id"] if act else None), "sha": spec_sha(),
            "note": "sha 为当前磁盘文件指纹；active 为正在生效的修订"}


def harness_get(rid):
    conn = db()
    row = conn.execute("SELECT * FROM harness_rev WHERE id=?", (rid,)).fetchone()
    conn.close()
    return _harness_row(row, with_content=True) if row else None


def _spec_flat(o, prefix=""):
    """把嵌套配置拍平成 {a.b[0].c: 值}，用于语义对比（格式变化不算改动）。"""
    out = {}
    if isinstance(o, dict):
        for k, v in o.items():
            out.update(_spec_flat(v, ("%s.%s" % (prefix, k)) if prefix else str(k)))
    elif isinstance(o, list):
        for i, v in enumerate(o):
            out.update(_spec_flat(v, "%s[%d]" % (prefix, i)))
    else:
        out[prefix] = o
    return out


def _spec_flat_text(text):
    """容错版：历史修订可能是坏 JSON（早期人工塞进来的），对比时不能因此报错。"""
    t = (text or "").strip()
    if not t:
        return {}
    try:
        return _spec_flat(json.loads(t))
    except Exception:
        return {"<无法解析的内容>": t[:200]}


def harness_diff(a=None, b=None):
    """两版之间的差异（a → b）。语义对比为主，原始行 diff 为辅。

    只比 JSON 语义：把配置用别的缩进重写一遍不算改动，否则 diff 全是噪音。
    """
    conn = db()
    if not a or not b:
        ids = [r["id"] for r in conn.execute("SELECT id FROM harness_rev ORDER BY id DESC LIMIT 2")]
        if len(ids) < 2:
            conn.close()
            return {"error": "至少需要两条修订才能对比"}
        b, a = ids[0], ids[1]
    a, b = int(a), int(b)
    ra = conn.execute("SELECT * FROM harness_rev WHERE id=?", (a,)).fetchone()
    rb = conn.execute("SELECT * FROM harness_rev WHERE id=?", (b,)).fetchone()
    conn.close()
    if not ra or not rb:
        return {"error": "修订不存在"}
    fa = _spec_flat_text(ra["content"])
    fb = _spec_flat_text(rb["content"])
    changes = []
    for k in sorted(set(fa) | set(fb)):
        if fa.get(k) != fb.get(k) or (k in fa) != (k in fb):
            changes.append({"path": k,
                            "from": fa.get(k),
                            "to": fb.get(k),
                            "kind": "added" if k not in fa else ("removed" if k not in fb else "changed")})
    diff = list(difflib.unified_diff((ra["content"] or "").splitlines(),
                                    (rb["content"] or "").splitlines(),
                                    fromfile="#%d" % a, tofile="#%d" % b, lineterm="", n=2))
    return {"from": a, "to": b, "from_sha": ra["sha"], "to_sha": rb["sha"],
            "changed_lines": len(changes), "changes": changes[:200],
            "raw": diff[:200]}


def reload_spec(text=None):
    """热加载 spec.json。原地更新 dict，让 engine.SPEC 等既有引用同步生效。

    先校验再替换：坏配置不许进内存（飞构回滚也一样走这里）。
    """
    new = load_spec_text(text if text is not None else _spec_text(), SPEC_PATH)
    for u in list(new.get("users") or {}):
        v = os.environ.get("FEINAO_PASS_" + u.upper())
        if v:
            new["users"][u] = v
    SPEC.clear()
    SPEC.update(new)
    _tok_cache.clear()  # 上游地址/口令可能变了，旧 token 不能再用
    return SPEC


def harness_activate(rid, by="admin"):
    """把某条修订写回 spec.json 并热加载。临时文件 + 原子替换，失败不留半截配置。"""
    rid = int(rid)
    conn = db()
    row = conn.execute("SELECT * FROM harness_rev WHERE id=?", (rid,)).fetchone()
    conn.close()
    if not row:
        return {"ok": False, "error": "修订 #%d 不存在" % rid}
    text = row["content"] or ""
    try:
        load_spec_text(text, "修订 #%d" % rid)  # 解析 + 结构校验：坏配置不许上线
    except SpecError as e:
        return {"ok": False, "error": str(e)[:200]}
    tmp = SPEC_PATH + ".tmp"
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, SPEC_PATH)
    reload_spec(text)
    conn = db()
    conn.execute("UPDATE harness_rev SET active=0")
    conn.execute("UPDATE harness_rev SET active=1 WHERE id=?", (rid,))
    conn.commit()
    conn.close()
    audit_log(by, "harness_activate", {"id": rid, "sha": row["sha"]}, "ok", True)
    return {"ok": True, "active": rid, "sha": row["sha"],
            "note": "已激活修订 #%d（sha %s），内存配置已热加载" % (rid, row["sha"])}


# 编辑者补丁不可触碰的键：这些是安全边界本身，边界不能自己改自己
PATCH_FORBIDDEN_PREFIXES = ("tool_policies", "approval", "modes", "limits",
                            "egress", "users", "providers", "upstreams")


def harness_patch(patch, note="", by="editor"):
    """编辑者提交配置补丁：只落一条 active=0 的修订 + 一张审批单，不改生效配置。

    审批人放行（approvals/decide approved）时才会真正激活；拒绝则修订留在历史里可查不可用。
    安全面（tool_policies/approval/modes/limits/egress/账号/上游）不在可补丁范围——
    "guardrail 写在可编辑文件里不算安全边界"，那我们就让 guardrail 不可被补丁编辑。"""
    role = user_role(by)
    if role not in ("editor", "admin"):
        return {"ok": False, "error": "角色 %s 无权提交补丁（限 editor/admin）" % role}
    if not isinstance(patch, list) or not patch:
        return {"ok": False, "error": "patch 必须是非空数组 [{path, value}]"}
    for p in patch:
        path = str(p.get("path") or "")
        if not path:
            return {"ok": False, "error": "补丁缺 path"}
        if any(path == pre or path.startswith(pre + ".") for pre in PATCH_FORBIDDEN_PREFIXES):
            return {"ok": False, "error": "%s 属于安全边界，不可被补丁修改（可改：pilots 等）" % path}
    import copy
    cand = copy.deepcopy(dict(SPEC))
    for p in patch:
        node = cand
        keys = str(p["path"]).split(".")
        for k in keys[:-1]:
            if not isinstance(node, dict) or k not in node:
                return {"ok": False, "error": "路径不存在：%s" % p["path"]}
            node = node[k]
        if not isinstance(node, dict) or keys[-1] not in node:
            return {"ok": False, "error": "路径不存在：%s" % p["path"]}
        node[keys[-1]] = p["value"]
    try:
        text = json.dumps(cand, ensure_ascii=False, indent=2) + "\n"
        load_spec_text(text, "补丁预检")  # 只验不装：坏配置在提交时就打回
    except SpecError as e:
        return {"ok": False, "error": "补丁会产生非法配置：%s" % str(e)[:160]}
    conn = db()
    sha = spec_sha(text)
    row = conn.execute("SELECT * FROM harness_rev WHERE sha=?", (sha,)).fetchone()
    if row and row["active"]:
        conn.close()
        return {"ok": False, "error": "补丁与当前生效配置相同，无需提交"}
    conn.execute("UPDATE harness_rev SET active=0 WHERE sha=?", (sha,))
    cur = conn.execute(
        "INSERT INTO harness_rev(sha,content,note,by,created,active) VALUES(?,?,?,?,?,0)",
        (sha, text, "[补丁] %s" % (note or "无备注"), by, _now()))
    rid = cur.lastrowid
    acur = conn.execute(
        "INSERT INTO approvals(task_id,step_idx,user,tool,args,reason,status,consumed,created) "
        "VALUES(NULL,0,?, 'harness.activate', ?, ?, 'pending', 0, ?)",
        (by, json.dumps({"rev_id": rid}),
         "编辑者补丁待审：%s（改动 %s）" % ((note or "无备注")[:160],
                                           ",".join(str(p.get("path")) for p in patch))[:300], _now()))
    aid = acur.lastrowid
    conn.commit()
    conn.close()
    audit_log(by, "harness_patch", {"rev": rid, "paths": [p.get("path") for p in patch]},
              "待审批 #%d" % aid, True)
    return {"ok": True, "rev_id": rid, "approval_id": aid, "sha": sha,
            "note": "补丁已落为修订 #%d（未生效），审批单 #%d 已挂出；放行后才会激活" % (rid, aid)}


# ---------------- LLM 网关（多供应商 + combo 降级链） ----------------

def _call_openai(p, messages, timeout=30, user="system"):
    key = os.environ.get(p.get("key_env", ""), "")
    if not key:
        raise RuntimeError("no key for " + p["name"])
    body = json.dumps({"model": p["model"], "messages": messages, "temperature": 0.4}).encode()
    req = urllib.request.Request(p["base"], data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Bearer " + key)
    t0 = time.time()
    with egress_open(req, timeout=timeout, purpose="llm:%s" % p["name"], user=user) as r:
        data = json.loads(read_capped(r).decode())
    lat = int((time.time() - t0) * 1000)
    text = data["choices"][0]["message"]["content"]
    u = data.get("usage", {})
    _usage(p["name"], p["model"], True, lat, u.get("prompt_tokens", 0), u.get("completion_tokens", 0))
    return text


def _mock_react_plan(goal):
    """把任务关键词映射成确定的动作序列（只给 mock 用，真模型不需要）。

    没有 Key 的环境也得能把 ReAct 真正跑起来：调工具、过权限预算、走审批闸门。
    否则基准任务集在 demo 模式下永远是 0 步 0 调用，双跑对照看不出任何差别。
    """
    g = goal or ""
    plan = []
    if any(k in g for k in ("写入", "入库", "录入", "存到知识库", "记到知识库")):
        title = re.sub(r"[，。,.；;]", " ", g).strip()[:20] or "mock 文档"
        plan.append(("ingest_doc", {"title": title, "body": g[:400]}))
    ent = ""
    if "订单" in g:
        ent = "order"
    elif "客户" in g:
        ent = "customer"
    elif "工单" in g:
        ent = "ticket"
    elif "文章" in g:
        ent = "article"
    elif "数据源" in g:
        ent = "sources"
    elif "指标" in g:
        ent = "metrics"
    if ent:
        system = "cc" if ent == "ticket" else ("kms" if ent == "article" else "bi")
        plan.append(("query_system", {"system": system, "entity": ent, "q": ""}))
    if any(k in g for k in ("知识库", "文档", "规定", "制度", "标准", "政策")):
        kws = [k for k in _keywords(g, 8) if len(k) >= 2][:2]
        plan.append(("search_docs", {"q": " ".join(kws) or g[:20]}))
    return plan


def _call_mock(messages, **kw):
    sys_txt = "\n".join(m.get("content", "") for m in messages if m.get("role") == "system")
    user_txt = "\n".join(m.get("content", "") for m in messages if m.get("role") == "user")
    if "[PILOT:react]" in sys_txt:
        # 会调工具的 mock：按任务关键词给出确定性动作序列，执行过的动作不再重复。
        hit = re.search(r"任务：(.*?)\n已执行：", user_txt, re.S)
        goal = (hit.group(1) if hit else user_txt).strip()
        plan = _mock_react_plan(goal)
        done = re.findall(r'"tool":\s*"([a-z_]+)"', user_txt)
        for tool, args in plan:
            if tool not in done:
                return json.dumps({"tool": tool, "args": args}, ensure_ascii=False)
        used = "、".join(sorted(set(done))) or "直接回答"
        return "（飞脑·mock）已完成：%s。依据：%s。" % (goal[:50], used)

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
            # 去掉内部标记（[#3 user] 这类），复述内容而不是复述记账格式
            top = [re.sub(r"^\[[^\]]*\]\s*", "", t) for sc, t in scored[:2]]
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
        # 没有真 Key 时也必须是"可核对"的回答：说清依据了几条、依据哪几篇，
        # 命中不到就直说不知道 —— mock 也不能编。
        titles = re.findall(r"\[DOC\d+\]\s*([^\n]+)", sys_txt)
        if not titles:
            return "（知识库未命中相关文档，不做推测回答；请先入库或换个问法）"
        body = sys_txt.split("\n", 1)[1] if "\n" in sys_txt else sys_txt
        body = re.sub(r"\[DOC\d+\][^\n]*", " ", body)   # 去掉标题行，只留正文
        snippet = " ".join(body.split())[:150]
        return "依据 %d 条文档回答：%s。原文要点：%s ……（mock 摘要，填入 LLM Key 后由模型生成）" % (
            len(titles), "、".join("《%s》" % t.strip() for t in titles), snippet)
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
            out = _call_openai(p, messages, user=user)
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


def demo_enabled():
    """内置假上游开关：FEINAO_DEMO 优先，其次 spec.demo.default。

    开源版默认开启，保证 clone 后不改任何配置就能跑通三个试点；
    生产部署在 .env 里写 FEINAO_DEMO=0 即走 spec.json 里真实的 base。
    """
    v = os.environ.get("FEINAO_DEMO")
    if v is not None:
        return v.strip().lower() not in ("0", "false", "no", "off", "")
    return bool((SPEC.get("demo") or {}).get("default"))


def _upstream_base(name):
    """上游基地址：FEINAO_UPSTREAM_<NAME> 环境变量 > demo_base（演示模式）> base。"""
    over = os.environ.get("FEINAO_UPSTREAM_" + name.upper())
    if over:
        return over if over.endswith("/") else over + "/"
    up = SPEC["upstreams"][name]
    if demo_enabled() and up.get("demo_base"):
        return up["demo_base"]
    return up["base"]


def _upstream_login(base, up, tmo, user=None):
    """拿上游 token。飞系各系统的登录路径不统一：新版走 auth/login，
    老版/演示桩走 api/login——两个都试，用错误报文区分「路径不对」和「密码不对」：
    路径不对（掉进鉴权中间件）报「未登录或令牌无效」，密码错报「账号或密码错误」。"""
    body = json.dumps({"username": up["user"], "password": up["pass"]}).encode()
    errs = []
    for path in ("api/auth/login", "api/login"):
        req = urllib.request.Request(base + path, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        try:
            with egress_open(req, timeout=min(10, tmo),
                             purpose="upstream-login", user=user) as r:
                d = json.loads(read_capped(r).decode())
            # 飞系真实系统把业务数据包在 {"code":..,"data":{..}} 里；演示桩是平的。两种都兼容。
            if isinstance(d, dict) and isinstance(d.get("data"), (dict, list)):
                d = d["data"]
            return d["token"]
        except EgressDenied:
            raise  # 出站被策略拒绝不是「登录失败」，别伪装成密码问题把人带偏
        except urllib.error.HTTPError as e:
            try:
                msg = (json.loads(e.read().decode()) or {}).get("message", "")
            except Exception:
                msg = ""
            errs.append("%s→%s" % (path, msg or ("HTTP %s" % e.code)))
            if "密码" in msg:
                break  # 路径对了但密码不对，换路径也没用，直接把话说明白
        except Exception as e:
            errs.append("%s→%s" % (path, str(e)[:80]))
    raise RuntimeError("上游登录失败（%s）" % "; ".join(errs))


def _upstream_get(name, path, user=None):
    up = SPEC["upstreams"][name]
    base = _upstream_base(name)
    tmo = tool_policy("query_system")["timeout_seconds"]  # 超时也在配置里，不在代码里
    tok = _tok_cache.get(name)
    if not tok:
        tok = _upstream_login(base, up, tmo, user)
        _tok_cache[name] = tok
    req = urllib.request.Request(base + path)
    req.add_header("Authorization", "Bearer " + tok)
    try:
        with egress_open(req, timeout=tmo, purpose="upstream:%s" % name,
                         user=user) as r:
            d = json.loads(read_capped(r).decode())
        if isinstance(d, dict) and isinstance(d.get("data"), (dict, list)):
            d = d["data"]  # 飞系真实系统的响应包装；演示桩没有这层
        return d
    except urllib.error.HTTPError as e:
        if e.code == 401:
            _tok_cache.pop(name, None)
        raise


def upstream_spec(name, user=None):
    return _upstream_get(name, "api/spec", user)


# 实体名来自模型生成的参数，拼进 URL 路径前必须验形：`../../admin/delete` 这类值
# 不该靠上游去挡。同源不同路径同样是越权，egress 只管域名管不到这里。
_ENTITY_RE = re.compile(r"^[A-Za-z0-9_\-]{1,40}$")


def upstream_rows(name, entity, q="", limit=20, user=None):
    ent = str(entity or "").strip()
    if not _ENTITY_RE.match(ent):
        raise ValueError("非法实体名 %r：只允许字母/数字/下划线/短横，最长 40" % ent[:40])
    try:
        n = max(1, min(int(limit), 200))
    except (TypeError, ValueError):
        n = 20
    path = "api/%s?limit=%d" % (ent, n)
    if q:
        path += "&q=" + urllib.request.quote(str(q)[:200])
    d = _upstream_get(name, path, user)
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
        spec = upstream_spec(system, user)
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
        rows = upstream_rows(system, ent_key, intent.get("q", ""), intent.get("limit", 20), user)
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
    eid = evidence_log(user, "nl2api", None, q, summary, provider,
                       sources=[{"system": system, "entity": ent_key, "rows": len(rows)}])
    result["evidence_id"] = eid
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
    refs = [h["title"] for h in hits]
    eid = evidence_log(user, "script", None, scenario, ans, provider,
                       sources=[{"title": t} for t in refs])
    audit_log(user, "pilot_script", {"scenario": scenario, "tone": tone}, ans[:200], True)
    return {"script": ans, "provider": provider, "refs": refs, "evidence_id": eid}


# ---------------- L2：轻量 ReAct（工具注册表） ----------------

TOOLS = {
    "search_docs": {"desc": "搜索飞系知识库文档", "args": {"q": "str"},
                    "fn": lambda a: rag_search(a.get("q", ""))},
    "query_system": {"desc": "查询飞系系统数据。system 取 kms/bi/cc；entity 用系统实体 key（如 bi 的 sources/metrics/dashboards/reports、kms 的 articles、cc 的 tickets）。"
                     "示例：查 BI 数据源 → {\"tool\":\"query_system\",\"args\":{\"system\":\"bi\",\"entity\":\"sources\",\"q\":\"\"}}", "args": {"system": "str", "entity": "str", "q": "str"},
                     "fn": lambda a: upstream_rows(a["system"], a["entity"], a.get("q", ""), 10)},
    # 写操作：会真的落库，所以 requires_confirm=true，必须人工放行（审批工作台）
    "ingest_doc": {"desc": "把一段内容写入飞脑知识库（写操作，需人工审批）", "args": {"title": "str", "body": "str"},
                   "fn": lambda a: {"doc_id": rag_ingest((a.get("title") or "未命名")[:120],
                                                        (a.get("body") or "")[:4000], "agent")}},
}


# ---------------- State 层：任务进度落库（Harness 第 2 层） ----------------

def task_create(user, goal, mode=None):
    conn = db()
    cur = conn.execute("INSERT INTO tasks(user,goal,status,created,updated,mode) VALUES(?,?, 'active',?,?,?)",
                       (user, goal[:300], time.strftime("%Y-%m-%dT%H:%M:%S"),
                        time.strftime("%Y-%m-%dT%H:%M:%S"), mode))
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


def _react_system_prompt(trace, q=""):
    skills = skill_match(q) if q else []
    skill_txt = ""
    if skills:
        skill_txt = " 相关技能：%s。" % "; ".join(
            "%s（%s：用 %s）" % (s["name"], s["use_when"], "/".join(s["tools"])) for s in skills)
    return ("你是飞脑Agent。可用工具：%s。如需工具，输出JSON：{\"tool\":\"名称\",\"args\":{...}}；"
            "若可回答，直接输出答案并遵守：%s [PILOT:react]"
            % (json.dumps({k: v["desc"] for k, v in TOOLS.items()}, ensure_ascii=False),
               VERIFY_TAIL + skill_txt))


def _abort_text(reason, d, task_id):
    """中断原因翻译成人话——用户看到的是能接的下一步，不是堆栈或错误码。"""
    if reason == "wall_clock_timeout":
        return ("这个任务已经跑了 %s 秒，超过 %s 秒上限，我先停下来。" % (d.get("elapsed"), d.get("limit")) +
                "已完成的步骤都记在任务 #%d 里，可以缩小范围重跑，或直接看已有结果。" % task_id)
    if reason == "repeated_identical_call":
        return ("连续 %d 次调用 %s 且参数完全相同，已停止。可能是参数不对或数据不存在——"
                "要不要换个查法？" % (int(d.get("count", 0)), d.get("tool") or "同一工具"))
    return "任务已中止（%s）。" % reason


def _abort_run(task_id, user, task, reason, detail, trace, provider, idx):
    """卡死中断：落 evidence(outcome=aborted) + 人话返回。不抛异常，不留半截任务。"""
    entry = {"aborted": reason}
    entry.update(detail)
    trace.append(entry)
    task_step(task_id, idx, "aborted", json.dumps(entry, ensure_ascii=False))
    task_finish(task_id, "aborted")
    msg = _abort_text(reason, detail, task_id)
    eid = evidence_log(user, "react", None, task, msg, provider,
                       sources=[s.get("tool") for s in trace if isinstance(s, dict) and s.get("tool")],
                       trace=trace,
                       context_tokens=sum(len(json.dumps(s, ensure_ascii=False)) for s in trace))
    audit_log(user, "react_abort", {"task": task, "task_id": task_id, "reason": reason},
              json.dumps(detail, ensure_ascii=False)[:200], False)
    return {"answer": msg + "（中断原因与现场已记录，证据 #%d）" % eid, "status": "aborted",
            "reason": reason, "task_id": task_id, "steps": trace,
            "provider": provider, "evidence_id": eid}


def react_run(task, user="admin", task_id=None, max_steps=None, mode=None):
    """ReAct 多步任务：每一步落 task_steps（State 层），工具调用过动作级权限/预算
    （Permission 层），结束后把证据落 evidence 表（Verification 层）。

    task_id 传入且该任务属于此用户且状态 active 时，从上次的步骤继续跑（断点续跑）。
    三条卡死上限来自 spec.limits：墙钟、三连相同调用、连续畸形输出——只增加判断，不改循环结构。
    mode 是会话模式（plan/default/auto/yolo），决定写/危险动作是放行、挂审还是拒绝；
    不传则取任务上记录的模式，再不济取 spec.modes.default。"""
    lim = limits_policy()
    mp = modes_policy()
    mode = (mode or "").strip() or None
    if mode and mode not in mp["definitions"]:
        return {"error": "未知模式 %s（可选：%s）" % (mode, "/".join(mp["definitions"]))}
    # spec.limits.max_steps 是硬上限，pilots.react_max_steps 是常规步数：取小的那个
    max_steps = min(int(max_steps or SPEC["pilots"]["react_max_steps"]), lim["max_steps"])
    trace = []
    used_counts = {}
    task_start = time.time()
    last_sig, same_count, malformed_count = None, 0, 0
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
        mode = mode or t.get("mode") or mp["default"]
    else:
        mode = mode or mp["default"]
        idx = 0
        task_id = task_create(user, task, mode=mode)
    if mode == "yolo" and user_role(user) not in (mp["yolo_allowed_roles"] or []):
        return {"error": "模式 yolo 仅限 %s 使用（当前角色无权）"
                         % (",".join(mp["yolo_allowed_roles"]) or "指定角色")}

    provider = "-"
    answer = None
    for step in range(max_steps):
        spent = time.time() - task_start
        if spent > lim["max_wall_clock_seconds"]:
            # 卡在慢接口上半小时，和步数超了不是一回事
            return _abort_run(task_id, user, task, "wall_clock_timeout",
                              {"elapsed": round(spent, 1), "limit": lim["max_wall_clock_seconds"]},
                              trace, provider, idx)
        raw, provider = llm_chat([
            {"role": "system", "content": _react_system_prompt(trace, task)},
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
        except Exception as e:
            malformed_count += 1
            if malformed_count >= lim["malformed_json_limit"]:
                # 连着吐坏格式，再问也没用：降级成纯文本，把模型原话交给用户
                answer = raw + "\n\n（模型连续 %d 次输出的调用格式无法解析，已降级为纯文本回答）" % malformed_count
                entry = {"degraded": "malformed_json", "count": malformed_count}
                trace.append(entry)
                task_step(task_id, idx, "degraded", json.dumps(entry, ensure_ascii=False))
                idx += 1
                break
            entry = {"error": "unparsable_tool_call", "count": malformed_count, "detail": str(e)[:120]}
            trace.append(entry)
            task_step(task_id, idx, "error", json.dumps(entry, ensure_ascii=False))
            idx += 1
            continue
        malformed_count = 0
        try:
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
            # 模式决定写/危险动作走哪条路：allow 直接执行，deny 拒绝，confirm/suspend 挂审批。
            # 但人工已经拒过的动作在哪个模式都保持拒绝——人的"不"不因模式被绕过。
            if pol["class"] in ("write", "dangerous"):
                rej = _rejected_before(task_id, call["tool"], call.get("args", {}))
                if rej:
                    entry = {"tool": call["tool"], "args": call.get("args", {}),
                             "denied": "该动作已被审批人拒绝（审批单 #%d）：%s；请勿重复提交"
                                       % (rej["id"], rej.get("note") or "无理由")}
                    trace.append(entry)
                    task_step(task_id, idx, "denied", json.dumps(entry, ensure_ascii=False))
                    idx += 1
                    continue
            act = mode_action(pol["class"], mode)
            if act == "deny":
                entry = {"tool": call["tool"], "args": call.get("args", {}),
                         "denied": "会话模式 %s 下不允许 %s 类动作" % (mode, pol["class"])}
                trace.append(entry)
                task_step(task_id, idx, "denied", json.dumps(entry, ensure_ascii=False))
                idx += 1
                continue
            if act in ("confirm", "suspend"):
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
            # 同一动作同一参数反复来，就是卡住了。放在权限/预算之后判断：
            # 被拒绝或被预算挡住的动作不算"重复执行"，否则会把正常的拒绝路径误判成卡死。
            sig = (call.get("tool"), json.dumps(call.get("args", {}), sort_keys=True, ensure_ascii=False))
            same_count = same_count + 1 if sig == last_sig else 1
            last_sig = sig
            if same_count >= lim["same_call_repeat_limit"]:
                return _abort_run(task_id, user, task, "repeated_identical_call",
                                  {"tool": call.get("tool"), "count": same_count},
                                  trace, provider, idx)
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
