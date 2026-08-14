"""节点运行明细表(task_node_runs): 每节点每次执行(含重试)一行。

用途(优化方案-指标落库):
- 记录各节点实际 Token 消耗 / 沙箱输出行数 / 耗时 / 错误, 供成本核算与性能分析
- task_node_runs 是 cost_records 的父表(run_id 关联), 防重试重复计费
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TaskNodeRun(Base):
    __tablename__ = "task_node_runs"
    __table_args__ = (
        Index("ix_task_node_runs_task_created", "task_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tasks.id", ondelete="CASCADE"), comment="任务ID(UUID)"
    )
    run_seq: Mapped[int] = mapped_column(Integer, default=1, comment="同任务同节点的执行序号(重试递增)")
    node_name: Mapped[str] = mapped_column(String(64), comment="节点名: supervisor/planner/coder/executor/reporter")
    model_name: Mapped[str | None] = mapped_column(String(128), nullable=True, comment="模型名(executor 为 sandbox)")
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0, comment="输入token数")
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0, comment="输出token数")
    output_rows: Mapped[int] = mapped_column(Integer, default=0, comment="沙箱输出行数(executor)")
    duration_ms: Mapped[int] = mapped_column(Integer, default=0, comment="执行耗时(毫秒)")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True, comment="失败原因(成功为空)")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<TaskNodeRun task={self.task_id} node={self.node_name} seq={self.run_seq}>"
