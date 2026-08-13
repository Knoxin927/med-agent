# 集中存放 M3 Agent 的框架无关领域层。
#
# 本目录拥有 AgentDecision、ToolCall、ToolObservation、AgentModelClient 端口、
# strict ToolRuntime 和有界工具调用循环；供应商 JSON、FastAPI、Chroma 客户端
# 与 httpx 对象都不得进入这些值对象，保持领域层可在不联网、不读 .env 的测试中运行。
