"""M3.6 Agent start/resume/cancel 路由：只做 HTTP 校验与 SSE 编码。"""

# 导入 AsyncIterator，标注 SSE 异步生成器。
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from inspect import isawaitable
# 导入 FastAPI 组件，路由不拥有 graph/store 逻辑。
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
# 导入公开事件编码与服务错误。
from app.agent.api.events import PublicEvent, encode_agent_sse
from app.agent.api.service import AgentApiError, AgentApiService
from app.agent.store.port import AgentRunRecord


class StartAgentRequest(BaseModel):
    """启动 Agent run 的最小请求。"""

    # 问题必须非空；空白会在服务层再次拒绝。
    question: str = Field(min_length=1)


class ResumeAgentRequest(BaseModel):
    """恢复 pending approval 的最小请求。"""

    # decision 只允许 approve/reject/cancel，不接受替换 graph state。
    decision: str = Field(min_length=1)
    # owner 默认本地操作者，API 不做公网鉴权。
    owner: str = "local-owner"
    # 可选 version 用于客户端乐观并发保护。
    expected_version: int | None = None


def create_agent_router(
    get_service: Callable[[], AgentApiService | Awaitable[AgentApiService]],
) -> APIRouter:
    """创建只依赖服务工厂的 Agent 路由。"""

    router = APIRouter(prefix="/agent")

    async def resolve_service() -> AgentApiService:
        # 生产路径可能异步懒装配；测试路径可直接返回服务。
        service = get_service()
        if isawaitable(service):
            service = await service
        return service

    def stream_public_items(items: Iterator[PublicEvent | AgentRunRecord], *, status_code: int) -> StreamingResponse:
        """把服务增量事件编码为 SSE；记录对象只用于结束迭代，不输出。"""

        async def event_stream() -> AsyncIterator[str]:
            for item in items:
                if isinstance(item, PublicEvent):
                    # 每个 CAS 提交后的事件立即 flush 给客户端。
                    yield encode_agent_sse(item)

        return StreamingResponse(event_stream(), media_type="text/event-stream", status_code=status_code)

    @router.post("/runs")
    async def start_run(request: StartAgentRequest) -> StreamingResponse:
        # 空白问题在 SSE 开始前返回 JSON 错误。
        if not request.question.strip():
            raise HTTPException(status_code=422, detail="question 不能为空白")
        try:
            items = (await resolve_service()).iter_start(request.question)
        except AgentApiError as error:
            raise HTTPException(status_code=error.status_code, detail=error.message) from error
        return stream_public_items(items, status_code=201)

    @router.post("/runs/{run_id}/resume")
    async def resume_run(run_id: str, request: ResumeAgentRequest) -> StreamingResponse:
        if request.decision not in {"approve", "reject", "cancel"}:
            raise HTTPException(status_code=422, detail="decision 不合法")
        try:
            items = (await resolve_service()).iter_resume(
                run_id,
                request.decision,  # type: ignore[arg-type]
                owner=request.owner,
                expected_version=request.expected_version,
            )
        except AgentApiError as error:
            raise HTTPException(status_code=error.status_code, detail=error.message) from error
        return stream_public_items(items, status_code=200)

    @router.post("/runs/{run_id}/cancel", status_code=202)
    async def cancel_run(run_id: str) -> dict:
        try:
            record, events = (await resolve_service()).cancel(run_id)
        except AgentApiError as error:
            raise HTTPException(status_code=error.status_code, detail=error.message) from error
        # cancel 返回 202 JSON 确认；若已有终态投影，同时附带脱敏事件。
        return {
            "run_id": record.run_id,
            "version": record.version,
            "status": record.status.value,
            "events": [{"event": event.event, "data": event.data} for event in events],
        }

    return router
