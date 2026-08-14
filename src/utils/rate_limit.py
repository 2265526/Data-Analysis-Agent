"""全局限流(阶段3 OR-11): Redis 令牌桶。

- 桶容量 burst, 每秒补充 rate/60 个令牌, 每消耗一次 LLM 调用扣 1 个
- Lua 脚本保证原子性(多线程/多进程下不超发)
- Redis 不可用时降级放行(限流是保护手段, 不能因它阻断业务)
"""
from __future__ import annotations

import time

from src.api.deps import get_redis
from src.utils.logger import get_logger
from src.utils.metrics import metrics
from src.utils.settings import get_settings

logger = get_logger(__name__)
settings = get_settings()

# 令牌桶 Lua: 返回 1=允许, 0=拒绝; key 内保存 "tokens:last_refill"
_TOKEN_BUCKET_LUA = """
local data = redis.call('GET', KEYS[1])
local rate = tonumber(ARGV[1])            -- 每秒补充速率
local burst = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local tokens, last = burst, now
if data then
  local sep = string.find(data, ':')
  if sep then
    tokens = tonumber(string.sub(data, 1, sep - 1))
    last = tonumber(string.sub(data, sep + 1))
  end
end
tokens = math.min(burst, tokens + (now - last) * rate)
if tokens >= 1 then
  tokens = tokens - 1
  redis.call('SET', KEYS[1], tokens .. ':' .. now, 'EX', 60)
  return 1
else
  redis.call('SET', KEYS[1], tokens .. ':' .. now, 'EX', 60)
  return 0
end
"""


class RateLimiter:
    """Redis 令牌桶限流器。"""

    def __init__(self, rate_per_min: int | None = None, burst: int | None = None) -> None:
        self._rate_per_sec = (rate_per_min or settings.rate_limit_per_min) / 60.0
        self._burst = burst or settings.rate_limit_burst

    def allow(self, key: str = "llm") -> bool:
        """消耗一个令牌; 返回是否允许。Redis 不可用或异常时放行。"""
        try:
            redis = next(get_redis())
            result = redis.eval(
                _TOKEN_BUCKET_LUA,
                1,
                f"rate_limit:{key}",
                self._rate_per_sec,
                self._burst,
                time.time(),
            )
            return bool(result)
        except Exception as exc:  # noqa: BLE001
            logger.warning("rate_limiter_unavailable", key=key, error=str(exc))
            return True  # 降级放行

    def wait_until_available(self, key: str = "llm", max_wait: int | None = None) -> bool:
        """等待令牌可用; 返回是否在期限内获得。超时返回 False。"""
        deadline = time.monotonic() + (max_wait or settings.rate_limit_wait_seconds)
        while time.monotonic() < deadline:
            if self.allow(key):
                return True
            time.sleep(0.5)
        metrics.inc("rate_limit_rejections_total", labels={"key": key})
        return False


# 全局实例: LLM 调用统一走它
limiter = RateLimiter()
