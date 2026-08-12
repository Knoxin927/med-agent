# 公开可复现说明

本仓库公开候选只导出最小可离线验证材料，不导出完整私有评测语料、真实 Provider trace 或 `.env`。

## 可公开验证的内容

1. `fixtures/public/health_topics.txt`：最小非敏感文本 fixture
2. `fixtures/public/evaluation-v2.schema.json`：评测样本 schema
3. `fixtures/public/sample-result.json`：synthetic-only 契约结果
4. `fixtures/public/agent-run-summary.json`：真实 Provider 矩阵的**脱敏汇总**（v1-bound pass）
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
- dense/hybrid/rerank/rewrite 正式 details 全量正文
- 真实 Provider 原始 prompt / answer / tool payload
- 本机 `.env`、密钥、个人路径

## 当前工程事实（公开摘要 + 私有仓可核验）

- 聊天默认检索：hybrid（M7 v2 fixed 实测；R@5 0.9486 / R@10 0.9760 / MRR@10 0.8392）
- 生产索引：v2 fixed 975 chunks / 74 sources
- 切片默认：fixed 300/50；section-sentence 对比后不采用
- rewrite-dense：正式负结果，offline-only
- dense-rerank：正式 5-round R@5/R@10 0.9418、MRR@10 0.8986，offline-only（成本与 R@10 不及 hybrid）
- Agent 完整矩阵：`live-v2-full-matrix-20260813-v1bound`，decision=`pass`（shared 8/8，agent-only 18/18，tool 12/12，approval 6/6）
- 边界：Agent pass 绑定 evaluation/corpora/v1；**不是**生产 v2 索引 pass
