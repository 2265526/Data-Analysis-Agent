"""结果缓存(阶段3 OR-06): Redis KV 缓存, TTL 默认 1 天。

- planner:{query_hash} -> plan JSON (同需求直接复用拆解结果)
- coder:{schema_hash}:{task_hash} -> 生成代码 (同需求+同 schema 复用代码)
- Redis 不可用时降级为未命中(缓存是加速, 不能因它阻断业务)
"""
from __future__ import annotations

import json
from typing import Any, Optional

from src.api.deps import get_redis
from src.utils.logger import get_logger
from src.utils.metrics import metrics
from src.utils.settings import get_settings

logger = get_logger(__name__)
settings = get_settings()


def cache_get(key: str) -> Optional[str]:
    """读缓存; 命中记 cache_hits_total, 未命中记 cache_misses_total。"""
    try:
        redis = next(get_redis())
        value = redis.get(key)
        if value is not None:
            metrics.inc("cache_hits_total", labels={"key_prefix": key.split(":")[0]})
            return value
    except Exception as exc:  # noqa: BLE001
        logger.warning("cache_get_failed", key=key, error=str(exc))
        return None
    metrics.inc("cache_misses_total", labels={"key_prefix": key.split(":")[0]})
    return None


def cache_set(key: str, value: str, ttl: int | None = None) -> None:
    """写缓存(带 TTL)。失败仅告警。"""
    try:
        redis = next(get_redis())
        redis.set(key, value, ex=ttl or settings.cache_ttl_seconds)
    except Exception as exc:  # noqa: BLE001
        logger.warning("cache_set_failed", key=key, error=str(exc))


def cache_get_json(key: str) -> Optional[Any]:
    """读缓存并解析 JSON。"""
    raw = cache_get(key)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


def cache_set_json(key: str, value: Any, ttl: int | None = None) -> None:
    """序列化写入缓存。"""
    try:
        cache_set(key, json.dumps(value, ensure_ascii=False), ttl=ttl)
    except (TypeError, ValueError) as exc:  # noqa: BLE001
        logger.warning("cache_set_json_failed", key=key, error=str(exc))
