"""查询执行记录表(query_runs): 数据血缘与溯源 —— 每个数字可解释的来源。

用途(血缘与溯源):
- executor 每次执行 SQL 成功后写一条: SQL 全文、解析出的涉及表、返回行数、耗时
- reporter 报告"数据来源与溯源"附录按 task_id 查询本表, 说明报告中每个数字
  来自哪条 SQL、哪些表、返回多少行、什么时间执行
- 对应 OpenLineage 简化版的 Run 模型(Job=一次任务, Run=单条 SQL 执行)
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from src.models import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class QueryRun(Base):
    __tablename__ = "query_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String(36), index=True, comment="关联任务 ID")
    run_order: Mapped[int] = mapped_column(Integer, default=0, comment="任务内执行序号(步骤号)")
    sql_text: Mapped[str] = mapped_column(Text, comment="实际执行的 SQL 全文")
    tables: Mapped[list] = mapped_column(JSON, default=list, comment="SQL 涉及的表(sqlglot 解析)")
    status: Mapped[str] = mapped_column(String(16), default="success", comment="success/error")
    rows_returned: Mapped[int] = mapped_column(Integer, default=0, comment="返回行数")
    duration_ms: Mapped[int] = mapped_column(Integer, default=0, comment="执行耗时(毫秒)")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<QueryRun task={self.task_id} order={self.run_order} rows={self.rows_returned}>"
