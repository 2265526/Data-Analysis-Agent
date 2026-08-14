"""数据源注册表(data_sources): 让真实业务数据可配置接入。

产品背景(PM 评估 P0): 平台原只连一个写死的供应链演示库(settings.database_url),
客户真实数据无法进入。本表提供"数据源配置化": 管理员注册任意只读 PostgreSQL
连接(连接串 AES-256-GCM 加密落库), 任务提交时可指定数据源, 执行/表结构注入
按数据源路由, 新库/新表无需改代码。

约束与说明(v1):
- 仅支持 PostgreSQL 只读连接(与执行引擎一致)
- tables_whitelist: 空列表 = 该库全部表可见; 非空 = 仅白名单内表参与 schema 注入
- 数据权限(data_policy_rules)按表名全局生效, 跨数据源同名表共用同一策略
- MCP EXPLAIN 预检仅对主库启用(子进程连接串固定), 非主库数据源走本地只读执行
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from src.models import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DataSource(Base):
    __tablename__ = "data_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(
        String(64), unique=True, comment="数据源名称(唯一, 如 '主业务库' / '供应链分析库')"
    )
    db_url_enc: Mapped[str] = mapped_column(
        Text, comment="连接串(AES-256-GCM 加密落库, 如 postgresql://user:pass@host:5432/db)"
    )
    tables_whitelist: Mapped[list] = mapped_column(
        JSON, default=list, comment="表白名单(空=全部表可见)"
    )
    description: Mapped[str] = mapped_column(String(256), default="", comment="说明")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, comment="启用/停用")
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="创建人")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )
