"""监控指标模块单元测试(纯标准库, 不依赖项目其他模块)。"""
from __future__ import annotations

import pytest

from src.utils.metrics import Counter, Histogram, MetricRegistry


def test_counter_inc_and_render() -> None:
    registry = MetricRegistry()
    registry.inc("task_executed_total", labels={"status": "completed"})
    registry.inc("task_executed_total", labels={"status": "completed"})
    registry.inc("task_executed_total", labels={"status": "failed"})

    text = registry.snapshot()
    assert '# TYPE task_executed_total counter' in text
    assert 'task_executed_total{status="completed"} 2' in text
    assert 'task_executed_total{status="failed"} 1' in text


def test_counter_without_labels() -> None:
    registry = MetricRegistry()
    registry.inc("self_heal_successes_total")
    registry.inc("self_heal_successes_total", 2.0)
    text = registry.snapshot()
    assert "self_heal_successes_total 3" in text


def test_histogram_bucket_sum_count() -> None:
    registry = MetricRegistry()
    buckets = (0.5, 1.0, 2.0)
    for v in (0.3, 0.7, 1.5, 5.0):
        registry.observe("sandbox_exec_duration_seconds", v, buckets=buckets)

    text = registry.snapshot()
    assert '# TYPE sandbox_exec_duration_seconds histogram' in text
    assert 'sandbox_exec_duration_seconds_bucket{le="0.5"} 1' in text
    assert 'sandbox_exec_duration_seconds_bucket{le="1.0"} 2' in text
    assert 'sandbox_exec_duration_seconds_bucket{le="2.0"} 3' in text
    assert 'sandbox_exec_duration_seconds_bucket{le="+Inf"} 4' in text
    assert "sandbox_exec_duration_seconds_sum 7.5" in text
    assert "sandbox_exec_duration_seconds_count 4" in text


def test_histogram_with_labels_escapes() -> None:
    registry = MetricRegistry()
    registry.observe("llm_tokens_total", 100, labels={"node": 'coder"x', "type": "prompt"})
    text = registry.snapshot()
    assert 'llm_tokens_total_sum{node="coder\\"x",type="prompt"} 100' in text


def test_label_order_stable() -> None:
    registry = MetricRegistry()
    registry.inc("tool_param_rejections_total", labels={"tool": "sql_validator", "reason": "empty"})
    text = registry.snapshot()
    # 标签按字典序输出: reason 在 tool 之前
    assert 'tool_param_rejections_total{reason="empty",tool="sql_validator"} 1' in text


def test_invalid_metric_name_raises() -> None:
    with pytest.raises(ValueError):
        Counter("1invalid")
    with pytest.raises(ValueError):
        Counter("has space")


def test_type_conflict_raises() -> None:
    registry = MetricRegistry()
    registry.inc("some_metric")
    with pytest.raises(ValueError):
        registry.observe("some_metric", 1.0)


def test_reset_clears_all() -> None:
    registry = MetricRegistry()
    registry.inc("a_metric")
    registry.observe("b_metric", 1.0)
    registry.reset()
    assert registry.snapshot() == ""
