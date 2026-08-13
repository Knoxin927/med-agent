"""解析 OpenAI-compatible SSE 上游流，不向业务层泄漏第三方帧。"""

# 导入 json，解析上游每个 data 帧中的 JSON。
import json
# 导入 AsyncIterator，标注异步文字增量流。
from collections.abc import AsyncIterator
# 导入 Any，表达第三方 JSON 的运行时形状。
from typing import Any

# 导入 httpx，异步读取上游 HTTP SSE 响应。
import httpx


# 表示上游网络、状态码或帧格式错误；对外只返回脱敏描述。
class UpstreamError(RuntimeError):
    """OpenAI-compatible 服务没有满足预期的流式契约。"""


# 把严格校验过的 OpenAI delta JSON 转换成可输出的文字或 None。
def parse_openai_delta(payload_text: str) -> str | None:
    # 尝试把 data 内容解析成 JSON。
    try:
        payload: Any = json.loads(payload_text)
    # 损坏 JSON 不是可忽略帧。
    except json.JSONDecodeError as error:
        raise UpstreamError("上游返回了无法解析的数据") from error
    # 顶层必须是对象。
    if not isinstance(payload, dict):
        raise UpstreamError("上游返回数据结构不正确")
    # choices 必须是非空列表。
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise UpstreamError("上游返回数据结构不正确")
    # 当前最小契约只读取第一项。
    first_choice = choices[0]
    if not isinstance(first_choice, dict):
        raise UpstreamError("上游返回数据结构不正确")
    # delta 必须是对象。
    delta = first_choice.get("delta")
    if not isinstance(delta, dict):
        raise UpstreamError("上游返回数据结构不正确")
    # role-only 帧没有 content，按设计合法忽略。
    if "content" not in delta:
        return None
    # 读取本帧正文；JSON null 是部分 OpenAI-compatible 服务的无正文结束帧。
    content = delta["content"]
    # null 不携带正文，按空增量忽略而不是误判为上游故障。
    if content is None:
        return None
    # 除 JSON null 外，content 必须是字符串。
    if not isinstance(content, str):
        raise UpstreamError("上游返回数据结构不正确")
    # 空字符串不产生 token。
    return content or None


# 对外提供一个只产出非空文字增量的窄上游客户端。
class OpenAiCompatibleLlmClient:
    # 保存已创建的异步 HTTP 客户端和模型配置。
    def __init__(self, base_url: str, model: str, api_key: str, timeout_seconds: float) -> None:
        # 保存模型名，写入上游请求体。
        self._model = model
        # 创建只在本机进程内保存 Authorization header 的 HTTP 客户端。
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout_seconds,
            trust_env=False,
        )

    # 将已构造的消息发送给上游，并逐段产出可显示文字。
    async def stream(self, messages: list[dict[str, str]]) -> AsyncIterator[str]:
        # 记录是否收到唯一允许正常结束的 [DONE] 标记。
        received_done = False
        # 以流模式发送请求，避免把完整回答积累在内存中。
        async with self._client.stream(
            "POST",
            "/chat/completions",
            json={"model": self._model, "messages": messages, "stream": True},
        ) as response:
            # 非 2xx 是上游失败，不能伪装成正常流。
            if response.status_code < 200 or response.status_code >= 300:
                raise UpstreamError("上游模型服务不可用")
            # 只接受 SSE 响应，避免错误 HTML/JSON 被当作 token。
            if "text/event-stream" not in response.headers.get("content-type", ""):
                raise UpstreamError("上游响应不是 SSE 流")
            # 逐行读取 SSE 文本。
            async for line in response.aiter_lines():
                # 空行、注释和 event 标签不携带本阶段需要的数据。
                if not line or line.startswith(":") or line.startswith("event:"):
                    continue
                # 非 data 行不属于支持的最小协议。
                if not line.startswith("data:"):
                    raise UpstreamError("上游 SSE 帧格式不正确")
                # 取出 data: 后的实际载荷。
                data_text = line.removeprefix("data:").strip()
                # DONE 后任何新的 data 都属于协议错误。
                if received_done:
                    raise UpstreamError("上游在结束后继续发送数据")
                # 只有 [DONE] 能正常结束上游流。
                if data_text == "[DONE]":
                    received_done = True
                    continue
                # 解析普通增量帧。
                content = parse_openai_delta(data_text)
                # 只把非空文字交给回答编排层。
                if content is not None:
                    yield content
        # HTTP EOF 不是生成完成证明；必须显式收到 [DONE]。
        if not received_done:
            raise UpstreamError("上游流在完成前中断")

    # 关闭连接池，供应用关闭或客户端断开后的资源清理调用。
    async def aclose(self) -> None:
        # 释放 httpx 持有的连接资源。
        await self._client.aclose()
