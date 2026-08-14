"""数据字典表(schema_dict): 管理员为表/列维护中文名, 任何数据源的表/列都可标注。

背景: 默认库无 COMMENT, 内置映射(_TABLE_CN/_COLUMN_CN)只覆盖演示供应链表;
真实业务库的英文表名/字段名对业务管理员不可读。本表让管理员在「数据字典」
页面为任意表的表/列补充中文名, 前端选择下拉与勾选清单即时生效。

优先级(从高到低): 数据字典表 > 数据库 COMMENT(pg_description) > 内置映射。
- column_name 空串 = 表级中文名; 非空 = 该表某列的中文名
- 按 (table_name, column_name) 全局唯一(不区分数据源, v1 简化)
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SchemaDict(Base):
    __tablename__ = "schema_dict"
    __table_args__ = (
        UniqueConstraint("table_name", "column_name", name="uq_schema_dict_table_column"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    table_name: Mapped[str] = mapped_column(String(128), index=True, comment="表名")
    column_name: Mapped[str] = mapped_column(
        String(128), default="", comment="列名(空串=表级中文名)"
    )
    cn_name: Mapped[str] = mapped_column(String(256), comment="中文名/业务说明")
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )
