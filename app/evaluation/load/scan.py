"""M5.2 输入/报告共用敏感扫描：发现敏感键名、密钥或健康正文立即失败。"""


# 敏感字段名：任何层级出现这些键都 fail-closed。
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
    "checkpoint",
    "raw_state",
    "request_body",
    "response_body",
    "health_info",
    "patient_health",
    "medical_history",
}
# 敏感值标记：密钥格式、授权头、私有 token 与健康正文一旦出现即失败。
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


def scan_load_payload(payload: object, path: str = "payload") -> None:
    """递归扫描敏感键与敏感值，命中即 fail-closed。"""

    if isinstance(payload, dict):
        for key, value in payload.items():
            lower_key = str(key).lower()
            if lower_key in _SENSITIVE_KEYS:
                raise ValueError(f"敏感字段扫描命中: {path}.{key}")
            scan_load_payload(value, f"{path}.{key}")
    elif isinstance(payload, list):
        for index, value in enumerate(payload):
            scan_load_payload(value, f"{path}[{index}]")
    elif isinstance(payload, str):
        lower_value = payload.lower()
        if any(marker in lower_value for marker in _SENSITIVE_VALUE_MARKERS):
            raise ValueError(f"敏感值扫描命中: {path}")
