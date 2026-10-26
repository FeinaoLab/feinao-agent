# 飞脑 LCM 线上接入与真 Key 接入说明（2026-10-26）

> 项目：飞脑 AI Agent 网关（feinao-agent，第 52 号项目）
> 线上：http://42.193.158.63/nao/ ｜ 手机：http://42.193.158.63/nao/m/
> 本次范围：① LCM 无损上下文接入线上对话链路 ② 真实 LLM Key 接入能力

---

## 一、这次改了什么

### 1. 对话主链路改为 LCM（服务端持有记忆）

原来：前端每轮把全部 messages 回传给服务端（无状态），聊长了要么截断丢事，要么请求体爆炸。
现在：前端只发本轮这句话 + `conv_id`，服务端组装上下文 —— 历史摘要 + 最近原文热区 + 关键词回溯片段。

- `POST /api/chat {text, conv_id?}` → 返回 answer / conv_id / provider / context_tokens / summaries / detail_back / stats
- 旧 `{messages: [...]}` 调用**保持兼容**，老客户端不受影响
- `GET /api/lcm_convs`：会话列表（轮次、最后一句），支持前端切换历史会话
- 桌面工作台：新增会话选择条、新会话按钮、每轮底部显示「上下文 N token / 摘要 N 段 / 回溯 #序号」
- 手机 PWA：同样改为 conv_id 会话，换设备不丢上下文

### 2. 真实 LLM Key 接入（不配 Key 也能跑）

- Key 支持 **`.env` 文件**（`/opt/feinao-agent/.env`、项目根 `.env`），优先级低于真实环境变量，**改 Key 不用动 systemd 单元**
- 新增 `GET /api/providers/health[?ping=1]`：报每个供应商 Key 是否就位；`ping=1` 时真发一次最小请求，返回延迟或具体错误
- 降级链不变：deepseek → hunyuan → mock，任一 Key 缺失自动跳过

---

## 二、公网验收（2026-10-26 18:45 实测）

| 项 | 结果 |
|---|---|
| 服务状态 | `fei-feinao-agent` active，`/nao/api/health` 200 |
| 登录 | admin/admin123 通过 |
| 首轮对话 | 返回 conv_id=1，provider=mock，context_tokens=114 |
| 灌 14 轮日常对话后问早期事实 | 「机房在哪个城市？预算多少？」→ 命中「客户是恒启制造，预算 380 万，机房在深圳坪山」，回溯原文命中 #1 |
| 会话统计 | 32 条消息 / 原始 971 token / 该轮上下文 838 token / 摘要节点 20 个（L1+L2 两层） |
| 会话列表 | `GET /api/lcm_convs` 返回 [{"id":1,"turns":32}] |
| 供应商体检 | deepseek / hunyuan 报「未配置 Key」，mock 可用 |
| 前端资源 | `/nao/`、`/nao/app.js`、`/nao/style.css`、`/nao/icon.svg`、`/nao/m/`、`/nao/m/app.js` 全部 200，且 app.js 已含新会话逻辑 |

> 说明：第 1 轮回答是「（上下文中没有相关信息，无法回答）」，这是 mock 模型的设计行为（只从给定上下文作答），换成真 Key 后即为正常对话。

---

## 三、真 Key 现状：只差一把钥匙

**已确认**：服务器全盘扫描（`/opt` 下 yml/yaml/properties/json/env + systemd 单元）没有可复用的 DeepSeek / 混元 / OpenAI Key，本机工作区同样没有。所以「真实模型下的效果」这一项，卡在 Key 本身，不是代码。

**代码链路已经验证到位**（不等 Key 就能测）：新增 `tests/test_provider.py`，在本地起一个**兼容 OpenAI 协议的真实 HTTP 桩服务**，验证了：

1. `.env` 文件读取生效，且不覆盖已有环境变量
2. 调用时正确携带 `Authorization: Bearer <key>`、正确传 model
3. 用量落库（provider/model/latency/prompt_tokens/completion_tokens）全部正确
4. `providers/health?ping=1` 能报延迟、能报具体错误
5. 拔掉 Key 后自动降级到 mock，体检如实报「未配置 Key（降级到下一家）」

真 Key 走到的是同一条代码路径，换上即生效。

### 开启方式（任选一种）

**方式 A：一条命令（推荐）**
```bash
python Temp/feinao_set_key.py deepseek sk-你的Key
# 脚本会：写 /opt/feinao-agent/.env（chmod 600）→ 重启服务 → 调 health?ping=1 实测
```
查看当前状态：`python Temp/feinao_set_key.py --check`

**方式 B：手工两行**
```bash
echo 'FEINAO_DEEPSEEK_KEY=sk-你的Key' >> /opt/feinao-agent/.env
systemctl restart fei-feinao-agent
```

开启后建议再跑一次真实模型版的 LCM 对照：
```bash
python feinao-agent/tools/lcm_compare.py     # 本机配同一把 Key 后执行
```

---

## 三·五、Key 只回指纹（2026-10-26 19:15 追加）

参照 dsh-our-free-model 的做法补的：`GET /api/providers/health` 不再只报 true/false，而是回 `hasKey` + `fingerprint`（末四位）+ `key_len`，**完整 Key 在任何响应里都不出现**；ping 失败的错误信息也会过一遍 `_scrub()` 脱敏。

线上实测（临时塞一把假 Key，验完即撤）：
- 带 Key：`{"hasKey": true, "fingerprint": "…1234", "key_len": 24}`
- 撤掉后：`{"hasKey": false, "fingerprint": null, "key_len": 0}`
- 响应全文不含完整 Key；`.env` 已清理干净

硬约束有测试守着：`tests/test_provider.py` 断言响应 JSON 里不得出现完整 Key 字符串。

## 四、测试与回归

- 本地 `pytest tests/ -q` → **12/12 通过**（API 全链路 1 + LCM 8 + 供应商真实链路 2 + 指纹脱敏 1）
- 线上不重启失败、不丢数据：部署脚本先备份到 `/opt/_bak/feinao-before-lcm-<时间戳>.tar.gz`
- 线上数据库 `feinao.db` 未随包覆盖（打包排除 *.db），老会话数据完整保留

---

## 五、遗留事项

1. **真实模型下的 LCM 质量未评**：摘要 prompt、回溯 topk、预算 1200 这三个参数目前是按 mock 经验调的，接上真 Key 后要重测一轮再定参
2. **回溯仍是关键词匹配**，不是语义检索；会话量上千后要换向量召回
3. **摘要质量依赖 LLM**：无 Key 时走抽取式兜底，能保住数字/人名，但文风生硬
4. 部署中继已用完，需下线（避免常驻入口）

---

*文档生成：2026-10-26 18:50 ｜ 维护：WorkBuddy Agent*
