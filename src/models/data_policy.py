"""数据级权限策略表(RBAC 扩展): 表级/列级/行级 数据访问控制。

模型语义(对齐业界 Apache Ranger RLEF/CLM 思路, 应用层 SQL 改写强制):

- target_type: role | user —— 策略绑定到角色或具体用户; 用户级策略优先于角色级
- table_name: 业务表(如 orders / customers), 无策略 = 默认允许
- row_filter: 行级过滤(WHERE 表达式片段, 如 "orders.order_date >= now() - interval '90 days'"),
  由改写引擎注入查询; 管理员配置的受信 SQL 表达式
- col_access: JSON, 列 -> allow | mask | deny
    - allow: 原样可见
    - mask: 以 mask_expression(或全局默认 '***')脱敏
    - deny: 禁止访问(查询引用该列直接拒绝)
- mask_expression: 该规则下所有 mask 列使用的脱敏表达式(PostgreSQL 方言, 可选;
  缺省用全局默认掩码 '***')
- enabled: 软开关, 关闭即视为无策略(默认允许)

改写引擎见 src/tools/data_policy.py, 强制执行点:
- src/nodes/executor.py(agent 生成的 SQL, 提交执行前)
- src/api/routes.py drill_task_board(下钻 SQL)
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base

# 列访问模式枚举(与 data_policy.py 共用)
COL_ALLOW = "allow"
COL_MASK = "mask"
COL_DENY = "deny"
COL_MODES = {COL_ALLOW, COL_MASK, COL_DENY}

# 目标类型枚举
TARGET_ROLE = "role"
TARGET_USER = "user"
TARGET_TYPES = {TARGET_ROLE, TARGET_USER}

# 全局默认掩码表达式(列被 mask 且未配置 mask_expression 时使用)
DEFAULT_MASK_EXPRESSION = "'***'"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DataPolicyRule(Base):
    """一条数据权限规则: (目标, 表) 维度的行过滤 + 列访问控制。"""

    __tablename__ = "data_policy_rules"
    __table_args__ = (
        UniqueConstraint(
            "target_type", "target_name", "table_name", name="uq_data_policy_target_table"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    target_type: Mapped[str] = mapped_column(
        String(16), comment="role | user"
    )
    target_name: Mapped[str] = mapped_column(
        String(64), index=True, comment="角色名(如 user/approver)或用户名"
    )
    table_name: Mapped[str] = mapped_column(
        String(128), index=True, comment="业务表名"
    )
    row_filter: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="行级过滤 WHERE 表达式片段(受信 SQL, 注入查询)"
    )
    col_access: Mapped[dict] = mapped_column(
        JSON, default=dict, comment='{"column": "allow|mask|deny", ...}'
    )
    mask_expression: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="mask 列使用的脱敏表达式(PostgreSQL 方言, 缺省 \'***\')"
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, comment="软开关")
    created_by: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="创建人(管理员)"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )
