#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""命令行跑一轮双跑对照（P0-2）：不用开服务，直接读本机配置与库。

用法示例：
  python tools/bench.py --list
  python tools/bench.py --cand-rev 12 --repeats 3
  python tools/bench.py --patch pilots.react_max_steps=8 --note "放宽步数上限"
  python tools/bench.py --patch tool_policies.search_docs.max_calls_per_run=1 --tasks react-01,react-02
  python tools/bench.py --cand-rev 12 --json > bench.json
  python tools/bench.py --compare 7            # 只看作业 #7 的结论行（base vs 候选）
  python tools/bench.py --compare 7,8          # 跨轮比：第一版候选 vs 第二版候选

它做的事和不做的事：
  做：同一批任务在 base 与候选修订上各跑 N 次，指标落库，打印两列对照
  做：跑完直接给一句人话结论（可以上线 / 先别上 / 加样本再判 / 任务集没覆盖），不给总分
  不做：不写磁盘配置、不改 active 标记（候选修订以 active=0 落库，等你看完再决定要不要上线）
  注意：跑的过程中内存配置会在两版之间切换，别在业务高峰期跑
"""
import argparse
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "backend"))

import ai  # noqa: E402
import bench  # noqa: E402


def _w(s):
    """终端显示宽度：中文按 2 列算，否则表格会歪。"""
    return sum(2 if ord(c) > 127 else 1 for c in str(s))


def _pad(s, width, align="<"):
    s = str(s)
    fill = " " * max(0, width - _w(s))
    return fill + s if align == ">" else s + fill


def _print_table(summary):
    base = {p["task_id"]: p for p in summary["base"]["per_task"]}
    cand = {p["task_id"]: p for p in summary["cand"]["per_task"]}
    ids = sorted(set(base) | set(cand))
    cols = [("任务", 13), ("类型", 9), ("通过率 base→cand", 16), ("步数", 8),
            ("工具调用", 9), ("被拒", 6), ("审批", 6), ("证据", 6)]
    head = "  ".join(_pad(c, w) for c, w in cols)
    print(head)
    print("-" * _w(head))
    for tid in ids:
        b, c = base.get(tid, {}), cand.get(tid, {})
        ok = "%s → %s" % (b.get("ok_rate", "-"), c.get("ok_rate", "-"))
        cells = [tid, b.get("kind") or c.get("kind"), ok,
                 "%s→%s" % (b.get("steps", "-"), c.get("steps", "-")),
                 "%s→%s" % (b.get("tool_calls", "-"), c.get("tool_calls", "-")),
                 "%s→%s" % (b.get("denied", "-"), c.get("denied", "-")),
                 "%s→%s" % (b.get("approvals", "-"), c.get("approvals", "-")),
                 "%s→%s" % (b.get("evidence_rate", "-"), c.get("evidence_rate", "-"))]
        print("  ".join(_pad(v, w) for v, (_, w) in zip(cells, cols)))
        for f in (c.get("fails") or [])[:1]:
            print("      ↳ 候选失败原因：%s" % f)


def _print_verdict(out):
    """打结论行（P0-3）：不给总分，只给差异与建议。"""
    v, a, b = out["verdict"], out["a"], out["b"]
    print("对照  作业 #%s/%s（修订 #%s，通过率 %s）  vs  作业 #%s/%s（修订 #%s，通过率 %s）"
          % (a["job"], a["stage"], a["rev"], a["ok_rate"],
             b["job"], b["stage"], b["rev"], b["ok_rate"]))
    print("")
    print("结论：%s" % v["headline"])
    print("建议：%s" % v["suggest"])
    print("")
    for line in v["lines"]:
        print("  · %s" % line)
    changed = [t for t in out["by_task"] if t["verdict"] != "same"]
    if changed:
        label = {"better": "变好", "worse": "变坏", "unclear": "判不了",
                 "new": "只在一侧", "gone": "只在一侧"}
        print("")
        print("有变化的任务：")
        for t in changed:
            print("  %s %s  %s/%s 次 → %s/%s 次  %s"
                  % (_pad(t["task_id"], 12), _pad(label[t["verdict"]], 8),
                     t["ok_n_a"], t["runs_a"], t["ok_n_b"], t["runs_b"],
                     ("；新失败：%s" % t["new_fails"][0]) if t["new_fails"] else ""))
    if v["caveats"]:
        print("")
        for c in v["caveats"]:
            print("注意：%s" % c)


def main(argv=None):
    ap = argparse.ArgumentParser(description="飞脑基准任务集双跑对照")
    ap.add_argument("--list", action="store_true", help="只列任务集，不跑")
    ap.add_argument("--base-rev", type=int, help="基线修订 id，默认当前生效修订")
    ap.add_argument("--cand-rev", type=int, help="候选修订 id（已有修订）")
    ap.add_argument("--patch", action="append", default=[],
                    help="在 base 上改一个键，形如 pilots.react_max_steps=8，可重复")
    ap.add_argument("--tasks", help="只跑这些任务，逗号分隔")
    ap.add_argument("--repeats", type=int, default=3, help="每个任务每版跑几次，默认 3")
    ap.add_argument("--note", default="", help="这轮改动想验证什么")
    ap.add_argument("--by", default="cli", help="操作人（写进审计与修订备注）")
    ap.add_argument("--user", default=None,
                    help="用哪个账号执行任务（默认 admin；指定 viewer 可专门测越权路径）")
    ap.add_argument("--compare", help="只出结论：作业 id，或两个作业 a,b（跨轮比）")
    ap.add_argument("--json", action="store_true", help="输出原始 JSON（给脚本用）")
    a = ap.parse_args(argv)

    ai.init_db()
    bench.init_db()

    if a.compare:
        ids = [x.strip() for x in a.compare.split(",") if x.strip()]
        if not ids or len(ids) > 2 or not all(i.isdigit() for i in ids):
            print("--compare 要写成 <作业id> 或 <作业id>,<作业id>")
            return 2
        out = bench.compare(int(ids[0]), int(ids[1]) if len(ids) > 1 else None)
        if a.json:
            print(json.dumps(out, ensure_ascii=False, indent=2))
            return 0 if not out.get("error") else 1
        if out.get("error"):
            print("比不了：%s" % out["error"])
            return 1
        _print_verdict(out)
        return 0

    if a.list:
        for t in bench.load_tasks():
            print("%-12s %-9s %s" % (t["id"], t["kind"], t.get("title") or t.get("goal") or t.get("q")))
        return 0

    if not a.cand_rev and not a.patch:
        print("要么 --cand-rev（已有修订），要么 --patch（在 base 上改几个键）。先用 --list 看看任务集。")
        return 2

    patches = []
    for raw in a.patch:
        if "=" not in raw:
            print("--patch 要写成 path=value，收到：%s" % raw)
            return 2
        path, val = raw.split("=", 1)
        try:
            val = json.loads(val)
        except ValueError:
            pass  # 不是合法 JSON 就当字符串（比如 "admin,ops" 这种就别写了）
        patches.append({"path": path.strip(), "value": val})

    if ai.demo_enabled():
        import demo
        port = demo.start_upstream()
        demo.seed_docs()
        # 走 stderr：--json 时 stdout 必须只有 JSON，否则脚本没法直接吃
        sys.stderr.write("[demo] 假上游 127.0.0.1:%d（FEINAO_DEMO=0 可切到 spec.json 里的真系统）\n" % port)

    r = bench.run_job(base_rev=a.base_rev, cand_rev=a.cand_rev, patch=patches or None,
                      task_ids=[x.strip() for x in (a.tasks or "").split(",") if x.strip()] or None,
                      repeats=a.repeats, note=a.note, by=a.by, user=a.user)
    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0 if r.get("ok") else 1
    if r.get("error") and not r.get("job_id"):
        print("跑不了：%s" % r["error"])
        return 1

    print("")
    print("基准任务集双跑：作业 #%s  任务 %d 个 × 每版 %d 次"
          % (r["job_id"], r["tasks"], r["repeats"]))
    print("base 修订 #%s（%s）  vs  候选修订 #%s（%s）"
          % (r["base_rev"], r["base_sha"], r["cand_rev"], r["cand_sha"]))
    print("")
    _print_table(r["summary"])
    b, c, d = r["summary"]["base"], r["summary"]["cand"], r["summary"]["delta"]
    print("")
    print("汇总  通过率 %s → %s（差 %+g）  证据完整率 %s → %s  被拒 %s → %s  审批交接 %s → %s"
          % (b["ok_rate"], c["ok_rate"], d["ok_rate"], b["evidence_rate"], c["evidence_rate"],
             b["denied"], c["denied"], b["approvals"], c["approvals"]))
    v = r.get("verdict") or {}
    if v.get("headline"):
        print("")
        print("结论：%s" % v["headline"])
        print("建议：%s" % v["suggest"])
        for line in v.get("lines") or []:
            print("  · %s" % line)
        for cv in v.get("caveats") or []:
            print("注意：%s" % cv)
    print("配置已还原：%s（磁盘 spec.json 未被改动）" % ("是" if r["restored"] else "否——请立刻检查！"))
    if r.get("error"):
        print("注意：%s" % r["error"])
    print("逐条记录：GET /api/bench/detail?id=%s ，或直接看库里的 bench_run 表" % r["job_id"])
    return 0 if r.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
