"""M5.3 事件接收器：并发安全写入，契约失败只记 violation 计数。"""

# 导入 threading，保证并发 append 不串写。
import threading
# 导入 dataclass asdict，导出 JSON。
from dataclasses import asdict
from decimal import Decimal
# 导入 Sequence。
from collections.abc import Sequence
# 导入 Any。
from typing import Any

# 导入解析、指标与扫描。
from app.observability.events import parse_observability_event
from app.observability.metrics import LocalMetricRegistry
from app.observability.scan import ObservabilityScanError
from app.observability.types import ObservabilityEvent


class ObservabilityRecorder:
    """本地事件记录器：合法事件入库，非法事件只递增 violation。"""

    def __init__(self, *, label_cardinality_limit: int = 32) -> None:
        # 保护 events 列表与 metrics 注册表。
        self._lock = threading.Lock()
        # 已接受事件。
        self._events: list[ObservabilityEvent] = []
        # 已接受事件身份，拒绝重放导致的重复计数。
        self._event_ids: set[str] = set()
        # 本地指标。
        self.metrics = LocalMetricRegistry(label_cardinality_limit=label_cardinality_limit)

    def record_raw_event(self, payload: object) -> bool:
        """尝试记录原始事件字典；成功返回 True，契约失败返回 False。"""

        try:
            event = parse_observability_event(payload)
        except (ValueError, ObservabilityScanError):
            # 设计要求：丢弃事件，不保存被拒字段，只递增固定计数。
            self.metrics.record_contract_violation()
            return False
        return self.record_event(event)

    def record_event(self, event: ObservabilityEvent) -> bool:
        """记录事件；任一校验或容量失败时只递增 violation 并返回 False。"""

        try:
            validated = parse_observability_event(asdict(event))
        except (TypeError, ValueError, ObservabilityScanError):
            self.metrics.record_contract_violation()
            return False

        labels = {
            "request_kind": validated.request_kind,
            "tool_name": validated.tool_name,
            "status": validated.status,
            "error_code": validated.error_code or "none",
            "model_id": validated.model_id,
            "tool_version": validated.tool_version,
        }
        with self._lock:
            if validated.event_id in self._event_ids:
                self.metrics.record_contract_violation()
                return False
            amounts: dict[str, int | float | Decimal] = {
                "request_total": 1,
                "request_duration_ms_sum": Decimal(str(validated.duration_ms)),
            }
            amounts[
                "request_success_total" if validated.status == "ok" else "request_error_total"
            ] = 1
            try:
                self.metrics.increment_many(labels, amounts)
            except ValueError:
                self.metrics.record_contract_violation()
                return False
            self._events.append(validated)
            self._event_ids.add(validated.event_id)
            return True

    def events(self) -> tuple[ObservabilityEvent, ...]:
        """返回当前已接受事件快照。"""

        with self._lock:
            return tuple(self._events)

    def export(self) -> dict[str, Any]:
        """导出事件与指标，供报告层使用。"""

        with self._lock:
            return {
                "events": [asdict(item) for item in self._events],
                "metrics": self.metrics.snapshot(),
            }


def correlate_events(events: Sequence[ObservabilityEvent]) -> dict[str, list[str]]:
    """按 run_id 聚合 call_id，验证关联稳定性。"""

    mapping: dict[str, list[str]] = {}
    for event in events:
        mapping.setdefault(event.run_id, []).append(event.call_id)
    # 每个 run 下 call_id 保持插入顺序，便于并发测试断言。
    return {run_id: list(call_ids) for run_id, call_ids in mapping.items()}
