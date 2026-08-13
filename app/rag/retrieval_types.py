"""定义不依赖 Chroma 或模型库的稳定检索结果值对象。"""

# 导入 dataclass，用简洁语法定义只保存数据的不可变类型。
from dataclasses import dataclass


# frozen=True 表示结果创建后不能被后续流程意外修改。
@dataclass(frozen=True)
# 定义 M1.3 向 M1.4 提供的稳定检索结果契约。
class RetrievalResult:
    # 保存命中的文本块原文，供后续生成阶段作为上下文。
    text: str
    # 保存脱敏后的来源文件名，便于回答时给出引用。
    source_name: str
    # 保存文本块在原文中的顺序编号，便于定位和排序。
    chunk_index: int
    # 保存 Chroma 返回的 cosine distance；数值越小表示越接近。
    distance: float
