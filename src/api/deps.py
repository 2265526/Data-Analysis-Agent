"""FastAPI 依赖注入: DB Session / Redis 客户端。"""
from __future__ import annotations

from typing import Generator

from redis import Redis
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.utils.settings import get_settings

settings = get_settings()

# 生产使用 psycopg2; pool_pre_ping 避免连接失效
engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)

_redis_pool: Redis | None = None


def get_db() -> Generator[Session, None, None]:
    """FastAPI 依赖: 每次请求一个数据库会话, 用完关闭。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_redis() -> Generator[Redis, None, None]:
    """FastAPI 依赖: 共享 Redis 连接池(任务状态临时缓存/队列)。

    protocol=2: 兼容旧版 Redis(<6.0 不支持 RESP3/HELLO)。
    """
    global _redis_pool
    if _redis_pool is None:
        _redis_pool = Redis.from_url(
            settings.redis_url, decode_responses=True, protocol=2
        )
    yield _redis_pool
