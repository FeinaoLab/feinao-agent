# -*- coding: utf-8 -*-
"""本地测试环境（feinao-test/ + 8081）的护栏测试。

这里测的不是业务，是「测试环境不会把生产搞坏」这件事本身：
  · 护栏拦不拦得住指向生产的路径
  · 测试配置和生产配置除了地址/端口/口令以外是否同构（不同构的验收等于自欺欺人）
  · 脱敏是不是真的脱了（拿一个塞满敏感数据的库进去，看它出来什么样）
  · 端到端：起在测试端口上，模型是 mock，工具真调到，生产配置一个字节没动
"""
import hashlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import testenv as te  # noqa: E402

E2E_PORT = 8081   # 约定端口；被占（本地正开着测试环境）时往后顺延，见 _pick_port


def _pick_port():
    for p in (te.DEFAULT_PORT, 8088, 8089):
        if te.probe("127.0.0.1", p, timeout=1)["state"] != "open":
            return p
    pytest.skip("8081/8088/8089 都被占用了")


def _sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _flat(obj, prefix=""):
    out = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(_flat(v, "%s.%s" % (prefix, k) if prefix else k))
    elif isinstance(obj, list):
        out[prefix] = json.dumps(obj, ensure_ascii=False, sort_keys=True)
    else:
        out[prefix] = obj
    return out


# ---------------------------------------------------------------- 护栏

def test_guard_accepts_the_test_env():
    assert te.guard(te.TEST_SPEC, te.TEST_DB, te.DEFAULT_PORT) == []


def test_guard_refuses_production_spec_and_db():
    errs = te.guard(te.PROD_SPEC, te.TEST_DB, te.DEFAULT_PORT)
    assert any("生产配置" in e for e in errs), errs
    errs = te.guard(te.TEST_SPEC, te.PROD_DB, te.DEFAULT_PORT)
    assert any("生产库" in e for e in errs), errs
    # 换到别的目录也不行：不是「名字叫生产」才拦，是「不在测试目录里」就拦
    errs = te.guard(os.path.join(ROOT, "tmp-spec.json"), te.TEST_DB, te.DEFAULT_PORT)
    assert any("feinao-test/" in e for e in errs), errs


def test_guard_refuses_real_upstream_and_odd_ports(monkeypatch):
    monkeypatch.setenv("FEINAO_DEMO", "0")
    errs = te.guard(te.TEST_SPEC, te.TEST_DB, te.DEFAULT_PORT)
    assert any("假上游" in e for e in errs), errs
    assert te.guard(te.TEST_SPEC, te.TEST_DB, te.DEFAULT_PORT, allow_real_upstream=True) == []
    monkeypatch.delenv("FEINAO_DEMO", raising=False)
    errs = te.guard(te.TEST_SPEC, te.TEST_DB, 9999)
    assert any("端口" in e for e in errs), errs
    assert te.guard(te.TEST_SPEC, te.TEST_DB, 8090) == [], "8080-8099 之间应允许"


# ---------------------------------------------------------------- 同构

ALLOWED_DIFF = set()
for _p in ("brand.name", "brand.full", "brand.tagline", "brand.port", "demo.upstream_port",
           "approval.name", "memory.name"):   # 后两个只是页面标题，带「测试」二字更容易认
    ALLOWED_DIFF.add(_p)
for _u in ("kms", "bi", "cc"):
    for _f in ("base", "demo_base", "label", "user", "pass"):
        ALLOWED_DIFF.add("upstreams.%s.%s" % (_u, _f))


def test_test_spec_is_isomorphic_to_production():
    """测试配置只许在「地址/端口/文案/口令」上不同。

    一旦有人为了跑通而把 modes、limits、tool_policies 改松，这里的验收就不再代表
    生产行为——那种改动必须被看见，所以让它红。
    """
    prod = json.load(io.open(te.PROD_SPEC, encoding="utf-8"))
    test = json.load(io.open(te.TEST_SPEC, encoding="utf-8"))
    a, b = _flat(prod), _flat(test)
    diff = sorted(set(a) | set(b))
    diff = [k for k in diff if a.get(k, "<missing>") != b.get(k, "<missing>")]
    unexpected = [k for k in diff if k not in ALLOWED_DIFF]
    assert not unexpected, "测试配置与生产配置在这些键上分叉了：%s" % unexpected
    assert test["brand"]["port"] == te.DEFAULT_PORT
    assert test["demo"]["default"] is True
    assert test["demo"]["upstream_port"] != prod["demo"]["upstream_port"], \
        "假上游端口别和生产默认端口撞（撞了会误连到真上游）"


# ---------------------------------------------------------------- 脱敏

def test_scrub_covers_the_usual_suspects():
    hits = {}
    raw = ("联系 13812345678 / 0571-88886666，邮箱 zhang.san@corp.example.com，"
           "身份证 110101199003078888，来源 IP 10.12.34.56，"
           "token sk-abcdefghijklmnop123，云密钥 AKIDabcdEFGH12345678")
    out = te.scrub(raw, hits)
    for bad in ("13812345678", "zhang.san@corp.example.com", "110101199003078888",
                "10.12.34.56", "sk-abcdefghijklmnop123", "AKIDabcdEFGH12345678"):
        assert bad not in out, "没脱掉：%s → %s" % (bad, out)
    assert hits.get("mobile", 0) == 1 and hits.get("email") == 1


def test_scrub_db_walks_every_text_column(tmp_path):
    db = str(tmp_path / "dirty.db")
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE messages(id INTEGER PRIMARY KEY, content TEXT)")
    conn.execute("CREATE TABLE some_future_table(id INTEGER PRIMARY KEY, blob TEXT)")
    conn.execute("CREATE TABLE harness_rev(id INTEGER PRIMARY KEY, sha TEXT, content TEXT, note TEXT)")
    conn.execute("INSERT INTO messages(content) VALUES(?)", ("客户电话 13900001111",))
    # 新表也得被覆盖：靠手工列表一定会漏，所以按「所有表的文本列」遍历
    conn.execute("INSERT INTO some_future_table(blob) VALUES(?)", ("运维联系 ops@corp.example.com",))
    spec = '{"upstreams":{"bi":{"base":"http://10.0.0.9:8041/bi/"}}}'
    conn.execute("INSERT INTO harness_rev(sha,content,note) VALUES(?,?,?)",
                 ("old-sha", spec, "原始"))
    conn.commit()
    conn.close()

    hits = {}
    te.scrub_db(db, hits)
    conn = sqlite3.connect(db)
    msg = conn.execute("SELECT content FROM messages").fetchone()[0]
    fut = conn.execute("SELECT blob FROM some_future_table").fetchone()[0]
    sha, content, note = conn.execute("SELECT sha,content,note FROM harness_rev").fetchone()
    conn.close()
    assert "13900001111" not in msg
    assert "ops@corp.example.com" not in fut
    assert "10.0.0.9" not in content, "配置原文里的内网地址也要脱"
    assert sha == hashlib.sha256(content.encode("utf-8")).hexdigest(), \
        "脱敏改了内容就必须重算 sha，否则飞构页会判定版本被篡改"
    assert "脱敏副本" in note


# ---------------------------------------------------------------- 端到端

def _http(port, path, method="GET", body=None, token=None):
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), method=method)
    if body is not None:
        req.data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8", "ignore") or "{}")


def test_test_env_runs_on_its_own_port_without_touching_production():
    port = _pick_port()

    class _Args(object):
        from_db = None

    te.cmd_prep(_Args())          # 自足：CI 里没有 feinao-test/ 也能跑
    prod_before = _sha(te.PROD_SPEC)

    env = dict((k, v) for k, v in os.environ.items() if not k.startswith("FEINAO_"))
    env["PYTHONUNBUFFERED"] = "1"
    proc = subprocess.Popen([sys.executable, "tools/testenv.py", "--serve", "--port", str(port)],
                            cwd=ROOT, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        ok = False
        for _ in range(60):
            time.sleep(0.5)
            try:
                st, j = _http(port, "/api/health")
                if st == 200:
                    ok = True
                    break
            except Exception:
                continue
        assert ok, "测试环境没起来"
        assert "本地测试" in j.get("service", ""), "health 里的服务名说明用的不是测试配置：%s" % j

        st, j = _http(port, "/api/login", "POST", {"username": "admin", "password": "admin123"})
        assert st == 200 and j.get("token")
        tok = j["token"]

        st, j = _http(port, "/api/ai/react", "POST",
                      {"task": "查一下 BI 里西部能源的订单情况"}, token=tok)
        assert st == 200
        assert j.get("provider") == "mock", "测试环境不该走真模型：%s" % j.get("provider")
        steps = j.get("steps") or []
        assert any(s.get("obs") for s in steps), "工具没被真正调到，链路没验到：%s" % steps
        assert not any(s.get("error") for s in steps), "工具报错了：%s" % steps

        st, j = _http(port, "/api/ai/policy", token=tok)
        assert st == 200 and (j.get("modes") or {}).get("default") == "default"
    finally:
        proc.terminate()
        try:
            out = proc.communicate(timeout=10)[0].decode("utf-8", "ignore")
        except subprocess.TimeoutExpired:
            proc.kill()
            out = ""
        if proc.returncode not in (0, -15, None) and out:
            sys.stderr.write(out[-1500:])

    assert _sha(te.PROD_SPEC) == prod_before, "生产配置被改动了！"
    # 只看文件变大太弱：0 字节的库也会「变大」。要看表里真有东西。
    conn = sqlite3.connect(te.TEST_DB)
    try:
        docs = conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
        tasks = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    finally:
        conn.close()
    assert docs >= 9, "测试库里的种子文档没落进来（docs=%d）——多半是 ai.DB_PATH 还指着别的库" % docs
    assert tasks >= 1, "刚才那次 ReAct 没写进测试库（tasks=%d）：数据没走副本" % tasks


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
