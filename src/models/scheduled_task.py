"""定时分析任务表(scheduled_tasks): 定期自动执行分析并推送结果。

产品背景(PM 评估 P1): 平台原只能"人发起 -> 跑一次"。本表让管理员配置
定时任务(如每天早上 9 点跑销售日报), 由进程内调度器(APScheduler)按 cron
触发 -> 创建 Task 走完整流水线 -> 完成后给 owner 写站内通知。

- cron: 标准 5 段 cron 表达式(如 "0 9 * * *" = 每天 09:00)
- data_source_id: 执行时使用的数据源(空=主库)
- 调度器: src/tools/scheduler.py, FastAPI startup 时启动, 单机部署生效
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ScheduledTask(Base):
    __tablename__ = "scheduled_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), comment="任务名称(如 每日销售日报)")
    query: Mapped[str] = mapped_column(Text, comment="自然语言分析需求")
    cron: Mapped[str] = mapped_column(
        String(64), default="0 9 * * *", comment="cron 表达式(分 时 日 月 周)"
    )
    data_source_id: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="数据源 id(空=主库; 兼容旧字段)")
    data_source_ids: Mapped[list] = mapped_column(
        JSON, default=list, comment="跨数据源: 目标数据源 id 列表(空=[主库]; 仅管理员可配)"
    )
    owner: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="创建人/结果接收人")
    # 非程序员友好的频率描述(由后端翻译为 cron 存库; cron 字段始终为真值)
    schedule_type: Mapped[str] = mapped_column(
        String(16), default="daily", comment="daily/weekly/monthly/custom"
    )
    schedule_time: Mapped[str] = mapped_column(String(8), default="09:00", comment="执行时间 HH:MM")
    schedule_weekday: Mapped[str] = mapped_column(
        String(16), default="1", comment="weekly 时: cron 星期值 0=周日..6=周六, 逗号分隔"
    )
    notify_users: Mapped[list] = mapped_column(
        JSON, default=list, comment="推送人员范围(仅管理员可设; 空=仅创建人收到结果通知)"
    )
    # 永久审批: 定时任务触发的分析不再逐单挂起审批, 由管理员一次性"永久审批"
    approval_status: Mapped[str] = mapped_column(
        String(16), default="pending", comment="pending/approved/rejected"
    )
    approved_by: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="永久审批人")
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, comment="启用/停用")
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
