# 公开可复现说明

本仓库公开候选只导出最小可离线验证材料，不导出完整私有评测语料、真实 Provider trace 或 `.env`。

## 可公开验证的内容

1. `fixtures/public/health_topics.txt`：最小非敏感文本 fixture
2. `fixtures/public/evaluation-v2.schema.json`：评测样本 schema
3. `fixtures/public/sample-result.json`：synthetic-only 契约结果
4. `fixtures/public/agent-run-summary.json`：真实 Provider 矩阵的**脱敏汇总**（hold）
5. `tests/contract/test_public_contract.py`：clone 后可离线执行的契约测试
6. `LICENSE`：MIT

## 本地验证

```powershell
# 在 med-agent 项目根
$py = ".\.venv-m1-2\Scripts\python.exe"
& $py -m pytest tests/contract -q
```

## 不可公开或未导出的内容

- 完整 MedlinePlus v2 语料与 164 条确认评测集
- dense/hybrid 正式 details 全量正文
- 真实 Provider 原始 prompt / answer / tool payload
- 本机 `.env`、密钥、个人路径

## 当前工程事实（私有仓可核验）

- 聊天默认检索：hybrid（M7 v2 fixed 实测）
- 生产索引：v2 fixed 975 chunks / 74 sources
- Agent 完整矩阵：`evaluation/agent/reports/live-v2-full-matrix-20260812/`，decision=`hold`
