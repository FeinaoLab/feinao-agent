# 飞脑 — 可审计优先的 Agent 网关

> 一个入口 · 智达全系

[English](README.md) · **中文**

绝大多数 Agent 演示告诉你模型**说了什么**；这个项目给你看实际**发生了什么**、以及**谁批的**。

飞脑是一个零依赖（Python 标准库 + SQLite FTS5）的 Agent 网关参考实现：多供应商降级、
知识问答带引用、自然语言查数、ReAct 工具调用、LCM 无损上下文、证据链、动作级权限与预算、
任务进度落库、审批闸门、跨会话长期记忆。

## 三条命令跑起来

```bash
git clone https://github.com/dukema81/feinao-agent.git && cd feinao-agent
python backend/server.py          # 零依赖，不需要 pip install
# 浏览器开 http://127.0.0.1:8060/ ，三角色：admin/admin123 · ops01/ops123456 · view01/view123456
```

默认就是**演示模式**：内置假上游（:8099，讲和生产一致的 `api/login` + `api/spec` + `api/<entity>` 契约）
+ 9 篇种子知识文档，三个试点都能点、能出引用、能走审批。想换成真回答，在 `.env` 里填一个
LLM Key（`FEINAO_QWEN_KEY` / `FEINAO_DEEPSEEK_KEY` / `FEINAO_HUNYUAN_KEY`）；一个都不填时自动
降级到 mock，功能全在、回答质量弱。

<p align="center">
  <img src="docs/screenshot-chat.png" width="860" alt="知识问答带引用（演示模式，mock）">
  <br><sub>开箱即用的知识问答：演示数据 + mock 模型，回答带引用。</sub>
</p>
<p align="center">
  <img src="docs/screenshot-approvals.png" width="860" alt="飞审审批工作台">
  <br><sub>审批闸门：写操作带着工具、参数、发起人、任务目标停在待审批区，点「放行并执行」才真跑。</sub>
</p>

## 核心能力

- **LLM 多供应商网关**：DeepSeek / 混元 / qwen / mock 按序降级重试（combo 链），用量落库（usage 表）；Key 支持环境变量与 `.env`，无 Key 自动降级 mock
- **供应商体检**：`GET /api/providers/health[?ping=1]` —— 只回 `hasKey` / `fingerprint`（末四位）/ `key_len`，**任何响应都不出现完整 Key**；ping=1 时真发一次最小请求验证有效性与延迟，错误信息过一遍脱敏
- **试点1 · 知识问答（RAG）**：SQLite FTS5 检索（LIKE 兜底）+ 带引用回答
- **试点2 · 智能查数（NL2API）**：自然语言 → 实体匹配（spec 驱动）→ 上游 REST 查询 → LLM 总结；不生成裸 SQL，天然防注入
- **试点3 · 客服话术**：场景 → 五段式话术生成（可挂知识库参考）
- **L2 ReAct Agent**：工具注册表（search_docs / query_system / ingest_doc）+ 步数上限
- **LCM 无损上下文（长期会话）**：原文全量落库 + 分块摘要 + 摘要上卷成 DAG + 关键词回溯原文；上下文受 token 预算约束，历史再长也不膨胀
- **验证型收尾要求（VERIFY_TAIL）**：所有面向用户的回答（rag / react / LCM / 兼容链路）系统提示强制要求「结论附依据、产出物给位置、未验证要标注」——完成必须是可验证产物，不是模型自述；有测试守卫（`tests/test_provider.py`）
- **证据链（v1.3）**：每次回答落 `evidence` 表——真实命中的文档/回溯轮次、工具调用链、上下文规模、是否标注未验证；由代码记录"实际发生了什么"，与模型自述互相独立。接口 `GET /api/evidence?conv_id=N`
- **动作级权限/预算（v1.3）**：`spec.json` 的 `tool_policies` 按动作声明 `roles` / `max_calls_per_run` / `requires_confirm`，ReAct 强制执行；拒绝不静默，原因喂回模型并落 `denied` 步骤
- **任务进度落库（v1.3）**：`tasks` + `task_steps`（tool_call / tool_result / denied / pending / error / answer / replay），步数耗尽保持 `active` 可 `task_id` 断点续跑；接口 `GET /api/tasks`、`GET /api/tasks/detail?id=N`
- **飞审审批工作台（v1.4）**：`requires_confirm` 的真闸门——写操作自动挂审批单（工具/参数/发起人/任务目标），人工「放行并执行」后系统真执行该动作并让任务自动续跑，拒绝则动作不执行、理由回给模型；重复提交防扰 + 幂等回放；接口 `GET /api/approvals`、`POST /api/approvals/decide`（详见 `docs/飞审审批工作台-上线说明.md`）
- **飞忆跨会话长期记忆（v1.5）**：从会话抽取事实/决定/偏好/产出物，默认待确认、人工点头才生效；新会话按关键词检索注入（带 M#编号/日期/来源会话），命中不到不注入；三道过滤（反虚构 grounding、过程噪声、去重+新值取代旧值）；接口 `GET /api/memory`、`POST /api/memory/decide`、`POST /api/memory/extract`（详见 `docs/飞忆跨会话记忆-上线说明.md`）
- **飞构·配置版本化（v1.7）**：`spec.json` 有历史、能对比、可回滚——起服自动快照（幂等），语义 diff 精确到键（改缩进不算改动），回滚=原子写盘+热加载不重启，脏 JSON 拒绝上线；admin 专属，回滚必留审计；接口 `GET /api/harness`、`POST /api/harness/{snapshot,activate}`（详见 `docs/Harness版本化-上线说明.md`）
- **演示自足化（v1.6）**：`backend/demo.py` 提供 KMS / BI / 工单三个假上游与 9 篇种子文档；`FEINAO_DEMO=1`（默认）时随服务启动，`FEINAO_DEMO=0` 时走 `spec.json` 里每个上游的 `base`
- **审计与配额**：全工具/LLM 调用写 audit；viewer 无审计权限

## 接自己的上游

上游只需三个端点：

```
POST api/login   {username,password} → {token}
GET  api/spec    → {entities:[{key,label,desc}]}
GET  api/<entity>?limit=20&q=关键词 → {rows:[...], total:N}
```

然后：

```bash
FEINAO_DEMO=0
FEINAO_UPSTREAM_KMS=http://your-kms:8050/kms/
FEINAO_UPSTREAM_BI=http://your-bi:8041/bi/
FEINAO_UPSTREAM_CC=http://your-desk:8043/cc/
```

环境变量优先级最高，其次 `demo_base`（演示模式），最后 `base`。

## LCM 无损上下文（已接线上）

对话主链路已默认走 LCM：前端只发本轮文本 + `conv_id`，历史摘要、热区原文、回溯片段都在服务端组装，会话记忆不再依赖前端回传全量 messages（旧 `{messages}` 调用保持兼容）。

```bash
python tools/lcm_compare.py      # 对照实验：常规截断 vs LCM vs LCM+回溯
```

实测结果（详见 `docs/lcm-原型验证报告.md`）：
- 42 轮会话、考早期事实：常规"只留最近 6 轮"命中 **0/8**，LCM **8/8**
- 上下文压到 161 token 的极端预算：只给摘要 0/8，**加上原文回溯 8/8**
- 历史拉长到 162 轮：原始 4621 token → 实际喂模型 1283 token（省 **72%**）

接口：
- `POST /api/chat {text, conv_id?}` —— 线上对话主链路（LCM）
- `GET /api/lcm_convs` —— 会话列表（带轮次与最后一句）
- `GET /api/lcm?conv_id=` —— 库结构统计与摘要节点（DAG 视图）
- `POST /api/ai/lcm {text, conv_id?}` —— LCM 直连（含完整 stats）
- `POST /api/ai/lcm_detail {conv_id, q}` —— 回溯原文

## 测试

```bash
python -m pytest tests/ -q     # 38 项：API 全链路 + LCM + 审批 + 记忆 + 演示上游契约 + 供应商真实链路
```

CI（`.github/workflows/ci.yml`）在 Python 3.9 / 3.11 / 3.13 上跑全套，另有一个
"不装任何依赖、clone 后直接起服务并问一句带引用的话"的冒烟 job。

## 目录

- `backend/` — `engine.py`（HTTP）+ `ai.py`（网关/RAG/试点/ReAct/审批）+ `lcm.py`（无损上下文）+ `memory.py`（跨会话记忆）+ `demo.py`（内置假上游与种子数据）+ `spec.json`
- `frontend/` — 对话工作台 SPA（对话 + 三试点 + 审计看板 + 飞审 + 飞忆）
- `mobile/` — PWA（对话 + 模式切换）
- `tests/` — API 集成测试（真实起服全链路）+ LCM + 审批 + 记忆 + 演示契约
- `docs/` — 建设方案、LCM 验证报告、各能力上线说明、[Harness² 对照与差距清单](docs/Harness2-对照与差距清单.md)、[Harness 版本化上线说明](docs/Harness版本化-上线说明.md)
- `tools/` — Logo + UI 巡检 + LCM 对照实验脚本

## 部署前必读

这是**参考实现**，不是加固过的产品。至少做三件事：

1. 改掉出厂口令 `admin123 / ops123456 / view123456`（服务启动检测到会打印 `[warn] default password in use`）
2. 不要把 :8060 直接暴露公网，前置 nginx 做 TLS 与来源限制
3. 设 `FEINAO_DEMO=0`，否则真上游永远不会被调用

详见 [SECURITY.md](SECURITY.md)，贡献约定见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 生产部署（参考）

systemd（8060）+ nginx `/nao/`（桌面）与 `/nao/m/`（手机）。真实 Key 放 `/opt/feinao-agent/.env`
（参考 `.env.example`），改 Key / 换模型后 `systemctl restart fei-feinao-agent` 生效，无需改代码。

MIT License。
