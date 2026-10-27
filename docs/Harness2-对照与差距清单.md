# Harness² 与飞脑：技术对照与差距清单

> 对象：Google Research 开源项目 `google-research/harness2`（Apache-2.0，v0.1.0，2026-10-08 首发）
> 论文：*Harness²: Recursive Agent Harnessing for an Open World*，arXiv:2610.14224（2026-10-09）
> 对照方：飞脑 feinao-agent v1.6（后端 2284 行 + 测试 1179 行，零依赖）
> 写作口径：只写能落到代码上的差距，不写"愿景"。凡标注"工作量大/小"的，按我一个人改的节奏估。

---

## 0. 一句话结论

Harness² 解决的是**"谁来改进 harness"**——把 harness 变成可被自动搜索、自动对比、自动沉淀的对象。
飞脑解决的是**"改进之后谁负责"**——把每一次动作变成有证据、有预算、有人签字的记录。

两者不冲突，缺口在中间：**飞脑有可被改进的 harness，但没有改进回路；Harness² 有改进回路，但没有治理回路。**
所以对飞脑的正确动作不是"照着抄一个自改进"，而是**把自改进塞进现有的飞审闸门里**——这一步他们没做，也不打算做，是我们能站住的差异位。

---

## 1. Harness² 是什么（先把对象说准）

### 1.1 定义

harness = 模型之外让它能当 agent 跑起来的那层系统（prompt、工具、记忆、技能、编排）；harnessing = 优化这层。
Harness² 的主张：**冻结模型权重，只改 harness**，在"设计阶段没见过"的开放任务上在线改，且**不需要标准答案**。

### 1.2 两层递归

| 层 | 名字 | 做法 | 解决什么 |
|---|---|---|---|
| 任务级 | Task-level recursion | **Propose–Probe–Compose**：提出编辑 → 在同一任务上探测效果 → 从多次执行的行为对比中合成新 harness | 没有 ground-truth verifier 时，仍然能判断"哪种改法更好" |
| 编排级 | Harnessing-level recursion | 把采纳的 harness 与"有证据支持的教训"写进一个常驻 *improvement harness*，指导下一次任务 | 单任务的经验不要蒸发，能迁移到新场景 |

任务级递归有两个扩展轴（README 明说）：

- **parallel**：探测 k 个通用候选 + k 个领域候选，产出 k 个组合（探索更宽）
- **sequential**：把已改进的 harness 与累积证据沿着 k 次 Propose–Probe–Compose 继续推（收敛更深）

命令层面就是 `--k 1` / `--k K --mode parallel` / `--k K --mode sequential`，`--substrate cx` 换 Codex 执行器。

### 1.3 八个可编辑组件

| # | 组件 | 管什么 |
|---|---|---|
| 1 | system prompt | 上下文构造 |
| 2 | guardrails | 上下文构造 / 行为约束 |
| 3 | memory | 上下文构造 |
| 4 | skills | 上下文构造（可复用能力说明） |
| 5 | tool guidance | 工具接口 |
| 6 | scripts | 工具接口（可执行脚本） |
| 7 | subagents | 委派编排 |
| 8 | plugins | 工具循环中的 middleware |

关键点：**这八件全部是"可编辑文件"**，编辑者是模型（默认 Claude Opus 4.8 跑在 OpenCode 里），所以编辑器本身也是这套系统的一部分。

### 1.4 他们自报的成绩（论文表，三次均值）

| 基准 | 配置 | base → +1 step |
|---|---|---|
| WorkBuddy-Bench（办公/网页/代码） | Gemini 3.5 Flash / OpenCode | 72.4 → 77.5 |
| | Gemini 3.5 Flash / Codex | 74.5 → 77.5 |
| | GLM-5 / OpenCode | 64.2 → **75.7** |
| | GLM-5 / Codex | 64.4 → 75.3 |
| JobBench（专业工作） | Gemini 3.5 Flash / OpenCode | 31.5 → 36.0 |
| | GLM-5 / OpenCode | 20.6 → 24.2 |
| LAB Partial（法律，按项通过率） | Gemini 3.5 Flash / OpenCode | 79.9 → 81.9 |
| | GLM-5 / OpenCode | 59.6 → 71.4 |

最扎眼的一条：**GLM-5 加上改进后的 harness（75.7）反超了 Gemini 3.5 Flash 的原始 harness（72.4）**。
言下之意——不少"模型差距"其实是 harness 差距。k=5 还能再加（WorkBuddy-Bench +3.9、LAB +6.7）。

### 1.5 作者自己标出的限制（这段建议原样记住）

- **编辑者是前沿模型**（Claude Opus 4.8），收益里有多少是"强模型替弱模型写作业"分不干净
- LAB 用 partial credit，**不是基准方官方分数**
- 基线由作者自己跑，**外部尚未复现**
- 成本随 k 放大（k=1 大约 4 次额外运行/步，k=5 约 20 次）
- README 明写**非 Google 官方支持产品**
- 安全警告：executor **网络不受限**、Codex **关沙箱**运行、公开 rubric 可能在线可得、**写在可编辑文件里的 guardrail 不等于安全边界**

最后一条是整个项目最值得停一秒的地方：一个"能改自己护栏"的系统，护栏本身不能是它自己。这正是飞脑的接口。

### 1.6 工程门槛（决定"能不能真跑"）

- 必须 Linux + Python 3.12 + uv；模型跑通还要 Node 20+、gcloud CLI、Vertex AI 项目
- LAB 要 Pandoc / Poppler，WorkBuddy-Bench 要 Docker
- 有 `harness2-demo`：确定性编辑器 + 真 Python 执行，**不需要任何模型凭据**——这是它最友好的部分

---

## 2. 逐组件对照：飞脑现在有什么、缺什么

飞脑没有任何"harness"这个词，但它的 `spec.json` + 四个模块其实已经是 harness 的形状。

| Harness² 组件 | 飞脑对应物 | 位置 | 可编辑？ | 自动优化？ |
|---|---|---|---|---|
| system prompt | `_react_system_prompt()` + `lcm.ask()` 里的 system 拼装 | `ai.py:921`、`lcm.py:262` | ❌ 硬编码字符串 | ❌ |
| guardrails | `tool_policies` + `policy_check()`（角色/预算/需确认三段） | `spec.json`、`ai.py:186–222` | ✅ 配置级 | ❌ |
| memory | 飞忆（抽取→pending→人工 activate→检索注入 top3） | `memory.py`（441 行） | ✅ 数据级（人审） | ⚠️ 半自动：抽取自动、采纳人工 |
| skills | 三个 pilot 是写死函数，不是可注册技能 | `pilot_ask / pilot_nl2api / pilot_script` | ❌ | ❌ |
| tool guidance | `TOOLS` 字典的工具描述 + ReAct 提示词 | `ai.py:850` | ❌ 硬编码 | ❌ |
| scripts | 无沙箱脚本执行（**有意不做**） | — | — | — |
| subagents | 无：ReAct 单线程逐步，没有派发/回收 | — | — | — |
| plugins | `upstreams` 三件（飞知/飞瞰/飞音）+ providers 四级降级 | `spec.json` | ✅ 配置级 | ❌ |

**八个组件里，我们覆盖 5 个的"存在性"，只有 2 个（guardrails、upstreams）算真正配置化，0 个可被自动优化。**
换句话说：**形状是对的，回路是空的。**

### 2.1 逐条注解

**system prompt（最该先动的一件）**
飞脑的 system 是两处字符串拼接：ReAct 的任务提示 + LCM 的上下文提示，都写死在代码里。改一次要动代码、跑测试、重新部署。Harness² 把它当文件。
差距不在"能不能改"，而在**改完没有版本、没有对照、没有回滚**。这是 P0。

**guardrails（我们反而比它清楚）**
它的 guardrail 是"可编辑文件里的一段话"，作者自己承认这不算安全边界。飞脑的 guardrail 是**代码侧的 policy_check**：角色不符直接拒、预算超了直接拒、需确认的动作挂单等人。这一层我们是硬闸门，它是软约束。
但飞脑的 policy 只限"调哪个工具、调几次"，**不限"改 harness 这件事本身"**——这就是缺口。

**memory（有记忆，但没有"改进记忆"）**
飞忆存的是事实（fact）、决定（decision）、偏好（preference）、产物（artifact），检索后注入 system 的**回答侧**。
Harness² 的 improvement harness 存的是"哪种改法在这类任务上有效"，注入的是**编辑侧**。
我们已经有 `kind=decision` 的槽位，缺的是"谁往里写、写了给谁用"。

**skills / subagents（真缺）**
三个 pilot 是三条写死的调用链，不是可组合技能；ReAct 是单 agent 顺序执行，没有子任务派发。这两件是 Harness² 能"重组能力"的物理基础，我们完全没有——但它们也是工作量最大的两件，不该先做。

**scripts（建议继续不做）**
"让 agent 写脚本并执行"和"零依赖可审计"是两个方向的取舍。飞脑的立场应该是明确的：**不执行模型生成的代码**。这一条要在文档里写明是设计决定，不是欠账。

---

## 3. 两种回路，别混为一谈

| | Harness² | 飞脑 |
|---|---|---|
| 回路目标 | 让 harness 更好 | 让动作可追责 |
| 驱动力 | 行为对比（无标准答案） | 证据链 + 预算 + 人工放行 |
| 判定信号 | 两次执行的行为差异 | 代码侧记录的事实（evidence / audit / usage） |
| 失败处理 | 丢弃没被采纳的编辑 | 拒绝落 denied、回喂模型、不重复挂单 |
| 最坏情况 | 改坏了自己不知道（护栏可编辑） | 卡住不动（闸门多） |
| 适用场景 | 研究、批量离线任务 | 企业内网、要签字的流程 |

一句话：**它是搜索引擎，我们是刹车和行车记录仪。** 装了行车记录仪不会让车变快，但没有它，改装过的车没人敢上路。

---

## 4. 差距清单（按"做了就有用"排序）

### P0 — 不做，后面全是空谈

**P0-1 harness 版本化与回滚** ✅ 已落地（2026-10-27，见 `docs/Harness版本化-上线说明.md`；
实现比原计划多做了一层语义 diff——用别的缩进重写文件不算改动，diff 精确到键路径）
- 现状：~~`spec.json` 是单文件，改动无历史、无 diff、无回滚，线上改了只能靠备份碰运气~~
  已改为 `harness_rev` 表：启动自动快照（幂等）、admin 专属接口、原子写盘 + 热加载回滚、
  脏 JSON 拒绝上线、回滚必留审计；测试 9 例，全套 47 passed
- 为什么：任何自动/半自动改进，第一步都是"能回到上一版"
- 最小做法：`harness_rev` 表（rev / sha256 / content / note / by / created / active），启动时按 hash 判重；`GET /api/harness`、`POST /api/harness/activate`
- 风险：低。不动行为，只加记录
- 工作量：小（半天）

**P0-2 固定任务集 + 双跑对照** ✅ 已落地（2026-10-27，见 `docs/基准任务集与双跑对照-上线说明.md`；
比原计划多做了"候选即修订"——patch 出来的候选以 active=0 落库，先测再决定要不要上线）
- 现状：~~没有基准任务集，三个 pilot 是演示口径，"改了到底有没有变好"无法回答~~
  已改为 `bench/tasks.json`（15 个任务，覆盖 RAG / NL2API / 话术 / ReAct / 审批）+ `bench_job`/`bench_run` 表；
  `POST /api/bench/run {base_rev, cand_rev | patch, tasks, repeats}` 与命令行 `python tools/bench.py`；
  通过率由任务自带 `expect` 判定（代码判据），其余指标全部从 `task_steps`/`approvals`/`usage`/`evidence` 复算；
  每格默认跑 3 次取均值；跑完内存配置还原并自检 sha，磁盘 spec.json 不动；测试 7 例，全套 65 passed
- 为什么：Harness² 的全部说服力来自"同一批任务、两次执行、比行为"
- 指标（从我们已有的事实里算，不用引入新采集）：任务完成率、步数、工具调用数、被 policy 拒次数、人工介入次数、
  token 成本（+ 字符数作为无 token 上报时的代理）、证据链完整率
- 风险：中。模型不稳定会让双跑噪声大 → 每格跑 3 次取均值（本实现默认就是 3，可 1–5）
- 遗留：双跑期间内存配置会在两版之间切换，只能安静时段跑、且全局串行；页面化留给 P2-2

**P0-3 对照式判定，而不是绝对打分** ✅ 已落地（2026-10-27，见 `docs/对照式判定-上线说明.md`）
- 现状：~~只有"成功/失败"和 evidence 文本，没有人读得懂的分数~~
  已改为 `bench.compare(job_a, job_b?)`：产出 `verdict`（六种 tone + 一句人话结论 + 建议动作 +
  前提清单）、`diff`（九个指标的方向与幅度）、`by_task`（逐任务判稳 + 新的失败原因）、
  `by_kind`（哪个能力族吃到了改动）、`scope`（任务集不同时只比交集）；
  接口 `GET /api/bench/compare?a=&b=`（admin），命令行 `--compare 7` / `--compare 7,8`，
  跑完一轮也会直接把结论行打出来
- 判稳规则（这一层的全部智力）：单任务差 1 次执行一律标"判不了"，只有"差 ≥2 次"或
  "两侧全中/全失"才算数；每格只跑 1 次时任何差异都不判胜负。
  本轮新增两个过程事实 `bench_run.errors` / `dups`（出错动作数、重复动作数），
  按"多少个任务变了"统计而不是宏平均——否则"1 个任务被拦 5 次"会被摊成 0.05
- 为什么：~~开放任务没有标准答案，但"同一任务两次执行的差异"是可判定的~~
  仍然成立；我们比 Harness² 多的那一样（evidence 由代码写）现在真的用上了：
  结论里的每句话都能回到 `bench_run` / `task_steps` / `approvals` / `evidence` 复核
- 没做：统计显著性检验（15 个任务做 t 检验只是给噪声穿白大褂）、页面（P2-2）、自动激活（仍走飞构人工确认）
- 风险：低（不改 agent 行为，只在读侧归纳）
- 工作量：中偏小（1 天，实际半天 + 半天写文档与用例；测试 76 passed）

### P1 — 让回路闭合

**P1-1 改进经验层（harness-level recursion 的最小实现）**
- 把"哪类改法在哪类任务上有效"写进 `memory`，`kind=decision`，`slot=harness:<组件>:<任务族>`
- 关键区别：这条记忆**注入给编辑者**，不注入给最终回答
- 为什么先做这个不做自动编辑：它没有风险，却能把每次人工调参的收益留下来

**P1-2 编辑者隔离 + 补丁必须过飞审**
- 新增角色 `editor`：只能产出 harness 补丁（JSON patch），不能直接写 `spec.json`
- 补丁自动挂成 `approvals` 一行审批单，理由里带双跑对比；人放行才 activate
- `approvers` 与 `tool_policies` 本身**不可被补丁修改**（白名单外一律拒）——直接把"guardrail 可编辑"的风险关掉
- 这条是飞脑相对 Harness² 唯一真正领先的地方，值得单独写进 README

**P1-3 技能注册表（skills 从 3 个写死函数变成 N 条可组合）**
- `skills` 表：name / when / how / tools / 允许角色
- ReAct 的 system 里按检索命中注入 top2 技能说明
- 工作量中偏大（1–2 天），收益是"加能力不用改代码"

### P2 — 规模化才需要

| 项 | 内容 | 为什么现在不做 |
|---|---|---|
| P2-1 递归预算 | `spec.harness.max_probes` / `max_cost_tokens` / 并发上限，把 k 变成可审计的配给 | 没有任务集之前，k 无意义 |
| P2-2 harness 修订可视化 | 前端"飞构"页：修订列表、两次对比、指标曲线 | 先把接口做出来再谈页面 |
| P2-3 跨模型对照 | 同一 harness 换 provider 跑，证明"提升来自 harness 而非模型" | 需要多 Key 稳定可用 |
| P2-4 subagents | ReAct 内派发子任务、合并结果 | 与"零依赖 + 可审计"冲突面最大，最后做 |

---

## 5. 反过来的优势：飞脑有、Harness² 没有

写这份清单时最容易犯的错是"只列自己缺的"。下面这五条是我们确实站在前面的：

1. **代码侧证据链**。evidence / audit / usage 三张表由服务端写，和模型自述互相独立。Harness² 的"对比证据"是被探测执行产生的行为差异，但**没有对外的、可供第三方核查的记录**。
2. **真闸门而非软约束**。`requires_confirm` → 挂单 → 人放行才执行 → 自动续跑 → 拒绝落 denied 回喂模型。他们的 README 自己承认"guardrail 写在可编辑文件里不算安全边界"。
3. **动作级权限与预算**。角色 × 工具 × 次数 × 幂等回放，四件事在 `policy_check` 里按固定顺序判（角色 → 回放/挂起 → 预算），且预算计数在执行前落账——执行抛错不会白嫖额度。
4. **任务状态可恢复**。tasks / task_steps 落库，`waiting_approval` 之后能接着跑；他们是一次性 run 完就结束。
5. **零依赖 + 单机可跑**。纯标准库 + SQLite，2000 行级后端。他们至少要 Linux + Python 3.12 + uv，跑真基准还要 Node 20 / gcloud / Docker。

这五条合起来就是一句话：**他们让 agent 变强，我们让 agent 敢用。** 两个都缺一不可，但我们不该在他们的赛道上比。

---

## 6. 如果真要跟：三段路线与验收标准

**第一阶段（半天到一天）：把 harness 变成有历史的对象**
交付：`harness_rev` 表 + `/api/harness` 两接口 + 启动时快照 + 一键回滚；测试 3 例
验收：改一次 `tool_policies`，能查到两版 diff，能回到上一版且行为一致

**第二阶段（一到两天）：让"改得好不好"可回答**
交付：`bench/tasks.json` + `/api/bench/run` + `/api/bench/compare` + 对比结论行；测试 4 例
验收：同一任务 base 与 candidate 双跑，输出步数/拒次/介入/成本四项差异，且两次跑的证据链都能查到

**第三阶段（两到三天）：闭合回路，但每一步都过闸门**
交付：`editor` 角色 + 补丁 → 审批单 + 放行后 activate + 经验写回 `memory(kind=decision)`
验收：一个补丁从生成到生效，全程可查审批人、对比证据、生效前后的 bench 差异；补丁试图改 `approvers` 时被拒

**不建议做的**：自动执行模型生成的脚本；让编辑者拥有 `approvers` 写权限；在没有任务集的情况下引入 k>1。

---

## 7. 结论与对外叙事

技术上：Harness² 值得读的是**"用行为对比代替标准答案"这个判定思路**，不是那八个组件的清单——组件清单我们本来就有，判定思路我们没有。
工程上：它把"harness 也是要被测试的对象"这件事做成了 default，飞脑目前把 harness 当配置文件。

对外叙事建议保持我们已有的那句，别跟着讲自改进：
> 别人在比 agent 能自动干多少事；我们在证明它干了什么、危险动作等谁点头。

如果要提 Harness²，只用一句话：**"它证明了 harness 是独立变量；我们给这个变量加了版本、对照和闸门。"**

---

## 参考

- 代码：https://github.com/google-research/harness2 （Apache-2.0，v0.1.0）
- 论文：https://arxiv.org/abs/2610.14224
- 项目页：https://harness2.github.io
- 基准说明：`docs/BENCHMARKS.md`（JobBench / LAB / WorkBuddy-Bench）
- 飞脑对应实现：`backend/spec.json`、`backend/ai.py`（policy_check / approvals / react_run）、`backend/memory.py`、`backend/lcm.py`、`docs/飞审审批工作台-上线说明.md`、`docs/飞忆跨会话记忆-上线说明.md`

---

*本文档所有 Harness² 数据均引自其 README 与论文摘要，未做独立复现；飞脑侧的事实（行数、表结构、函数位置）为写作时本地代码实测。*
