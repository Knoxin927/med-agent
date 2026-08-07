"""M3.4 AgentRunStore 的框架无关端口与测试实现。"""

# 公开 store 的领域对象与内存 fake；PostgreSQL adapter 需经过单独依赖确认。
from app.agent.store.memory import InMemoryAgentRunStore
from app.agent.store.checkpoint import AgentRunCheckpointCoordinator
from app.agent.store.port import (
    AgentRunRecord, AgentRunStore, NewAgentRun, PersistedRunControl, RunStatus, StoreConflict,
    RunNotFound, TerminalRun,
)

__all__ = [
    "AgentRunRecord", "AgentRunStore", "NewAgentRun", "PersistedRunControl", "RunStatus",
    "StoreConflict", "RunNotFound", "TerminalRun", "InMemoryAgentRunStore", "AgentRunCheckpointCoordinator",
]
