"""M5.3 敏感扫描：命中敏感键/值时丢弃事件，只记契约违规计数。"""


# 敏感字段名：不允许进入日志或指标 label。
_SENSITIVE_KEYS = {
    "api_key",
    "secret",
    "authorization",
    "password",
    "token",
    "prompt",
    "query",
    "messages",
    "answer_text",
    "answer",
    "source_text",
    "checkpoint",
    "raw_state",
    "hidden_reasoning",
    "health_info",
    "patient_health",
    "medical_history",
}
# 敏感值标记。
_SENSITIVE_VALUE_MARKERS = (
    "sk-",
    "api_key=",
    "bearer ",
    "password=",
    "secret=",
    "authorization=",
    "-----begin",
    "ghp_",
    "akia",
    "高血压",
    "糖尿病",
    "病历",
    "患者姓名",
)


class ObservabilityScanError(ValueError):
    """可观测性契约扫描失败。"""


def scan_observability_payload(payload: object, path: str = "payload") -> None:
    """递归扫描敏感键与敏感值；命中即抛 ObservabilityScanError。"""

    if isinstance(payload, dict):
        for key, value in payload.items():
            lower_key = str(key).lower()
            if lower_key in _SENSITIVE_KEYS:
                raise ObservabilityScanError(f"敏感字段扫描命中: {path}.{key}")
            scan_observability_payload(value, f"{path}.{key}")
    elif isinstance(payload, list):
        for index, value in enumerate(payload):
            scan_observability_payload(value, f"{path}[{index}]")
    elif isinstance(payload, str):
        lower_value = payload.lower()
        if any(marker in lower_value for marker in _SENSITIVE_VALUE_MARKERS):
            raise ObservabilityScanError(f"敏感值扫描命中: {path}")
