"""M3.6 本机 Agent API 依赖装配：只组装已有 runner/store/tool，不复制 graph 分支。"""

# 导入 Callable/Any，声明检索闭包与 OpenAI 工具 schema。
from collections.abc import Callable
from typing import Any

# 导入 Agent API 服务与 graph/store/approval 既有端口。
from app.agent.api.service import AgentApiService
from app.agent.approval import InMemoryApprovalRepository
from app.agent.graph.runner import AgentGraphRunner
from app.agent.model_client import OpenAiToolCallingClient
from app.agent.search_knowledge_tool import build_search_knowledge_tool_spec
from app.agent.store import AgentRunCheckpointCoordinator, InMemoryAgentRunStore
from app.agent.tool_runtime import ToolRuntime, ToolSpec
from app.agent.tools.create_follow_up_request import CreateFollowUpRequestService
from app.agent.types import ApprovalPolicy, ToolCall, ToolEffect, ToolObservation
from app.retrieval_strategies.types import RankedChunk


def _validate_create_follow_up_arguments(call: ToolCall) -> str | None:
    """严格校验本地随访工具参数；非法参数必须在审批前失败。"""

    # 只允许 topic，避免模型塞入隐藏字段绕过摘要冻结。
    unknown = set(call.arguments) - {"topic"}
    if unknown:
        return f"不允许的字段: {sorted(unknown)}"
    topic = call.arguments.get("topic")
    if not isinstance(topic, str) or not topic.strip():
        return "topic 必须是非空字符串"
    return None


def _execute_create_follow_up_should_never_run(call: ToolCall) -> ToolObservation:
    """副作用工具只能走审批后的 create_once 路径，runtime 不得直接执行。"""

    raise AssertionError(f"create_follow_up_request 不得由 runtime 直接执行: {call.call_id}")


def _build_create_follow_up_openai_tool() -> dict[str, Any]:
    """返回 create_follow_up_request 的最小 OpenAI 工具定义。"""

    return {
        "type": "function",
        "function": {
            "name": "create_follow_up_request",
            "description": "当用户明确要求创建、提交或安排本地随访请求时必须调用此工具。它会先进入人工审批，批准前不会写入；不要用文字回答代替工具调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": "随访主题，仅本地非敏感文本。",
                    }
                },
                "required": ["topic"],
                "additionalProperties": False,
            },
        },
    }


def build_create_follow_up_tool_spec() -> ToolSpec:
    """装配需审批的本地随访工具规格。"""

    return ToolSpec(
        tool_name="create_follow_up_request",
        openai_tool=_build_create_follow_up_openai_tool(),
        validator=_validate_create_follow_up_arguments,
        executor=_execute_create_follow_up_should_never_run,
        effect=ToolEffect.side_effect,
        approval_policy=ApprovalPolicy.required,
    )


def build_local_agent_api_service(
    retrieve: Callable[[str, int], list[RankedChunk]],
    *,
    base_url: str,
    model: str,
    api_key: str,
    timeout_seconds: float,
) -> AgentApiService:
    """用本机内存 store/approval 与真实模型端口装配可注入的 AgentApiService。"""

    # search_knowledge 需要 keyword top_k，与聊天 retrieve(question, top_k) 对齐。
    def tool_retrieve(query: str, *, top_k: int) -> list[RankedChunk]:
        return retrieve(query, top_k)

    # 只读检索 + 需审批随访；副作用仅在 allow_approved_effects 时允许注册。
    runtime = ToolRuntime(
        [
            build_search_knowledge_tool_spec(tool_retrieve),
            build_create_follow_up_tool_spec(),
        ],
        allow_approved_effects=True,
    )
    # 结构化 Tool Calling 客户端与固定 RAG 文字流客户端隔离，但复用同一上游配置。
    model_client = OpenAiToolCallingClient(
        base_url,
        model,
        api_key,
        timeout_seconds,
        runtime.list_definitions(),
    )
    # 本机进程内默认使用内存 store/approval；Postgres 路径留给显式依赖注入。
    store = InMemoryAgentRunStore()
    repository = InMemoryApprovalRepository()
    approval = CreateFollowUpRequestService(repository, repository)
    runner = AgentGraphRunner(model_client, runtime, approval_service=approval)
    coordinator = AgentRunCheckpointCoordinator(store, runner)
    service = AgentApiService(store, coordinator, approval_service=approval)

    def cleanup() -> None:
        # 先关 runtime 线程池，再关模型 HTTP 连接，避免测试/进程退出泄漏。
        runtime.close(wait=False)
        model_client.aclose()

    service.attach_cleanup(cleanup)
    return service
