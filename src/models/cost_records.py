"""成本明细表(cost_records): 每次 LLM 调用一条, 区分预估/实际。

用途(优化方案-成本核算与预算控制):
- cost_type: estimate=Planner 后预估(超限转审批的依据) / actual=实际调用落账
- run_id 关联 task_node_runs, 重试不会重复计费
- user_id 预留: 当前单用户 dev 模式为空, 接入 oauth2 后按用户维度核算
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class CostRecord(Base):
    __tablename__ = "cost_records"
    __table_args__ = (
        Index("ix_cost_records_task_created", "task_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tasks.id", ondelete="CASCADE"), comment="任务ID(UUID)"
    )
    run_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("task_node_runs.id", ondelete="SET NULL"), nullable=True,
        comment="关联节点运行明细; estimate 预估记录为空"
    )
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="用户ID(预留, oauth2 后非空)")
    node_name: Mapped[str] = mapped_column(String(64), comment="节点名")
    model_name: Mapped[str] = mapped_column(String(128), comment="模型名")
    cost_type: Mapped[str] = mapped_column(String(16), default="actual", comment="estimate=预估 | actual=实际")
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0, comment="输入token数")
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0, comment="输出token数")
    cost_amount: Mapped[float] = mapped_column(Numeric(16, 6), comment="成本金额(元)")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<CostRecord task={self.task_id} {self.cost_type}={self.cost_amount}>"
