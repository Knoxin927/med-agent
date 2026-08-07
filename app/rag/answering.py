"""把检索结果转换为受控提示词和稳定的回答事件。"""

# 导入 json，把事件数据编码为 SSE 的 JSON 字符串。
import json
# 导入 AsyncIterator，标注异步 LLM 文字流和回答事件流。
from collections.abc import AsyncIterator
# 导入 dataclass，定义不可变回答事件。
from dataclasses import dataclass
# 导入 Any，允许测试 fake LLM 使用相同最小接口。
from typing import Any

# 导入 httpx，把网络层异常转换成稳定的对外错误事件。
import httpx

# 导入上游适配错误，统一转成脱敏 error 事件。
from app.llm.client import UpstreamError
# 导入 M2 统一的有序检索结果，聊天边界不再直接消费 M1 distance。
from app.retrieval_strategies.types import RankedChunk


# 表示服务自己对外发出的稳定事件，而不是第三方原始帧。
@dataclass(frozen=True)
class AnswerEvent:
    # 保存 SSE event 名称，只允许 token、sources、error、done。
    event: str
    # 保存 JSON 可序列化的事件数据。
    data: dict[str, Any]


# 根据问题和同一份检索快照构造 LLM 消息。
def build_messages(question: str, results: list[RankedChunk]) -> list[dict[str, str]]:
    # 为每条资料添加来源编号，让模型看到的上下文可与 sources 对照。
    context = "\n\n".join(
        f"[来源 {index}: {result.source_name}#{result.chunk_index}]\n{result.text}"
        for index, result in enumerate(results, start=1)
    )
    # 系统消息固定回答边界，不能把模型输出当作医学诊断。
    system_message = (
        "你是医疗知识库问答助手。只能依据给定上下文回答；资料不足时明确说明。"
        "不要编造来源，不要把回答表述为医生诊断。"
    )
    # 用户消息同时携带真实问题和可追溯上下文。
    user_message = f"问题：{question}\n\n上下文：\n{context}"
    # 返回 OpenAI-compatible chat/completions 所需的最小消息形状。
    return [
        {"role": "system", "content": system_message},
        {"role": "user", "content": user_message},
    ]


# 从检索快照构造来源事件；它永远不依赖模型输出。
def build_sources_event(results: list[RankedChunk]) -> AnswerEvent:
    # 保存已写入的来源与块序号，保证引用按检索顺序去重。
    seen_source_chunks: set[tuple[str, int]] = set()
    # 逐条构造最终要返回的脱敏引用。
    items: list[dict[str, Any]] = []
    # 按当前 RankedChunk 快照顺序遍历检索结果。
    for result in results:
        # 同一文件同一块代表同一可追溯来源。
        source_chunk = (result.source_name, result.chunk_index)
        # 重复来源保留第一次出现的检索排名和方法名。
        if source_chunk in seen_source_chunks:
            continue
        # 记录该来源已经写入，防止后续重复。
        seen_source_chunks.add(source_chunk)
        # v2 只保留稳定来源、排名和方法，不公开 distance 或方法内 raw score。
        items.append(
            {
                "source_name": result.source_name,
                "chunk_index": result.chunk_index,
                "rank": result.rank,
                "method": result.method,
            }
        )
    # 返回单个 sources 事件。
    return AnswerEvent("sources", {"schema_version": 2, "items": items})


# 将检索快照和 LLM 异步文字流编排成固定顺序的回答事件。
async def stream_answer_events(
    results: list[RankedChunk],
    messages: list[dict[str, str]],
    llm_client: Any,
) -> AsyncIterator[AnswerEvent]:
    # 空检索没有可交给模型的可靠上下文，因此不调用 LLM。
    if not results:
        # 先明确返回空来源。
        yield build_sources_event(results)
        # 再用 done 表示这次正常的空结果已经结束。
        yield AnswerEvent("done", {})
        # 停止生成器，避免继续进入上游调用。
        return
    # 先取得本次请求独有的上游异步迭代器，便于在客户端断开时显式关闭它。
    upstream_stream = llm_client.stream(messages)
    # 捕获上游流中的网络和协议错误。
    try:
        # 逐个读取已经被适配器校验的非空文字增量。
        async for text in upstream_stream:
            # 每一段文字转换成服务自己的 token 事件。
            yield AnswerEvent("token", {"text": text})
    # 只向客户端暴露稳定、脱敏的错误信息。
    except (UpstreamError, httpx.HTTPError):
        # 流中失败后只能发送一次 error，不能再给出来源或 done。
        yield AnswerEvent("error", {"code": "upstream_error", "message": "上游模型服务不可用"})
        # 立即结束事件流。
        return
    # 无论正常结束、上游失败还是下游客户端断开，都要释放本次上游流。
    finally:
        # 异步生成器通常提供 aclose；测试替身也可选择实现同名方法。
        close_method = getattr(upstream_stream, "aclose", None)
        # 只关闭本次请求的流，不关闭由应用共享的 HTTP 连接池。
        if close_method is not None:
            await close_method()
    # 上游正常完成后，来源快照才可安全发送。
    yield build_sources_event(results)
    # done 表示完整事件序列已正常结束。
    yield AnswerEvent("done", {})


# 将稳定事件编码为标准 SSE 文本帧。
def encode_sse_event(event: AnswerEvent) -> str:
    # JSON 使用 ensure_ascii=False，保证中文正文不被转义为 Unicode 编号。
    data_text = json.dumps(event.data, ensure_ascii=False, separators=(",", ":"))
    # SSE 用 event 行标识事件名，用 data 行承载 JSON，并以空行结束一帧。
    return f"event: {event.event}\ndata: {data_text}\n\n"
