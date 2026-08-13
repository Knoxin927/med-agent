"""M5.4 raw 聚合：hit/miss/latency/eviction 必须可从 raw 重算，缺数据不填假 0。"""

# 导入 Sequence，统一接受 tuple/list。
from collections.abc import Sequence
# 导入 statistics，用于分位数；无样本时不调用。
from statistics import quantiles
# 导入 Any，输出 JSON 友好字典。
from typing import Any

# 导入值对象与结果标签。
from app.evaluation.cache.types import CacheManifest, CacheOutcome, CacheRawEvent


def _percentile(values: list[float], pct: float) -> float | None:
    """计算分位数；空列表返回 None。"""

    # 空样本不能伪造 0 延迟。
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    cuts = quantiles(sorted(values), n=100, method="inclusive")
    index = max(1, min(99, int(pct))) - 1
    return cuts[index]


def aggregate_cache_raw(
    events: Sequence[CacheRawEvent],
    manifest: CacheManifest,
) -> dict[str, Any]:
    """从 raw 重算缓存实验指标。"""

    if not events:
        raise ValueError("raw events 不能为空")

    # 按 outcome 计数，保证报告可解释。
    outcome_counts: dict[str, int] = {name: 0 for name in (
        CacheOutcome.hit,
        CacheOutcome.miss,
        CacheOutcome.bypass,
        CacheOutcome.evict,
        CacheOutcome.expire,
        CacheOutcome.rotation_miss,
        CacheOutcome.version_miss,
        CacheOutcome.stored,
    )}
    method_counts: dict[str, int] = {}
    latencies: list[float] = []
    get_latencies: list[float] = []
    set_latencies: list[float] = []
    hit_latencies: list[float] = []
    miss_like_latencies: list[float] = []

    for event in events:
        outcome_counts[event.outcome] = outcome_counts.get(event.outcome, 0) + 1
        method_counts[event.method] = method_counts.get(event.method, 0) + 1
        latencies.append(event.latency_ms)
        if event.operation == "get":
            get_latencies.append(event.latency_ms)
            # 命中尝试只认 get；set/rotate/evict 事件不得污染 hit_rate 分母。
            if event.outcome == CacheOutcome.hit:
                hit_latencies.append(event.latency_ms)
            if event.outcome in {
                CacheOutcome.miss,
                CacheOutcome.expire,
                CacheOutcome.rotation_miss,
                CacheOutcome.version_miss,
            }:
                miss_like_latencies.append(event.latency_ms)
        if event.operation == "set":
            set_latencies.append(event.latency_ms)

    total = len(events)
    # hit 只统计 get 成功命中，避免 set 事件 outcome 标签误入分子。
    hit_count = sum(
        1
        for event in events
        if event.operation == "get" and event.outcome == CacheOutcome.hit
    )
    # 命中分母只统计 get 上的“可尝试命中”结果，不含 bypass/set/evict/rotate 控制事件。
    attempt_outcomes = {
        CacheOutcome.hit,
        CacheOutcome.miss,
        CacheOutcome.expire,
        CacheOutcome.rotation_miss,
        CacheOutcome.version_miss,
    }
    attempt_count = sum(
        1
        for event in events
        if event.operation == "get" and event.outcome in attempt_outcomes
    )
    hit_rate = (hit_count / attempt_count) if attempt_count else None
    miss_rate = (
        ((attempt_count - hit_count) / attempt_count) if attempt_count else None
    )

    # 关键字段：若关键证据缺失，返回 None 而不是假 0，供 decision 判 hold。
    required_evidence = {
        "has_hit": hit_count > 0,
        "has_miss_or_variant": any(
            outcome_counts[name] > 0
            for name in (
                CacheOutcome.miss,
                CacheOutcome.expire,
                CacheOutcome.rotation_miss,
                CacheOutcome.version_miss,
            )
        ),
        "has_bypass": outcome_counts[CacheOutcome.bypass] > 0,
        "has_evict_or_capacity_signal": outcome_counts[CacheOutcome.evict] > 0,
        "has_rotation_or_version_invalidation": (
            outcome_counts[CacheOutcome.rotation_miss] > 0
            or outcome_counts[CacheOutcome.version_miss] > 0
        ),
        "has_ttl_expire": outcome_counts[CacheOutcome.expire] > 0,
    }
    missing_evidence = [name for name, ok in required_evidence.items() if not ok]

    return {
        "total_event_count": total,
        "outcome_counts": outcome_counts,
        "method_counts": method_counts,
        "attempt_count": attempt_count,
        "hit_count": hit_count,
        "hit_rate": hit_rate,
        "miss_rate": miss_rate,
        "eviction_count": outcome_counts[CacheOutcome.evict],
        "bypass_count": outcome_counts[CacheOutcome.bypass],
        "p50_latency_ms": _percentile(latencies, 50),
        "p95_latency_ms": _percentile(latencies, 95),
        "p50_get_latency_ms": _percentile(get_latencies, 50),
        "p95_get_latency_ms": _percentile(get_latencies, 95),
        "p50_set_latency_ms": _percentile(set_latencies, 50),
        "p95_set_latency_ms": _percentile(set_latencies, 95),
        "p50_hit_latency_ms": _percentile(hit_latencies, 50),
        "p50_miss_like_latency_ms": _percentile(miss_like_latencies, 50),
        "max_namespace_size_after": max(item.namespace_size_after for item in events),
        "manifest_key_version": manifest.key_version,
        "manifest_max_entries": manifest.max_entries,
        "manifest_default_ttl_seconds": manifest.default_ttl_seconds,
        "default_bypass": manifest.default_bypass,
        "required_evidence": required_evidence,
        "missing_evidence": missing_evidence,
        "has_complete_evidence": not missing_evidence,
    }
