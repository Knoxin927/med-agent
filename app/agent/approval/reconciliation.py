"""M3.5 未知副作用结果的纯 reconciliation 决策矩阵。"""

# 导入不可变值对象，保证查证决策不携带原始 arguments。
from dataclasses import dataclass
from typing import Literal

from app.agent.approval.port import ReconciliationContext


ReconciliationDecision = Literal["succeeded", "retry_allowed", "failed", "reconciliation_required"]


@dataclass(frozen=True)
class ReconciliationResult:
    """保存查证结论与是否允许再次 claim 的稳定结果。"""

    context: ReconciliationContext
    decision: ReconciliationDecision


def resolve_reconciliation(context: ReconciliationContext, attempt_count: int, max_attempts: int) -> ReconciliationResult:
    """把 matching/missing/mismatch/DB unavailable 映射为唯一安全分支。"""
    if attempt_count < 1 or max_attempts <= 0:
        raise ValueError("reconciliation 尝试次数不合法")
    if context.status == "matching":
        return ReconciliationResult(context, "succeeded")
    if context.status == "missing":
        decision: ReconciliationDecision = "retry_allowed" if attempt_count < max_attempts else "failed"
        return ReconciliationResult(context, decision)
    if context.status == "mismatch":
        return ReconciliationResult(context, "failed")
    return ReconciliationResult(context, "reconciliation_required")
