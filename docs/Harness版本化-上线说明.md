# 飞构 · Harness 版本化上线说明（v1.7）

> 一句话：把 `spec.json` 从"一个会被改坏的配置文件"变成"有历史、能对比、可回滚的对象"。
> 这是《Harness2-对照与差距清单》里 P0-1 的落地，也是后面一切自动改进的前提——
> 改坏了连退路都没有的系统，不配谈自改进。

## 动机

开源化时我们把大量行为开关放进了 `spec.json`：tool_policies（谁能调什么工具、调几次、
要不要人批）、approvals（审批人与有效期）、upstreams（真实上游与演示上游）、memory、
pilots。这份文件已经是 harness 本体，但它此前**没有版本、没有 diff、没有回滚**：

- 改坏了只能靠手工备份碰运气；
- "这次改动到底改了什么"只能靠人脑回忆；
- 将来要让编辑者（人或模型）提 harness 补丁，第一件事就是能回到上一版。

## 设计与原则

1. **快照即历史**：`harness_rev` 表存每次内容变化（sha256 前 16 位指纹 + 全文 + 备注 +
   操作人 + 时间）。启动时自动快照一次，内容没变则幂等跳过——重启不会刷噪音。
2. **语义对比，不是行对比**：diff 先把 JSON 拍平成 `a.b[0].c` 路径再比。
   用别的缩进重写一遍文件**不算改动**（否则任何脚本重排格式都会产生满屏假差异）；
   原始行 diff 保留在 `raw` 字段，方便对着文件看。
3. **回滚 = 写盘 + 热加载，不重启**：`activate` 用临时文件 + `os.replace` 原子替换，
   先验 JSON 语法（脏数据拒绝上线），再原地更新内存 dict——`engine.SPEC` 等旧引用
   同步生效，上游 token 缓存清空。正在跑的任务不受影响。
4. **只给 admin**：修订列表、内容、快照、激活全部 admin-only。spec 里有出厂口令，
   ops / viewer 连列表都看不到。
5. **回滚必留审计**：每次 activate 落一条 `audit`（tool=harness_activate）。

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/harness` | 修订列表 + 当前生效 id + 磁盘指纹 |
| GET | `/api/harness/detail?id=N` | 某一版完整内容 |
| GET | `/api/harness/diff?from=A&to=B` | 语义差异（不传 from/to 则比最近两版） |
| POST | `/api/harness/snapshot {note}` | 给当前磁盘配置记一版（内容没变则提示"与 #N 一致"） |
| POST | `/api/harness/activate {id}` | 回滚到某版并热加载 |

前端新增「飞构」页（admin 可见全部操作，其他角色入口可见但接口 403）。

## 已知边界（说清楚，免得当成 bug）

- **快照不含 `.env`**：Key 和 `FEINAO_PASS_*` 本来就不该进版本历史，留在 .env 是对的。
  但注意 `FEINAO_PASS_*` 会覆盖 spec 里的口令——回滚不会还原 .env 里的口令。
- **回滚不重启**：providers 列表变化能热生效（原地更新 dict）；如果未来有模块在
  import 时缓存了 SPEC 的**子对象引用**（而不是每次经 `ai.SPEC` 取），热加载对它无效。
  当前代码已核对：engine/lcm/memory 都是运行时取值，无此问题。
- **DB 里的旧快照可能是坏 JSON**：`harness_diff` 对解析失败的修订容错（标记
  `<无法解析的内容>`），`activate` 则直接拒绝——能看不能上。
- **这层只管 spec.json**：prompt 片段还硬编码在 `lcm.py` / `ai.py` 里，它们进版本历史
  要靠 git。P1 的技能注册表落地后，技能描述会挪进 spec，届时自动被这层覆盖。

## 验收

- 单测 `tests/test_harness.py` 9 例：启动快照、幂等、改配置出新版、回滚还原磁盘与内存、
  脏 JSON 拒绝激活、语义 diff 精确到键、纯格式变化不算改动、真服务上 ops 403 / admin 全通。
  全套 47 passed。
- 真服务实测：三版修订（启动快照 / 步数 6→4 / 审批有效期 24h→12h），diff 输出
  `approval.ttl_minutes: 1440 → 720` 一行；回滚后文件与内存同步还原；UI 实拍见
  `docs/screenshot-harness.png`。

## 与差距清单的关系

P0-1 ✅（本篇）。下一步按清单顺序：**P0-2 固定任务集 + 双跑对照**——有了它，
"这次配置改动有没有变好"才第一次变得可回答；P0-3 对照式判定依赖它的产出。
