"""构造 Agent 模型消息，并把 observation 回填为模型可见的结构化文本。"""

# 导入 json，把 observation 内容编码成稳定的 JSON 字符串。
import json
# 导入 Any，标注 OpenAI-compatible 消息字典的取值类型。
from typing import Any

# 导入 ToolCall 值对象，把它编码为 assistant 消息的 tool_calls 字段。
from app.agent.types import ToolCall, ToolObservation


# 固定 Agent 系统提示词，约束模型只能使用 search_knowledge 或直接给出完整回答。
AGENT_SYSTEM_PROMPT = (
    "你是医疗知识库 Agent。普通知识问题可以直接回答，也可以调用 search_knowledge 检索本地知识库后再回答。"
    "如果问题明显不在本地医疗知识库范围内，必须明确拒答，并在最终回答中同时包含“资料库”和“无法”这两个词，"
    "说明当前资料库无法支持该问题；不要编造库外答案。"
    "如果用户明确要求创建、提交或安排本地随访请求，必须先调用 create_follow_up_request，不能只用文字声称已创建；"
    "该工具需要人工审批，批准前不得声称副作用已经发生。"
    "一次只能调用一个工具，禁止同轮并行多个 tool_calls；若问题包含多个方面，请用一次 search_knowledge，把关键信息合并进同一个 query；调用得到结果后必须给出完整最终回答；不要编造来源，不要把回答表述为医生诊断。"
)


# 构造本轮初始消息：固定系统约束加上用户的问题。
def build_initial_agent_messages(question: str) -> list[dict[str, Any]]:
    """返回 [system, user] 两条消息，作为第一次 model.decide 的输入。"""

    # 系统消息固定 Agent 行为边界，模型不能自由改写它。
    return [
        # 固定系统约束排在首位，任何 OpenAI-compatible 端点都会优先遵守它。
        {"role": "system", "content": AGENT_SYSTEM_PROMPT},
        # 用户消息只携带真实问题，不预先注入检索上下文。
        {"role": "user", "content": f"问题：{question}"},
    ]


# 把一次已接受的 ToolCall 编码为 OpenAI assistant 消息，保留 call_id 与参数。
def format_assistant_tool_call_message(tool_call: ToolCall) -> dict[str, Any]:
    """返回 assistant 的 tool_calls 消息，与后续 observation 通过 call_id 配对。"""

    # arguments 在 OpenAI 协议中是 JSON 字符串，而不是对象；这里固定编码为非转义中文。
    arguments_text = json.dumps(tool_call.arguments, ensure_ascii=False)
    # 返回标准 OpenAI 形状：assistant 消息带唯一 tool_calls 项。
    return {
        # assistant 角色表示这是模型自己上一步的提议。
        "role": "assistant",
        # 提议工具调用时不携带正文，content 留空。
        "content": None,
        # tool_calls 是 OpenAI-compatible 端点回读 call_id 与参数的标准字段。
        "tool_calls": [
            {
                "id": tool_call.call_id,
                "type": "function",
                "function": {
                    "name": tool_call.tool_name,
                    "arguments": arguments_text,
                },
            }
        ],
    }


# 把成功 observation 的 chunk 快照编码成模型可读的非敏感上下文。
def _format_success_observation_content(observation: ToolObservation) -> str:
    """构造包含来源编号与原文的上下文，让模型基于快照而非重新检索来回答。"""

    # authority_payload 只属于 MCP 权威检索，绝不能回填给 Agent 模型。
    if observation.authority_payload is not None:
        raise ValueError("authority_payload 不得进入 Agent 消息编码")
    # 为每条 chunk 添加来源编号，与 M1.4 回答编排的上下文格式保持一致。
    context_lines = [
        f"[来源 {index}: {chunk.source_name}#{chunk.chunk_index}]\n{chunk.text}"
        # enumerate 从 1 开始编号，便于模型自然引用。
        for index, chunk in enumerate(observation.chunks, start=1)
    ]
    # 把上下文包装为单一 JSON 字段，便于模型稳定解析 observation 结构。
    return json.dumps(
        {"success": True, "chunks": "\n\n".join(context_lines)},
        ensure_ascii=False,
    )


# 把失败 observation 的稳定错误编码成模型可见的反馈，不泄露原始异常。
def _format_failure_observation_content(observation: ToolObservation) -> str:
    """构造脱敏失败反馈，让模型看到错误分类而不是堆栈或密钥细节。"""

    # 错误码已是字符串枚举，可直接放入 JSON；保持稳定布局便于测试断言。
    return json.dumps(
        {
            "success": False,
            "error_code": None if observation.error_code is None else observation.error_code.value,
            "error_message": observation.error_message,
        },
        ensure_ascii=False,
    )


# 把 observation 整体编码为 OpenAI tool 角色消息，作为下一次 model.decide 的输入。
def format_tool_observation_message(observation: ToolObservation) -> dict[str, Any]:
    """返回 tool 角色消息，用 tool_call_id 与上一步 assistant 提议严格配对。"""

    # 成功与失败分别构造内容，保证成功路径携带可用于回答的原文快照。
    if observation.success:
        content = _format_success_observation_content(observation)
    # 失败路径只给稳定的错误反馈，不在消息里重新执行检索。
    else:
        content = _format_failure_observation_content(observation)
    # 返回 OpenAI 标准的 tool 回答消息，tool_call_id 必须与 assistant 提议一致。
    return {"role": "tool", "tool_call_id": observation.call_id, "content": content}
