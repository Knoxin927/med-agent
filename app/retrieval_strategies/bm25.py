"""使用标准库构建可重建的 BM25 检索策略。"""

# 导入 log，计算 BM25 的逆文档频率。
from math import log
# 导入 Callable，为三种固定 tokenizer 声明共同类型。
from typing import Callable

# 导入 M1.1 文本块，复用稳定 identity 与原文。
from app.rag.chunking import TextChunk
# 导入跨策略统一结果契约与校验器。
from app.retrieval_strategies.types import RankedChunk, validate_ranked_chunks


# 固定 BM25 常用的词频饱和参数，避免调用方随意改变评测口径。
BM25_K1 = 1.5
# 固定 BM25 文档长度归一化参数，短文本不会被不合理地放大。
BM25_B = 0.75
# 统一表示切词函数的输入和输出类型。
Tokenizer = Callable[[str], list[str]]


# 判断字符是否属于常见 CJK 汉字区间。
def _is_han(character: str) -> bool:
    """只将基础汉字逐字或组成二元字块，标点不作为词项。"""

    # 使用 Unicode 区间，避免引入第三方中文分词依赖。
    return "\u4e00" <= character <= "\u9fff"


# 提取文本中连续 ASCII 字母或数字，并统一转换为小写。
def _ascii_terms(text: str) -> list[str]:
    """把 blood-pressure、BGE-M3 等英文与数字信号保留为可匹配词项。"""

    # 保存已经完成的 ASCII 连续词。
    terms: list[str] = []
    # 保存当前尚未结束的 ASCII 连续词字符。
    current: list[str] = []
    # 逐字符扫描，准确控制连续词的边界。
    for character in text:
        # ASCII 字母和数字属于同一个连续词。
        if character.isascii() and character.isalnum():
            # 用小写消除英文大小写造成的无意义差异。
            current.append(character.lower())
            # 当前连续词仍未结束。
            continue
        # 遇到分隔符时，先提交此前累积的连续词。
        if current:
            # 拼回完整词项后加入输出。
            terms.append("".join(current))
            # 清空缓存，开始识别下一个连续词。
            current = []
    # 文本末尾的连续词没有分隔符，也必须提交。
    if current:
        # 加入最后一个完整 ASCII 词项。
        terms.append("".join(current))
    # 返回保持原始出现顺序的词项。
    return terms


# 提取文本中按出现顺序排列的单个汉字。
def _han_characters(text: str) -> list[str]:
    """返回每个汉字一个词项，保留中文短语的局部匹配信号。"""

    # 只保留汉字，忽略空白、标点和其他无词义分隔符。
    return [character for character in text if _is_han(character)]


# 在每段连续汉字中生成重叠二元字块。
def _han_bigrams(text: str) -> list[str]:
    """返回相邻汉字组成的重叠二元词项，不跨标点连接。"""

    # 保存最终的二元字块。
    bigrams: list[str] = []
    # 保存当前连续汉字片段。
    current: list[str] = []
    # 逐字符识别连续的汉字片段。
    for character in text:
        # 汉字继续追加到当前片段。
        if _is_han(character):
            # 保留当前位置的汉字。
            current.append(character)
            # 尚未遇到片段边界。
            continue
        # 标点或非汉字结束当前片段。
        for index in range(len(current) - 1):
            # 相邻两个汉字组成一个重叠二元字块。
            bigrams.append("".join(current[index : index + 2]))
        # 清空片段，保证二元字块不跨边界。
        current = []
    # 处理文本末尾仍未关闭的汉字片段。
    for index in range(len(current) - 1):
        # 追加末尾片段中的每个相邻二元字块。
        bigrams.append("".join(current[index : index + 2]))
    # 返回按原文顺序排列的二元字块。
    return bigrams


# 第一种固定候选：逐汉字与 ASCII 连续词。
def tokenize_han_char_ascii(text: str) -> list[str]:
    """输出逐汉字加英文连续词，适合保留最细粒度的中文信号。"""

    # 先保留中文单字，再附加英文与数字连续词。
    return _han_characters(text) + _ascii_terms(text)


# 第二种固定候选：仅使用重叠二元字块。
def tokenize_han_bigram(text: str) -> list[str]:
    """输出重叠二元字块，用相邻字表达中文局部短语。"""

    # 直接返回二元字块，不混入单字或 ASCII 词项。
    return _han_bigrams(text)


# 第三种固定候选：单字、二元字块与 ASCII 连续词共同保留。
def tokenize_han_char_bigram_ascii(text: str) -> list[str]:
    """输出覆盖最全的标准库词项组合，供冻结评测集公平比较。"""

    # 三类词项都按自身稳定顺序追加，重复词保留给 BM25 统计词频。
    return _han_characters(text) + _han_bigrams(text) + _ascii_terms(text)


# 按设计中声明的顺序固定 tokenizer 身份和实现，平分时依赖该顺序决策。
TOKENIZERS: tuple[tuple[str, Tokenizer], ...] = (
    ("han-char-ascii-v1", tokenize_han_char_ascii),
    ("han-bigram-v1", tokenize_han_bigram),
    ("han-char-bigram-ascii-v1", tokenize_han_char_bigram_ascii),
)
# 冻结评测按既定字典序选择第一种候选；其身份会写入 hybrid 报告。
SELECTED_TOKENIZER_ID = "han-char-ascii-v1"


# 从受控候选中读取 tokenizer，拒绝任意调用方注入函数。
def get_tokenizer(tokenizer_id: str) -> Tokenizer:
    """按固定标识返回 tokenizer，未知标识立即失败。"""

    # 逐个比较固定候选，候选数量很小且顺序本身有决策意义。
    for candidate_id, tokenizer in TOKENIZERS:
        # 匹配时返回对应的标准库实现。
        if tokenizer_id == candidate_id:
            # 不允许调用方传入任意可执行对象。
            return tokenizer
    # 未知 tokenization 口径会使报告不可比较，必须拒绝。
    raise ValueError("tokenizer_id 不受支持")


# 为每个文档保存 BM25 查询所需的词项统计。
class Bm25RetrievalStrategy:
    """从冻结 TextChunk 重建内存索引，并按 BM25 分数检索。"""

    # 策略名称固定，供统一 runner 和报告追溯。
    method_name = "bm25"

    # 根据完整冻结 chunks 建立一次内存索引。
    def __init__(self, chunks: list[TextChunk], *, tokenizer_id: str) -> None:
        # 空语料不能定义文档频率或平均长度。
        if not chunks:
            # 调用方必须先通过 EvaluationBundle 的语料校验。
            raise ValueError("BM25 索引至少需要一个文本块")
        # 保存受控 tokenizer 身份，报告可据此解释词项口径。
        self.tokenizer_id = tokenizer_id
        # 读取固定 tokenizer，未知身份在构建期立即失败。
        self._tokenizer = get_tokenizer(tokenizer_id)
        # 按输入顺序保存原始 chunks，identity 仍由 source_name 和 chunk_index 定义。
        self._chunks = tuple(chunks)
        # 保存每篇文档的词频表，查询时无需重复切分原文。
        self._term_frequencies: list[dict[str, int]] = []
        # 保存每个词项出现于多少篇不同文档，用于计算 IDF。
        self._document_frequencies: dict[str, int] = {}
        # 保存每篇文档词项总数，用于长度归一化。
        self._document_lengths: list[int] = []
        # 逐篇构建可重建的词频和文档频率。
        for chunk in self._chunks:
            # 对正文执行当前冻结 tokenizer。
            terms = self._tokenizer(chunk.text)
            # 初始化本篇文档的词频表。
            frequencies: dict[str, int] = {}
            # 逐词累计 BM25 所需的词频。
            for term in terms:
                # 首次出现的词从零开始计数。
                frequencies[term] = frequencies.get(term, 0) + 1
            # 保存本篇词频表，索引顺序与 chunks 严格一致。
            self._term_frequencies.append(frequencies)
            # 保存文档长度，空文档合法但只会得到零分。
            self._document_lengths.append(len(terms))
            # 每个词项对当前文档频率只能贡献一次。
            for term in frequencies:
                # 累加包含该词的文档数量。
                self._document_frequencies[term] = (
                    self._document_frequencies.get(term, 0) + 1
                )
        # 计算 BM25 的平均文档长度，供所有查询共享。
        self._average_document_length = sum(self._document_lengths) / len(self._chunks)

    # 对一个问题返回至多 top_k 条按分数降序的唯一结果。
    def retrieve(
        self,
        question: str,
        *,
        top_k: int,
        case_id: str | None = None,
    ) -> list[RankedChunk]:
        """按 BM25 原始分数排序；分数只用于 BM25 内部诊断。"""

        del case_id

        # top_k 必须与公共策略契约一致，提前给出明确错误。
        if type(top_k) is not int or top_k <= 0:
            # bool 不能冒充整数 K。
            raise ValueError("top_k 必须是正整数")
        # 空白问题没有词项，不应伪造一组零分结果。
        if not isinstance(question, str) or not question.strip():
            # 返回空列表仍满足公共结果契约。
            return []
        # 查询中的重复词只计一次，避免用户重复输入无限放大分数。
        query_terms = set(self._tokenizer(question))
        # tokenizer 可能过滤掉全部字符，结果同样为空。
        if not query_terms:
            # 不返回与问题无关的零分文本块。
            return []
        # 保存每篇文档的 BM25 总分及稳定 identity。
        scored_chunks: list[tuple[float, TextChunk]] = []
        # 逐篇计算各查询词项的 BM25 贡献。
        for index, chunk in enumerate(self._chunks):
            # 取出当前文档的预计算词频。
            frequencies = self._term_frequencies[index]
            # 取出当前文档长度。
            document_length = self._document_lengths[index]
            # 从零开始累加查询词的得分。
            score = 0.0
            # 遍历去重后的查询词项。
            for term in sorted(query_terms):
                # 不在该文档中的词项不贡献分数。
                term_frequency = frequencies.get(term, 0)
                # 词频为零时跳过后续除法。
                if term_frequency == 0:
                    # 当前词项无需计算 IDF。
                    continue
                # 读取包含当前词项的文档数。
                document_frequency = self._document_frequencies[term]
                # 使用 BM25 的平滑 IDF，保证罕见词分数为正且有限。
                inverse_document_frequency = log(
                    1.0
                    + (len(self._chunks) - document_frequency + 0.5)
                    / (document_frequency + 0.5)
                )
                # 计算长度归一化分母，空文档不会进入本分支。
                length_normalizer = 1.0 - BM25_B + BM25_B * (
                    document_length / self._average_document_length
                )
                # 将词频饱和与长度归一化后的贡献加入总分。
                score += inverse_document_frequency * (
                    term_frequency * (BM25_K1 + 1.0)
                    / (term_frequency + BM25_K1 * length_normalizer)
                )
            # 只保留至少命中一个查询词的文档，零分不应污染候选集。
            if score > 0.0:
                # 保存原始块，稍后统一完成稳定排序。
                scored_chunks.append((score, chunk))
        # 先按分数降序，再按稳定 identity 升序，确保跨轮结果不漂移。
        scored_chunks.sort(
            key=lambda item: (-item[0], item[1].source_name, item[1].chunk_index)
        )
        # 将截断后的排序结果转换为统一 RankedChunk。
        ranked_results = [
            RankedChunk(
                text=chunk.text,
                source_name=chunk.source_name,
                chunk_index=chunk.chunk_index,
                rank=rank,
                method=self.method_name,
                score=score,
                score_kind="bm25_score",
                higher_is_better=True,
            )
            for rank, (score, chunk) in enumerate(scored_chunks[:top_k], start=1)
        ]
        # 在离开策略边界前验证唯一 identity 和连续排名。
        validate_ranked_chunks(
            ranked_results,
            method_name=self.method_name,
            top_k=top_k,
        )
        # 返回已验证的 BM25 候选。
        return ranked_results
