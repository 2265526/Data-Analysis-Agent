"""轻量进程内监控指标(纯标准库, 无第三方依赖)。

实现 Prometheus 文本格式的 Counter / Histogram, 线程安全,
通过 GET /metrics 暴露给 Prometheus/自建采集, 无需额外埋点框架。

用法:
    from src.utils.metrics import metrics

    metrics.inc("executor_failures_total", labels={"node": "executor"})
    metrics.observe("sandbox_exec_duration_seconds", 1.23, labels={"backend": "docker"})
    print(metrics.snapshot())   # Prometheus 文本格式

说明: 指标为进程内聚合, 多 worker 部署时各进程独立,
如需全局限量可后续将 snapshot 结果周期上报到 Prometheus PushGateway 或 ELK。
"""
from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional, Tuple

# 默认直方图分桶(秒级, 覆盖沙箱执行 0.1s~120s 区间)
DEFAULT_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 120)

_METRIC_NAME_RE = None  # 惰性编译, 避免 import 开销


def _validate_name(name: str) -> None:
    """指标名必须合法(Prometheus 命名规范)。"""
    global _METRIC_NAME_RE
    if _METRIC_NAME_RE is None:
        import re

        _METRIC_NAME_RE = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")
    if not _METRIC_NAME_RE.match(name):
        raise ValueError(f"invalid metric name: {name!r}")


def _label_key(labels: Optional[Dict[str, str]]) -> Tuple[Tuple[str, str], ...]:
    """标签排序成元组, 保证输出顺序稳定。"""
    return tuple(sorted((labels or {}).items()))


def _escape_label(value: Any) -> str:
    """Prometheus 标签值转义(引号/反斜杠)。"""
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


class Counter:
    """单调递增计数器(支持 labels)。"""

    def __init__(self, name: str, help_text: str = "") -> None:
        _validate_name(name)
        self.name = name
        self.help = help_text or f"{name} counter"
        self._values: Dict[Tuple[Tuple[str, str], ...], float] = {}

    def inc(self, value: float = 1.0, labels: Optional[Dict[str, str]] = None) -> None:
        key = _label_key(labels)
        self._values[key] = self._values.get(key, 0.0) + value

    def render(self) -> List[str]:
        lines = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} counter"]
        for key, count in sorted(self._values.items()):
            rendered_value = str(int(count)) if float(count).is_integer() else str(count)
            if key:
                labels = ",".join(f'{k}="{_escape_label(v)}"' for k, v in key)
                lines.append(f"{self.name}{{{labels}}} {rendered_value}")
            else:
                lines.append(f"{self.name} {rendered_value}")
        return lines


class Histogram:
    """数值分布统计(支持 labels), 输出 sum/count/bucket 三组序列。"""

    def __init__(self, name: str, help_text: str = "", buckets: Tuple[float, ...] = DEFAULT_BUCKETS) -> None:
        _validate_name(name)
        self.name = name
        self.help = help_text or f"{name} histogram"
        self.buckets = tuple(sorted(buckets))
        # value[key] = (sum: float, count: int, bucket_counts: List[int])
        self._values: Dict[Tuple[Tuple[str, str], ...], List[Any]] = {}

    def observe(self, value: float, labels: Optional[Dict[str, str]] = None) -> None:
        key = _label_key(labels)
        entry = self._values.get(key)
        if entry is None:
            entry = [0.0, 0, [0] * len(self.buckets)]
            self._values[key] = entry
        entry[0] += float(value)
        entry[1] += 1
        for i, upper in enumerate(self.buckets):
            if float(value) <= upper:
                entry[2][i] += 1

    def render(self) -> List[str]:
        lines = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} histogram"]
        for key, (total, count, bucket_counts) in sorted(self._values.items()):
            base_labels = dict(key)

            def series(name_suffix: str = "", extra: Optional[Dict[str, Any]] = None) -> str:
                """渲染带标签的完整指标名, 如 name_bucket{node="x",le="0.5"}。"""
                labels = dict(base_labels)
                if extra:
                    labels.update(extra)
                if labels:
                    inner = ",".join(f'{k}="{_escape_label(v)}"' for k, v in sorted(labels.items()))
                    return f"{self.name}{name_suffix}{{{inner}}}"
                return f"{self.name}{name_suffix}"

            for i, upper in enumerate(self.buckets):
                lines.append(f"{series('_bucket', {'le': upper})} {bucket_counts[i]}")
            lines.append(f"{series('_bucket', {'le': '+Inf'})} {count}")
            lines.append(f"{series('_sum')} {total}")
            lines.append(f"{series('_count')} {count}")
        return lines


class MetricRegistry:
    """指标注册表: 按名聚合, 线程安全, 自动注册 Counter/Histogram。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._metrics: Dict[str, Any] = {}

    def inc(
        self,
        name: str,
        value: float = 1.0,
        labels: Optional[Dict[str, str]] = None,
        help_text: str = "",
    ) -> None:
        with self._lock:
            metric = self._metrics.get(name)
            if metric is None:
                metric = Counter(name, help_text)
                self._metrics[name] = metric
            elif not isinstance(metric, Counter):
                raise ValueError(f"metric {name!r} already registered as non-counter")
            metric.inc(value, labels)

    def observe(
        self,
        name: str,
        value: float,
        labels: Optional[Dict[str, str]] = None,
        help_text: str = "",
        buckets: Tuple[float, ...] = DEFAULT_BUCKETS,
    ) -> None:
        with self._lock:
            metric = self._metrics.get(name)
            if metric is None:
                metric = Histogram(name, help_text, buckets)
                self._metrics[name] = metric
            elif not isinstance(metric, Histogram):
                raise ValueError(f"metric {name!r} already registered as non-histogram")
            metric.observe(value, labels)

    def snapshot(self) -> str:
        """输出完整 Prometheus 文本格式(供 GET /metrics)。"""
        with self._lock:
            lines: List[str] = []
            for metric in self._metrics.values():
                lines.extend(metric.render())
            return "\n".join(lines) + ("\n" if lines else "")

    def snapshot_dict(self) -> dict:
        """输出结构化指标数据(供管理看板 API 直接使用, 免解析文本)。

        结构: {指标名: {"type": "counter"|"histogram", "help": str, "series": [...]}}
        - counter:   series 为 [{"labels": {...}, "value": float}]
        - histogram: series 为 [{"labels": {...}, "sum", "count", "buckets": [...]}],
                     顶层另含 "buckets"(分桶上界)。
        """
        with self._lock:
            result: Dict[str, Any] = {}
            for name, metric in self._metrics.items():
                if isinstance(metric, Counter):
                    result[name] = {
                        "type": "counter",
                        "help": metric.help,
                        "series": [
                            {"labels": dict(key), "value": value}
                            for key, value in sorted(metric._values.items())
                        ],
                    }
                elif isinstance(metric, Histogram):
                    result[name] = {
                        "type": "histogram",
                        "help": metric.help,
                        "buckets": list(metric.buckets),
                        "series": [
                            {
                                "labels": dict(key),
                                "sum": total,
                                "count": count,
                                "buckets": bucket_counts,
                            }
                            for key, (total, count, bucket_counts) in sorted(metric._values.items())
                        ],
                    }
            return result

    def reset(self) -> None:
        """清空全部指标(主要用于测试)。"""
        with self._lock:
            self._metrics.clear()


# 全局注册表单例(进程内共享)
metrics = MetricRegistry()
