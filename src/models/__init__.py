"""SQLAlchemy ORM 模型: 任务状态表 + 审计日志表 + 用户表。"""
from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """所有 ORM 模型的基类(Alembic autogenerate 依赖其 metadata)。"""


from src.models.task import Task  # noqa: E402
from src.models.audit import AuditLog  # noqa: E402
from src.models.user import User  # noqa: E402
from src.models.model_routes import ModelRoute  # noqa: E402
from src.models.task_node_runs import TaskNodeRun  # noqa: E402
from src.models.cost_records import CostRecord  # noqa: E402
from src.models.metric_definition import MetricDefinition  # noqa: E402
from src.models.query_run import QueryRun  # noqa: E402
from src.models.chat_session import ChatSession, ChatMessage  # noqa: E402
from src.models.data_policy import DataPolicyRule  # noqa: E402
from src.models.data_source import DataSource  # noqa: E402
from src.models.scheduled_task import ScheduledTask  # noqa: E402
from src.models.notification import Notification  # noqa: E402
from src.models.schema_dict import SchemaDict  # noqa: E402

__all__ = [
    "Base",
    "Task",
    "AuditLog",
    "User",
    "ModelRoute",
    "TaskNodeRun",
    "CostRecord",
    "MetricDefinition",
    "QueryRun",
    "ChatSession",
    "ChatMessage",
    "DataPolicyRule",
    "DataSource",
    "ScheduledTask",
    "Notification",
    "SchemaDict",
]
