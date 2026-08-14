"""用户表: 本地 JWT 认证的用户与角色(本地部署方案, 见开发流程 6.3 备注)。

- password_hash: PBKDF2-HMAC-SHA256(见 src/utils/security.py), 不存明文
- roles: JSON 列表, 如 ["user", "approver", "admin"], 用于 require_role 权限校验
- 由 scripts/init_users.py 创建/更新; 企业 IdP 对接时本表可作为本地兜底
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    """本地用户账号(登录 + 角色授权)。"""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(
        String(64), unique=True, index=True, comment="登录用户名"
    )
    password_hash: Mapped[str] = mapped_column(String(256), comment="PBKDF2 哈希, 不存明文")
    roles: Mapped[list] = mapped_column(JSON, default=list, comment='角色列表, 如 ["user","approver","admin"]')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)
