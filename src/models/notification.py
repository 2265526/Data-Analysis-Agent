"""站内通知表(notifications): 定时任务结果推送 + 后续可扩展审批提醒。

- 目前写入方: 定时任务完成/失败后给 owner 写一条结果通知
- 读取方: 前端全局铃铛(未读角标 + 列表), 用户可标记已读
- 与外部渠道(邮件/IM)解耦: 现有 notifier.py 负责外部推送, 本表是站内兜底
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user: Mapped[str] = mapped_column(String(64), index=True, comment="接收人用户名")
    title: Mapped[str] = mapped_column(String(128), comment="标题(如 定时任务完成)")
    content: Mapped[str] = mapped_column(Text, default="", comment="内容摘要")
    task_id: Mapped[str | None] = mapped_column(String(36), nullable=True, comment="关联任务 id")
    kind: Mapped[str] = mapped_column(String(32), default="scheduled", comment="scheduled/approval/system")
    read: Mapped[bool] = mapped_column(Boolean, default=False, comment="已读")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)
