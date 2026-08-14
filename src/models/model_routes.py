"""模型路由表(model_routes): 节点->模型->单价->优先级。

用途(优化方案-成本核算/模型分级):
- 成本核算: cost_records 落库时按本表单价计算金额
- 熔断时机: Planner 后按本表单价预估任务总成本, 超上限转人工审批
- 故障切换: 同节点多行(enabled=true)按 priority 升序取用, 主模型故障时切换备选
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ModelRoute(Base):
    __tablename__ = "model_routes"
    __table_args__ = (
        # 同一节点可配多行(priority 区分主备模型, 开发流程 2.2 主备切换)
        UniqueConstraint("node", "priority", name="uq_model_routes_node_priority"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    node: Mapped[str] = mapped_column(String(64), comment="节点名: supervisor/planner/coder/reporter")
    model_name: Mapped[str] = mapped_column(String(128), comment="模型名, 如 deepseek-chat / qwen-flash")
    price_per_1k_prompt: Mapped[float] = mapped_column(Numeric(16, 6), comment="每1k输入token单价(元)")
    price_per_1k_completion: Mapped[float] = mapped_column(Numeric(16, 6), comment="每1k输出token单价(元)")
    priority: Mapped[int] = mapped_column(Integer, default=1, comment="优先级(数字小优先), 主模型故障时切换备选")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, comment="是否启用; false=停用该路由")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<ModelRoute node={self.node} model={self.model_name} p={self.priority} enabled={self.enabled}>"
