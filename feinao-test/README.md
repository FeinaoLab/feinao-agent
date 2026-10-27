# feinao-test/

跑验收的地方。这里的东西全是副本 —— 随便改、随便删、改坏了重建。

| 文件 | 是什么 |
| --- | --- |
| `spec.test.json` | 与 `backend/spec.json` 同构的测试配置：只改端口、上游地址、展示名和口令 |
| `feinao-test.db` | 脱敏库副本，由 `tools/testenv.py --prep` 生成（不入库） |
| `DB-INFO.json` | 这份副本的来源、脱敏命中次数、行数与 sha256 |

```bash
python tools/testenv.py --prep     # 生成或重建库副本
python tools/testenv.py --serve    # 在 8081 起服
python tools/testenv.py --check    # 冒烟：health / 登录 / 检索 / ReAct / 策略 / 基准
```

把 `FEINAO_SPEC` 指向 `backend/spec.json`、或把 `FEINAO_DB` 指向 `backend/feinao.db`，
`--serve` 会直接拒绝启动。这不是保险丝，是门锁。
