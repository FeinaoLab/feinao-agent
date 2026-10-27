#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地测试环境（8081）——跑验收不碰生产。

为什么要有这个东西
------------------
验收必须能反复跑、能随便跑坏。所以它得有两个硬约束：
  1. 配置必须是**副本**（FEINAO_SPEC 指向 feinao-test/spec.test.json），
     改坏了就删掉重建，绝不会把生产 spec.json 写脏；
  2. 数据库必须是**副本**（FEINAO_DB 指向 feinao-test/feinao-test.db），
     数据已脱敏，随便灌随便删。

用法
----
  python tools/testenv.py --preflight          # 体检：端口能不能通、配置归谁、依赖齐不齐
  python tools/testenv.py --prep               # 建目录 + 生成脱敏库副本（幂等）
  python tools/testenv.py --prep --from <库>   # 从指定库复制并脱敏（库里没有内容时用模板生成）
  python tools/testenv.py --serve              # 在 8081 起服（自动带上面两个环境变量）
  python tools/testenv.py --check              # 对 8081 跑冒烟：health / 登录 / 检索 / ReAct

护栏（--serve 一律先过一遍，过不了直接退出）
-------------------------------------------
  · spec 必须在 feinao-test/ 目录下，指向 backend/spec.json 直接拒绝
  · db 必须在 feinao-test/ 目录下，指向 backend/feinao.db 直接拒绝
  · 端口必须是 8081（要用别的端口得 --port 显式给，且只接受 8080-8099）
  · FEINAO_DEMO=0 被拒绝，除非 --real-upstream（真上游 = 真数据 = 别拿它当测试）
"""
import argparse
import hashlib
import io
import json
import os
import re
import socket
import sqlite3
import sys
import time
import urllib.error
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(BASE, "backend")
TEST_DIR = os.path.join(BASE, "feinao-test")
TEST_SPEC = os.path.join(TEST_DIR, "spec.test.json")
TEST_DB = os.path.join(TEST_DIR, "feinao-test.db")
PROD_SPEC = os.path.join(BACKEND, "spec.json")
PROD_DB = os.path.join(BACKEND, "feinao.db")
DEFAULT_PORT = 8081
PROVIDER_KEYS = ("FEINAO_QWEN_KEY", "FEINAO_DEEPSEEK_KEY", "FEINAO_HUNYUAN_KEY")

# 生产上真机的地址，只用于体检探测，不写进任何配置
DEFAULT_HOST = os.environ.get("FEINAO_HOST") or "42.193.158.63"


# ------------------------------------------------------------------ 护栏

def _inside(path, root):
    a = os.path.normcase(os.path.abspath(path))
    b = os.path.normcase(os.path.abspath(root)) + os.sep
    return a.startswith(b)


def force_mock():
    """把模型也钉成 mock：验收要可复现、可免费重跑。

    本机 shell 里往往配着真 Key（做别的事用的），不显式清掉的话，测试会悄悄
    走真模型 —— 既花钱又不可复现，同一条用例昨天过今天不过，你就不知道是代码变了
    还是模型抽风。清掉的 Key 只在当前进程生效，不动任何配置文件。
    """
    hit = [k for k in PROVIDER_KEYS if os.environ.get(k)]
    for k in PROVIDER_KEYS:
        os.environ[k] = ""
    return hit


def guard(spec=None, db=None, port=None, allow_real_upstream=False):
    """把「会不会碰生产」变成一个能返回、能测的函数，而不是靠人记。"""
    spec = spec or TEST_SPEC
    db = db or TEST_DB
    errs = []
    if not _inside(spec, TEST_DIR):
        errs.append("配置必须放在 feinao-test/ 里：%s" % spec)
    if os.path.normcase(os.path.abspath(spec)) == os.path.normcase(os.path.abspath(PROD_SPEC)):
        errs.append("拒绝启动：FEINAO_SPEC 指向了生产配置 backend/spec.json")
    if not _inside(db, TEST_DIR):
        errs.append("库必须放在 feinao-test/ 里：%s" % db)
    if os.path.normcase(os.path.abspath(db)) == os.path.normcase(os.path.abspath(PROD_DB)):
        errs.append("拒绝启动：FEINAO_DB 指向了生产库 backend/feinao.db")
    if port is not None and port != DEFAULT_PORT and not (8080 <= int(port) <= 8099):
        errs.append("测试端口只接受 8081 或 8080-8099 之间，实际 %s" % port)
    if os.environ.get("FEINAO_DEMO") == "0" and not allow_real_upstream:
        errs.append("测试环境必须用假上游：FEINAO_DEMO=0 会连真系统。真要用真上游加 --real-upstream")
    if not os.path.isfile(spec):
        errs.append("配置不存在：%s（先跑 --prep）" % spec)
    return errs


def fingerprint(path):
    if not os.path.isfile(path):
        return None
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


# ------------------------------------------------------------------ 脱敏

PATTERNS = (
    ("mobile", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    ("email", re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")),
    ("idcard", re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")),
    ("apikey", re.compile(r"\b(?:sk|ak|ghp|gho|xoxb)-[A-Za-z0-9_\-]{12,}")),
    ("akid", re.compile(r"\bAKID[A-Za-z0-9]{10,}")),
    ("ipv4", re.compile(r"(?<![\d.])(?!127\.0\.0\.1|0\.0\.0\.0)(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")),
)
MASK = {"mobile": "138****0000", "email": "user@example.com", "idcard": "110101********0000",
        "apikey": "***REDACTED***", "akid": "AKID***REDACTED***", "ipv4": "203.0.113.1"}


def scrub(text, hits):
    if not isinstance(text, str) or not text:
        return text
    for name, rx in PATTERNS:
        def sub(m):
            hits[name] = hits.get(name, 0) + 1
            return MASK[name]
        text = rx.sub(sub, text)
    return text


def _text_columns(conn, table):
    try:
        cols = conn.execute("PRAGMA table_info(%s)" % table).fetchall()
    except sqlite3.Error:
        return []
    return [c[1] for c in cols if (c[2] or "").upper().startswith(("TEXT", "CHAR", "CLOB", ""))]


def _tables(conn):
    return [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()]


def scrub_db(path, hits):
    """整库过一遍：所有文本列都脱敏，不靠手工列表（手工列表一定会漏新表）。"""
    conn = sqlite3.connect(path)
    try:
        for table in _tables(conn):
            if table.endswith("_fts") or table in ("docs_fts_data", "docs_fts_idx", "docs_fts_docsize", "docs_fts_config"):
                continue
            cols = _text_columns(conn, table)
            if not cols:
                continue
            try:
                rows = conn.execute("SELECT rowid,%s FROM %s" % (",".join(cols), table)).fetchall()
            except sqlite3.Error:
                continue
            for row in rows:
                new = [scrub(v, hits) for v in row[1:]]
                if new != list(row[1:]):
                    conn.execute("UPDATE %s SET %s WHERE rowid=?" %
                                 (table, ",".join("%s=?" % c for c in cols)), tuple(new) + (row[0],))
        # harness_rev 的 sha 是内容哈希：脱敏改了内容就必须重算，否则飞构页会认为版本被篡改
        hv = conn.execute("SELECT id,content FROM harness_rev").fetchall() if "harness_rev" in _tables(conn) else []
        for rid, content in hv:
            conn.execute("UPDATE harness_rev SET sha=?, note=COALESCE(note,'')||'（脱敏副本）' WHERE id=?",
                         (hashlib.sha256((content or "").encode("utf-8")).hexdigest(), rid))
        conn.commit()
    finally:
        conn.close()


def db_stats(path):
    conn = sqlite3.connect(path)
    try:
        out = {}
        for t in _tables(conn):
            try:
                out[t] = conn.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
            except sqlite3.Error:
                pass
        return out
    finally:
        conn.close()


def seed_template(db_path):
    """没有源库时：用项目自带的假上游种子文档 + 一条演示会话，造一份干净副本。"""
    sys.path.insert(0, BACKEND)
    os.environ["FEINAO_SPEC"] = TEST_SPEC
    os.environ["FEINAO_DB"] = db_path
    import ai  # noqa: E402  必须在环境变量之后 import：SPEC/DB_PATH 是 import 期定死的

    # 但「之后 import」并不够：测试里 ai 可能已经被别的用例加载过、DB_PATH 还指着 tests/test.db。
    # 这时只设环境变量是无效的（模块级常量不会重读），种子会写进别的库，而测试库里只有 0 字节。
    # 所以这里直接钉住模块属性，用完还回去。
    orig = (ai.SPEC_PATH, ai.DB_PATH)
    ai.SPEC_PATH = TEST_SPEC
    ai.DB_PATH = db_path
    try:
        ai.reload_spec(io.open(TEST_SPEC, encoding="utf-8").read())
        ai.init_db()
        conn = ai.db()
        conn.execute("INSERT INTO conversations(user,title,created) VALUES(?,?,?)",
                     ("admin", "【测试副本】住宿报销标准是多少", ai._now()))
        cid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute("INSERT INTO messages(conv_id,role,content,created) VALUES(?,?,?,?)",
                     (cid, "user", "住宿报销标准是多少", ai._now()))
        conn.execute("INSERT INTO messages(conv_id,role,content,created) VALUES(?,?,?,?)",
                     (cid, "assistant", "按《差旅费管理办法》：一线城市 600 元/晚，其他城市 400 元/晚。", ai._now()))
        conn.commit()
        conn.close()
        import demo  # noqa: E402
        return demo.seed_docs()
    finally:
        ai.SPEC_PATH, ai.DB_PATH = orig
        try:
            ai.reload_spec(io.open(ai.SPEC_PATH, encoding="utf-8").read())
        except Exception:
            pass


# ------------------------------------------------------------------ 端口体检

def probe(host, port, kind="http", timeout=6):
    """不只是「通不通」：区分 三种失败形态，因为它们指向三种不同的原因。

    open          TCP 通、应用层有响应 —— 正常
    rst           TCP 握得上，一读数据就被 RST —— 中间设备按协议特征切断（办公网 DPI 的典型指纹）
    timeout       连 SYN 都没有回应 —— 端口没开或被丢包
    refused       对端明确回 RST —— 端口没监听
    """
    t0 = time.time()
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect((host, int(port)))
    except socket.timeout:
        return {"port": port, "state": "timeout", "ms": int((time.time() - t0) * 1000),
                "detail": "SYN 无回应：端口没开或被防火墙丢包"}
    except ConnectionRefusedError:
        return {"port": port, "state": "refused", "ms": int((time.time() - t0) * 1000),
                "detail": "对端回 RST：端口没有监听"}
    except OSError as e:
        return {"port": port, "state": "error", "ms": int((time.time() - t0) * 1000), "detail": str(e)[:80]}
    try:
        if kind == "ssh":
            s.settimeout(timeout)
            banner = s.recv(128)
            if banner.startswith(b"SSH-"):
                return {"port": port, "state": "open", "ms": int((time.time() - t0) * 1000),
                        "detail": banner.decode("ascii", "ignore").strip()[:60]}
            return {"port": port, "state": "open", "ms": int((time.time() - t0) * 1000),
                    "detail": "握手成功但没读到 SSH banner：%r" % banner[:20]}
        s.sendall(b"GET /api/health HTTP/1.0\r\nHost: %s\r\n\r\n" % host.encode())
        s.settimeout(timeout)
        head = s.recv(64)
        return {"port": port, "state": "open" if head.startswith(b"HTTP/") else "open",
                "ms": int((time.time() - t0) * 1000),
                "detail": head.split(b"\r\n")[0].decode("latin-1") or "(no response)"}
    except ConnectionResetError:
        return {"port": port, "state": "rst", "ms": int((time.time() - t0) * 1000),
                "detail": "TCP 握手成功，应用层数据一发出去就被 RST"}
    except socket.timeout:
        return {"port": port, "state": "open-no-app", "ms": int((time.time() - t0) * 1000),
                "detail": "连上了但应用层不回应（可能不是这个协议）"}
    except OSError as e:
        return {"port": port, "state": "error", "ms": int((time.time() - t0) * 1000), "detail": str(e)[:80]}
    finally:
        s.close()


def preflight(host=None, ports=(80, 8081, 22, 8060)):
    host = host or DEFAULT_HOST
    rows = []
    for p in ports:
        rows.append(probe(host, p, kind="ssh" if p == 22 else "http"))
    return rows


def preflight_report(host=None):
    rows = preflight(host)
    print("端口体检 → %s" % (host or DEFAULT_HOST))
    for r in rows:
        print("  %-5s %-12s %5dms  %s" % (r["port"], r["state"], r["ms"], r["detail"]))
    by = {r["port"]: r for r in rows}
    ssh = by.get(22) or {}
    web = by.get(80) or {}
    print()
    if web.get("state") == "open":
        print("  · 80 通：HTTP 明文，办公网 DPI 放行 —— 部署/中继走这条路是可行的")
    if ssh.get("state") == "rst":
        print("  · 22 是「握得上、一读就断」（RST），不是「连不上」：")
        print("    TCP 三次握手能成（中间设备代答），但 SSH banner 一到应用层就被切断。")
        print("    这是办公网 DPI 按协议特征封 SSH 的指纹，跟密码/密钥无关，换端口也一样。")
        print("    绕法：切手机热点（换出口）、或走 80/443 上的 HTTP 中继（我们现有部署链就是这么干的）。")
    elif ssh.get("state") == "open":
        print("  · 22 当前可用：这条网络没封 SSH（换到办公网再验一次）")
    elif ssh.get("state") == "timeout":
        print("  · 22 超时：出口直接把 SYN 丢了（比 RST 更粗的封法）")
    return rows


# ------------------------------------------------------------------ 起服

def cmd_prep(args):
    os.makedirs(TEST_DIR, exist_ok=True)
    src = args.from_db
    hits = {}
    if src:
        if not os.path.isfile(src):
            print("源库不存在：%s" % src)
            return 2
        if os.path.normcase(os.path.abspath(src)) == os.path.normcase(os.path.abspath(TEST_DB)):
            print("源库和目标是同一个文件")
            return 2
        for p in (TEST_DB, TEST_DB + "-wal", TEST_DB + "-shm"):
            if os.path.exists(p):
                os.remove(p)
        with open(src, "rb") as f, open(TEST_DB, "wb") as g:
            g.write(f.read())
        scrub_db(TEST_DB, hits)
        origin = "copy+scrub: %s" % os.path.basename(src)
    else:
        for p in (TEST_DB, TEST_DB + "-wal", TEST_DB + "-shm"):
            if os.path.exists(p):
                os.remove(p)
        n = seed_template(TEST_DB)
        scrubbed = 0
        scrub_db(TEST_DB, hits)
        origin = "template（假上游种子 %d 篇 + 1 条演示会话）" % n
    rows = db_stats(TEST_DB)
    # 0 字节的库也「生成成功」过：那是 import 顺序把种子写进了别的库。
    # 这类假绿比报错更难查，所以这里直接判死。
    if not rows.get("docs") or not rows.get("users"):
        print("生成失败：库是空的（docs=%s users=%s，文件 %d 字节）"
              % (rows.get("docs"), rows.get("users"), os.path.getsize(TEST_DB)))
        return 5
    info = {"origin": origin, "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "db": os.path.basename(TEST_DB), "sha256": fingerprint(TEST_DB),
            "scrub_hits": hits, "rows": rows,
            "spec": os.path.basename(TEST_SPEC),
            "spec_sha256": fingerprint(TEST_SPEC)}
    with io.open(os.path.join(TEST_DIR, "DB-INFO.json"), "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(info, ensure_ascii=False, indent=2) + "\n")
    print("已生成 %s" % TEST_DB)
    print("  来源：%s" % origin)
    print("  脱敏命中：%s" % (hits or "无（模板库里本来就没有真数据）"))
    print("  行数：%s" % ", ".join("%s=%d" % (k, v) for k, v in sorted(rows.items()) if v))
    print("  指纹：%s" % (info["sha256"] or "")[:16])
    return 0


def cmd_serve(args):
    port = int(args.port or DEFAULT_PORT)
    errs = guard(TEST_SPEC, TEST_DB, port, args.real_upstream)
    if errs:
        print("拒绝启动：")
        for e in errs:
            print("  · " + e)
        if not os.path.isfile(TEST_SPEC) or not os.path.isfile(TEST_DB):
            print("\n先跑：python tools/testenv.py --prep")
        return 2
    prod_before = fingerprint(PROD_SPEC)
    os.environ["FEINAO_SPEC"] = TEST_SPEC
    os.environ["FEINAO_DB"] = TEST_DB
    os.environ["FEINAO_PORT"] = str(port)
    if not args.real_upstream:
        os.environ["FEINAO_DEMO"] = "1"
    cleared = [] if args.real_llm else force_mock()
    sys.path.insert(0, BACKEND)
    import engine  # noqa: E402
    print("=" * 68)
    print(" 本地测试环境 · 端口 %d" % port)
    print(" 配置 %s" % TEST_SPEC)
    print(" 数据 %s" % TEST_DB)
    print(" 模型 %s" % ("真模型（--real-llm）" if args.real_llm else "mock（已清掉 %s）" % (",".join(cleared) or "无 Key")))
    print(" 生产配置 backend/spec.json 指纹 %s（起服前，结束后会再核一次）" % (prod_before or "缺失")[:16])
    print("=" * 68)
    try:
        engine.run()
    finally:
        prod_after = fingerprint(PROD_SPEC)
        if prod_after != prod_before:
            print("[ALERT] 生产配置 backend/spec.json 被改动了！%s → %s"
                  % ((prod_before or "")[:16], (prod_after or "")[:16]))
            return 3
        print("退出：生产配置未被触碰（指纹一致）")
    return 0


# ------------------------------------------------------------------ 验收冒烟

def _http(method, path, body=None, token=None, port=None):
    url = "http://127.0.0.1:%d%s" % (int(port or DEFAULT_PORT), path)
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, {}
    except Exception as e:
        return -1, {"error": str(e)[:120]}


def cmd_check(args):
    port = int(args.port or DEFAULT_PORT)
    rows = []
    st, d = _http("GET", "/api/health", port=port)
    svc = d.get("service") or ""
    rows.append(("health", st == 200, "status=%s %s" % (st, svc[:28])))
    if st == 200 and "本地测试" not in svc:
        rows.append(("配置归属", False, "health 里的服务名不是测试配置：%s" % (svc or "空")))
    st, d = _http("POST", "/api/login", {"username": "admin", "password": "admin123"}, port=port)
    tok = d.get("token")
    rows.append(("login", bool(tok), "admin → %s" % ("有 token" if tok else d.get("error"))))
    if tok:
        st, d = _http("POST", "/api/ai/ask", {"q": "住宿报销标准是多少"}, token=tok, port=port)
        rows.append(("ask+引用", st == 200 and bool(d.get("sources")),
                     "%d 条来源" % len(d.get("sources") or [])))
        st, d = _http("POST", "/api/ai/react", {"task": "查一下 BI 里西部能源的订单情况"}, token=tok, port=port)
        steps = d.get("steps") or []
        tools = len([s for s in steps if s.get("obs")])
        prov = d.get("provider") or "-"
        rows.append(("react", st == 200 and bool(steps) and prov == "mock",
                     "provider=%s，%d 步，%d 次工具调用" % (prov, len(steps), tools)))
        if prov not in ("mock", "-"):
            rows.append(("模型归属", False, "跑了真模型（%s）：这条链路的结论不可复现，也不该花这笔钱" % prov))
        elif tools == 0:
            rows.append(("工具链路", False, "mock 一个工具都没调到：ReAct 链路没被真正验过"))
        st, d = _http("GET", "/api/ai/policy", token=tok, port=port)
        rows.append(("policy", st == 200, "模式 %s" % ((d.get("modes") or {}).get("default"))))
        st, d = _http("GET", "/api/bench", token=tok, port=port)
        rows.append(("bench", st == 200, "%d 个任务" % len(d.get("tasks") or [])))
    print("8081 验收冒烟（全部走测试配置与测试库）")
    for name, ok, note in rows:
        print("  %s %-10s %s" % ("✓" if ok else "✗", name, note))
    bad = [r for r in rows if not r[1]]
    print("\n%d/%d 通过" % (len(rows) - len(bad), len(rows)))
    return 0 if not bad else 4


def cmd_status(args):
    print("测试目录 %s" % TEST_DIR)
    for name in ("spec.test.json", "feinao-test.db", "DB-INFO.json"):
        p = os.path.join(TEST_DIR, name)
        print("  %-16s %s" % (name, ("存在 %s" % (fingerprint(p) or "")[:16]) if os.path.isfile(p) else "缺失"))
    if os.path.isfile(TEST_SPEC):
        try:
            spec = json.load(io.open(TEST_SPEC, encoding="utf-8"))
            print("  端口 %s / 假上游端口 %s / demo=%s / 用户 %s"
                  % (spec["brand"]["port"], spec["demo"]["upstream_port"],
                     spec["demo"]["default"], ",".join(sorted(spec["users"]))))
        except Exception as e:
            print("  配置读不了：%s" % e)
    print("  生产配置 %s" % (("指纹 " + (fingerprint(PROD_SPEC) or "")[:16]) if os.path.isfile(PROD_SPEC) else "缺失"))
    print("  生产库   %s" % (("存在" if os.path.isfile(PROD_DB) else "本机没有（正常：生产库在服务器上）")))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="本地测试环境（8081）：跑验收不碰生产")
    ap.add_argument("--preflight", action="store_true", help="体检端口/配置/依赖")
    ap.add_argument("--host", default=None, help="体检目标主机（默认 %s）" % DEFAULT_HOST)
    ap.add_argument("--prep", action="store_true", help="建目录 + 生成脱敏库副本")
    ap.add_argument("--from", dest="from_db", default=None, help="从这个库复制并脱敏")
    ap.add_argument("--serve", action="store_true", help="在 8081 起服")
    ap.add_argument("--check", action="store_true", help="对 8081 跑冒烟")
    ap.add_argument("--status", action="store_true", help="看测试目录现状")
    ap.add_argument("--port", default=None, help="端口（默认 8081）")
    ap.add_argument("--real-upstream", action="store_true", help="允许连真上游（默认禁止）")
    ap.add_argument("--real-llm", action="store_true", help="允许用真模型（默认强制 mock：可复现、不花钱）")
    a = ap.parse_args(argv)

    if a.preflight:
        preflight_report(a.host)
        return 0
    if a.status:
        return cmd_status(a)
    if a.prep:
        return cmd_prep(a)
    if a.serve:
        return cmd_serve(a)
    if a.check:
        return cmd_check(a)
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
