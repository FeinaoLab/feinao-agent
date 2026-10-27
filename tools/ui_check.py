# -*- coding: utf-8 -*-
"""飞脑 UI 巡检（零依赖）：API 探活 + 无头浏览器工作台截图。
用法：python tools/ui_check.py [端口]   # 默认取 spec.json 端口
产物：tools/shots/home.png（工作台截图），控制台输出巡检结论。"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else json.load(
    open(os.path.join(BASE_DIR, "backend", "spec.json"), encoding="utf-8")
)["brand"]["port"]
BASE = "http://127.0.0.1:%d" % PORT
SHOTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shots")

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append("%s %s %s" % ("✅" if ok else "❌", name, detail))


def get(path):
    try:
        with urllib.request.urlopen(BASE + path, timeout=5) as r:
            return r.status, r.read()
    except Exception as e:
        return 0, str(e).encode()


# 1. API 探活
code, body = get("/api/health")
check("health", code == 200 and b'"ok"' in body, code)
code, body = get("/api/providers")
ok = code == 200 and b'"providers"' in body
check("providers", ok, code)
if ok:
    body_json = json.loads(body.decode("utf-8"))
    check("品牌名", True, "providers=" + ",".join(x["name"] for x in body_json.get("providers", [])))
code, _ = get("/")
check("门店页", code == 200, code)
code, _ = get("/app.js")
check("前端脚本", code == 200, code)
code, _ = get("/icon.svg")
check("Logo", code == 200, code)

# 2. 无头浏览器截图（找 Edge/Chrome）
browser = ""
for p in (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
):
    if os.path.exists(p):
        browser = p
        break
if browser:
    os.makedirs(SHOTS, exist_ok=True)
    shot = os.path.join(SHOTS, "home.png")
    profile = tempfile.mkdtemp(prefix="fei_ui_profile_")  # 浏览器缓存进系统临时目录，不污染项目
    try:
        r = subprocess.run(
            [browser, "--headless=new", "--disable-gpu", "--no-first-run",
             "--user-data-dir=" + profile, "--force-device-scale-factor=1",
             "--window-size=1280,860", "--virtual-time-budget=8000",
             "--screenshot=" + shot, BASE + "/"],
            capture_output=True, timeout=60,
        )
        check("门店截图", os.path.exists(shot) and os.path.getsize(shot) > 10000, shot)
    finally:
        shutil.rmtree(profile, ignore_errors=True)
else:
    check("门店截图", False, "未找到 Edge/Chrome")

print("\n".join(PASS + FAIL))
print("巡检结论：%d 通过 / %d 失败" % (len(PASS), len(FAIL)))
sys.exit(1 if FAIL else 0)
