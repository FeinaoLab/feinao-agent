# -*- coding: utf-8 -*-
"""Self-contained demo fixtures (zero dependency, no network, deterministic).

The gateway is designed to talk to three upstream REST systems (a KMS, a BI
service and a support desk). Those are private deployments, so a plain
`git clone` would leave the "smart query" pilot dead on arrival. This module
ships a deterministic stand-in instead:

  * a tiny read-only HTTP service that speaks the same `api/login` +
    `api/spec` + `api/<entity>` contract, mounted under :8099 by default;
  * a handful of knowledge-base documents so the RAG pilot has something to
    cite on first boot.

Everything is in-memory and generated from the constants below, so a demo run
is reproducible and needs no fixtures on disk.

Turn it off with FEINAO_DEMO=0 (production deployments point at the real
upstreams declared by `base` in spec.json).
"""
from __future__ import annotations

import json
import os
import socket
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_PORT = 8099

# ---------------------------------------------------------------- upstream data
# Each system exposes `entities` via api/spec and rows via api/<entity>.
# Rows are fixed rather than random so screenshots, docs and tests agree.

_ORDERS = [
    {"id": 1001, "customer": "华南制造集团", "amount": 128000, "status": "已付款", "region": "华南", "date": "2026-09-03"},
    {"id": 1002, "customer": "云启科技", "amount": 46800, "status": "已发货", "region": "华东", "date": "2026-09-05"},
    {"id": 1003, "customer": "北方物流", "amount": 92000, "status": "待付款", "region": "华北", "date": "2026-09-08"},
    {"id": 1004, "customer": "智联零售", "amount": 23500, "status": "已付款", "region": "华东", "date": "2026-09-11"},
    {"id": 1005, "customer": "西部能源", "amount": 310000, "status": "已发货", "region": "西北", "date": "2026-09-14"},
    {"id": 1006, "customer": "华南制造集团", "amount": 67000, "status": "已付款", "region": "华南", "date": "2026-09-19"},
    {"id": 1007, "customer": "东岸传媒", "amount": 15400, "status": "已退款", "region": "华东", "date": "2026-09-22"},
    {"id": 1008, "customer": "云启科技", "amount": 88000, "status": "已付款", "region": "华东", "date": "2026-09-27"},
    {"id": 1009, "customer": "中原农业", "amount": 41200, "status": "待付款", "region": "华中", "date": "2026-10-02"},
    {"id": 1010, "customer": "西部能源", "amount": 156000, "status": "已付款", "region": "西北", "date": "2026-10-06"},
    {"id": 1011, "customer": "北方物流", "amount": 7300, "status": "已发货", "region": "华北", "date": "2026-10-11"},
    {"id": 1012, "customer": "智联零售", "amount": 128900, "status": "已付款", "region": "华东", "date": "2026-10-15"},
]

_CUSTOMERS = [
    {"id": 201, "name": "华南制造集团", "tier": "KA", "industry": "制造业", "owner": "ops01", "since": "2023-04"},
    {"id": 202, "name": "云启科技", "tier": "A", "industry": "软件", "owner": "ops01", "since": "2024-01"},
    {"id": 203, "name": "北方物流", "tier": "A", "industry": "物流", "owner": "admin", "since": "2024-06"},
    {"id": 204, "name": "智联零售", "tier": "B", "industry": "零售", "owner": "ops01", "since": "2025-02"},
    {"id": 205, "name": "西部能源", "tier": "KA", "industry": "能源", "owner": "admin", "since": "2022-11"},
    {"id": 206, "name": "东岸传媒", "tier": "B", "industry": "传媒", "owner": "ops01", "since": "2025-08"},
    {"id": 207, "name": "中原农业", "tier": "C", "industry": "农业", "owner": "admin", "since": "2026-03"},
]

_ARTICLES = [
    {"id": 1, "title": "新员工入职指引", "author": "HR", "status": "已发布", "updated": "2026-08-12"},
    {"id": 2, "title": "费用报销标准与流程", "author": "财务", "status": "已发布", "updated": "2026-07-30"},
    {"id": 3, "title": "客户合同审批要点", "author": "法务", "status": "已发布", "updated": "2026-06-18"},
    {"id": 4, "title": "线上故障分级与响应", "author": "运维", "status": "已发布", "updated": "2026-09-02"},
    {"id": 5, "title": "API 网关接入规范", "author": "架构组", "status": "评审中", "updated": "2026-10-09"},
    {"id": 6, "title": "数据分级与脱敏规则", "author": "安全", "status": "已发布", "updated": "2026-09-21"},
    {"id": 7, "title": "供应商准入清单", "author": "采购", "status": "草稿", "updated": "2026-10-14"},
]

_TICKETS = [
    {"id": 5001, "subject": "登录后提示无权限", "priority": "P1", "status": "处理中", "owner": "ops01"},
    {"id": 5002, "subject": "报表导出为空", "priority": "P2", "status": "待接入", "owner": "ops01"},
    {"id": 5003, "subject": "发票信息需要变更", "priority": "P3", "status": "已解决", "owner": "admin"},
    {"id": 5004, "subject": "批量导入超时", "priority": "P1", "status": "处理中", "owner": "admin"},
    {"id": 5005, "subject": "手机端白屏", "priority": "P2", "status": "待接入", "owner": "ops01"},
    {"id": 5006, "subject": "历史消息查不到", "priority": "P2", "status": "已解决", "owner": "ops01"},
]

# 与生产飞瞰 BI 的实体对齐（sources/metrics），基准任务在新旧两套环境都能跑
_SOURCES = [
    {"id": 1, "name": "订单主库", "type": "MySQL", "status": "在线", "owner": "admin"},
    {"id": 2, "name": "行为日志", "type": "Kafka", "status": "在线", "owner": "ops01"},
    {"id": 3, "name": "财务中台", "type": "API", "status": "在线", "owner": "admin"},
]

_METRICS = [
    {"id": 1, "name": "日活跃用户数", "caliber": "DAU", "owner": "admin", "status": "生效"},
    {"id": 2, "name": "支付成功率", "caliber": "成功笔数/总笔数", "owner": "admin", "status": "生效"},
    {"id": 3, "name": "客单价", "caliber": "GMV/下单人数", "owner": "ops01", "status": "生效"},
    {"id": 4, "name": "退货率", "caliber": "退货单/出货单", "owner": "ops01", "status": "生效"},
]

SYSTEMS = {
    "kms": {
        "label": "KMS documentation",
        "entities": [{"key": "article", "label": "知识文章", "desc": "title/author/status"}],
        "rows": {"article": _ARTICLES},
    },
    "bi": {
        "label": "BI dataset",
        "entities": [
            {"key": "order", "label": "订单", "desc": "customer/amount/status/region"},
            {"key": "customer", "label": "客户", "desc": "name/tier/industry/owner"},
            {"key": "sources", "label": "数据源", "desc": "name/type/status/owner"},
            {"key": "metrics", "label": "指标字典", "desc": "name/caliber/owner/status"},
        ],
        "rows": {"order": _ORDERS, "customer": _CUSTOMERS,
                 "sources": _SOURCES, "metrics": _METRICS},
    },
    "cc": {
        "label": "Support desk",
        "entities": [{"key": "ticket", "label": "工单", "desc": "subject/priority/status/owner"}],
        "rows": {"ticket": _TICKETS},
    },
}

TOKEN_PREFIX = "demo-token-"


# ---------------------------------------------------------------- seed documents
# Enough coverage that `?mode=rag` returns a citable hit for the sample
# questions in README (报销 / 故障 / 数据脱敏 / 权限).

SEED_DOCS = [
    ("费用报销标准与流程",
     "差旅报销：市内交通凭票实报，单次不超过 200 元；住宿一线城市每晚 600 元、二线 450 元、其他 350 元。"
     "业务招待需事前审批，单次上限 2000 元并附事由与参与人。报销单每月 15 日前提交，财务 3 个工作日内审核。"
     "超标准部分需部门负责人书面说明，否则不予报销。"),
    ("线上故障分级与响应",
     "P0：核心交易不可用或数据丢失风险，5 分钟内在值班群拉起，30 分钟内给出止损方案。"
     "P1：主流程降级但可用，15 分钟内响应，2 小时内恢复。P2：非核心功能异常，24 小时内处理。"
     "P3：体验与文案问题，排入迭代。所有 P0/P1 必须在 48 小时内产出复盘，写明时间线、根因、改进项与责任人。"),
    ("数据分级与脱敏规则",
     "数据分四级：L1 公开、L2 内部、L3 敏感、L4 核心。L3 及以上禁止出现在测试环境与截图，"
     "手机号中间四位掩码，身份证仅保留前 6 后 4，银行卡仅保留后 4 位。"
     "对外提供数据须经数据 owner 审批并留存用途说明，日志中不得打印明文凭证。"),
    ("API 网关接入规范",
     "所有对上游系统的调用必须经网关，禁止业务侧直连数据库。每个动作在 spec.json 的 tool_policies 中声明"
     "roles / max_calls_per_run / requires_confirm 三项策略：只读动作用 max_calls_per_run 限流，"
     "写动作一律 requires_confirm=true 走审批。网关对上游要求 api/login 返回 token，后续请求用 Bearer 头。"),
    ("新员工入职指引",
     "入职前一周 HR 发送材料清单；入职当天领取设备并完成账号开通（邮箱、Git、VPN、内部系统三件套）。"
     "试用期 3 个月，第 1 周完成安全培训与业务地图学习，第 1 个月与主管做目标对齐，第 3 个月做转正答辩。"),
    ("客户合同审批要点",
     "合同金额 50 万以下由销售总监审批，50 万至 200 万加签财务与法务，200 万以上需总经理审批。"
     "付款条件不得优于「3-6-1」（预付款 30%、交付 60%、验收 10%）。违约金上限不超过合同总额 20%。"
     "涉及数据出境的条款必须由安全团队会签。"),
    ("供应商准入清单",
     "准入需提供营业执照、近一年审计报告、信息安全承诺书三项材料。涉及个人信息处理的供应商还需提供"
     "等保备案证明与数据处理协议。准入有效期 2 年，到期前 30 天发起复评。"),
    ("客服响应标准与话术要点",
     "在线咨询首响 30 秒内，人工接入 3 分钟内；工单 P1 15 分钟响应、2 小时内给出方案。"
     "发货时效：现货 24 小时内出库，预售按商品页承诺时间；物流轨迹超过 48 小时未更新，"
     "须主动联系承运商并向客户说明进度。客户投诉先共情再定责，未核实前不得承诺赔付。"
     "退换货 7 天无理由，质量问题 30 天内可换，运费由责任方承担。"),
    ("飞脑网关能力说明",
     "飞脑是一个零依赖的 Agent 网关参考实现：多供应商按序降级、知识问答带引用、自然语言查数、"
     "ReAct 工具调用、LCM 无损上下文、证据链、动作级权限与预算、任务进度落库、审批闸门、跨会话长期记忆。"
     "所有面向用户的回答要求结论附依据，无法验证的部分必须标注未验证。"),
]


# ---------------------------------------------------------------- mock upstream
class _Handler(BaseHTTPRequestHandler):
    server_version = "FeiNaoDemoUpstream/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # keep demo output clean
        pass

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _parts(self):
        return [p for p in urllib.parse.urlparse(self.path).path.split("/") if p]

    def do_POST(self):
        parts = self._parts()
        # POST /<system>/api/login
        if len(parts) == 3 and parts[1] == "api" and parts[2] == "login":
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
            except Exception:
                body = {}
            if not body.get("username"):
                return self._json(401, {"error": "username/password required"})
            return self._json(200, {"token": TOKEN_PREFIX + parts[0], "expires_in": 3600})
        return self._json(404, {"error": "not found"})

    def do_GET(self):
        parts = self._parts()
        if len(parts) < 3 or parts[1] != "api":
            return self._json(404, {"error": "not found"})
        sysname = parts[0]
        sysdef = SYSTEMS.get(sysname)
        if not sysdef:
            return self._json(404, {"error": "unknown system: %s" % sysname})
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return self._json(401, {"error": "token required"})

        if parts[2] == "spec":
            return self._json(200, {"system": sysname, "label": sysdef["label"],
                                    "entities": sysdef["entities"]})

        entity = parts[2]
        rows = sysdef["rows"].get(entity)
        if rows is None:
            return self._json(404, {"error": "unknown entity: %s" % entity})
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        q = (qs.get("q", [""])[0] or "").strip()
        try:
            limit = int(qs.get("limit", ["20"])[0])
        except ValueError:
            limit = 20
        out = rows
        if q:
            ql = q.lower()
            out = [r for r in rows
                   if any(ql in str(v).lower() for v in r.values())]
        return self._json(200, {"rows": out[:max(1, min(limit, 200))],
                               "total": len(out)})


def _port_open(port, host="127.0.0.1"):
    s = socket.socket()
    s.settimeout(0.2)
    try:
        return s.connect_ex((host, port)) == 0
    finally:
        s.close()


def start_upstream(port=None):
    """Start the stand-in upstream on a daemon thread. Returns the port.

    Idempotent: if something is already listening on the port (another test
    process, a previous run) we reuse it instead of failing.
    """
    port = int(port or os.environ.get("FEINAO_DEMO_PORT") or DEFAULT_PORT)
    if _port_open(port):
        return port
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    except OSError:
        return port
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, name="feinao-demo-upstream",
                     daemon=True).start()
    return port


def seed_docs():
    """Insert the sample knowledge base once. Safe to call repeatedly."""
    import ai  # local import: demo is loaded from engine, after ai is ready

    conn = ai.db()
    try:
        n = conn.execute("SELECT COUNT(*) c FROM docs WHERE source='demo'").fetchone()["c"]
    finally:
        conn.close()
    if n:
        return 0
    # rag_ingest also maintains the FTS5 mirror, so reuse it instead of raw INSERT.
    for title, body in SEED_DOCS:
        ai.rag_ingest(title, body, "demo")
    return len(SEED_DOCS)
