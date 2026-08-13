"""M5.4 缓存 key 构造：HMAC 摘要 + 版本化 namespace，query 原文永不进入 key。"""

# 导入 hashlib/hmac，用受控 secret 生成不可逆摘要。
import hashlib
import hmac
# 导入 re，校验 key 各段格式。
import re

# 导入方法冻结集合。
from app.evaluation.cache.types import CACHE_METHODS


# 设计冻结的 key 格式：
# cache-v<key_version>:<corpus_version>:<model_version>:<tool_version>:<method>:<hmac_digest>
_SEGMENT_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")
_CACHE_KEY_PATTERN = re.compile(
    r"^cache-v(?P<key_version>[1-9]\d*):"
    r"(?P<corpus_version>[A-Za-z0-9._-]+):"
    r"(?P<model_version>[A-Za-z0-9._-]+):"
    r"(?P<tool_version>[A-Za-z0-9._-]+):"
    r"(?P<method>[A-Za-z0-9._-]+):"
    r"(?P<digest>[0-9a-f]{64})$"
)


def _require_key_segment(field_name: str, field_value: str) -> str:
    """要求 key 段只含可解析字符，避免构出 parse 失败的 key。"""

    if not isinstance(field_value, str) or not field_value:
        raise ValueError(f"{field_name} 必须是非空字符串")
    if not _SEGMENT_PATTERN.fullmatch(field_value):
        raise ValueError(
            f"{field_name} 只能包含字母/数字/./_/-，且不能含冒号或空白: {field_value}"
        )
    return field_value


def build_cache_key(
    *,
    secret: str,
    key_version: int,
    corpus_version: str,
    model_version: str,
    tool_version: str,
    method: str,
    query_material: str,
) -> str:
    """用 HMAC-SHA256 生成版本化 cache key。

    query_material 只在内存里参与 HMAC，绝不能写入 key 原文、日志或报告。
    普通测试必须注入固定 fake secret，而不是读仓库或 .env。
    """

    if not isinstance(secret, str) or not secret:
        raise ValueError("HMAC secret 必须是非空字符串，且只能来自受控运行时配置")
    if isinstance(key_version, bool) or not isinstance(key_version, int) or key_version <= 0:
        raise ValueError("key_version 必须是正整数")
    if not isinstance(query_material, str) or not query_material:
        raise ValueError("query_material 必须是非空字符串")
    corpus_version = _require_key_segment("corpus_version", corpus_version)
    model_version = _require_key_segment("model_version", model_version)
    tool_version = _require_key_segment("tool_version", tool_version)
    method = _require_key_segment("method", method)
    if method not in CACHE_METHODS:
        raise ValueError(f"method 不在冻结集合中: {method}")

    # 只用 secret 与 query_material 生成 digest；最终 key 只保留 digest。
    digest = hmac.new(
        secret.encode("utf-8"),
        query_material.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    key = (
        f"cache-v{key_version}:"
        f"{corpus_version}:"
        f"{model_version}:"
        f"{tool_version}:"
        f"{method}:"
        f"{digest}"
    )
    # 自检：query 原文绝对不能出现在 key 字符串中。
    if query_material in key:
        raise ValueError("cache key 构造失败：query_material 泄漏到 key 原文")
    # 自检：构造结果必须能被同一套 parser 接受，防止“能 build 不能 parse”。
    parse_cache_key(key)
    return key


def parse_cache_key(cache_key: str) -> dict[str, str | int]:
    """解析并校验 cache key 各段。"""

    if not isinstance(cache_key, str) or not cache_key:
        raise ValueError("cache_key 必须是非空字符串")
    match = _CACHE_KEY_PATTERN.fullmatch(cache_key)
    if match is None:
        raise ValueError(f"cache_key 格式不合法: {cache_key}")
    method = match.group("method")
    if method not in CACHE_METHODS:
        raise ValueError(f"cache_key.method 不在冻结集合中: {method}")
    return {
        "key_version": int(match.group("key_version")),
        "corpus_version": match.group("corpus_version"),
        "model_version": match.group("model_version"),
        "tool_version": match.group("tool_version"),
        "method": method,
        "digest": match.group("digest"),
    }
