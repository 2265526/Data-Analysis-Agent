"""熔断器模块单元测试(纯标准库)。"""
from __future__ import annotations

import time

import pytest

from src.utils.circuit import CircuitBreaker, CircuitOpenError


def _always_ok() -> str:
    return "ok"


def _always_fail() -> None:
    raise RuntimeError("boom")


def test_closed_allows_calls() -> None:
    breaker = CircuitBreaker("t1", failure_threshold=3, open_timeout=1)
    assert breaker.call(_always_ok) == "ok"
    assert breaker.state == "closed"


def test_open_after_threshold_failures() -> None:
    trips = []
    breaker = CircuitBreaker(
        "t2",
        failure_threshold=3,
        open_timeout=60,
        on_trip=lambda name: trips.append(name),
    )
    for _ in range(3):
        with pytest.raises(RuntimeError):
            breaker.call(_always_fail)
    assert breaker.state == "open"
    assert trips == ["t2"]  # 仅触发一次回调

    # 打开期间直接拒绝, 不执行函数
    with pytest.raises(CircuitOpenError):
        breaker.call(_always_ok)


def test_success_resets_failure_count() -> None:
    breaker = CircuitBreaker("t3", failure_threshold=3, open_timeout=60)
    for _ in range(2):
        with pytest.raises(RuntimeError):
            breaker.call(_always_fail)
    assert breaker.call(_always_ok) == "ok"  # 成功重置连续失败计数
    assert breaker.state == "closed"


def test_half_open_recovers_after_timeout() -> None:
    breaker = CircuitBreaker("t4", failure_threshold=2, open_timeout=0.05)
    for _ in range(2):
        with pytest.raises(RuntimeError):
            breaker.call(_always_fail)
    assert breaker.state == "open"

    time.sleep(0.1)  # 冷却期过后进入 half_open, 试探请求放行
    assert breaker.call(_always_ok) == "ok"
    assert breaker.state == "closed"


def test_half_open_probe_failure_reopens() -> None:
    breaker = CircuitBreaker("t5", failure_threshold=2, open_timeout=0.05)
    for _ in range(2):
        with pytest.raises(RuntimeError):
            breaker.call(_always_fail)
    assert breaker.state == "open"

    time.sleep(0.1)
    with pytest.raises(RuntimeError):  # 半开试探失败 -> 立即重新打开
        breaker.call(_always_fail)
    assert breaker.state == "open"


def test_decorator_usage() -> None:
    breaker = CircuitBreaker("t6", failure_threshold=2, open_timeout=60)

    @breaker
    def guarded(x: int) -> int:
        return x * 2

    assert guarded(21) == 42
    for _ in range(2):
        with pytest.raises(RuntimeError):
            breaker.call(_always_fail)
    with pytest.raises(CircuitOpenError):
        guarded(1)


def test_validation_errors() -> None:
    with pytest.raises(ValueError):
        CircuitBreaker("t7", failure_threshold=0)
    with pytest.raises(ValueError):
        CircuitBreaker("t8", success_threshold=0)


def test_reset_manual() -> None:
    breaker = CircuitBreaker("t9", failure_threshold=2, open_timeout=60)
    for _ in range(2):
        with pytest.raises(RuntimeError):
            breaker.call(_always_fail)
    assert breaker.state == "open"
    breaker.reset()
    assert breaker.state == "closed"
    assert breaker.call(_always_ok) == "ok"
