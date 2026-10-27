# Security Policy

## 这个项目是什么定位

飞脑是一个**参考实现**（reference implementation），用来演示 Agent 网关怎么做证据链、
动作级权限、审批闸门和长期记忆。它默认按"能在本机跑起来"配置，不是"能直接挂公网"配置。
上生产前请至少读完本文件的「部署前必查」一节。

## 部署前必查

1. **改掉出厂口令。** `spec.json` 里的 `admin123 / ops123456 / view123456` 只用于本地演示。
   服务启动时如果检测到仍在用出厂口令，会在 stdout 打印 `[warn] default password in use`。
   改法：`FEINAO_PASS_ADMIN=...` 等环境变量（仅对新建库生效），或直接改 `users` 表。
2. **不要暴露 `:8060` 到公网。** 前置 nginx 做 TLS 与来源限制；`/m/` 手机端同理。
3. **Key 只放 `.env` 或环境变量。** `.env` 已在 `.gitignore` 里。Key 永远不会出现在任何
   接口响应中：`/api/providers/health` 只返回 `hasKey` / 末四位 `fingerprint` / `key_len`。
   如果你发现某处泄漏了完整 Key，按下面的流程上报，不要开 issue。
4. **数据库里有会话原文。** `feinao.db` 保存完整对话、证据链、审批记录。它不进 Git，
   但备份与权限要按敏感数据对待（`chmod 600`，不要放共享目录）。
5. **`FEINAO_DEMO=1` 时 :8099 是假上游**，任何人可登录读取示例数据。生产必须设
   `FEINAO_DEMO=0`，否则你接的真上游永远不会被调用。

## 已知的设计取舍

- 会话 token 存在内存里（`TOKENS` dict），进程重启后需重新登录。这是刻意的：
  参考实现不引入 Redis，也不落库明文 token。
- 审批与记忆的"人工确认"由 `spec.json` 的 `approvers` / 角色决定，没有细到字段级的 RBAC。
  需要多级审批请在自己的分支上扩展 `approval_handle`。
- 上游调用没有做 SSRF 白名单校验；`FEINAO_UPSTREAM_*` 属于运维级配置，别开放给终端用户。

## 上报漏洞

请**不要**开公开 issue。用 GitHub 的
[Private vulnerability reporting](../../security/advisories/new) 提交，或邮件联系仓库维护者。
我们尽量在 7 天内给出确认。请附：复现步骤、影响范围、你的环境（Python 版本 / OS）。

会认真处理的问题：认证绕过、越权审批、Key 泄漏、SQL 注入、路径穿越、上游请求伪造。
属于已知取舍的（见上）不算漏洞，但欢迎讨论。
