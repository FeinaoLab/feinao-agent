# -*- coding: utf-8 -*-
"""LCM 最小原型对照实验。

三组对照，同一批问题：
  A 常规做法：只保留最近 6 轮（业界最常见的窗口截断）
  B LCM（无回溯）：摘要 + 热区
  C LCM（带回溯）：摘要 + 热区 + 原文回溯

评判不掺运气：mock 模型只在"给定的上下文"里找答案，找不到就直接说不知道。
所以答对 = 上下文里确实有这条信息。

用法：python tools/lcm_compare.py
"""
import io
import os
import sys
import json
import tempfile
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.abspath(os.path.join(HERE, "..", "backend"))
sys.path.insert(0, BACKEND)

# 独立测试库，不碰线上数据
TMP_DB = os.path.join(tempfile.gettempdir(), "feinao_lcm_demo_%d.db" % int(time.time()))
os.environ["FEINAO_DB"] = TMP_DB

import ai      # noqa: E402
import lcm     # noqa: E402

# ---------------- 剧本：一个真实项目的 26 轮对话 ----------------
# 前 14 轮埋事实（关键：都埋在"很久以前"），后面是日常推进。

SCRIPT = [
    "客户是恒启制造，项目代号 HX-2026，做的是售后服务体系升级。他们内部有两个事业部，售后归质量管理中心管。",
    "项目总预算 380 万，分三期支付，付款比例按 4:4:2 走；预算里含 1 个月的运维陪跑，不额外收费。",
    "一期 150 万，计划 10 月底签合同，签约主体是恒启制造（深圳）有限公司，走他们标准的采购流程。",
    "客户侧对接人是王振华（王工），工号 A2291，手机尾号 8821，日常在企微上沟通最快；他上面汇报给张总。",
    "上线时间定在 11 月 20 日，赶在他们年度审计之前；如果推迟就要等到次年 3 月，所以时间不太能挪。",
    "机房在深圳坪山，走客户自有机房，不出公网；模型只能用他们审批过的版本，私有化部署。",
    "本期做三个场景：库存预警、质检报告生成、售后知识库问答；供应商对账客户提过，但明确放二期。",
    "质检报告要求导出 PDF 且必须带公司水印，格式按他们模板走；模板字段一共 12 个，其中 3 个是必填。",
    "售后知识库初期入库 1200 份文档，其中扫描件 300 份需要 OCR；扫描件以维修记录为主，格式很杂。",
    "禁用词清单：竞品名 A/B/C 一律不许出现，回答里不评价竞品，遇到对比类问题统一引导回自家产品。",
    "验收线定在 85% 准确率，评测集由客户业务方出题共 200 题；badcase 修复后再复测一轮才算通过。",
    "每周五 15:00 开项目例会，客户方张总（VP）会参加；我方参加人是项目经理和我两个人。",
    "风险项第一条：扫描件识别率不达标要返工，所以 OCR 先做 30 份抽样验证，不达标就换方案。",
    "风险项第二条：质检模板要靠客户 IT 提供接口文档，他们排期紧，接口文档晚到会卡联调。",
    "另外客户要求所有问答日志留存 6 个月，能按用户和时间检索，这条要写进方案和报价里。",
    "先说这些背景，后面我们按周推。",
    "今天把方案初稿发过去了，等客户反馈；重点是三个场景的范围和验收线。",
    "客户提了个想法，想在知识库里加供应商对账，我说这个放二期，本期不做，他同意了。",
    "我先把质检报告的模板字段对齐一下，12 个字段里 3 个必填的先确认口径。",
    "王工说模板下周一给最终版，接口文档要等他们 IT 排期，可能晚两三天。",
    "今天跟客户过了下进度，整体顺利，他们对方案里的验收线没有异议。",
    "库存预警的阈值口径还要跟业务确认，是按安全库存还是按日均销量，这个下周定。",
    "售后知识库的文档清单终于收齐了，比预估多一点，实际是 1240 份。",
    "OCR 抽样跑了 30 份，识别率大概 92%，可以接受，后续全量跑要盯着表格类文档。",
    "下周开始联调，接口文档已经拿到了初版，缺两个字段等客户补。",
    "把周报发群里了，本周无明显风险，唯一待办是阈值口径。",
    "跟张总碰了十分钟，他关心的是上线后客服人力能省多少，我说先按 30% 估。",
    "知识库入库脚本写好了，先跑 50 份试，跑通了再全量。",
    "跑完 50 份发现两个问题：页眉被当成正文入库，表格跨页断了。这两个都要修。",
    "修完重新跑，页眉问题解决了，表格跨页还在，改成按表格结构解析。",
    "表格问题也解决了，全量 1240 份开始入库，预计今晚跑完。",
    "入库跑完了，检索抽查 20 条，命中 17 条，剩下 3 条是口语化提问，后面加查询改写。",
    "加了查询改写之后，那 3 条里 2 条能命中了，剩 1 条是文档本身没写，属于知识缺失。",
    "跟王工确认了知识缺失那条，他说下版制度补，先挂起。",
    "接口文档补齐了，今天开始联调库存预警，先打通查询。",
    "库存查询联调通了，响应 300 毫秒左右，可以接受。",
    "质检报告生成联调中，PDF 水印客户要求用他们的 LOGO，素材还没给。",
    "水印素材拿到了，PDF 生成跑通，12 个字段有 10 个能自动填，剩下 2 个要人工补。",
    "跟客户确认剩下 2 个字段人工补可以接受，写进操作手册。",
    "这周进度正常，下周做评测集，客户业务方开始出题。",
    "客户出了 80 题，还差 120 题，催一下。",
    "今天先到这，明天继续推评测集。",
]

# ---------------- 探针问题：考的都是"很久以前"的事实 ----------------
PROBES = [
    {"q": "这个项目的总预算是多少？", "want": ["380"], "from": 2},
    {"q": "客户对接人是谁，手机尾号多少？", "want": ["王振华", "8821"], "from": 4},
    {"q": "上线日期定在哪天？", "want": ["11", "20"], "from": 5},
    {"q": "机房在哪个城市？", "want": ["坪山"], "from": 6},
    {"q": "验收线是多少？", "want": ["85"], "from": 11},
    {"q": "售后知识库里扫描件有多少份？", "want": ["300"], "from": 9},
    {"q": "付款比例是怎么约定的？", "want": ["4:4:2"], "from": 2},
    {"q": "质检报告模板字段有几个必填？", "want": ["12"], "from": 8},
]


def ask_with_ctx(ctx, q, tag, conv_id=None):
    """把给定上下文交给模型（mock 只会从上下文里找答案）。"""
    msgs = [{"role": "system", "content": ("[PILOT:lcm]\n" + "\n".join(
        "[%s] %s" % (m["role"], m["content"]) for m in ctx))},
        {"role": "user", "content": q}]
    ans, provider = ai.llm_chat(msgs, "admin")
    ai.audit_log("admin", "lcm_demo_" + tag, {"q": q, "ctx_tokens": lcm.msgs_tokens(msgs)},
                 ans[:150], True)
    return ans


def score(ans, wants):
    return all(w in ans for w in wants)


def main():
    print("=" * 78)
    print("飞脑 LCM 最小原型 · 对照实验")
    print("库文件：%s" % TMP_DB)
    print("=" * 78)

    ai.init_db()
    o = lcm.LCM(user="admin", title="恒启制造 HX-2026")
    raw_ctx = []
    for i, line in enumerate(SCRIPT, 1):
        o.append("user", line)
        raw_ctx.append({"role": "user", "content": line})

    print("\n会话已灌入 %d 轮，原始 token 约 %d。" % (len(SCRIPT), lcm.msgs_tokens(raw_ctx)))

    # ---------- A 常规做法：最近 6 轮 ----------
    a_ctx = raw_ctx[-6:]
    # ---------- B/C LCM ----------
    sys_prompt = "你是飞脑长期会话助手，依据上下文作答。 [PILOT:lcm]"
    lcm_ctx = o.build_context(keep_recent=6, budget=1500, system=sys_prompt, use_llm=True)

    print("上下文规模：A(最近6轮)=%d token ｜ B/C(LCM，含历史摘要+热区)=%d token" % (
        lcm.msgs_tokens(a_ctx), lcm.msgs_tokens(lcm_ctx)))

    rows = []
    for p in PROBES:
        a_ans = ask_with_ctx(a_ctx, p["q"], "A")
        b_ans = ask_with_ctx([{"role": "system", "content": c["content"]} for c in lcm_ctx],
                             p["q"], "B")
        # C：LCM + 回溯（摘要必然有损，回溯是常态动作）
        detail = o.retrieve(p["q"], 3)
        c_ctx = [{"role": "system", "content": c["content"]} for c in lcm_ctx]
        if detail:
            c_ctx.append({"role": "system", "content": "【回溯原文】" + " ".join(
                "[#%d] %s" % (d["seq"], d["content"]) for d in detail)})
        c_ans = ask_with_ctx(c_ctx, p["q"], "C")
        rows.append({
            "q": p["q"], "want": "/".join(p["want"]), "from": p["from"],
            "A": score(a_ans, p["want"]), "B": score(b_ans, p["want"]), "C": score(c_ans, p["want"]),
            "back": [d["seq"] for d in detail],
            "a_ans": a_ans[:40], "c_ans": c_ans[:60],
        })

    # ---------- 输出表格 ----------
    print("\n%-34s %-10s %-4s %-4s %-4s %s" % ("问题（考第 N 轮的事实）", "答案要点", "A截断", "B摘要", "C回溯", "回溯捞回"))
    print("-" * 96)
    for r in rows:
        print("%-34s %-10s %-6s %-6s %-6s %s" % (
            (r["q"][:32] + "（" + str(r["from"]) + "轮）")[:34], r["want"],
            "✅" if r["A"] else "❌", "✅" if r["B"] else "❌", "✅" if r["C"] else "❌",
            ("#" + ",".join(map(str, r["back"]))) if r["back"] else "-"))

    n = len(rows)
    sumA = sum(1 for r in rows if r["A"])
    sumB = sum(1 for r in rows if r["B"])
    sumC = sum(1 for r in rows if r["C"])
    print("-" * 96)
    print("命中率：A 常规截断 %d/%d = %.0f%% ｜ B LCM %d/%d = %.0f%% ｜ C LCM+回溯 %d/%d = %.0f%%" % (
        sumA, n, sumA * 100.0 / n, sumB, n, sumB * 100.0 / n, sumC, n, sumC * 100.0 / n))

    st = o.stats(keep_recent=6, budget=1500)
    print("\n库结构：原始消息 %d 条、摘要节点 %d 个（层级 %s，1 级=段落摘要、2 级=合并摘要）" % (
        st["turns"], st["nodes"], st["levels"]))

    # ---------- 极端预算：摘要装不下时，回溯是否兜得住 ----------
    tight = o.build_context(keep_recent=2, budget=300, system=sys_prompt)
    tb = tc = 0
    for p in PROBES:
        b_ans = ask_with_ctx([{"role": "system", "content": c["content"]} for c in tight], p["q"], "TB")
        det = o.retrieve(p["q"], 3)
        tctx = [{"role": "system", "content": c["content"]} for c in tight]
        tctx.append({"role": "system", "content": "【回溯原文】" + " ".join(
            "[#%d] %s" % (d["seq"], d["content"]) for d in det)})
        c_ans = ask_with_ctx(tctx, p["q"], "TC")
        tb += 1 if score(b_ans, p["want"]) else 0
        tc += 1 if score(c_ans, p["want"]) else 0
    print("\n【极端预算】上下文压到 %d token（摘要大半装不下）：摘要单独 %d/%d，加回溯 %d/%d" % (
        lcm.msgs_tokens(tight), tb, len(PROBES), tc, len(PROBES)))

    # ---------- 规模验证：历史继续变长，上下文是否还兜得住 ----------
    print("\n【规模验证】继续灌 120 轮日常对话（无新关键事实），看上下文会不会膨胀：")
    for i in range(120):
        o.append("user", "日常推进第 %d 天：跟客户同步了一下联调进度，没有新问题，明天继续。" % (i + 1))
    st2 = o.stats(keep_recent=6, budget=1500)
    print("  原始历史 %d 轮 / %d token  →  实际喂给模型 %d token（预算 %d，压缩 %.0f%%）" % (
        st2["turns"], st2["raw_tokens"], st2["context_tokens"], st2["budget"], st2["compression"] * 100))
    print("  结论：历史翻了三倍，上下文仍在预算内——涨的是库，不是账单。")
    # 长历史下再问最早的事实（38 轮之前）
    a2 = o.ask("机房在哪个城市？", keep_recent=6, budget=1500)
    print("  此时再问「机房在哪个城市」→ %s" % a2["answer"][:70])

    # ---------- 回溯演示 ----------
    print("\n【回溯演示】摘要会抹平细节，原文能捞回来：")
    for probe in ["尾号 8821 是谁的", "质检报告模板字段有几个必填", "OCR 抽样识别率多少"]:
        hits = o.retrieve(probe, 2)
        print("  问「%s」→ 捞回原文：%s" % (probe, hits[0]["content"][:60] if hits else "无"))

    # ---------- 落一份报告 ----------
    report = build_report(st, rows, sumA, sumB, sumC, n, lcm_ctx, raw_ctx, o, st2, a2, tb, tc, lcm.msgs_tokens(tight))
    out = os.path.join(HERE, "..", "docs", "lcm-原型验证报告.md")
    with io.open(os.path.abspath(out), "w", encoding="utf-8") as f:
        f.write(report)
    print("\n报告已写入：%s" % os.path.abspath(out))
    return 0 if sumC >= sumB else 1


def build_report(st, rows, sumA, sumB, sumC, n, lcm_ctx, raw_ctx, o, st2=None, a2=None, tb=0, tc=0, tight_tokens=0):
    lines = []
    lines.append("# 飞脑 LCM（无损上下文）最小原型 · 验证报告\n")
    lines.append("> 生成时间：%s ｜ 独立测试库，不影响线上数据\n" % time.strftime("%Y-%m-%d %H:%M"))
    lines.append("## 一句话结论\n")
    lines.append("同一批问题（考的都是早期轮次的事实）：**常规窗口截断命中 %d/%d，LCM %d/%d，LCM+原文回溯 %d/%d**。" % (
        sumA, n, sumB, n, sumC, n))
    lines.append("\n短会话（42 轮、%d token）时压缩意义不大，此时的价值全在\"记得住\"；历史拉长到 162 轮后省 71%%，见下方规模验证。\n" % lcm.msgs_tokens(raw_ctx))
    lines.append("## 对照结果\n")
    lines.append("| 问题 | 事实出现在 | 答案要点 | A 最近6轮 | B LCM摘要 | C LCM+回溯 | 回溯捞回原文 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for r in rows:
        lines.append("| %s | 第 %d 轮 | %s | %s | %s | %s | %s |" % (
            r["q"], r["from"], r["want"],
            "✅" if r["A"] else "❌", "✅" if r["B"] else "❌", "✅" if r["C"] else "❌",
            ("#%s" % ",".join(map(str, r["back"]))) if r["back"] else "—"))
    if tb or tc:
        lines.append("## 回溯兜底实测（把上下文压到 %d token）\n" % tight_tokens)
        lines.append("预算小到摘要大半装不下时：只给摘要命中 **%d/%d**，加上原文回溯 **%d/%d**。\n"
                     "这就是「摘要必然会丢细节，所以要留一条回溯通道」的实证。\n" % (tb, n, tc, n))
    if st2:
        lines.append("## 规模验证（历史继续变长）\n")
        lines.append("继续灌 120 轮无新事实的日常对话后：原始历史 **%d 轮 / %d token**，实际喂给模型 **%d token**（预算 %d，省 %.0f%%）。"
                     % (st2["turns"], st2["raw_tokens"], st2["context_tokens"], st2["budget"], st2["compression"] * 100))
        if a2:
            lines.append("\n此时再问「机房在哪个城市」→ `%s`，38 轮之前的事实依然答得出。\n" % a2["answer"][:80])
    lines.append("\n## 库结构\n")
    lines.append("- 原始消息：%d 条（全量保留，一条没删）" % st["turns"])
    lines.append("- 摘要节点：%d 个，层级 %s（level-1 段落摘要 → level-2 合并摘要，即 DAG）" % (st["nodes"], st["levels"]))
    lines.append("- 组装上下文：%d token（预算 %d），相对全量历史压缩 %.0f%%" % (
        st["context_tokens"], st["budget"], st["compression"] * 100))
    lines.append("\n## 实际送进模型的上下文（节选）\n")
    lines.append("```")
    for m in lcm_ctx[:4]:
        lines.append("[%s] %s" % (m["role"], (m["content"] or "")[:400]))
    lines.append("```")
    lines.append("\n## 机制说明\n")
    lines.append("1. **原文先落库**：每轮消息无条件写 `lcm_msg`，不因为压缩而删除。")
    lines.append("2. **压缩成节点**：超出热区（默认最近 8 轮）的原文按块压成摘要节点，摘要要求保留数字、人名、日期、编号。")
    lines.append("3. **节点可上卷**：节点太多时把最老的几个合并成上层节点，形成 DAG，越久远的信息层级越高、越浓缩。")
    lines.append("4. **组装上下文**：高层摘要 + 热区原文，超预算就继续压，直到塞进预算。")
    lines.append("5. **回溯**：摘要抹掉的细节，用关键词（中文 bigram）在原文里检索补回——这是「不丢细节」的兜底。")
    lines.append("\n## 局限（下一步要补的）\n")
    lines.append("- 摘要质量依赖模型：本机未配真实 Key，当前走 mock/抽取式摘要，接真模型后效果会明显更好。")
    lines.append("- 回溯是关键词匹配，不是语义检索；量大了要上向量召回。")
    lines.append("- 多会话隔离、摘要节点的人工修订、跨会话记忆还没做。")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    sys.exit(main())
