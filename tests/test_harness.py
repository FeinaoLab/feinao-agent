# -*- coding: utf-8 -*-
"""Harness 版本化测试（spec.json 的快照 / 对比 / 回滚）。

两层：① 直接驱动 ai 做版本语义（幂等、对比、激活、脏数据拒绝）；
② 起真服务验权限与路由（ops 不能切配置、admin 能回滚）。
全部用例只在临时 spec 副本上操作，绝不碰仓库里的 backend/spec.json。
"""
import io
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEST_DB = os.path.join(BASE_DIR, "tests", "test_harness.db")
TEST_SPEC = os.path.join(BASE_DIR, "tests", "test_harness_spec.json")
ORIG_SPEC = os.path.join(BASE_DIR, "backend", "spec.json")

if os.path.exists(TEST_DB):
    os.remove(TEST_DB)
shutil.copyfile(ORIG_SPEC, TEST_SPEC)
ORIGINAL_TEXT = io.open(ORIG_SPEC, encoding="utf-8").read()

os.environ["FEINAO_DB"] = TEST_DB
os.environ["FEINAO_SPEC"] = TEST_SPEC
for _k in ("FEINAO_QWEN_KEY", "FEINAO_DEEPSEEK_KEY", "FEINAO_HUNYUAN_KEY"):
    os.environ[_k] = ""

sys.path.insert(0, os.path.join(BASE_DIR, "backend"))
import ai  # noqa: E402

# 用例顺序不可控（其他测试文件可能先 import 过 ai），这里强制把路径钉到副本上
ai.SPEC_PATH = TEST_SPEC
ai.reload_spec()
ai.init_db()

try:
    import pytest

    @pytest.fixture(autouse=True)
    def _reset_spec():
        """每条用例前后都把副本与内存配置还原，并把 active 钉在"原始内容"那一版上。

        只还原文件不够：上一条用例可能把 active 留在改过的版本上，
        下一条用例再取 _active_id() 就会拿到脏基准。
        """
        def _restore():
            with io.open(TEST_SPEC, "w", encoding="utf-8", newline="\n") as f:
                f.write(ORIGINAL_TEXT)
            ai.reload_spec()
            _, base = ai.harness_snapshot(note="原始配置")
            conn = ai.db()
            conn.execute("UPDATE harness_rev SET active=0")
            conn.execute("UPDATE harness_rev SET active=1 WHERE id=?", (base["id"],))
            conn.commit()
            conn.close()
        _restore()
        yield
        _restore()
except ImportError:  # 直接 python 运行本文件时没有 pytest
    pass


def _write_spec(obj_or_text):
    text = obj_or_text if isinstance(obj_or_text, str) else json.dumps(obj_or_text, ensure_ascii=False, indent=2)
    with io.open(TEST_SPEC, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return text


def _rev_count():
    conn = ai.db()
    n = conn.execute("SELECT COUNT(*) c FROM harness_rev").fetchone()["c"]
    conn.close()
    return n


def _active_id():
    conn = ai.db()
    row = conn.execute("SELECT id FROM harness_rev WHERE active=1").fetchone()
    conn.close()
    return row["id"] if row else None


def test_startup_snapshot():
    """起服自动留一版：sha 与磁盘一致，且恰好一条 active。"""
    rows = ai.harness_list()["rows"]
    assert rows, "init_db 应留下启动快照"
    assert rows[0]["sha"] == ai.spec_sha(), "最新修订的 sha 应与当前文件一致"
    assert sum(1 for r in rows if r["active"]) == 1, "active 必须唯一"


def test_snapshot_is_idempotent():
    """内容没变时再快照不产生新版本（否则每次重启都会刷一堆噪音）。"""
    before = _rev_count()
    created, row = ai.harness_snapshot(note="重复快照")
    assert created is False, "同内容不应新建修订"
    assert _rev_count() == before
    assert row["sha"] == ai.spec_sha()


def test_snapshot_new_rev_on_change():
    """改一处配置 → 新修订 → 旧的自动失活。"""
    old_id = _active_id()
    spec = json.loads(ORIGINAL_TEXT)
    spec["pilots"]["react_max_steps"] = 3
    _write_spec(spec)
    created, row = ai.harness_snapshot(note="压测：步数收紧到 3", by="admin")
    assert created is True and row["id"] != old_id
    assert _active_id() == row["id"], "新修订应自动成为 active"
    rows = {r["id"]: r for r in ai.harness_list()["rows"]}
    assert rows[old_id]["active"] is False, "旧修订应失活"


def test_activate_rolls_back_file_and_memory():
    """回滚：磁盘与内存配置都要回到旧版，且落一条审计。"""
    old_id = _active_id()
    spec = json.loads(ORIGINAL_TEXT)
    spec["pilots"]["react_max_steps"] = 3
    _write_spec(spec)
    _, new_row = ai.harness_snapshot(note="改坏一点", by="admin")
    assert ai.SPEC["pilots"]["react_max_steps"] == 6

    r = ai.harness_activate(old_id, by="admin")
    assert r["ok"] is True and r["active"] == old_id
    assert io.open(TEST_SPEC, encoding="utf-8").read() == ORIGINAL_TEXT, "磁盘应回到旧版"
    assert ai.SPEC["pilots"]["react_max_steps"] == 6, "内存里的 config 应热加载成旧版"
    assert _active_id() == old_id, "active 应指向被激活的版本"
    assert new_row["id"] != old_id

    conn = ai.db()
    row = conn.execute("SELECT * FROM audit WHERE tool='harness_activate' ORDER BY id DESC").fetchone()
    conn.close()
    assert row and str(old_id) in (row["args"] or ""), "回滚必须留审计"


def test_activate_missing_rev():
    r = ai.harness_activate(99999, by="admin")
    assert r["ok"] is False and "不存在" in r["error"]


def test_activate_rejects_broken_json():
    """脏修订不许上线：语法不对直接拒，文件保持原样。"""
    conn = ai.db()
    conn.execute("INSERT INTO harness_rev(sha,content,note,by,created,active) "
                 "VALUES(?,?,?,?,?,0)", ("deadbeef", "{not json", "坏数据", "test", ai._now()))
    rid = conn.execute("SELECT last_insert_rowid() i").fetchone()["i"]
    conn.commit()
    conn.close()
    r = ai.harness_activate(rid, by="admin")
    assert r["ok"] is False and "JSON" in r["error"]
    assert io.open(TEST_SPEC, encoding="utf-8").read() == ORIGINAL_TEXT
    assert not os.path.exists(TEST_SPEC + ".tmp"), "失败不许留临时文件"


def test_diff_between_revs():
    """对比两次修订：语义对比要精确指出改了哪个键、从什么改到什么。"""
    spec = json.loads(ORIGINAL_TEXT)
    spec["pilots"]["react_max_steps"] = 3
    _write_spec(spec)
    _, new_row = ai.harness_snapshot(note="改步数", by="admin")
    base = [r for r in ai.harness_list()["rows"] if r["sha"] == ai.spec_sha(ORIGINAL_TEXT)][0]
    d = ai.harness_diff(base["id"], new_row["id"])
    assert d["changed_lines"] == 1, "只改一个键，应只有一处差异：%s" % d["changes"]
    c = d["changes"][0]
    assert c["path"] == "pilots.react_max_steps" and c["from"] == 6 and c["to"] == 3, c
    assert d["raw"], "原始行 diff 也要保留，方便对着文件看"


def test_diff_ignores_formatting_only_change():
    """只改缩进/换行不算改动——否则任何脚本重写一遍都会产生一堆假差异。"""
    spec = json.loads(ORIGINAL_TEXT)
    _write_spec(json.dumps(spec, ensure_ascii=False, indent=4))  # 缩进从 2 变 4
    _, new_row = ai.harness_snapshot(note="只改格式", by="admin")
    base = [r for r in ai.harness_list()["rows"] if r["sha"] == ai.spec_sha(ORIGINAL_TEXT)][0]
    d = ai.harness_diff(base["id"], new_row["id"])
    assert d["changed_lines"] == 0, "格式变化不该算语义改动：%s" % d["changes"]


# ---------------- 起真服务：权限与路由 ----------------

API_PORT = 8062
API = "http://127.0.0.1:%d" % API_PORT


def _req(path, method="GET", body=None, token=None):
    r = urllib.request.Request(API + path, method=method)
    if body is not None:
        r.data = json.dumps(body).encode()
        r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(r, timeout=20) as x:
            return x.status, json.loads(x.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8", "ignore") or "{}")


def test_api_permissions_and_rollback():
    """ops 只能看不能切；admin 回滚后配置文件真的变了。"""
    env = dict(os.environ, FEINAO_DB=os.path.join(BASE_DIR, "tests", "test_harness_api.db"),
               FEINAO_SPEC=TEST_SPEC, FEINAO_PORT=str(API_PORT), FEINAO_DEMO="0")
    for p in (env["FEINAO_DB"], env["FEINAO_DB"] + "-wal", env["FEINAO_DB"] + "-shm"):
        if os.path.exists(p):
            os.remove(p)
    proc = subprocess.Popen([sys.executable, "server.py"], cwd=os.path.join(BASE_DIR, "backend"), env=env)
    try:
        for _ in range(30):
            time.sleep(0.4)
            try:
                if _req("/api/health")[0] == 200:
                    break
            except Exception:
                continue
        else:
            raise AssertionError("服务没起来")

        _, j = _req("/api/login", "POST", {"username": "admin", "password": "admin123"})
        admin = j["token"]
        _, j = _req("/api/login", "POST", {"username": "ops01", "password": "ops123456"})
        ops = j["token"]

        st, _ = _req("/api/harness", token=ops)
        assert st == 403, "ops 不该看到配置历史（含出厂口令），实际 %d" % st
        st, _ = _req("/api/harness/activate", "POST", {"id": 1}, token=ops)
        assert st == 403, "ops 不该能切配置，实际 %d" % st

        st, j = _req("/api/harness", token=admin)
        assert st == 200 and j["rows"], "admin 应拿到修订列表"
        assert j["active"] == j["rows"][0]["id"]
        first = j["rows"][-1]["id"]

        # 造一版新配置并激活回来
        spec = json.loads(io.open(TEST_SPEC, encoding="utf-8").read())
        spec["pilots"]["react_max_steps"] = 2
        _write_spec(spec)
        st, j = _req("/api/harness/snapshot", "POST", {"note": "API 造的版本"}, token=admin)
        assert st == 200 and j["created"] is True, j
        st, j = _req("/api/harness/activate", "POST", {"id": first}, token=admin)
        assert st == 200 and j["active"] == first, j
        assert json.loads(io.open(TEST_SPEC, encoding="utf-8").read())["pilots"]["react_max_steps"] == 6

        st, _ = _req("/api/harness/activate", "POST", {"id": 99999}, token=admin)
        assert st == 404
        st, j = _req("/api/harness/detail?id=%d" % first, token=admin)
        assert st == 200 and j["content"].startswith("{")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
