"""定义跨检索策略通用的有序、可追溯结果契约。"""

# 导入 math，拒绝 NaN 和无穷等无法排序的分数。
import math
# 导入 dataclass，定义只承载结果字段的不可变对象。
from dataclasses import dataclass
# 导入 PureWindowsPath，防止 source_name 伪装成本机路径。
from pathlib import PureWindowsPath
# 导入 Protocol，声明策略调用方依赖的窄接口。
from typing import Protocol


# frozen=True 防止排序结果在评测或报告阶段被意外篡改。
@dataclass(frozen=True)
class RankedChunk:
    """保存一条跨策略可比较排名和该策略内部诊断分数。"""

    # 保存后续提示词与离线审阅需要的原文。
    text: str
    # 保存脱敏纯文件名，作为稳定身份的一部分。
    source_name: str
    # 保存从零开始的块序号，作为稳定身份的一部分。
    chunk_index: int
    # 保存最终名次，必须与列表位置连续一致。
    rank: int
    # 保存产出该结果的策略名称。
    method: str
    # 保存只用于当前方法内部诊断的原始分数。
    score: float | None
    # 有分数时说明其语义，例如 cosine_distance。
    score_kind: str | None
    # 有分数时说明数值更大是否代表更相关。
    higher_is_better: bool | None


# 声明评测器和未来聊天装配共同依赖的最小策略接口。
class RetrievalStrategy(Protocol):
    """把一个问题转换为不超过 top_k 条的有序结果。"""

    # 策略实例必须主动声明自己的稳定方法名称。
    method_name: str

    # 返回列表位置即最终 rank 的结果。
    def retrieve(self, question: str, *, top_k: int) -> list[RankedChunk]:
        """检索问题并返回经过契约校验的有序文本块。"""


# 检查任意策略返回值是否满足统一排名、身份和分数契约。
def validate_ranked_chunks(
    results: list[RankedChunk],
    *,
    method_name: str,
    top_k: int,
) -> None:
    """发现任何策略契约漂移时显式失败，不进行静默修正。"""

    # 方法名必须由策略显式声明，空文本没有可追溯意义。
    if not isinstance(method_name, str) or not method_name.strip():
        # 调用方不能省略方法身份。
        raise ValueError("method_name 必须是非空字符串")
    # top_k 必须是普通正整数，bool 不能冒充 0 或 1。
    if type(top_k) is not int or top_k <= 0:
        # 非法 K 无法定义最大结果数。
        raise ValueError("top_k 必须是正整数")
    # 结果数不允许超过调用方请求数量。
    if len(results) > top_k:
        # 避免策略绕过评测固定 Top-K 口径。
        raise ValueError("策略结果数量不能超过 top_k")
    # 记录已经出现的稳定身份。
    seen_identities: set[tuple[str, int]] = set()
    # 按列表顺序检查每一条结果。
    for expected_rank, result in enumerate(results, start=1):
        # rank 必须正好等于列表位置，不能跳号或重排。
        if result.rank != expected_rank:
            # 调用方无需猜测该用字段还是位置作为真实排名。
            raise ValueError("策略结果 rank 必须连续且等于列表位置")
        # 每条结果都必须声明本策略的方法名。
        if result.method != method_name:
            # 混入其他策略会污染同一轮评测。
            raise ValueError("策略结果 method 与 method_name 不一致")
        # 文本必须有实际内容，空文档不能进入后续上下文。
        if not isinstance(result.text, str) or not result.text.strip():
            # 不接受 None、空白或其他隐式转换。
            raise ValueError("策略结果 text 必须是非空字符串")
        # 来源必须是纯文件名，保持 M1 的隐私边界。
        if (
            not isinstance(result.source_name, str)
            or not result.source_name.strip()
            or result.source_name in {".", ".."}
            or "/" in result.source_name
            or "\\" in result.source_name
            or bool(PureWindowsPath(result.source_name).drive)
        ):
            # 不让 absolute path 或目录分隔符进入离线报告。
            raise ValueError("策略结果 source_name 必须是脱敏纯文件名")
        # 块序号必须是普通非负整数。
        if type(result.chunk_index) is not int or result.chunk_index < 0:
            # bool 也被 type 检查拒绝。
            raise ValueError("策略结果 chunk_index 必须是非负整数")
        # 组合本条稳定 identity。
        identity = (result.source_name, result.chunk_index)
        # 同一块不能重复占据多个排名。
        if identity in seen_identities:
            # 评测器不能用去重掩盖策略 bug。
            raise ValueError("策略结果不能包含重复 chunk identity")
        # 记录已验证的身份。
        seen_identities.add(identity)
        # score 缺失时，其他 score 元数据也必须缺失。
        if result.score is None:
            # 不允许半份分数元数据造成误解。
            if result.score_kind is not None or result.higher_is_better is not None:
                # 调用方应在有 score 时一次性提供三项字段。
                raise ValueError("score 缺失时 score 元数据也必须为 None")
            # 当前结果无需继续检查数值方向。
            continue
        # bool、字符串、NaN 和无穷都不是可诊断的数值分数。
        if (
            isinstance(result.score, bool)
            or not isinstance(result.score, (int, float))
            or not math.isfinite(float(result.score))
        ):
            # 拒绝不可排序或隐式转换的分数。
            raise ValueError("策略结果 score 必须是有限数值")
        # 有 score 时必须提供非空语义标签。
        if not isinstance(result.score_kind, str) or not result.score_kind.strip():
            # raw score 的含义不能由消费者猜测。
            raise ValueError("score 存在时 score_kind 必须是非空字符串")
        # 方向字段必须是严格 bool，不接受 0/1。
        if type(result.higher_is_better) is not bool:
            # 方向缺失会让诊断排序无法解释。
            raise ValueError("score 存在时 higher_is_better 必须是布尔值")
