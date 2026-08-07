"""M3.6 Agent 公开 API 与 SSE 适配层。"""

# 公开路由装配入口；领域事实仍由 store/runner 拥有。
from app.agent.api.routes import create_agent_router
from app.agent.api.service import AgentApiService

__all__ = ["AgentApiService", "create_agent_router"]
