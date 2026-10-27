# 飞脑AI Agent网关

> 一个入口 · 智达全系

飞系全系 52 个项目的 AI Agent 统一网关（第 52 号项目）：零依赖（Python 标准库 + SQLite FTS5）。

## 核心能力
- **LLM 多供应商网关**：DeepSeek / 混元 / mock 兜底，按序降级重试（combo 链），用量落库（usage 表）；Key 支持环境变量与 `.env` 文件，无 Key 自动降级 mock
- **供应商体检**：`GET /api/providers/health[?ping=1]` —— 只回 `hasKey` / `fingerprint`（末四位）/ `key_len`，**任何响应都不出现完整 Key**；ping=1 时真发一次最小请求验证有效性与延迟，错误信息过一遍脱敏
- **试点1 · 知识问答（RAG）**：SQLite FTS5 检索（LIKE 兜底）+ 带引用回答
- **试点2 · 智能查数（NL2API）**：自然语言 → 实体匹配（spec 驱动）→ 上游 REST 查询 → LLM 总结；不生成裸 SQL，天然防注入
- **试点3 · 客服话术**：场景 → 五段式话术生成（可挂知识库参考）
- **L2 ReAct Agent**：工具注册表（search_docs / query_system）+ 步数上限
- **LCM 无损上下文（长期会话）**：原文全量落库 + 分块摘要 + 摘要上卷成 DAG + 关键词回溯原文；上下文受 token 预算约束，历史再长也不膨胀
- **验证型收尾要求（VERIFY_TAIL）**：所有面向用户的回答（rag / react / LCM / 兼容链路）系统提示强制要求「结论附依据、产出物给位置、未验证要标注」——完成必须是可验证产物，不是模型自述；有测试守卫（tests/test_provider.py）
- **证据链（v1.3）**：每次回答落 `evidence` 表——真实命中的文档/回溯轮次、工具调用链、上下文规模、是否标注未验证；由代码记录"实际发生了什么"，与模型自述互相独立。接口 `GET /api/evidence?conv_id=N`
- **动作级权限/预算（v1.3）**：`spec.json` 的 `tool_policies` 按动作声明 `roles` / `max_calls_per_run` / `requires_confirm`，ReAct 强制执行；拒绝不静默，原因喂回模型并落 `denied` 步骤
- **任务进度落库（v1.3）**：`tasks` + `task_steps`（tool_call / tool_result / denied / pending / error / answer / replay），步数耗尽保持 `active` 可 `task_id` 断点续跑；接口 `GET /api/tasks`、`GET /api/tasks/detail?id=N`
- **飞审审批工作台（v1.4）**：`requires_confirm` 的真闸门——写操作自动挂审批单（工具/参数/发起人/任务目标），人工「放行并执行」后系统真执行该动作并让任务自动续跑，拒绝则动作不执行、理由回给模型；重复提交防扰 + 幂等回放；接口 `GET /api/approvals`、`POST /api/approvals/decide`（详见 `docs/飞审审批工作台-上线说明.md`）
- **飞忆跨会话长期记忆（v1.5）**：Harness 第 1 层收口——从会话抽取事实/决定/偏好/产出物，默认待确认、人工点头才生效；新会话按关键词检索注入（带 M#编号/日期/来源会话），命中不到不注入；三道过滤（反虚构 grounding、过程噪声、去重+新值取代旧值）；接口 `GET /api/memory`、`POST /api/memory/decide`、`POST /api/memory/extract`（详见 `docs/飞忆跨会话记忆-上线说明.md`）
- **审计与配额**：全工具/LLM 调用写 audit；viewer 无审计权限

## 运行
```bash
cd backend
python server.py          # http://127.0.0.1:8060/ ，手机 /m/
# 三角色: admin/admin123 · ops01/ops123456 · view01/view123456
# LLM Key（可选，缺省走 mock）:
#   FEINAO_DEEPSEEK_KEY / FEINAO_HUNYUAN_KEY
```

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
python -m pytest tests/ -q     # 11 项：API 全链路 + LCM + 供应商真实链路
```

## 目录
- backend/  engine.py（HTTP）+ ai.py（网关/RAG/试点/ReAct）+ lcm.py（无损上下文）+ spec.json
- frontend/ 对话工作台 SPA（对话 + 三试点 + 审计看板）
- mobile/   PWA（对话 + 模式切换：知识问答/智能查数/客服话术）
- tests/    API 集成测试（真实起服全链路）+ LCM 单元/接口测试 + 供应商真实链路（本地 OpenAI 协议桩）
- docs/     建设方案 + LCM 原型验证报告
- tools/    Logo + UI 巡检 + LCM 对照实验脚本

## 生产部署
systemd（8060）+ nginx /nao/（桌面）与 /nao/m/（手机），已上线运行。

真实 Key 已上线（2026-10-27，阿里云百炼 qwen-flash）：Key 在 `/opt/feinao-agent/.env`（参考 `.env.example`），改 Key/换模型后 `systemctl restart fei-feinao-agent` 即生效，无需改代码；全缺时自动 mock 兜底。
