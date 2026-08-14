"""认证与权限: 本地 JWT + 企业 IdP 预留扩展。

- users 表(PostgreSQL)存账号与角色, 密码 PBKDF2-HMAC-SHA256 哈希
- POST /api/v1/auth/login 校验密码并签发 JWT(HS256, 密钥 settings.jwt_secret)
- 受保护路由通过 Depends(get_current_user) / Depends(require_role(...)) 校验
- auth_mode = "dev"    (默认): 直接放行内置开发用户, 单机免登录
- auth_mode = "oauth2" (本地): 验签 JWT + 查本地用户表; 未来对接企业 IdP
  时在 OAuth2JwtAuthProvider.authenticate 中换用 JWKS 验签即可(预留扩展点)

用法(FastAPI 路由):
    from src.api.auth import get_current_user, require_role

    @router.get("/tasks/{task_id}/status")
    def get_task_status(..., current_user: User = Depends(get_current_user)): ...

    @router.post("/tasks/{task_id}/approve", dependencies=[Depends(require_role("approver", "admin"))])
    def approve_task(...): ...
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import jwt
from fastapi import Depends, Header, HTTPException

from src.utils.logger import get_logger
from src.utils.settings import get_settings

logger = get_logger(__name__)
settings = get_settings()

AUTH_MODE_DEV = "dev"
AUTH_MODE_OAUTH2 = "oauth2"
JWT_ALGORITHM = "HS256"


@dataclass
class User:
    """当前登录用户(角色用于权限控制)。"""

    id: str
    name: str
    roles: List[str] = field(default_factory=list)


class AuthProvider(ABC):
    """认证统一接口(本地 JWT / 未来企业 IdP 共用)。"""

    mode = "base"

    @abstractmethod
    def authenticate(self, credentials: str) -> User:
        """根据凭证(Bearer token)解析用户; 无效凭证抛 HTTPException(401)。"""


class DevAuthProvider(AuthProvider):
    """默认实现:返回内置开发用户, 拥有全部角色。"""

    mode = AUTH_MODE_DEV

    def authenticate(self, credentials: str = "") -> User:
        return User(id="dev-user", name="开发用户", roles=["user", "approver", "admin"])


def get_jwt_secret() -> str:
    """获取 JWT 签名密钥; oauth2 模式必须配置, 缺失直接抛错防止弱密钥。"""
    secret = (settings.jwt_secret or "").strip()
    if len(secret) < 16:
        raise RuntimeError(
            "auth_mode=oauth2 需要在 .env 配置 JWT_SECRET(至少 16 字符), "
            "例如: JWT_SECRET=$(python -c 'import secrets; print(secrets.token_hex(32))')"
        )
    return secret


def create_access_token(username: str, secret: str, expires_minutes: int) -> str:
    """签发 JWT(HS256): sub=username, exp/iat 时间戳。"""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": username,
        "iat": now,
        "exp": now + timedelta(minutes=expires_minutes),
    }
    return jwt.encode(payload, secret, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str, secret: str) -> str:
    """验签并返回 sub(用户名); 无效/过期抛 jwt.PyJWTError。"""
    payload = jwt.decode(token, secret, algorithms=[JWT_ALGORITHM])
    username = payload.get("sub")
    if not username:
        raise jwt.InvalidTokenError("missing sub claim")
    return username


def _load_user_by_username(username: str) -> Optional["User"]:
    """按用户名查本地用户表(延迟导入, 避免认证模块强依赖 DB)。"""
    from src.models import User as UserModel

    from src.api.deps import SessionLocal

    db = SessionLocal()
    try:
        row = db.query(UserModel).filter(UserModel.username == username).first()
        if row is None:
            return None
        roles = row.roles or []
        return User(id=str(row.id), name=row.username, roles=[str(r) for r in roles])
    finally:
        db.close()


class OAuth2JwtAuthProvider(AuthProvider):
    """本地 JWT 认证(已实现): 验签 token -> 查本地用户表 -> 返回用户与角色。

    未来对接企业 IdP(企业微信/统一身份认证)时, 在本类基础上扩展:
    将 decode 换成企业 JWKS 验签、用户角色换成 IdP 返回即可, 路由无需改动。
    """

    mode = AUTH_MODE_OAUTH2

    def __init__(self) -> None:
        self._secret = get_jwt_secret()

    def authenticate(self, credentials: str) -> User:
        try:
            username = decode_access_token(credentials, self._secret)
        except jwt.PyJWTError as exc:
            raise HTTPException(status_code=401, detail="登录凭证无效或已过期") from exc

        user = _load_user_by_username(username)
        if user is None:
            raise HTTPException(status_code=401, detail="用户不存在或已停用")
        return user


_provider: Optional[AuthProvider] = None


def get_provider() -> AuthProvider:
    """按 settings.auth_mode 返回认证提供者(单例)。"""
    global _provider
    if _provider is None:
        mode = (settings.auth_mode or AUTH_MODE_DEV).lower()
        _provider = OAuth2JwtAuthProvider() if mode == AUTH_MODE_OAUTH2 else DevAuthProvider()
    return _provider


def get_current_user(
    authorization: Optional[str] = Header(default=None),
    provider: AuthProvider = Depends(get_provider),
) -> User:
    """FastAPI 依赖: 从 Authorization: Bearer <token> 解析当前用户。

    dev 模式忽略 token 直接放行; oauth2 模式由 provider 验签解析。
    """
    token = ""
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    try:
        return provider.authenticate(token)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 — 认证失败统一 401, 不泄露内部细节
        logger.warning("auth_failed", error=str(exc))
        raise HTTPException(status_code=401, detail="认证失败") from exc


def require_role(*roles: str):
    """返回 FastAPI 依赖: 校验当前用户是否拥有任一指定角色, 否则 403。

    用法: @router.post(..., dependencies=[Depends(require_role("approver"))])
    同时将当前用户对象作为依赖返回值注入(供 handler 使用, 现有调用忽略返回不受影响)。
    """

    def _dependency(current_user: User = Depends(get_current_user)) -> User:
        if not (set(roles) & set(current_user.roles)):
            raise HTTPException(
                status_code=403,
                detail=f"权限不足, 需要角色: {', '.join(roles)}",
            )
        return current_user

    return _dependency


def authenticate_user(username: str, password: str) -> Optional[User]:
    """校验用户名/密码(供 /auth/login 使用); 失败返回 None。"""
    from src.models import User as UserModel

    from src.api.deps import SessionLocal
    from src.utils.security import verify_password

    db = SessionLocal()
    try:
        row = db.query(UserModel).filter(UserModel.username == username).first()
        if row is None or not verify_password(password, row.password_hash):
            return None
        roles = row.roles or []
        return User(id=str(row.id), name=row.username, roles=[str(r) for r in roles])
    finally:
        db.close()
