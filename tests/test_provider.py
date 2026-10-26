# -*- coding: utf-8 -*-
"""真实供应商链路验证：本地起一个兼容 OpenAI 协议的桩服务，
验证 Key 读取（.env / 环境变量）、调用、用量记账、体检 ping 全链路。

意义：真 Key 到位时走的正是这条代码路径，桩跑通 = 换成真 Key 就能用，
不需要等有 Key 才能测。
"""
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "backend"))
os.environ["FEINAO_DB"] = os.path.join(BASE_DIR, "tests", "test_provider.db")

import ai  # noqa: E402

ai.init_db()          # 建表（users/usage/audit/docs）

STUB_PORT = 18099
STUB_BASE = "http://127.0.0.1:%d/v1/chat/completions" % STUB_PORT
SEEN = []


class Stub(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n).decode() or "{}")
        SEEN.append({"auth": self.headers.get("Authorization", ""), "model": body.get("model"),
                     "msgs": len(body.get("messages", []))})
        out = json.dumps({
            "choices": [{"message": {"role": "assistant", "content": "在线"}}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 2},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


def _start_stub():
    srv = ThreadingHTTPServer(("127.0.0.1", STUB_PORT), Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_env_file_loading(tmp_path):
    """Key 从 .env 文件读取，且不覆盖已有的真实环境变量。"""
    envp = tmp_path / "feinao.env"
    envp.write_text("# 注释行\nFEINAO_STUB_KEY=from-file\nFEINAO_EXISTING=file-value\n", encoding="utf-8")
    os.environ["FEINAO_ENV_FILE"] = str(envp)
    os.environ.pop("FEINAO_STUB_KEY", None)
    os.environ["FEINAO_EXISTING"] = "env-value"
    ai._load_env_file()
    assert os.environ.get("FEINAO_STUB_KEY") == "from-file", ".env 里的 Key 没读到"
    assert os.environ.get("FEINAO_EXISTING") == "env-value", "环境变量不该被 .env 覆盖"


def test_real_provider_chain():
    """有 Key → 真发 HTTP、带 Bearer、记用量；无 Key → 降级到下一家。"""
    srv = _start_stub()
    saved = ai.SPEC["providers"]
    try:
        ai.SPEC["providers"] = [
            {"name": "stub", "type": "openai", "base": STUB_BASE, "model": "stub-1",
             "key_env": "FEINAO_STUB_KEY"},
            {"name": "mock", "type": "mock"},
        ]
        os.environ["FEINAO_STUB_KEY"] = "sk-test-1234567890"

        t0 = time.time()
        ans, provider = ai.llm_chat([{"role": "user", "content": "在吗"}])
        assert provider == "stub" and ans.strip() == "在线", "真实供应商调用失败"
        assert SEEN and SEEN[-1]["auth"] == "Bearer sk-test-1234567890", "没带对 Bearer Key"
        assert SEEN[-1]["model"] == "stub-1", "model 传错"

        conn = ai.db()
        row = conn.execute("SELECT * FROM usage WHERE provider='stub' ORDER BY id DESC LIMIT 1").fetchone()
        conn.close()
        assert row and row["ok"] == 1 and row["latency_ms"] >= 0, "用量没记账"
        assert row["pt"] == 7 and row["ct"] == 2, "token 用量没记对"

        # 体检：Key 就位 + ping 打通
        h = ai.providers_health(ping=True)
        stub = [p for p in h["providers"] if p["name"] == "stub"][0]
        assert stub["key"] and stub["ok"] and stub["latency_ms"] >= 0, "ping 体检失败"

        # 拔掉 Key → 应降级到 mock，且体检如实报未配置
        os.environ.pop("FEINAO_STUB_KEY", None)
        ans2, provider2 = ai.llm_chat([{"role": "user", "content": "在吗"}])
        assert provider2 == "mock", "无 Key 时应降级到 mock"
        h2 = ai.providers_health()
        stub2 = [p for p in h2["providers"] if p["name"] == "stub"][0]
        assert stub2["key"] is False and "未配置" in stub2["note"], "无 Key 体检应如实报未配置"
    finally:
        ai.SPEC["providers"] = saved
        os.environ.pop("FEINAO_STUB_KEY", None)
        os.environ.pop("FEINAO_ENV_FILE", None)
        srv.shutdown()
