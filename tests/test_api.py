# -*- coding: utf-8 -*-
"""飞脑 Agent 网关 API 集成测试：真实起服 + mock LLM 全链路断言。"""
import json
import os
import subprocess
import sys
import time
import urllib.request

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8060
BASE = "http://127.0.0.1:%d" % PORT

# 本套测试断言依赖 mock 的确定性输出：强制屏蔽真实 Key（.env 只补缺失的环境变量，
# 这里显式置空即视为"无 Key"，供应商链会自动降级到 mock）。真实模型链路由
# test_provider.py（本地协议桩）和 tools/lcm_compare.py（真模型对照）负责。
for _k in ("FEINAO_QWEN_KEY", "FEINAO_DEEPSEEK_KEY", "FEINAO_HUNYUAN_KEY"):
    os.environ[_k] = ""


def req(path, method="GET", body=None, token=None):
    r = urllib.request.Request(BASE + path, method=method)
    if body is not None:
        r.data = json.dumps(body).encode()
        r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(r, timeout=30) as x:
            return x.status, json.loads(x.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8", "ignore") or "{}")


def test_full():
    proc = subprocess.Popen([sys.executable, "server.py"], cwd=os.path.join(BASE_DIR, "backend"),
                            env=dict(os.environ, FEINAO_DB=os.path.join(BASE_DIR, "tests", "test.db")))
    try:
        for _ in range(20):
            time.sleep(0.5)
            try:
                st, j = req("/api/health")
                if st == 200:
                    break
            except Exception:
                continue
        assert st == 200 and j["status"] == "ok", "health fail"

        st, j = req("/api/login", "POST", {"username": "admin", "password": "bad"})
        assert st == 401, "bad login should 401"

        st, j = req("/api/login", "POST", {"username": "admin", "password": "admin123"})
        assert st == 200 and j.get("token"), "login fail"
        tok = j["token"]

        st, j = req("/api/dashboard", token=tok)
        assert st == 200 and "counts" in j, "dashboard fail"

        # rag ingest + search
        st, j = req("/api/rag/ingest", "POST",
                    {"title": "飞系部署规范", "body": "飞系系统使用 systemd 部署，nginx 统一入口，零依赖 Python 标准库。"}, tok)
        assert st == 200, "ingest fail"
        st, j = req("/api/rag/search", "POST", {"q": "部署"}, tok)
        assert st == 200 and len(j["hits"]) >= 1, "rag search fail"

        # 试点1 知识问答（mock LLM）
        st, j = req("/api/ai/ask", "POST", {"q": "飞系系统怎么部署？"}, tok)
        assert st == 200 and j.get("answer") and j["sources"], "pilot ask fail"

        # 试点3 客服话术
        st, j = req("/api/ai/script", "POST", {"scenario": "客户嫌贵", "tone": "专业友好"}, tok)
        assert st == 200 and "话术" in j.get("script", ""), "pilot script fail"

        # 试点2 NL2API：上游不可达时应优雅报错（本地测试无 bi 上游）
        st, j = req("/api/ai/nl2api", "POST", {"q": "查一下商品数据", "system": "bi"}, tok)
        assert st == 200 and ("error" in j or "entity" in j), "pilot nl2api should degrade gracefully"

        # 自由对话 + 会话落库
        st, j = req("/api/chat", "POST", {"messages": [{"role": "user", "content": "你好飞脑"}]}, tok)
        assert st == 200 and j.get("answer"), "chat fail"
        st, j = req("/api/conversations", token=tok)
        assert st == 200 and len(j["rows"]) >= 1, "conversations fail"

        # ---- 线上 LCM 链路：服务端持有记忆，前端只发本轮文本 ----
        st, j = req("/api/chat", "POST", {"text": "项目预算 380 万，对接人王振华，机房在深圳坪山。"}, tok)
        assert st == 200 and j.get("conv_id") and "context_tokens" in j, "lcm chat fail"
        conv = j["conv_id"]

        # 灌 24 轮日常对话，把早期事实挤出热区
        for i in range(24):
            st, j = req("/api/chat", "POST",
                        {"text": "第 %d 天：跟客户同步进度，无异常。" % (i + 1), "conv_id": conv}, tok)
            assert st == 200, "lcm long chat fail at %d" % i

        # 早期事实仍然答得出（摘要/回溯兜底）
        st, j = req("/api/chat", "POST", {"text": "项目预算是多少？", "conv_id": conv}, tok)
        assert st == 200 and "380" in j.get("answer", ""), "lcm 早期事实答不上来：%s" % j.get("answer")
        assert j["stats"]["turns"] >= 26, "turns 统计不对"
        assert j["stats"]["context_tokens"] <= j["stats"]["budget"], "上下文超预算"

        # 会话列表带轮次
        st, j = req("/api/lcm_convs", token=tok)
        assert st == 200 and any(r["id"] == conv and r["turns"] >= 26 for r in j["rows"]), "lcm_convs fail"

        # 供应商体检（无 Key 时应如实报未配置，mock 兜底可用）
        st, j = req("/api/providers/health", token=tok)
        assert st == 200 and "deepseek" in j.get("chain", []), "providers health fail"
        assert any(p["name"] == "mock" and p["ok"] for p in j["providers"]), "mock 应可用"

        # ReAct（mock 下一步直接回答）
        st, j = req("/api/ai/react", "POST", {"task": "总结飞系架构"}, tok)
        assert st == 200 and j.get("answer"), "react fail"

        # 审计
        st, j = req("/api/audit", token=tok)
        assert st == 200 and len(j["rows"]) >= 3, "audit fail"

        # viewer 无审计权限
        st, j = req("/api/login", "POST", {"username": "view01", "password": "view123456"})
        vtok = j["token"]
        st, j = req("/api/audit", token=vtok)
        assert st == 403 or st == 401, "viewer should not see audit"

        # 静态页
        import urllib.request as ur
        assert ur.urlopen(BASE + "/").status == 200
        assert ur.urlopen(BASE + "/m/").status == 200
        assert ur.urlopen(BASE + "/m/config.js").status == 200
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
