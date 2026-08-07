"""读取 UTF-8 文本文档，并为后续检索准备可追溯文本块。"""

# 从 dataclasses 导入 dataclass，用较少代码定义只保存数据的类型。
from dataclasses import dataclass
# 从 pathlib 导入 Path，统一处理 Windows 和其他系统的文件路径。
from pathlib import Path


# frozen=True 表示对象创建后字段不可修改，避免后续流程意外篡改文本块。
@dataclass(frozen=True)
# 定义文本块数据模型，集中表达后续检索所需的三个字段。
class TextChunk:
    # 保存来源文件名，不保存绝对路径，以免泄露本机目录信息。
    source_name: str
    # 保存文本块从零开始的顺序编号，便于还原原文顺序。
    chunk_index: int
    # 保存当前文本块的实际文本内容。
    text: str


# 定义只读取 UTF-8 txt 文件的函数，并明确返回 Python 字符串。
def read_utf8_text(path: Path) -> str:
    # 将扩展名转为小写后检查，因此 .txt 和 .TXT 都可以读取。
    if path.suffix.lower() != ".txt":
        # 对不支持的格式抛出 ValueError，让调用方知道输入类型不合法。
        raise ValueError("仅支持读取 .txt 文本文件")

    # 使用明确的 UTF-8 编码读取全文；文件或编码错误由标准库原样抛出。
    return path.read_text(encoding="utf-8")


# 按固定字符数和重叠字符数，把原始文本转换成有序文本块列表。
def chunk_text(
    text: str,
    source_path: Path,
    chunk_size: int = 300,
    overlap: int = 50,
) -> list[TextChunk]:
    # 块大小必须为正数，否则无法产生有效文本块。
    if chunk_size <= 0:
        # 用 ValueError 明确告诉调用方参数本身不符合契约。
        raise ValueError("chunk_size 必须大于 0")
    # overlap 不能为负数，否则无法表达重叠字符数量。
    if overlap < 0:
        # 用 ValueError 拒绝负的重叠长度。
        raise ValueError("overlap 不能小于 0")
    # overlap 必须小于 chunk_size，否则步长会变成零或负数。
    if overlap >= chunk_size:
        # 提前失败可以避免切片循环永远不前进。
        raise ValueError("overlap 必须小于 chunk_size")
    # 纯空白文本没有检索价值，直接返回空列表。
    if not text.strip():
        # 返回空列表而不是制造一个空文本块。
        return []

    # 相邻块的起点间隔等于块大小减去重叠大小。
    step = chunk_size - overlap
    # 只保存最终生成的文本块，保持原文顺序。
    chunks: list[TextChunk] = []
    # 从原文第一个字符开始切片。
    start = 0
    # 记录从零开始的文本块序号。
    chunk_index = 0
    # 只要起点仍在文本范围内，就继续生成文本块。
    while start < len(text):
        # 按固定长度取得当前块，尾块可以短于 chunk_size。
        chunk_value = text[start : start + chunk_size]
        # 使用 Path.name 脱敏，只把文件名写入结果。
        chunks.append(
            TextChunk(
                source_name=source_path.name,
                chunk_index=chunk_index,
                text=chunk_value,
            )
        )
        # 如果当前块已经覆盖文本末尾，立即停止以避免重复尾块。
        if start + chunk_size >= len(text):
            # 当前块已经包含最后一个字符，不需要再计算下一个起点。
            break
        # 起点向前移动一个步长，同时保留 overlap 个字符重叠。
        start += step
        # 下一个文本块的序号递增一。
        chunk_index += 1

    # 返回按原文顺序排列的所有文本块。
    return chunks
