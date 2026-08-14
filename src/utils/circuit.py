"""通用熔断器(closed / open / half-open 状态机, 线程安全)。

用于保护 LLM / 沙箱等外部依赖: 连续失败达到阈值后快速失败(熔断打开),
避免下游故障拖垮整条流水线; 冷却期后进入半开状态试探恢复。
熔断打开瞬间触发 on_trip 回调, 用于上报监控指标(如 circuit_breaker_trips_total)。

用法:
    from src.utils.circuit import CircuitBreaker, CircuitOpenError
    from src.utils.metrics import metrics

    breaker = CircuitBreaker(
        "llm_call",
        failure_threshold=5,
        open_timeout=30.0,
        on_trip=lambda name: metrics.inc(
            "circuit_breaker_trips_total", labels={"breaker": name}
        ),
    )

    try:
        result = breaker.call(chat.invoke, messages)
    except CircuitOpenError:
        ...  # 走降级路径(缓存/规则引擎/人工)

也可用作装饰器: @breaker 包装无参调用函数。
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Optional

STATE_CLOSED = "closed"
STATE_OPEN = "open"
STATE_HALF_OPEN = "half_open"


class CircuitOpenError(RuntimeError):
    """熔断器打开期间拒绝调用时抛出。"""

    def __init__(self, name: str) -> None:
        super().__init__(f"circuit breaker '{name}' is open")
        self.name = name


class CircuitBreaker:
    """带冷却与半开探测的熔断器。

    Args:
        name: 熔断器标识(用于指标标签 / 错误信息)
        failure_threshold: closed 状态下连续失败多少次后打开(>=1)
        open_timeout: open 状态保持秒数, 之后进入 half_open(>=0)
        success_threshold: half_open 状态下连续成功多少次后闭合(>=1, 默认 1)
        on_trip: 打开瞬间回调, 接收 breaker 名, 用于上报指标/告警
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        open_timeout: float = 30.0,
        success_threshold: int = 1,
        on_trip: Optional[Callable[[str], None]] = None,
    ) -> None:
        if failure_threshold < 1 or success_threshold < 1:
            raise ValueError("failure_threshold / success_threshold must be >= 1")
        self.name = name
        self.failure_threshold = failure_threshold
        self.open_timeout = open_timeout
        self.success_threshold = success_threshold
        self.on_trip = on_trip

        self._lock = threading.Lock()
        self._state = STATE_CLOSED
        self._consecutive_failures = 0
        self._consecutive_successes = 0
        self._opened_at: Optional[float] = None

    # -- 状态查询(供指标/测试) --
    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    @property
    def consecutive_failures(self) -> int:
        with self._lock:
            return self._consecutive_failures

    # -- 核心调用 --
    def call(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """执行 func; 熔断打开时抛 CircuitOpenError, 不执行 func。"""
        if not self._allow_request():
            raise CircuitOpenError(self.name)
        try:
            result = func(*args, **kwargs)
        except Exception:
            self._record_failure()
            raise
        self._record_success()
        return result

    # -- 装饰器模式: 包装无参(或已绑定参数)的可调用对象 --
    def __call__(self, func: Callable[..., Any]) -> Callable[..., Any]:
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            return self.call(func, *args, **kwargs)

        wrapper.__name__ = getattr(func, "__name__", "wrapped")
        wrapper.__doc__ = getattr(func, "__doc__", None)
        return wrapper

    # -- 内部状态机 --
    def _allow_request(self) -> bool:
        """判断当前请求是否放行, 并做 open -> half_open 的状态迁移。"""
        with self._lock:
            if self._state == STATE_CLOSED:
                return True
            if self._state == STATE_OPEN:
                if self._opened_at is not None and time.monotonic() - self._opened_at >= self.open_timeout:
                    self._state = STATE_HALF_OPEN
                    self._consecutive_successes = 0
                    return True
                return False
            # half_open: 放行试探请求, 连续成功 success_threshold 次后闭合,
            # 任一试探失败则立即重新打开
            return True

    def _record_failure(self) -> None:
        with self._lock:
            if self._state == STATE_CLOSED:
                self._consecutive_failures += 1
                if self._consecutive_failures >= self.failure_threshold:
                    self._open()
            elif self._state == STATE_HALF_OPEN:
                # 试探失败: 立即重新打开
                self._consecutive_failures = 0
                self._open()

    def _record_success(self) -> None:
        with self._lock:
            if self._state == STATE_CLOSED:
                self._consecutive_failures = 0
            elif self._state == STATE_HALF_OPEN:
                self._consecutive_successes += 1
                if self._consecutive_successes >= self.success_threshold:
                    self._close()

    def _open(self) -> None:
        self._state = STATE_OPEN
        self._opened_at = time.monotonic()
        self._consecutive_successes = 0
        if self.on_trip is not None:
            self.on_trip(self.name)

    def _close(self) -> None:
        self._state = STATE_CLOSED
        self._consecutive_failures = 0
        self._consecutive_successes = 0
        self._opened_at = None

    def reset(self) -> None:
        """手动复位为 closed(测试/运维)。"""
        with self._lock:
            self._close()
