"""M3.6 公开事件值对象与 SSE 编码。"""

# 导入 json，把白名单字段编码为标准 SSE data 行。
import json
# 导入 dataclass，保证公开事件不可变。
from dataclasses import dataclass
# 导入 Any，承载已脱敏的 JSON 基础类型。
from typing import Any


@dataclass(frozen=True)
class PublicEvent:
    """只保存可对客户端公开的脱敏 SSE 事件。"""

    # 事件名只允许 run_started/tool_status/answer/sources/done/error。
    event: str
    # data 必须是 JSON 基础类型，不得包含异常对象或密钥。
    data: dict[str, Any]


def encode_agent_sse(event: PublicEvent) -> str:
    """把公开事件编码为标准 SSE 文本帧。"""

    # ensure_ascii=False 保留中文可读性；separators 避免多余空白。
    data_text = json.dumps(event.data, ensure_ascii=False, separators=(",", ":"))
    # 每帧以空行结束，客户端按帧解析。
    return f"event: {event.event}\ndata: {data_text}\n\n"
