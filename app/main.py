"""FastAPI 应用工厂和 M1.4 SSE 路由。"""

# 导入 asynccontextmanager，管理应用关闭时的 HTTP 连接释放。
from contextlib import asynccontextmanager
# 导入 asyncio，为首次依赖初始化提供并发保护和线程卸载。
import asyncio
# 导入 AsyncIterator 与 Callable，标注流式响应和可替换依赖。
from collections.abc import AsyncIterator, Callable
# 导入 dataclass，定义测试可注入的依赖集合。
from dataclasses import dataclass
# 导入 Any，允许真实与 fake LLM 使用同样的最小接口。
from typing import Any

# 导入 FastAPI 的请求模型、状态异常和应用类型。
from fastapi import FastAPI, HTTPException
# 导入 StreamingResponse，把异步事件编码为 text/event-stream。
from fastapi.responses import StreamingResponse
# 导入 Pydantic 字段约束，严格拒绝不合法 JSON 输入。
from pydantic import BaseModel, Field

# 导入真实上游客户端。
from app.llm.client import OpenAiCompatibleLlmClient
# 导入回答编排和 SSE 编码。
from app.rag.answering import build_messages, encode_sse_event, stream_answer_events
# 导入真实本地 Embedding。
from app.rag.embedding import BgeM3Embedder
# 导入 M7 已确认的 hybrid 聊天策略、BM25 与聊天边界值对象。
from app.rag.production_corpus import load_production_chunks
from app.retrieval_strategies.bm25 import SELECTED_TOKENIZER_ID, Bm25RetrievalStrategy
from app.retrieval_strategies.dense import DenseRetrievalStrategy
from app.retrieval_strategies.hybrid import HybridRrfRetrievalStrategy
from app.retrieval_strategies.types import RankedChunk
# 导入配置加载和脱敏配置异常。
from app.settings import AppSettings, SettingsError, load_settings
# 导入 Agent API 路由工厂与本机装配；未注入服务时不改变固定 RAG 行为。
from app.agent.api.assembly import build_local_agent_api_service
from app.agent.api.routes import create_agent_router
from app.agent.api.service import AgentApiService


# 表示 POST /chat/stream 接收的 JSON 请求。
class ChatRequest(BaseModel):
    # 问题必须至少包含一个非空白字符。
    question: str = Field(min_length=1)
    # top_k 必须是严格正整数，避免 JSON true 被当作整数 1。
    top_k: int = Field(default=3, gt=0, strict=True)


# 将真实或 fake 检索与 LLM 客户端集中成可注入依赖。
@dataclass
class AppDependencies:
    # 检索函数接收问题和 K，返回本轮检索快照。
    retrieve: Callable[[str, int], list[RankedChunk]]
    # LLM 客户端提供异步文字流。
    llm_client: Any
    # Agent API 服务可选；缺失时只提供固定 RAG，不伪造 Agent 成功。
    agent_service: AgentApiService | None = None


# 用真实配置、BGE-M3、BM25 与 Chroma 创建生产依赖；仅在首次聊天请求时调用。
def build_production_dependencies(settings: AppSettings) -> AppDependencies:
    # 即使调用方绕过 load_settings，也必须在创建任何生产资源前拒绝未批准策略。
    if settings.retrieval_method != "hybrid":
        raise SettingsError("RETRIEVAL_METHOD 仅允许 hybrid")
    # 创建一次 BGE-M3，后续聊天请求复用同一模型对象。
    encoder = BgeM3Embedder()
    # 与 Chroma 同批重建 fixed 切片语料，供 BM25 与 dense 融合。
    production_chunks = list(load_production_chunks())
    # dense 通道复用本机 Chroma；BM25 通道绑定同一批 TextChunk。
    dense_strategy = DenseRetrievalStrategy(encoder, settings.rag_chroma_path)
    bm25_strategy = Bm25RetrievalStrategy(
        production_chunks,
        tokenizer_id=SELECTED_TOKENIZER_ID,
    )
    # M7 正式报告确认的 hybrid_rrf 作为聊天默认检索。
    retrieval_strategy = HybridRrfRetrievalStrategy(dense_strategy, bm25_strategy)
    # 创建带超时和鉴权的上游客户端。
    llm_client = OpenAiCompatibleLlmClient(
        settings.llm_base_url,
        settings.llm_model,
        settings.llm_api_key,
        settings.llm_timeout_seconds,
    )
    # 定义绑定了 hybrid 策略的检索闭包。
    def retrieve(question: str, top_k: int) -> list[RankedChunk]:
        # 委托策略返回统一 RankedChunk，不把 raw score 泄漏到聊天边界。
        return retrieval_strategy.retrieve(question, top_k=top_k)

    # 本机进程内装配 Agent API：内存 store/approval + 真实 tool-calling 模型。
    agent_service = build_local_agent_api_service(
        retrieve,
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        api_key=settings.llm_api_key,
        timeout_seconds=settings.llm_timeout_seconds,
    )
    # 固定 RAG 与 Agent 共享同一 hybrid 检索闭包，但使用隔离的模型客户端。
    return AppDependencies(retrieve, llm_client, agent_service)


# 创建可生产运行、也可由测试注入 fake 依赖的 FastAPI 应用。
def create_app(dependencies: AppDependencies | None = None) -> FastAPI:
    # 在关闭时释放已创建的真实或 fake LLM 连接。
    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        # 将传入 fake 依赖保存到应用状态，不触发真实模型加载。
        application.state.dependencies = dependencies
        # 把控制权交给 FastAPI 处理请求。
        yield
        # 读取运行期可能懒创建的依赖。
        current_dependencies = application.state.dependencies
        # 先关闭 Agent 持有的 runtime/模型客户端，再关闭固定 RAG 文字流客户端。
        if current_dependencies is not None and current_dependencies.agent_service is not None:
            closer = getattr(current_dependencies.agent_service, "close", None)
            if closer is not None:
                closer()
        # 取得可选的异步关闭方法。
        close_method = getattr(current_dependencies.llm_client, "aclose", None) if current_dependencies else None
        # 真实客户端存在关闭方法时释放连接池。
        if close_method is not None:
            await close_method()
    # 创建带资源生命周期管理的 FastAPI 应用。
    application = FastAPI(lifespan=lifespan)
    # 提前保存注入依赖，使不启动 lifespan 的轻量 ASGI 测试也能读取一致状态。
    application.state.dependencies = dependencies
    # 每个应用实例各有一把锁，确保并发首请求只初始化一套真实依赖。
    application.state.dependencies_lock = asyncio.Lock()

    async def ensure_dependencies() -> AppDependencies:
        # 生产首次请求才加载 .env、模型和 Chroma；测试注入路径直接返回。
        current = application.state.dependencies
        if current is not None:
            return current
        async with application.state.dependencies_lock:
            current = application.state.dependencies
            if current is not None:
                return current
            try:
                settings = load_settings()
            except SettingsError as error:
                raise HTTPException(status_code=503, detail=str(error)) from error
            current = await asyncio.to_thread(build_production_dependencies, settings)
            application.state.dependencies = current
            return current

    async def get_agent_service() -> AgentApiService:
        # 生产路径可懒装配 Agent；测试未注入时仍返回稳定 503。
        current = await ensure_dependencies()
        if current.agent_service is None:
            raise HTTPException(status_code=503, detail="Agent 服务未装配")
        return current.agent_service

    # 挂载本机 Agent start/resume/cancel；固定 /chat/stream 仍保持原实现。
    application.include_router(create_agent_router(get_agent_service))

    # 注册健康检查，保持 M0 的对外契约不变。
    @application.get("/health")
    def health_check() -> dict[str, str]:
        # 返回 Python 字典，FastAPI 会自动序列化为 JSON。
        return {"status": "ok"}

    # 注册 M1.4 的流式问答接口。
    @application.post("/chat/stream")
    async def chat_stream(request: ChatRequest) -> StreamingResponse:
        # 空白字符串虽然通过最小长度，也必须在联网前拒绝。
        if not request.question.strip():
            raise HTTPException(status_code=422, detail="question 不能为空白")
        # 读取已经注入或之前请求创建的依赖；与 Agent 路径共享同一套懒加载。
        current_dependencies = await ensure_dependencies()
        # 检索失败也必须发生在 SSE 开始前。
        try:
            # BGE-M3 编码和 Chroma 查询都是同步工作，放入线程避免阻塞其他 SSE 请求。
            results = await asyncio.to_thread(current_dependencies.retrieve, request.question, request.top_k)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        # 用同一份结果快照构造模型消息。
        messages = build_messages(request.question, results) if results else []

        # 定义将内部事件逐帧编码为 SSE 的异步生成器。
        async def event_stream() -> AsyncIterator[str]:
            # 逐个产生 token、sources、done 或 error 的标准文本帧。
            async for event in stream_answer_events(results, messages, current_dependencies.llm_client):
                yield encode_sse_event(event)
        # 声明标准 SSE Content-Type，交给客户端持续读取。
        return StreamingResponse(event_stream(), media_type="text/event-stream")

    # 返回工厂创建的应用，供 Uvicorn 和 pytest 使用。
    return application


# 创建默认生产应用；它在首次聊天请求前不会读取密钥或加载模型。
app = create_app()
