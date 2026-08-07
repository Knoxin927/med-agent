"""M5.5 detail 聚合：区分 known_cost_sum、not_available_count 与 coverage_denominator。"""

# 导入 Sequence，统一接受 tuple/list。
from collections.abc import Sequence
# 导入 Any，输出 JSON 友好字典。
from typing import Any

# 导入值对象与冻结状态。
from app.evaluation.cost.types import CostDetail


def aggregate_cost_details(
    details: Sequence[CostDetail],
) -> dict[str, Any]:
    """从 detail 重算成本汇总；缺失金额不填假 0。"""

    if not details:
        raise ValueError("details 不能为空")

    # 按 request_kind 聚合，保证单位/币种不混淆。
    by_kind: dict[str, dict[str, Any]] = {}
    total_known_cost = 0.0
    total_not_available_count = 0
    total_known_count = 0

    for detail in details:
        kind = detail.request_kind
        bucket = by_kind.setdefault(
            kind,
            {
                "detail_count": 0,
                "known_cost_sum": 0.0,
                "not_available_count": 0,
                "known_count": 0,
                "usage_sum": {},
                "sample_ids": [],
            },
        )
        bucket["detail_count"] += 1
        bucket["sample_ids"].append(detail.detail_id)
        for key, value in detail.usage.items():
            bucket["usage_sum"][key] = bucket["usage_sum"].get(key, 0) + value

        if detail.total_cost is None:
            # 缺 usage 或价格：进 not_available_count，不进 known_cost_sum。
            bucket["not_available_count"] += 1
            total_not_available_count += 1
        else:
            bucket["known_count"] += 1
            bucket["known_cost_sum"] += detail.total_cost
            total_known_count += 1
            total_known_cost += detail.total_cost

    # coverage_denominator = 每个 kind 的 known+not_available 总数；用于计算可用金额覆盖率。
    coverage_denominator = sum(item["detail_count"] for item in by_kind.values())
    total_count = coverage_denominator
    # 只要存在任一 not_available，整体金额就不能声明为完整 known。
    summary_amount = None if total_not_available_count > 0 else total_known_cost

    # 按 kind 拍平 usage_sum，便于报告稳定排序。
    for bucket in by_kind.values():
        bucket["usage_sum"] = {key: bucket["usage_sum"][key] for key in sorted(bucket["usage_sum"])}

    return {
        "by_kind": by_kind,
        "total_count": total_count,
        "total_known_count": total_known_count,
        "total_not_available_count": total_not_available_count,
        "coverage_denominator": coverage_denominator,
        "coverage_known_ratio": (
            total_known_count / total_count if total_count else None
        ),
        "known_cost_sum": total_known_cost,
        "summary_amount": summary_amount,
        # 必备字段完整才允许 decision 给 synthetic_only，否则 hold。
        "has_complete_evidence": total_count > 0,
    }
