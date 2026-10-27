# -*- coding: utf-8 -*-
"""策略数据化（任务 3）测试：spec 顶层段 + 结构校验 + 生效策略接口。

要守住的三件事：
① 坏配置拒绝启动，且报错要指名道姓（不是 KeyError，不是静默用默认值）；
② 可选段缺省时补默认值、不写盘（飞构快照必须还是文件原文）；
③ 对外接口只回结构与取值，绝不带口令。
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC_FILE = os.path.join(BASE_DIR, "backend", "spec.json")
BAD_JSON = os.path.join(BASE_DIR, "tests", "test_policy_bad.json")
PORT = 8061
BASE = "http://127.0.0.1:%d" % PORT

for _k in ("FEINAO_QWEN_KEY", "FEINAO_DEEPSEEK_KEY", "FEINAO_HUNYUAN_KEY"):
    os.environ[_k] = ""
os.environ.setdefault("FEINAO_DB", os.path.join(BASE_DIR, "tests", "test_policy.db"))

sys.path.insert(0, os.path.join(BASE_DIR, "backend"))
import ai  # noqa: E402

ai.init_db()

# 这些用例会替换 ai.llm_chat 造现场：用完必须还原，否则后面的模块（如 test_provider）
# 会拿着桩跑真链路，失败得莫名其妙。测试之间的隔离和代码本身一样重要。
import pytest  # noqa: E402

_ORIG_LLM = ai.llm_chat


@pytest.fixture(autouse=True)
def _restore_llm():
    yield
    ai.llm_chat = _ORIG_LLM

ORIGINAL_TEXT = open(SPEC_FILE, encoding="utf-8").read()


def _spec_dict():
    return json.loads(ORIGINAL_TEXT)


def _drop(spec, key):
    d = dict(spec)
    d.pop(key, None)
    return d


def test_validate_reports_missing_section():
    """缺顶层段要报段名，而不是运行到一半 KeyError。"""
    for key in ("tool_policies", "approval", "upstreams"):
        try:
            ai.validate_spec(_drop(_spec_dict(), key), "测试配置")
            assert False, "缺 %s 应当报错" % key
        except ai.SpecError as e:
            assert key in str(e), "报错要指名缺哪段：%s" % e


def test_validate_rejects_bad_values():
    """枚举取值错、upstream 不是 http(s)、模式缺动作，都必须拒绝。"""
    s = _spec_dict()
    s["tool_policies"]["ingest_doc"]["class"] = "write_sometimes"
    try:
        ai.validate_spec(s)
        assert False, "class 取值非法应报错"
    except ai.SpecError as e:
        assert "class" in str(e)

    s = _spec_dict()
    s["upstreams"]["kms"]["base"] = "file:///etc/passwd"
    try:
        ai.validate_spec(s)
        assert False, "非 http(s) 上游应报错"
    except ai.SpecError as e:
        assert "http" in str(e)

    s = _spec_dict()
    del s["modes"]["definitions"]["plan"]["dangerous"]
    try:
        ai.validate_spec(s)
        assert False, "模式缺动作应报错"
    except ai.SpecError as e:
        assert "plan" in str(e) and "dangerous" in str(e)


def test_defaults_filled_without_touching_disk():
    """老配置（没有 modes/limits/egress）也能跑：默认值只补内存，文件一个字节都不动。"""
    before = open(SPEC_FILE, "rb").read()
    s = _drop(_drop(_drop(_spec_dict(), "modes"), "limits"), "egress")
    loaded = ai.load_spec_text(json.dumps(s, ensure_ascii=False))
    assert loaded["modes"]["default"] == "default"
    assert loaded["modes"]["definitions"]["plan"]["write"] == "suspend"
    assert loaded["limits"]["same_call_repeat_limit"] == 3
    assert loaded["egress"]["enabled"] is False
    # 缺 class 的老策略按 requires_confirm 反推
    assert loaded["tool_policies"]["ingest_doc"]["class"] == "write"
    assert loaded["tool_policies"]["query_system"]["class"] == "read"
    assert open(SPEC_FILE, "rb").read() == before, "加载不能改磁盘上的 spec"


def test_tool_policy_reads_class_and_timeout():
    pol = ai.tool_policy("query_system")
    assert pol["class"] == "read" and pol["timeout_seconds"] == 15
    pol = ai.tool_policy("ingest_doc")
    assert pol["class"] == "write" and pol["requires_confirm"] is True
    # 未声明的工具走保守默认：当写操作、要确认
    pol = ai.tool_policy("tool_not_declared")
    assert pol["class"] == "read" and pol["requires_confirm"] is False


def test_limits_cap_react_steps():
    """limits.max_steps 是硬上限：pilots 写 6 也顶不过它。"""
    old = ai.SPEC["limits"]["max_steps"]
    n = [0]

    def fake_llm(messages, user="system"):
        n[0] += 1
        return json.dumps({"tool": "search_docs", "args": {"q": "上限%d" % n[0]}}), "stub"
    ai.llm_chat = fake_llm
    ai.SPEC["limits"]["max_steps"] = 2
    try:
        r = ai.react_run("步数上限压测", user="admin")
    finally:
        ai.SPEC["limits"]["max_steps"] = old
    calls = [s for s in r["steps"] if s.get("obs")]
    assert len(calls) == 2, "硬上限 2 步，实际执行 %d" % len(calls)


def test_broken_spec_refuses_start():
    """坏 JSON 起服必须直接失败，而不是静默降级——这是安全项。"""
    with open(BAD_JSON, "w", encoding="utf-8") as f:
        f.write('{"brand": {"name": "飞脑"}, "users": {"admin": "admin123"},')  # 截断的 JSON
    try:
        p = subprocess.run([sys.executable, "server.py"],
                           cwd=os.path.join(BASE_DIR, "backend"),
                           env=dict(os.environ, FEINAO_SPEC=BAD_JSON,
                                    FEINAO_DB=os.path.join(BASE_DIR, "tests", "test_policy_bad.db"),
                                    FEINAO_PORT="8062"),
                           capture_output=True, text=True, timeout=60)
        err = (p.stderr or "") + (p.stdout or "")
        assert p.returncode != 0, "坏配置不该起服成功：%s" % err[-300:]
        assert "SpecError" in err or "不是合法 JSON" in err, "要报清是配置问题：%s" % err[-300:]
    finally:
        if os.path.exists(BAD_JSON):
            os.remove(BAD_JSON)


def test_policy_endpoint():
    """真服务：未登录 401；登录后能看到生效策略，且不含任何口令。"""
    db = os.path.join(BASE_DIR, "tests", "test_policy.db")
    if os.path.exists(db):
        os.remove(db)
    proc = subprocess.Popen([sys.executable, "server.py"], cwd=os.path.join(BASE_DIR, "backend"),
                            env=dict(os.environ, FEINAO_DB=db, FEINAO_PORT=str(PORT)))
    try:
        st = 0
        for _ in range(30):
            time.sleep(0.5)
            try:
                with urllib.request.urlopen(BASE + "/api/health", timeout=3) as x:
                    st = x.status
                if st == 200:
                    break
            except Exception:
                continue
        assert st == 200, "health fail"

        try:
            urllib.request.urlopen(BASE + "/api/ai/policy", timeout=5)
            assert False, "未登录应 401"
        except urllib.error.HTTPError as e:
            assert e.code == 401

        r = urllib.request.Request(BASE + "/api/login", method="POST",
                                   data=json.dumps({"username": "admin", "password": "admin123"}).encode(),
                                   headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(r, timeout=10) as x:
            tok = json.loads(x.read().decode())["token"]

        r = urllib.request.Request(BASE + "/api/ai/policy", headers={"Authorization": "Bearer " + tok})
        with urllib.request.urlopen(r, timeout=10) as x:
            raw = x.read().decode()
        pol = json.loads(raw)
        assert set(pol["modes"]["definitions"]) == {"plan", "default", "auto", "yolo"}
        assert pol["limits"]["max_wall_clock_seconds"] == 600
        assert pol["egress"]["enabled"] is False
        assert pol["tools"]["ingest_doc"]["class"] == "write"
        assert pol["tools"]["query_system"]["requires_confirm"] is False
        for secret in ("admin123", "ops123456", "view123456"):
            assert secret not in raw, "策略接口不得回口令：%s" % secret
    finally:
        proc.terminate()
        proc.wait(timeout=10)
