"""指标定义表(metric_definitions): 指标/语义层 —— 口径锁定的唯一事实来源。

用途(指标/语义层):
- 把"销售额/订单量/客单价"等指标的**计算口径**固化入库(agg + expr + filter),
  避免 LLM 生成 SQL 时自由发挥导致口径漂移(同一指标不同人算出不同数字)
- coder 生成 SQL 时注入"可用指标目录", 约束聚合逻辑遵循锁定口径
- reporter 的"数据口径"章节与"血缘溯源"以本表为权威依据
- 指标支持别名(alias, 同义词)供 LLM 召回、状态(active/deprecated)支持口径下线
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from src.models import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class MetricDefinition(Base):
    __tablename__ = "metric_definitions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name_en: Mapped[str] = mapped_column(String(128), unique=True, comment="指标英文标识(唯一), SQL 中口径以此为准")
    name_cn: Mapped[str] = mapped_column(String(128), comment="指标中文名(展示用)")
    alias: Mapped[list] = mapped_column(JSON, default=list, comment="同义词/业务叫法, 供 LLM 检索召回, 如 ['销售额','成交额','GMV']")
    description: Mapped[str] = mapped_column(Text, default="", comment="业务口径自然语言描述")
    agg: Mapped[str] = mapped_column(String(32), default="sum", comment="聚合方式: sum/count/count_distinct/avg/max/min")
    expr: Mapped[str] = mapped_column(Text, default="", comment="口径表达式(如 total_item_amount), 口径锁定的核心")
    filter: Mapped[str] = mapped_column(Text, default="", comment="默认过滤条件(口径限定, 如 order_status='已完成')")
    unit: Mapped[str] = mapped_column(String(32), default="", comment="单位, 如 元/笔/%")
    source_tables: Mapped[list] = mapped_column(JSON, default=list, comment="口径涉及的物理表, 血缘溯源用")
    category: Mapped[str] = mapped_column(String(64), default="general", comment="指标分类, 如 sales/order/ratio")
    status: Mapped[str] = mapped_column(String(16), default="active", comment="active=可用 / deprecated=已废弃(LLM 不再使用)")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<MetricDefinition {self.name_en}({self.name_cn}) agg={self.agg}({self.expr})>"
