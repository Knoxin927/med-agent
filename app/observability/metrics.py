"""M5.3 本地指标：只允许冻结 label，并强制 cardinality 上限。"""

# 导入 threading，保证并发写指标时不串写。
import threading
import math
import re
import sys
from decimal import Decimal
# 导入 Sequence，统一接受事件列表。
from collections.abc import Sequence
# 导入 Any，输出 JSON 友好结构。
from typing import Any

# 导入值对象。
from app.observability.types import (
    METRIC_LABEL_CARDINALITY_LIMIT,
    METRIC_LABEL_KEYS,
    ObservabilityEvent,
    OBSERVABILITY_FIELD_VALUE_ALLOWLIST,
    EVENT_STATUSES,
    REQUEST_KINDS,
)


_LABEL_VALUE_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")
_MAX_JSON_FINITE_DECIMAL = Decimal(str(sys.float_info.max))


def _labels_from_event(event: ObservabilityEvent) -> dict[str, str]:
    """从事件提取指标 label；error_code 为空时写 none。"""

    return {
        "request_kind": event.request_kind,
        "tool_name": event.tool_name,
        "status": event.status,
        "error_code": event.error_code or "none",
        "model_id": event.model_id,
        "tool_version": event.tool_version,
    }


def _validate_labels(labels: dict[str, str]) -> None:
    """校验 label 键集合与内容。"""

    unknown = sorted(set(labels).difference(METRIC_LABEL_KEYS))
    if unknown:
        raise ValueError(f"指标 label 包含白名单外字段: {unknown}")
    missing = sorted(METRIC_LABEL_KEYS.difference(labels))
    if missing:
        raise ValueError(f"指标 label 缺少字段: {missing}")
    for key, value in labels.items():
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"指标 label.{key} 必须是非空字符串")
        if not _LABEL_VALUE_PATTERN.fullmatch(value):
            raise ValueError(f"指标 label.{key} 必须是受限标识符")
        allowed = OBSERVABILITY_FIELD_VALUE_ALLOWLIST.get(key)
        if key == "request_kind":
            allowed = REQUEST_KINDS
        elif key == "status":
            allowed = EVENT_STATUSES
        if allowed is not None and value not in allowed:
            raise ValueError(f"指标 label.{key} 不在当前冻结 allowlist 中")


class LocalMetricRegistry:
    """线程安全的本地计数器/直方图注册表。"""

    def __init__(self, *, label_cardinality_limit: int = METRIC_LABEL_CARDINALITY_LIMIT) -> None:
        if (
            isinstance(label_cardinality_limit, bool)
            or not isinstance(label_cardinality_limit, int)
            or label_cardinality_limit <= 0
        ):
            raise ValueError("label_cardinality_limit 必须是正整数")
        # 每个 label key 允许出现的不同值数量上限。
        self.label_cardinality_limit = label_cardinality_limit
        # 保护 counters / seen_values，避免并发串写。
        self._lock = threading.Lock()
        # 计数器：metric_name -> label_tuple -> count。
        self._counters: dict[str, dict[tuple[tuple[str, str], ...], int | Decimal]] = {}
        # 已见 label 值：label_key -> set(values)
        self._seen_values: dict[str, set[str]] = {key: set() for key in METRIC_LABEL_KEYS}
        # 契约违规计数：未知字段/敏感扫描失败只递增这个固定计数。
        self.contract_violation_count = 0

    def _can_remember_label_values(self, labels: dict[str, str]) -> None:
        """预检 label cardinality，不修改当前状态。"""

        for key, value in labels.items():
            seen = self._seen_values[key]
            if value not in seen and len(seen) >= self.label_cardinality_limit:
                raise ValueError(
                    f"指标 label.{key} 超过 cardinality 上限 {self.label_cardinality_limit}"
                )
    def _remember_label_values(self, labels: dict[str, str]) -> None:
        """在预检完成后记录 label 值。"""

        for key, value in labels.items():
            self._seen_values[key].add(value)

    def increment_many(self, labels: dict[str, str], amounts: dict[str, int | float | Decimal]) -> None:
        """原子更新同一事件派生出的多项指标。"""

        _validate_labels(labels)
        if not amounts:
            raise ValueError("amounts 不能为空")
        for metric_name, amount in amounts.items():
            if not isinstance(metric_name, str) or not metric_name:
                raise ValueError("metric_name 必须是非空字符串")
            if isinstance(amount, bool) or not isinstance(amount, (int, float, Decimal)):
                raise ValueError("amount 必须是非负有限数值")
            is_valid_amount = (
                amount.is_finite() and amount >= 0
                if isinstance(amount, Decimal)
                else math.isfinite(amount) and amount >= 0
            )
            if not is_valid_amount:
                raise ValueError("amount 必须是非负有限数值")
        label_items = tuple(sorted(labels.items()))
        with self._lock:
            self._can_remember_label_values(labels)
            updates: list[tuple[str, tuple[tuple[str, str], ...], int | Decimal]] = []
            for metric_name, amount in amounts.items():
                bucket = self._counters.get(metric_name, {})
                normalized_amount = (
                    Decimal(str(amount)) if metric_name == "request_duration_ms_sum" else amount
                )
                next_value = bucket.get(label_items, 0) + normalized_amount
                if metric_name == "request_duration_ms_sum" and next_value > _MAX_JSON_FINITE_DECIMAL:
                    raise ValueError("request_duration_ms_sum 超出安全 JSON 数值范围")
                updates.append((metric_name, label_items, next_value))
            self._remember_label_values(labels)
            for metric_name, key, next_value in updates:
                bucket = self._counters.setdefault(metric_name, {})
                bucket[key] = next_value

    def increment(
        self,
        metric_name: str,
        labels: dict[str, str],
        *,
        amount: int = 1,
    ) -> None:
        """递增计数器；label 不合法或超限时失败。"""

        if amount <= 0:
            raise ValueError("amount 必须是正整数")
        self.increment_many(labels, {metric_name: amount})

    def record_contract_violation(self) -> None:
        """记录一次契约违规；不保存被拒字段内容。"""

        with self._lock:
            self.contract_violation_count += 1
            # 固定无标签计数不消费业务 label cardinality，也不携带被拒内容。
            bucket = self._counters.setdefault("observability_contract_violation", {})
            key: tuple[tuple[str, str], ...] = ()
            bucket[key] = bucket.get(key, 0) + 1

    def snapshot(self) -> dict[str, Any]:
        """导出可 JSON 化的指标快照。"""

        with self._lock:
            counters: dict[str, list[dict[str, Any]]] = {}
            for metric_name, series in self._counters.items():
                rows: list[dict[str, Any]] = []
                for label_items, count in sorted(series.items(), key=lambda item: item[0]):
                    rows.append(
                        {
                            "labels": dict(label_items),
                            "value": float(count) if isinstance(count, Decimal) else count,
                        }
                    )
                counters[metric_name] = rows
            return {
                "counters": counters,
                "contract_violation_count": self.contract_violation_count,
                "label_cardinality": {
                    key: len(values) for key, values in sorted(self._seen_values.items())
                },
                "label_cardinality_limit": self.label_cardinality_limit,
            }


def build_metrics_from_events(
    events: Sequence[ObservabilityEvent],
    *,
    label_cardinality_limit: int = METRIC_LABEL_CARDINALITY_LIMIT,
) -> dict[str, Any]:
    """从合法事件重算本地指标。"""

    registry = LocalMetricRegistry(label_cardinality_limit=label_cardinality_limit)
    for event in events:
        labels = _labels_from_event(event)
        # 总请求数。
        amounts: dict[str, int | float | Decimal] = {
            "request_total": 1,
            "request_duration_ms_sum": Decimal(str(event.duration_ms)),
        }
        amounts["request_success_total" if event.status == "ok" else "request_error_total"] = 1
        registry.increment_many(labels, amounts)
    return registry.snapshot()
