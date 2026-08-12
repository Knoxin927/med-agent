"""热路径结构化日志：固定字段 JSON 一行，默认开启，可关闭。

字段只允许脱敏元数据：request_id / run_id / tool_name / latency_ms / status / route。
禁止写入 question、query、密钥、DSN、网页正文。
本模块是工程运维面，不修改 M5.3 ObservabilityRecorder，也不把 production_logging_claim 写成 true。
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import uuid
from collections.abc import Callable, Mapping
from typing import Any

# 固定字段顺序，便于测试与 grep。
HOT_PATH_LOG_FIELDS = (
    "schema",
    "request_id",
    "run_id",
    "route",
    "tool_name",
    "latency_ms",
    "status",
    "error_code",
)

_LOGGER = logging.getLogger("med_agent.hot_path")
if not _LOGGER.handlers:
    _handler = logging.StreamHandler(sys.stderr)
    _handler.setFormatter(logging.Formatter("%(message)s"))
    _LOGGER.addHandler(_handler)
    _LOGGER.setLevel(logging.INFO)
    _LOGGER.propagate = False

# 测试可替换 sink；生产默认写 stderr 一行 JSON。
_sink: Callable[[str], None] | None = None


def hot_path_logging_enabled() -> bool:
    """HOT_PATH_LOG=0/false/off 关闭；缺省与 1/true/on 开启。"""

    raw = (os.getenv("HOT_PATH_LOG") or "1").strip().lower()
    return raw not in {"0", "false", "off", "no"}


def set_hot_path_log_sink(sink: Callable[[str], None] | None) -> None:
    """测试注入 sink；传 None 恢复默认 logger。"""

    global _sink
    _sink = sink


def new_request_id() -> str:
    """生成短 request_id，不含用户输入。"""

    return f"req-{uuid.uuid4().hex[:16]}"


def emit_hot_path_log(
    *,
    route: str,
    status: str,
    latency_ms: int | float,
    request_id: str | None = None,
    run_id: str | None = None,
    tool_name: str | None = None,
    error_code: str | None = None,
) -> dict[str, Any] | None:
    """写一条脱敏热路径日志；关闭时返回 None 且不输出。"""

    if not hot_path_logging_enabled():
        return None
    # 只允许有限 status，避免把自由文本当状态。
    normalized_status = status if status in {"ok", "error", "rejected", "timeout"} else "error"
    payload: dict[str, Any] = {
        "schema": "hot_path_v1",
        "request_id": request_id or new_request_id(),
        "run_id": run_id or "none",
        "route": route,
        "tool_name": tool_name or "none",
        "latency_ms": int(max(0, round(float(latency_ms)))),
        "status": normalized_status,
        "error_code": error_code or "none",
    }
    # 防御：禁止额外键混入敏感字段。
    line = json.dumps({key: payload[key] for key in HOT_PATH_LOG_FIELDS}, ensure_ascii=False, separators=(",", ":"))
    if _sink is not None:
        _sink(line)
    else:
        _LOGGER.info(line)
    return payload


def timed_ms(started: float | None = None) -> tuple[float, Callable[[], int]]:
    """返回 (start, latency_fn)；latency_fn 给出整毫秒。"""

    start = time.perf_counter() if started is None else started

    def latency() -> int:
        return int(max(0, round((time.perf_counter() - start) * 1000)))

    return start, latency


def assert_no_sensitive_keys(payload: Mapping[str, Any]) -> None:
    """测试辅助：拒绝 question/query/password 等键。"""

    banned = {"question", "query", "password", "api_key", "authorization", "dsn", "text", "messages"}
    lowered = {str(key).lower() for key in payload}
    overlap = lowered & banned
    if overlap:
        raise AssertionError(f"hot path log contains banned keys: {sorted(overlap)}")
