# 🧠 飞脑 Agent (Feinao Agent)

> 审计优先的 AI Agent 网关 — 让 AI 行动可追溯、可控制、可恢复

[![Python](https://img.shields.io/badge/Python-3.8+-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Zero Dependencies](https://img.shields.io/badge/dependencies-zero-brightgreen.svg)]()

## ✨ 特性

- 🔍 **完整审计追踪** — 每个动作都有代码级证据记录
- 🔐 **细粒度权限** — 按动作类型控制 Agent 能做什么
- 👤 **人工审批门** — 关键操作需要真人确认
- 🔄 **任务可恢复** — 中断后可从断点继续
- 💾 **跨会话记忆** — Agent 记住之前的对话和上下文
- 🪶 **零依赖** — 纯 Python 标准库 + SQLite

## 🚀 快速开始

```bash
# 克隆仓库
git clone https://github.com/dukema81/feinao-agent.git
cd feinao-agent

# 运行（无需安装依赖！）
python main.py
```

## 📖 核心概念

### 审计日志

每个 Agent 动作都会被记录：

```json
{
  "timestamp": "2026-10-28T10:30:00Z",
  "action": "file_write",
  "target": "/path/to/file.py",
  "status": "approved",
  "evidence": "diff..."
}
```

### 权限控制

```python
permissions = {
    "file_read": "auto",      # 自动允许
    "file_write": "approve",  # 需要审批
    "shell_exec": "deny",     # 禁止执行
}
```

### 跨会话记忆

Agent 会自动保存和加载上下文：

```python
agent.remember("用户偏好深色主题")
agent.recall()  # 下次会话自动恢复
```

## 🏗️ 架构

```
┌─────────────────────────────────────┐
│           用户界面 (TUI/Web)         │
├─────────────────────────────────────┤
│         审批门 (Approval Gate)       │
├─────────────────────────────────────┤
│    权限引擎  │  审计日志  │  记忆系统  │
├─────────────────────────────────────┤
│           Agent 执行引擎             │
├─────────────────────────────────────┤
│        SQLite (持久化存储)           │
└─────────────────────────────────────┘
```

## 📚 使用场景

- **代码助手** — 安全地让 AI 帮你写代码，危险操作需审批
- **自动化运维** — 记录所有操作，出问题可追溯
- **数据分析** — AI 处理敏感数据时的合规保障
- **客服机器人** — 关键决策需要人工确认

## 🤝 贡献

欢迎提交 Issue 和 Pull Request！

## 📄 许可证

MIT License © 2026 Duke Martin

---

**Made with ❤️ by [Duke Martin](https://github.com/dukema81)**
