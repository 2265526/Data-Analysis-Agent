"""多会话数据模型: chat_sessions(会话) + chat_messages(消息)。

设计(参考 open-webui chat/chat_message 表):
- 会话按 owner(用户名)隔离: 所有查询强制 owner == 当前用户, 前端只做展示
- 会话标题: 创建时默认"新对话", 首条用户消息到达后由后端取前 30 字自动更新
- 消息内容快照: assistant 任务消息存 report_content 快照(report_snapshot),
  历史会话可原样回放, 不依赖任务文件是否仍存在; 同时存 task_id 供溯源
- 会话删除时消息级联删除(DB ON DELETE CASCADE)
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ChatSession(Base):
    __tablename__ = "chat_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner: Mapped[str] = mapped_column(String(64), index=True, comment="会话所属用户名(隔离依据)")
    title: Mapped[str] = mapped_column(String(200), default="新对话", comment="会话标题(首条消息自动生成)")
    is_pinned: Mapped[bool] = mapped_column(default=False, comment="置顶: 列表优先展示")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, index=True
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<ChatSession {self.id} owner={self.owner} title={self.title[:12]}>"


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("chat_sessions.id", ondelete="CASCADE"),
        index=True,
        comment="所属会话(删除会话时级联删除消息)",
    )
    role: Mapped[str] = mapped_column(String(20), comment="user / assistant")
    type: Mapped[str] = mapped_column(String(20), default="text", comment="text / task / chat")
    content: Mapped[str] = mapped_column(Text, default="", comment="消息正文(markdown)")
    task_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True, comment="关联分析任务")
    report_snapshot: Mapped[str | None] = mapped_column(Text, nullable=True, comment="报告内容快照(历史回放)")
    has_pdf: Mapped[bool] = mapped_column(default=False, comment="任务生成了 PDF")
    has_board: Mapped[bool] = mapped_column(default=False, comment="任务生成了看板")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<ChatMessage {self.id} session={self.session_id} role={self.role} type={self.type}>"
