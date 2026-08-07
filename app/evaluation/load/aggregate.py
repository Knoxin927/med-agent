"""M5.2 raw 聚合：每个汇总数字都必须能从 raw 重算，缺样本不填假 0。"""

# 导入 Sequence，统一接受 tuple/list。
from collections.abc import Sequence
# 导入 statistics，用于分位数；无样本时不调用。
from statistics import quantiles
# 导入 Any，输出 JSON 友好字典。
from typing import Any

# 导入值对象。
from app.evaluation.load.types import LoadManifest, LoadPhase, LoadRawSample


def _percentile(values: list[float], pct: float) -> float | None:
    """计算分位数；空列表返回 None。"""

    # 空样本不能伪造 0 延迟。
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    # statistics.quantiles 使用 n=100 时返回 99 个分割点；pct 取对应下标。
    cuts = quantiles(sorted(values), n=100, method="inclusive")
    index = max(1, min(99, int(pct))) - 1
    return cuts[index]


def _is_success(sample: LoadRawSample) -> bool:
    """统一成功判定：2xx 或 stdio 约定 0，且无 error_code。"""

    if sample.error_code is not None:
        return False
    return sample.status_code == 0 or 200 <= sample.status_code < 300


def _bucket_metrics(rows: Sequence[LoadRawSample], window_seconds: float) -> dict[str, Any]:
    """对单个 scenario+concurrency 的 measurement 样本计算指标。"""

    sample_count = len(rows)
    success_rows = [item for item in rows if _is_success(item)]
    error_rows = [item for item in rows if not _is_success(item)]
    latencies = [item.full_latency_ms for item in rows]
    first_tokens = [
        item.first_token_latency_ms
        for item in rows
        if item.first_token_latency_ms is not None
    ]
    error_codes: dict[str, int] = {}
    for item in error_rows:
        code = item.error_code or f"status_{item.status_code}"
        error_codes[code] = error_codes.get(code, 0) + 1
    cpu_values = [item.cpu_pct for item in rows]
    rss_values = [item.rss_mb for item in rows]
    success_count = len(success_rows)
    error_count = len(error_rows)
    return {
        # 正式样本数；必须等于或对照 measurement_count。
        "sample_count": sample_count,
        # 成功请求数。
        "success_count": success_count,
        # 失败请求数；失败仍留在 sample_count 分母。
        "error_count": error_count,
        # 错误率；无样本时为 None。
        "error_rate": (error_count / sample_count) if sample_count else None,
        # 延迟分位数；空样本时为 None。
        "p50_latency_ms": _percentile(latencies, 50),
        "p95_latency_ms": _percentile(latencies, 95),
        "p99_latency_ms": _percentile(latencies, 99),
        # 首 token 分位数；场景不适用时可能全为 None。
        "p50_first_token_ms": _percentile(first_tokens, 50),
        "p95_first_token_ms": _percentile(first_tokens, 95),
        # 吞吐按成功数 / 固定窗口计算；窗口为 0 时不可用。
        "throughput_rps": (
            success_count / window_seconds
            if sample_count and window_seconds > 0
            else None
        ),
        # 错误码直方图，便于 hold 原因定位。
        "error_codes": error_codes,
        # 资源范围，只保留 min/max，不保存完整序列。
        "cpu_pct_min": min(cpu_values) if cpu_values else None,
        "cpu_pct_max": max(cpu_values) if cpu_values else None,
        "rss_mb_min": min(rss_values) if rss_values else None,
        "rss_mb_max": max(rss_values) if rss_values else None,
    }


def aggregate_load_raw(
    samples: Sequence[LoadRawSample],
    manifest: LoadManifest,
) -> dict[str, Any]:
    """从 raw 重算 scenario x concurrency 指标。"""

    if not samples:
        raise ValueError("raw samples 不能为空")
    # 只统计 measurement；warmup 单独计数，不进正式分母。
    measurement = [item for item in samples if item.phase == LoadPhase.measurement]
    warmup = [item for item in samples if item.phase == LoadPhase.warmup]
    result: dict[str, Any] = {
        "scenarios": {},
        "total_sample_count": len(samples),
        "warmup_sample_count": len(warmup),
        "measurement_sample_count": len(measurement),
        "has_insufficient_samples": False,
    }
    insufficient = False
    for scenario in manifest.scenarios:
        scenario_metrics: dict[str, Any] = {}
        for concurrency in manifest.concurrency_levels:
            rows = [
                item
                for item in measurement
                if item.scenario_id == scenario.scenario_id
                and item.concurrency == concurrency
            ]
            metrics = _bucket_metrics(rows, manifest.window_seconds)
            # 未达到预注册 measurement_count 的档位必须 hold。
            expected = manifest.measurement_count
            metrics["expected_sample_count"] = expected
            metrics["sample_count_ok"] = metrics["sample_count"] >= expected
            if not metrics["sample_count_ok"]:
                insufficient = True
            scenario_metrics[str(concurrency)] = metrics
        result["scenarios"][scenario.scenario_id] = scenario_metrics
    result["has_insufficient_samples"] = insufficient
    return result
