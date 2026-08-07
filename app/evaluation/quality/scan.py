"""M5.1 输入/报告共用敏感扫描：发现敏感键名、密钥或健康正文立即失败。"""


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
    "grade_evidence",
    "health",
}
# 敏感值标记：密钥格式、授权头、私有 token 与健康正文一旦出现即失败。
# 中文健康词刻意保持窄列表，避免把“患者教育”等正常合成证据误伤成密钥。
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


def scan_report_payload(payload: object, path: str = "payload") -> None:
    """递归扫描敏感键与敏感值，命中即 fail-closed。"""

    # 字典同时检查键名和值；键名本身可能是凭据字段。
    if isinstance(payload, dict):
        for key, value in payload.items():
            lower_key = str(key).lower()
            # 敏感键名命中立即报错。
            if lower_key in _SENSITIVE_KEYS:
                raise ValueError(f"敏感字段扫描命中: {path}.{key}")
            # 字符串值再检查常见密钥/授权/正文标记。
            scan_report_payload(value, f"{path}.{key}")
    elif isinstance(payload, list):
        for index, value in enumerate(payload):
            scan_report_payload(value, f"{path}[{index}]")
    elif isinstance(payload, str):
        lower_value = payload.lower()
        # 任何值命中固定敏感标记都必须失败，不能只检查键名。
        if any(marker in lower_value for marker in _SENSITIVE_VALUE_MARKERS):
            raise ValueError(f"敏感值扫描命中: {path}")
