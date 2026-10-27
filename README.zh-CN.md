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

## 改配置之前，先跑一轮双跑

同一批固定任务（`bench/tasks.json`，15 个，覆盖 RAG / NL2API / 话术 / ReAct / 审批）在旧配置与
候选配置上各跑一遍，比的是一行行能复查的事实（步数、工具调用、被拒、审批、证据），不是感觉：

```bash
python tools/bench.py --list                                    # 先看任务集
python tools/bench.py --patch pilots.react_max_steps=8 --note "放宽步数上限"
```

候选配置以修订形式落库但不生效（`active=0`），跑完内存配置自动还原并自检 sha；磁盘上的
`spec.json` 一个字都不动。觉得确实更好，再去「飞构」页激活它。

跑完给的不是分数，是一句能逐条复核的话：

```
结论：先别上线：1 个任务稳定变坏（react-01），新的失败原因是「没有执行预期动作 search_docs（实际：无）」
建议：先别上线
  · 有效性：通过率 1 → 0.75。0 个任务稳定变好、1 个稳定变坏、0 个只差一次判不了、3 个逐条一致
  · 过程：被策略拒 1 个任务上升（0 → 1）
注意：这轮走的是 mock 上游（没有真实模型调用）：只说明链路与策略生效，不能当模型能力结论
```

只差一次执行的任务一律标「判不了」，不写成「变好」——把噪声写成结论，比不给结论更糟。

## 验收跑在副本上：8081，不碰生产

`feinao-test/` 里放一份测试配置和一份脱敏库副本，`FEINAO_SPEC` / `FEINAO_DB` / `FEINAO_PORT`
三个开关由工具固定，不用手写、不用记：

```bash
python tools/testenv.py --prep       # 生成脱敏库副本（没有源库就用假上游种子造一份）
python tools/testenv.py --serve      # 8081 起服
python tools/testenv.py --check      # 冒烟：health / 登录 / 检索 / ReAct / 策略 / 基准
python tools/testenv.py --preflight  # 端口体检：open / rst / timeout / refused 是四件事
```

护栏是硬拒绝，不是提醒：`FEINAO_SPEC` 指向 `backend/spec.json`、`FEINAO_DB` 指向
`backend/feinao.db`、`FEINAO_DEMO=0`、端口越界——任何一条都直接起不来。它还会把本机 shell 里的
LLM Key 清空成 mock（只在本进程），因为「测试时悄悄走了真模型」的代价不是钱，是同一条用例
昨天过今天不过、你分不清是代码变了还是模型抽风。

测试配置与生产配置由测试强制同构（只许端口、上游地址、展示名、口令不同）。想靠调松
`modes` / `limits` / `tool_policies` 让验收变绿，测试会红。

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
- **策略数据化 + 卡死上限（v1.8）**：模式/上限/出站白名单进 `spec.json` 顶层段，与业务配置共用飞构的版本、语义 diff、一次性回滚；坏配置拒绝启动且指名报错；ReAct 三条中断（墙钟 / 三连相同调用 / 连续畸形输出）落证据链并给人话提示；接口 `GET /api/ai/policy`（详见 `docs/策略数据化与卡死上限-上线说明.md`）
- **飞构·配置版本化（v1.7）**：`spec.json` 有历史、能对比、可回滚——起服自动快照（幂等），语义 diff 精确到键（改缩进不算改动），回滚=原子写盘+热加载不重启，脏 JSON 拒绝上线；admin 专属，回滚必留审计；接口 `GET /api/harness`、`POST /api/harness/{snapshot,activate}`（详见 `docs/Harness版本化-上线说明.md`）
- **基准任务集 + 双跑对照（v1.9）**：`bench/tasks.json` 固定 15 个任务（RAG / NL2API / 话术 / ReAct / 审批），同一批任务在 base 与候选修订上各跑 N 次，指标全部来自 `task_steps`/`approvals`/`usage`/`evidence` 的真实事实，通过率由任务自带的 `expect` 判定（代码判据，不是模型打分）；候选以 `active=0` 落库、跑完内存还原并自检 sha；接口 `GET /api/bench`、`POST /api/bench/run`、命令行 `python tools/bench.py`（详见 `docs/基准任务集与双跑对照-上线说明.md`）
- **对照式判定（v2.0）**：双跑结果不再给总分，而是给「同一批任务两次执行的差」——`bench.compare(job_a, job_b?)` 产出六种结论（先别上线 / 有得有失 / 可以上线 / 只买到成本 / 白捡的省 / 测不出差别）加一句人话结论、逐任务判稳与新的失败原因、按能力族的分项；单任务差 1 次执行一律标「判不了」，每格只跑 1 次时不判胜负；新增 `bench_run.errors` / `dups` 两个过程事实，按「多少个任务变了」统计而非宏平均；接口 `GET /api/bench/compare`、命令行 `--compare 7`（详见 `docs/对照式判定-上线说明.md`）
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
python -m pytest tests/ -q     # 83 项：API 全链路 + LCM + 审批 + 记忆 + 演示上游契约 + 配置版本化 + 卡死上限 + 基准双跑 + 对照判定 + 本地测试环境护栏
```

CI（`.github/workflows/ci.yml`）在 Python 3.9 / 3.11 / 3.13 上跑全套，另有一个
"不装任何依赖、clone 后直接起服务并问一句带引用的话"的冒烟 job。

## 目录

- `backend/` — `engine.py`（HTTP）+ `ai.py`（网关/RAG/试点/ReAct/审批）+ `lcm.py`（无损上下文）+ `memory.py`（跨会话记忆）+ `demo.py`（内置假上游与种子数据）+ `spec.json`
- `frontend/` — 对话工作台 SPA（对话 + 三试点 + 审计看板 + 飞审 + 飞忆）
- `mobile/` — PWA（对话 + 模式切换）
- `bench/` — 固定基准任务集 `tasks.json`（15 个任务，五类能力）+ `tools/bench.py` 双跑命令行
- `tests/` — API 集成测试（真实起服全链路）+ LCM + 审批 + 记忆 + 演示契约 + 配置版本化 + 基准双跑
- `docs/` — 建设方案、LCM 验证报告、各能力上线说明、[Harness² 对照与差距清单](docs/Harness2-对照与差距清单.md)、[Harness 版本化上线说明](docs/Harness版本化-上线说明.md)、[策略数据化与卡死上限](docs/策略数据化与卡死上限-上线说明.md)、[基准任务集与双跑对照](docs/基准任务集与双跑对照-上线说明.md)、[对照式判定](docs/对照式判定-上线说明.md)、[本地测试环境与端口体检](docs/本地测试环境与端口体检-上线说明.md)
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
