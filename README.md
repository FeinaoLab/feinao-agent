# 飞脑AI Agent网关

> 一个入口 · 智达全系

飞系全系 51 项目的 AI Agent 统一网关（P0 交付）：零依赖（Python 标准库 + SQLite FTS5）。

## 核心能力
- **LLM 多供应商网关**：DeepSeek / 混元 / mock 兜底，按序降级重试（combo 链），用量落库（usage 表），Key 走环境变量（FEINAO_DEEPSEEK_KEY / FEINAO_HUNYUAN_KEY），无 Key 自动降级 mock
- **试点1 · 知识问答（RAG）**：SQLite FTS5 检索（LIKE 兜底）+ 带引用回答
- **试点2 · 智能查数（NL2API）**：自然语言 → 实体匹配（spec 驱动）→ 上游 REST 查询 → LLM 总结；不生成裸 SQL，天然防注入
- **试点3 · 客服话术**：场景 → 五段式话术生成（可挂知识库参考）
- **L2 ReAct Agent**：工具注册表（search_docs / query_system）+ 步数上限
- **审计与配额**：全工具/LLM 调用写 audit；viewer 无审计权限

## 运行
```bash
cd backend
python server.py          # http://127.0.0.1:8060/ ，手机 /m/
# 三角色: admin/admin123 · ops01/ops123456 · view01/view123456
# LLM Key（可选，缺省走 mock）:
#   FEINAO_DEEPSEEK_KEY / FEINAO_HUNYUAN_KEY
```

## 测试
```bash
python -m pytest tests/ -q
```

## 目录
- backend/  engine.py（HTTP）+ ai.py（网关/RAG/试点/ReAct）+ spec.json
- frontend/ 对话工作台 SPA（对话 + 三试点 + 审计看板）
- mobile/   PWA（对话 + 模式切换：知识问答/智能查数/客服话术）
- tests/    API 集成测试（真实起服全链路）
- docs/     建设方案
- tools/    Logo + UI 巡检

## 生产部署
systemd（8060）+ nginx /nao/（桌面）与 /nao/m/（手机）；配置真实 Key 后即接真实模型，未配置自动 mock 兜底，全链路可用。
