# M6.1 候选文件人工复核表

下表为每个待发布文件生成用途、许可证来源和外部数据状态。所有行都必须由项目所有者复核后，才能申请创建仓库授权。

| 文件 | 用途 | 许可证来源 | 外部数据状态 |
| --- | --- | --- | --- |
| `.dockerignore` | 容器构建上下文排除规则 | 项目原创配置，项目所有者待确认 | 不含业务外部数据 |
| `.gitignore` | Git 本地派生文件排除规则 | 项目原创配置，项目所有者待确认 | 不含业务外部数据 |
| `Dockerfile` | 本地容器构建说明 | 项目原创配置，基础镜像许可证待复核 | 不应包含外部数据，待项目所有者复核 |
| `LICENSE` | 公开代码的 MIT 许可证文本 | 项目所有者已选择 MIT；不重新授权 MedlinePlus 原始材料 | 仅为许可证文本，不含业务数据 |
| `README.md` | 公开候选仓库的技术 README，描述架构、能力边界、启动命令与指标来源 | 项目原创文档，创建公开仓库前由项目所有者确认 | 只引用公开候选内的源码/测试路径与 synthetic-only 边界声明，不含个体身份信息或真实医学数据 |
| `app/agent/__init__.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/agent/api/__init__.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/agent/api/assembly.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/agent/api/events.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/agent/api/projector.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/agent/api/routes.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/agent/api/service.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/agent/approval/__init__.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/agent/approval/memory.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/agent/approval/port.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/agent/approval/postgres.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/agent/approval/reconciliation.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/agent/db/__init__.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/agent/db/migrations.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/agent/evaluation/__init__.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/agent/evaluation/aggregate.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/agent/evaluation/decision.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/agent/evaluation/grader.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/agent/evaluation/manifest.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/agent/evaluation/reporting.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/agent/evaluation/runner.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/agent/evaluation/types.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/agent/graph/__init__.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/agent/graph/approval.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/agent/graph/runner.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/agent/graph/state.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/agent/loop.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/agent/messages.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/agent/model_client.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/agent/search_knowledge_tool.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/agent/store/__init__.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/agent/store/checkpoint.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/agent/store/memory.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/agent/store/port.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/agent/store/postgres.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/agent/tool_runtime.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/agent/tools/__init__.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/agent/tools/create_follow_up_request.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/agent/types.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/evaluation/__init__.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/evaluation/cache/__init__.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/evaluation/cache/aggregate.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/evaluation/cache/decision.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/evaluation/cache/keys.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/evaluation/cache/manifest.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/evaluation/cache/reporting.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/evaluation/cache/scan.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/evaluation/cache/store.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/evaluation/cache/types.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/evaluation/closure/__init__.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/evaluation/closure/aggregate.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/evaluation/closure/decision.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/evaluation/closure/manifest.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/evaluation/closure/reporting.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/evaluation/closure/scan.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/evaluation/closure/types.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/evaluation/comparison.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/evaluation/cost/__init__.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/evaluation/cost/aggregate.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/evaluation/cost/decision.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/evaluation/cost/manifest.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/evaluation/cost/reporting.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/evaluation/cost/scan.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/evaluation/cost/types.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/evaluation/data.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/evaluation/load/__init__.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/evaluation/load/aggregate.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/evaluation/load/decision.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/evaluation/load/manifest.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/evaluation/load/reporting.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/evaluation/load/scan.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/evaluation/load/types.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/evaluation/metrics.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/evaluation/quality/__init__.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/evaluation/quality/aggregate.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/evaluation/quality/annotations.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/evaluation/quality/decision.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/evaluation/quality/details.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/evaluation/quality/manifest.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/evaluation/quality/reporting.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/evaluation/quality/scan.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/evaluation/quality/types.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/evaluation/reporting.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/evaluation/rerank_smoke.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/evaluation/rewrite_snapshot.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/evaluation/runner.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/evaluation/types.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/evaluation/worker.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/llm/__init__.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/llm/client.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/main.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/mcp/__init__.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/mcp/authority_assembly.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/mcp/authority_entrypoint.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/mcp/authority_fetch.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/mcp/authority_mode.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/mcp/authority_registry.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/mcp/authority_search.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/mcp/authority_types.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/mcp/codec.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/mcp/errors.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/mcp/knowledge_entrypoint.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/mcp/knowledge_search.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/mcp/probe.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/mcp/registry.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/mcp/retrieval_assembly.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/mcp/server.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/observability/__init__.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/observability/decision.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/observability/events.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/observability/metrics.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/observability/recorder.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/observability/reporting.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/observability/scan.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/observability/types.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/ops/__init__.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/ops/hot_path_log.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/rag/__init__.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/rag/answering.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/rag/chunking.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/rag/embedding.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/rag/ingestion.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/rag/production_corpus.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/rag/retrieval.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/rag/retrieval_types.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/rag/vector_store.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/retrieval_strategies/__init__.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/retrieval_strategies/bm25.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/retrieval_strategies/dense.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/retrieval_strategies/hybrid.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/retrieval_strategies/rerank.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/retrieval_strategies/rewrite.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/retrieval_strategies/types.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `app/settings.py` | 医疗 Agent 的可审计应用源代码 | 项目原创代码，创建公开仓库前由项目所有者确认 | 不应包含外部数据，待项目所有者逐文件复核 |
| `docker-compose.yml` | 本地多服务启动编排 | 项目原创配置，镜像许可证待复核 | 不应包含外部数据，待项目所有者复核 |
| `docs/PUBLIC_REPRODUCIBILITY.md` | 说明公开 clone 的离线复现范围、命令和非目标 | 项目原创文档 | 不含真实 provider、私有报告、个人路径或医疗个案 |
| `fixtures/public/agent-run-summary.json` | 公开评测样本结构 schema 与脱敏 synthetic-only fixture 结果 | 项目原创 schema 与结果 fixture | 不含外部语料、真实 query、答案、provider trace 或个人数据 |
| `fixtures/public/evaluation-v2.schema.json` | 公开评测样本结构 schema 与脱敏 synthetic-only fixture 结果 | 项目原创 schema 与结果 fixture | 不含外部语料、真实 query、答案、provider trace 或个人数据 |
| `fixtures/public/health_topics.txt` | 公开 clone 的最小、非敏感检索与切片输入 fixture | 项目原创文本，不复制真实语料或医疗问答 | 仅含抽象健康教育工程样本，不含个人身份信息、病例或真实评测数据 |
| `fixtures/public/sample-result.json` | 公开评测样本结构 schema 与脱敏 synthetic-only fixture 结果 | 项目原创 schema 与结果 fixture | 不含外部语料、真实 query、答案、provider trace 或个人数据 |
| `requirements-dev.txt` | 开发与测试依赖声明 | 第三方依赖许可证需按依赖清单逐项复核 | 依赖名称和版本，不含业务外部数据 |
| `requirements.txt` | 运行时依赖声明 | 第三方依赖许可证需按依赖清单逐项复核 | 依赖名称和版本，不含业务外部数据 |
| `tests/contract/test_public_contract.py` | 验证公开 fixture、schema 和基础切片契约的离线测试 | 项目原创测试代码 | 只使用公开 fixture，不含私有测试、个人信息或真实评测数据 |

复核结论：pending_owner_review
