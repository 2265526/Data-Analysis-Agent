"""任务状态表: 记录每次分析任务的进度、结果路径与错误信息。"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Task(Base):
    """任务状态表(前端每 2s 轮询该表状态)。"""

    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, comment="task_id(UUID)")
    user_query: Mapped[str] = mapped_column(Text, comment="用户自然语言需求")
    session_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True, index=True, comment="所属会话(多轮上下文关联; 单轮提交为空)"
    )
    data_source_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="数据源 id(空=主库 settings.database_url)"
    )
    source: Mapped[str] = mapped_column(
        String(16), default="manual", comment="来源: manual=手动提交, scheduled=定时任务触发"
    )
    created_by: Mapped[str | None] = mapped_column(String(128), nullable=True, comment="提交者用户名(审计用)")
    status: Mapped[str] = mapped_column(
        String(24),
        default="pending",
        index=True,
        comment="pending/running/awaiting_approval/completed/failed",
    )
    progress: Mapped[str] = mapped_column(Text, default="", comment="进度描述/节点进度事件, 如 'coder_retry_2'")
    current_node: Mapped[str] = mapped_column(String(64), default="", comment="当前节点名")
    progress_detail: Mapped[str | None] = mapped_column(Text, nullable=True, comment="进度明细文本(BR-06)")
    progress_percent: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="进度百分比 0-100(BR-06)")
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False, comment="清理时标记归档, 删除文件但保留审计(CR-02)")
    result_path: Mapped[str | None] = mapped_column(String(512), nullable=True, comment="报告相对路径 /static/reports/...")
    summary: Mapped[str | None] = mapped_column(Text, nullable=True, comment="报告摘要")
    error_log: Mapped[str | None] = mapped_column(Text, nullable=True, comment="失败原因")
    retry_count: Mapped[int] = mapped_column(Integer, default=0, comment="累计重试次数")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)
