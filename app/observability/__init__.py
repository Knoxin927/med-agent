"""M5.3 可观测性包：本地脱敏事件与指标，不接入公网监控。"""

# 导出值对象，方便测试与 CLI 直接引用。
from app.observability.types import (
    EVENT_STATUSES,
    LOG_EVENT_FIELDS,
    METRIC_LABEL_CARDINALITY_LIMIT,
    METRIC_LABEL_KEYS,
    REQUEST_KINDS,
    EventStatus,
    ObservabilityEvent,
    ObservabilityManifest,
    RequestKind,
)

__all__ = [
    "EVENT_STATUSES",
    "LOG_EVENT_FIELDS",
    "METRIC_LABEL_CARDINALITY_LIMIT",
    "METRIC_LABEL_KEYS",
    "REQUEST_KINDS",
    "EventStatus",
    "ObservabilityEvent",
    "ObservabilityManifest",
    "RequestKind",
]
