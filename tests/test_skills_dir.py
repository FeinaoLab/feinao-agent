# -*- coding: utf-8 -*-
"""任务 4：SKILL.md 技能目录化测试。

覆盖：frontmatter 解析、enabled=false 跳过、工具未注册拒绝、幂等导入（只补缺不覆盖）、
导入后 skill_match 能命中。全部走临时目录，不碰仓库 skills/。
"""
import io
import json
import os
import shutil
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEST_DB = os.path.join(BASE_DIR, "tests", "test_skills.db")
TEST_SPEC = os.path.join(BASE_DIR, "tests", "test_skills_spec.json")
for _p in (TEST_DB, TEST_DB + "-wal", TEST_DB + "-shm"):
    if os.path.exists(_p):
        os.remove(_p)
shutil.copyfile(os.path.join(BASE_DIR, "backend", "spec.json"), TEST_SPEC)

os.environ["FEINAO_DB"] = TEST_DB
os.environ["FEINAO_SPEC"] = TEST_SPEC

sys.path.insert(0, os.path.join(BASE_DIR, "backend"))
import ai  # noqa: E402

ai.init_db()
TMP = os.path.join(BASE_DIR, "tests", "_tmp_skills_dir")


def _mk(name, text):
    d = os.path.join(TMP, name)
    os.makedirs(d, exist_ok=True)
    io.open(os.path.join(d, "SKILL.md"), "w", encoding="utf-8").write(text)


def setup_module(module):
    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(TMP)
    _mk("查库存数据", "---\nname: 查库存数据\nuse_when: 查 BI 销售数据\ntools: [query_system]\n---\n先查 sources 再查数。")
    _mk("禁用技能", "---\nname: 禁用技能\nuse_when: 不应被导入生效\ntools: [query_system]\nenabled: false\n---\n关闭的技能。")
    _mk("坏工具", "---\nname: 坏工具\nuse_when: 工具不存在\ntools: [no_such_tool]\n---\n应被拒绝。")
    _mk("无工具", "---\nname: 无工具\nuse_when: 缺 tools\n---\n应被拒绝。")


def teardown_module(module):
    shutil.rmtree(TMP, ignore_errors=True)


def test_scan_parses_frontmatter():
    found = {s["name"]: s for s in ai.skills_scan_dir(TMP)}
    assert set(found) == {"查库存数据", "禁用技能", "坏工具", "无工具"}
    s = found["查库存数据"]
    assert s["tools"] == ["query_system"] and s["enabled"] == 1
    assert "sources" in s["how"]
    assert found["禁用技能"]["enabled"] == 0
    assert found["无工具"]["tools"] == []


def test_import_rejects_invalid_tools():
    r = ai.skills_import_from_dir(by="tester", base=TMP)
    # 两个有效文件都导入：查库存数据（enabled=1）与禁用技能（enabled=0 落库）
    assert set(r["imported"]) == {"查库存数据", "禁用技能"}
    reasons = {x["name"]: x["reason"] for x in r["skipped_invalid"]}
    assert "坏工具" in reasons and "未注册" in reasons["坏工具"]
    assert "无工具" in reasons and "tools 为空" in reasons["无工具"]


def test_import_idempotent_and_non_destructive():
    # 改一下库里的 how（模拟人工调优），重导不能覆盖
    conn = ai.db()
    conn.execute("UPDATE skills SET how='人工调优过的说明' WHERE name='查库存数据'")
    conn.commit()
    conn.close()
    r = ai.skills_import_from_dir(by="tester", base=TMP)
    assert r["imported"] == []                                # 已存在 → 幂等跳过
    conn = ai.db()
    row = conn.execute("SELECT how FROM skills WHERE name='查库存数据'").fetchone()
    conn.close()
    assert row["how"] == "人工调优过的说明"


def test_imported_skill_matches():
    hits = ai.skill_match("帮我查一下 BI 的销售数据")
    assert any(h["name"] == "查库存数据" for h in hits)


def test_disabled_skill_not_served():
    """enabled=0 的技能导入后在注册表里存在但默认不启用。"""
    conn = ai.db()
    row = conn.execute("SELECT enabled FROM skills WHERE name='禁用技能'").fetchone()
    conn.close()
    assert row and row["enabled"] == 0
    assert not any(s["name"] == "禁用技能" for s in ai.skills_list())
