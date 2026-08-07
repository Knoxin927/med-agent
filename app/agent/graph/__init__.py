"""M3.3 图编排的项目拥有状态层；LangGraph 接入前不依赖任何框架对象。"""
# 公开唯一 production runner，调用方不应从此包获得旧 bounded loop。
from app.agent.graph.runner import AgentGraphRunner
from app.agent.graph.approval import apply_approval_outcome

__all__ = ["AgentGraphRunner", "apply_approval_outcome"]
