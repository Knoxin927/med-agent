"""加载与生产 hybrid 检索绑定的冻结语料块。"""

from pathlib import Path

from app.evaluation.data import load_manifest_chunks
from app.rag.chunking import TextChunk

# 项目根目录。
PROJECT_ROOT = Path(__file__).resolve().parents[2]
# M7 fixed 切片评测 manifest：与 hybrid 正式报告同一语料口径。
PRODUCTION_CORPUS_MANIFEST = (
    PROJECT_ROOT / "evaluation" / "manifests" / "v2-review-candidate-fixed.json"
)


def load_production_chunks(
    project_root: Path | None = None,
    *,
    manifest_path: Path | None = None,
) -> tuple[TextChunk, ...]:
    """按冻结 manifest 重建 TextChunk，供 Chroma 入库与 BM25 索引共用。"""

    root = PROJECT_ROOT if project_root is None else project_root
    path = PRODUCTION_CORPUS_MANIFEST if manifest_path is None else manifest_path
    return load_manifest_chunks(root, path)
