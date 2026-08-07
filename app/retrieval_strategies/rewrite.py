"""为 M2.4 提供受控 Query 改写与 dense 检索的离线编排。"""

# 导入 dataclasses.replace，用于只替换策略方法名并保留 dense 证据字段。
from dataclasses import dataclass, replace
# 导入 Mapping，声明 usage 是只读键值视图而不是可变字典。
from collections.abc import Mapping
# 导入 MappingProxyType，把上游 usage 副本冻结为只读映射。
from types import MappingProxyType
# 导入 Protocol，声明真实 API 与 fake 测试共享的窄改写接口。
from typing import Protocol

# 导入 httpx，向 OpenAI-compatible 服务发送一次非流式请求。
import httpx

# 导入统一检索策略契约、排名结果和返回值校验器。
from app.retrieval_strategies.types import (
    RankedChunk,
    RetrievalStrategy,
    validate_ranked_chunks,
)


# 表示改写输出或改写服务不满足本阶段受控契约。
class QueryRewriteError(RuntimeError):
    """Query 改写未生成可安全交给检索器的一行文本。"""


# 保存一次改写的文本和上游身份，供快照把结果与证据绑定。
@dataclass(frozen=True)
class QueryRewriteResult:
    """表示经过校验的改写文本、模型身份和可选 token usage。"""

    # 保存最终交给 dense 的单行查询文本。
    rewritten_query: str
    # 保存真实上游响应声明的模型身份。
    model: str
    # 保存上游提供的只读 usage；没有提供时明确为 None。
    usage: Mapping[str, int] | None


# 声明改写器只接收原问题并返回一条受控查询文本。
class QueryRewriter(Protocol):
    """隔离真实 HTTP 适配器与离线 fake 的最小改写边界。"""

    # 将一个用户问题改写为语义等价且更利于检索的查询。
    def rewrite(self, question: str) -> QueryRewriteResult:
        """返回改写文本及其模型证据，错误时显式失败。"""


# 固定 system prompt，限制模型只做单行语义等价改写。
QUERY_REWRITE_SYSTEM_PROMPT = (
    "只把用户问题改写为一行、语义等价且更利于检索的中文查询；"
    "不要回答问题、不要添加解释。"
)

# 限制空文本这种上游瞬态异常的总请求次数，防止无限重试产生不可控成本。
EMPTY_CONTENT_MAX_ATTEMPTS = 3


# 将已有运行配置适配为一次非流式 OpenAI-compatible Query 改写请求。
class OpenAiCompatibleQueryRewriter:
    """调用配置中的 `/chat/completions` 并返回受控改写证据。"""

    # 保存客户端和资源所有权，方便测试注入 MockTransport 后安全关闭。
    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str,
        timeout_seconds: float,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        # 真实构造必须保留用户传入的 URL，不硬编码项目外部服务地址。
        self._client = client or httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout_seconds,
            trust_env=False,
        )
        # 记录请求体使用的配置模型名。
        self._model = model
        # 标记客户端是否由本类创建，避免关闭调用方注入的测试客户端。
        self._owns_client = client is None

    # 从当前项目已校验的 AppSettings 创建生产改写器。
    @classmethod
    def from_settings(cls) -> "OpenAiCompatibleQueryRewriter":
        """只从现有配置入口读取 URL、model、密钥和超时。"""

        # 延迟导入配置，普通 fake 测试不会加载 .env 或要求密钥。
        from app.settings import load_settings

        # 复用项目已有的密钥校验和 URL 校验，不复制第二套配置逻辑。
        settings = load_settings()
        # 将已经校验的配置传入真实适配器。
        return cls(
            settings.llm_base_url,
            settings.llm_model,
            settings.llm_api_key,
            settings.llm_timeout_seconds,
        )

    # 执行受限次数的同步、非流式请求并解析为结构化改写结果。
    def rewrite(self, question: str) -> QueryRewriteResult:
        """返回文本、响应模型和 usage；只有空文本可重试，其他错误立即失败。"""

        # 原问题必须有内容，避免向 API 发送无法解释的空用户消息。
        if not isinstance(question, str) or not question.strip():
            # 不让空输入消耗一次真实 API 请求。
            raise QueryRewriteError("Query 改写输入不能为空")
        # 构造固定消息结构，system 约束放在 user 问题之前。
        request_payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": QUERY_REWRITE_SYSTEM_PROMPT},
                {"role": "user", "content": question},
            ],
            "stream": False,
            "temperature": 0,
            "max_tokens": 96,
            # Query 改写不需要展示推理，关闭思考以把有限输出预算留给完整单行查询。
            "thinking": {"type": "disabled"},
        }
        # 只给空白文本保留有限重试机会，其他协议或网络问题必须立即暴露。
        for attempt in range(1, EMPTY_CONTENT_MAX_ATTEMPTS + 1):
            # 发送请求时只捕获可安全归一化的 httpx 异常，不回显请求或响应内容。
            try:
                response = self._client.post("/chat/completions", json=request_payload)
            except httpx.TimeoutException:
                # 超时不泄露 URL、请求头或底层异常文本。
                raise QueryRewriteError("Query 改写请求超时") from None
            except httpx.HTTPError:
                # 其他网络错误统一成脱敏描述。
                raise QueryRewriteError("Query 改写请求失败") from None
            # 非 2xx 不读取响应体，避免服务端错误正文进入异常或日志。
            if response.status_code < 200 or response.status_code >= 300:
                # 状态码足以定位认证、路由或服务问题，且不包含响应体、密钥或请求头。
                raise QueryRewriteError(
                    f"Query 改写服务返回非成功状态（HTTP {response.status_code}）"
                )
            # 解析 JSON 时不把供应商返回正文放进错误消息。
            try:
                payload = response.json()
            except ValueError:
                # HTML、纯文本或损坏 JSON 都属于上游协议错误。
                raise QueryRewriteError("Query 改写响应不是有效 JSON") from None
            # 顶层必须是对象，不能从数组或标量猜测字段。
            if not isinstance(payload, dict):
                # 结构漂移必须 fail-closed。
                raise QueryRewriteError("Query 改写响应结构不正确")
            # 响应 model 是快照身份的一部分，缺失时不能使用请求 model 替代。
            response_model = payload.get("model")
            # choices 必须是非空列表，当前契约只使用第一项。
            choices = payload.get("choices")
            if not isinstance(response_model, str) or not response_model.strip():
                # 不允许把请求配置冒充真实响应身份。
                raise QueryRewriteError("Query 改写响应缺少模型身份")
            if not isinstance(choices, list) or not choices:
                # 空 choices 没有可验证的改写文本。
                raise QueryRewriteError("Query 改写响应缺少 choices")
            # 第一项必须是对象，并包含 message 对象。
            first_choice = choices[0]
            message = first_choice.get("message") if isinstance(first_choice, dict) else None
            # message.content 是唯一允许的文本来源。
            content = message.get("content") if isinstance(message, dict) else None
            # 空白字符串是已知上游瞬态现象，最多重试三次且绝不回退为原问题。
            if isinstance(content, str) and not content.strip():
                # 前两次空文本继续请求，第三次保留明确的诊断信息。
                if attempt < EMPTY_CONTENT_MAX_ATTEMPTS:
                    continue
                # 到达上限后 fail-closed，调用方可记录真实失败而非伪造查询。
                raise QueryRewriteError(
                    f"Query 改写结果连续 {EMPTY_CONTENT_MAX_ATTEMPTS} 次为空"
                )
            # 复用统一输出校验，拒绝非字符串、多行或其他不受控输出。
            rewritten_query = validate_rewritten_query(content)
            # 已拿到有效单行文本，退出重试循环并继续处理 usage。
            break
        # usage 缺失可以保留为 None；兼容服务可能额外返回嵌套明细。
        usage = payload.get("usage")
        if usage is not None:
            # 顶层 usage 必须是对象，不能把任意供应商结构写入快照。
            if not isinstance(usage, dict):
                # 不把供应商任意对象写入快照。
                raise QueryRewriteError("Query 改写 usage 结构不正确")
            # 只保留 OpenAI-compatible 通用的三项平面 token 数，嵌套明细不参与成本证据。
            token_fields = ("prompt_tokens", "completion_tokens", "total_tokens")
            normalized_usage_values = {
                key: usage[key]
                for key in token_fields
                if key in usage and type(usage[key]) is int and usage[key] >= 0
            }
            # 服务只返回嵌套明细或异常值时明确为不可用，不能编造零 token。
            normalized_usage = (
                MappingProxyType(normalized_usage_values)
                if normalized_usage_values
                else None
            )
        else:
            # 缺失 usage 必须明确记录为 None，不能补零。
            normalized_usage = None
        # 返回已绑定模型身份和 usage 的不可变改写结果。
        return QueryRewriteResult(rewritten_query, response_model.strip(), normalized_usage)

    # 释放真实客户端连接池；注入测试客户端由测试自行管理。
    def close(self) -> None:
        """关闭本类创建的 HTTP 客户端。"""

        # 只关闭自己创建的资源，避免破坏调用方传入的 MockTransport 客户端。
        if self._owns_client:
            # httpx.Client.close 不产生网络请求，只释放连接池资源。
            self._client.close()


# 校验并规范化改写器的文本输出，避免不受控内容进入检索。
def validate_rewritten_query(value: object) -> str:
    """返回去除首尾空白的一行改写，非法值抛出 QueryRewriteError。"""

    # 只接受字符串，不能把 None、数字或对象隐式转换成查询。
    if not isinstance(value, str):
        # 类型不符合说明上游响应结构或 fake 契约已漂移。
        raise QueryRewriteError("Query 改写结果必须是字符串")
    # 去除 API 或 fake 无意携带的首尾空白。
    rewritten_query = value.strip()
    # 空白文本没有检索语义，不能回退为原问题掩盖上游错误。
    if not rewritten_query:
        # 明确失败，让调用方记录这次改写不可用。
        raise QueryRewriteError("Query 改写结果不能为空")
    # 回车或换行说明模型添加了解释、多条候选或错误格式。
    if "\r" in rewritten_query or "\n" in rewritten_query:
        # 不拆分或选取某一行，避免擅自改变模型表达的语义。
        raise QueryRewriteError("Query 改写结果必须是一行文本")
    # 返回唯一允许交给 dense 检索的受控文本。
    return rewritten_query


# 校验改写结果的元数据，确保快照不会写入无法追溯的模型身份。
def validate_query_rewrite_result(value: object) -> QueryRewriteResult:
    """返回字段已验证的 QueryRewriteResult，非法值显式失败。"""

    # 结果必须是本模块定义的不可变对象，避免随意对象伪装响应。
    if not isinstance(value, QueryRewriteResult):
        # 统一错误类型，调用方无需处理供应商对象结构。
        raise QueryRewriteError("Query 改写结果结构不正确")
    # 复用单行文本校验，避免两条路径产生不同规则。
    rewritten_query = validate_rewritten_query(value.rewritten_query)
    # 模型身份必须是非空字符串，缺失时不能发布快照。
    if not isinstance(value.model, str) or not value.model.strip():
        # 不接受空模型名或隐式对象转换。
        raise QueryRewriteError("Query 改写响应缺少模型身份")
    # usage 缺失是上游允许的情况，用 None 明确记录而不是补零。
    if value.usage is not None:
        # usage 必须是字典，便于后续以 JSON 原样保存。
        if not isinstance(value.usage, Mapping):
            # 不接受数组、字符串或供应商自定义对象。
            raise QueryRewriteError("Query 改写 usage 结构不正确")
        # 逐项限制为非负整数，避免把不可解释值写入成本证据。
        for key, token_count in value.usage.items():
            # usage 键必须是非空字符串，数值不能是 bool。
            if (
                not isinstance(key, str)
                or not key.strip()
                or type(token_count) is not int
                or token_count < 0
            ):
                # 任何一项异常都会使整条改写证据失效。
                raise QueryRewriteError("Query 改写 usage 必须是非负整数映射")
    # 返回规范化文本和去除模型首尾空白后的新对象。
    normalized_usage = (
        MappingProxyType(dict(value.usage)) if value.usage is not None else None
    )
    # 返回独立只读 usage 副本，避免调用方保留原映射后篡改结果。
    return QueryRewriteResult(rewritten_query, value.model.strip(), normalized_usage)


# 将改写和已有 dense 策略组合成仍遵守公共 RetrievalStrategy 的实验策略。
class RewriteDenseRetrievalStrategy:
    """先生成受控改写，再以改写文本委托同一个 dense 检索器。"""

    # 固定方法身份，报告可据此区分原问题 dense 与改写后的 dense。
    method_name = "rewrite-dense"

    # 保存改写器与已存在的 dense 策略，不在此处创建网络或模型资源。
    def __init__(self, rewriter: QueryRewriter, dense_strategy: RetrievalStrategy) -> None:
        # rewrite-dense 只能比较同一 dense 检索器，拒绝误接 hybrid 或 rerank。
        if dense_strategy.method_name != "dense":
            # 方法身份错误会破坏 M2.4 的单变量实验矩阵。
            raise ValueError("RewriteDenseRetrievalStrategy 只能委托 dense 策略")
        # 保存只依赖窄文本接口的改写器。
        self._rewriter = rewriter
        # 保存由调用方管理生命周期的 dense 策略。
        self._dense_strategy = dense_strategy

    # 先改写原问题，再返回与公共 RankedChunk 契约一致的实验结果。
    def retrieve(self, question: str, *, top_k: int) -> list[RankedChunk]:
        """改写问题并委托 dense，任一阶段失败都向调用方显式传播。"""

        # 改写器负责外部调用或 fake；策略本身不关心其具体实现。
        # 先验证文本和模型身份，再只取受控文本交给 dense。
        rewrite_result = validate_query_rewrite_result(self._rewriter.rewrite(question))
        # 提取已经绑定元数据的单行查询。
        rewritten_query = rewrite_result.rewritten_query
        # 只把已经受控的一行改写交给同一个 dense 策略。
        dense_results = self._dense_strategy.retrieve(rewritten_query, top_k=top_k)
        # 复制 dense 结果，仅替换方法身份以保留原始距离分数及其方向。
        rewritten_results = [
            replace(result, method=self.method_name) for result in dense_results
        ]
        # 在公共边界再次校验连续排名、唯一 identity 和保留的分数语义。
        validate_ranked_chunks(
            rewritten_results,
            method_name=self.method_name,
            top_k=top_k,
        )
        # 返回可由现有评测器直接消费的统一结果列表。
        return rewritten_results
