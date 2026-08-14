"""应用启动引导: 建表(幂等) + 默认管理员种子(幂等)。

- 首次启动自动创建默认管理员 admin/admin(角色 user/approver/admin)
- 之后启动检测到已存在则跳过, 不覆盖已有密码/角色
- 依赖 scripts/init_db.py 的表结构; 本模块 create_all 兜底保证可直接运行
"""
from __future__ import annotations

from src.api.deps import SessionLocal, engine
from src.models import Base
from src.utils.logger import get_logger
from src.utils.security import hash_password

logger = get_logger(__name__)

DEFAULT_ADMIN_USERNAME = "admin"
DEFAULT_ADMIN_PASSWORD = "admin"
DEFAULT_ADMIN_ROLES = ["user", "approver", "admin"]


def _ensure_chat_session_columns() -> None:
    """幂等列迁移: 为已存在的 chat_sessions 表补充新列(新部署由 create_all 直接建全)。

    create_all 不会给已存在表加列, 故需手动 ALTER; 检查 information_schema 幂等执行。
    """
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    if "chat_sessions" not in insp.get_table_names():
        return
    existing = {c["name"] for c in insp.get_columns("chat_sessions")}
    if "is_pinned" not in existing:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "ALTER TABLE chat_sessions ADD COLUMN is_pinned BOOLEAN NOT NULL DEFAULT FALSE"
                )
            )
        logger.info("chat_sessions_migrated", column="is_pinned")


def _ensure_task_session_column() -> None:
    """幂等列迁移: 为已存在的 tasks 表补充 session_id 列(多轮上下文关联)。"""
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    if "tasks" not in insp.get_table_names():
        return  # 新表由 create_all 创建, 无需迁移
    existing = {c["name"] for c in insp.get_columns("tasks")}
    if "session_id" not in existing:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE tasks ADD COLUMN session_id INTEGER"))
        logger.info("tasks_migrated", column="session_id")
    if "data_source_id" not in existing:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE tasks ADD COLUMN data_source_id INTEGER"))
        logger.info("tasks_migrated", column="data_source_id")
    if "source" not in existing:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE tasks ADD COLUMN source VARCHAR(16) DEFAULT 'manual'"))
        logger.info("tasks_migrated", column="source")


def _ensure_scheduled_task_columns() -> None:
    """幂等列迁移: scheduled_tasks 补充非程序员友好的频率字段。"""
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    if "scheduled_tasks" not in insp.get_table_names():
        return
    existing = {c["name"] for c in insp.get_columns("scheduled_tasks")}
    for col, ddl in (
        ("schedule_type", "ALTER TABLE scheduled_tasks ADD COLUMN schedule_type VARCHAR(16) DEFAULT 'daily'"),
        ("schedule_time", "ALTER TABLE scheduled_tasks ADD COLUMN schedule_time VARCHAR(8) DEFAULT '09:00'"),
        ("schedule_weekday", "ALTER TABLE scheduled_tasks ADD COLUMN schedule_weekday VARCHAR(16) DEFAULT '1'"),
        ("notify_users", "ALTER TABLE scheduled_tasks ADD COLUMN notify_users JSON"),
        ("data_source_ids", "ALTER TABLE scheduled_tasks ADD COLUMN data_source_ids JSON"),
        ("approval_status", "ALTER TABLE scheduled_tasks ADD COLUMN approval_status VARCHAR(16) DEFAULT 'pending'"),
        ("approved_by", "ALTER TABLE scheduled_tasks ADD COLUMN approved_by VARCHAR(64)"),
        ("approved_at", "ALTER TABLE scheduled_tasks ADD COLUMN approved_at TIMESTAMPTZ"),
    ):
        if col not in existing:
            with engine.begin() as conn:
                conn.execute(text(ddl))
            logger.info("scheduled_tasks_migrated", column=col)


def _ensure_default_data_policies() -> None:
    """幂等种子: 普通 user 角色对强 PII 列的默认掩码规则(默认允许 + 敏感列预置掩码)。

    - user 角色: customers.phone / id_card / address、suppliers.contact_phone -> mask('***')
    - approver / admin 角色: 同表同列显式 allow(豁免)——标准账号 roles 含 user,
      若不给高权限角色配豁免, user 的掩码会粘在审批人/管理员身上
    - 不掩码金额/成本等业务指标列, 避免破坏日常销售分析(需要时管理员自行配置)
    """
    from src.models import DataPolicyRule

    # (表, 列清单): 每张表为 user(掩码) 与 approver/admin(豁免) 生成规则
    SENSITIVE_COLS = {
        "customers": ["phone", "id_card", "address"],
        "suppliers": ["contact_phone"],
    }

    db = SessionLocal()
    try:
        for table, cols in SENSITIVE_COLS.items():
            for target_name, mode in (("user", "mask"), ("approver", "allow"), ("admin", "allow")):
                exists = (
                    db.query(DataPolicyRule)
                    .filter(
                        DataPolicyRule.target_type == "role",
                        DataPolicyRule.target_name == target_name,
                        DataPolicyRule.table_name == table,
                    )
                    .first()
                )
                if exists is not None:
                    continue
                db.add(
                    DataPolicyRule(
                        target_type="role",
                        target_name=target_name,
                        table_name=table,
                        col_access={c: mode for c in cols},
                        created_by="system",
                    )
                )
                logger.info(
                    "default_data_policy_seeded",
                    target=target_name,
                    table=table,
                    mode=mode,
                )
        db.commit()
    finally:
        db.close()


def ensure_schema_and_default_admin() -> None:
    """建表(幂等) + 轻量列迁移 + 默认管理员 admin/admin(幂等)。"""
    from src.models import User as UserModel

    Base.metadata.create_all(bind=engine)
    _ensure_chat_session_columns()
    _ensure_task_session_column()
    _ensure_scheduled_task_columns()
    _ensure_default_data_policies()

    db = SessionLocal()
    try:
        exists = (
            db.query(UserModel).filter(UserModel.username == DEFAULT_ADMIN_USERNAME).first()
        )
        if exists is None:
            db.add(
                UserModel(
                    username=DEFAULT_ADMIN_USERNAME,
                    password_hash=hash_password(DEFAULT_ADMIN_PASSWORD),
                    roles=DEFAULT_ADMIN_ROLES,
                )
            )
            db.commit()
            logger.info("default_admin_created", username=DEFAULT_ADMIN_USERNAME)
        else:
            logger.info("default_admin_exists", username=DEFAULT_ADMIN_USERNAME)
    finally:
        db.close()
