"""审计日志表: 记录每次请求的输入输出、智能体调用路径、审批人/时间, 满足合规审计。"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AuditLog(Base):
    """审计日志(等保 / GDPR 合规)。"""

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    event: Mapped[str] = mapped_column(String(64), comment="事件类型: task_submitted/node_started/approved/...")
    actor: Mapped[str | None] = mapped_column(String(128), nullable=True, comment="操作者(用户/审批人)")
    node_name: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="智能体节点")
    client_ip: Mapped[str | None] = mapped_column(String(45), nullable=True, comment="客户端 IP(IPv4/IPv6, CR-04)")
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True, comment="客户端 User-Agent(CR-04)")
    state_before: Mapped[dict | None] = mapped_column(JSON, nullable=True, comment="操作前状态快照(脱敏后, CR-04)")
    state_after: Mapped[dict | None] = mapped_column(JSON, nullable=True, comment="操作后状态快照(脱敏后, CR-04)")
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True, comment="事件详情(输入输出/决策依据, 脱敏后)")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
