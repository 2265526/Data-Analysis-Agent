"""认证/权限单元测试: dev 模式放行 + 本地 JWT 认证(签发/验签/角色)。"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

import jwt as pyjwt

from src.api.auth import (
    AUTH_MODE_DEV,
    AUTH_MODE_OAUTH2,
    DevAuthProvider,
    OAuth2JwtAuthProvider,
    User,
    authenticate_user,
    create_access_token,
    decode_access_token,
    get_current_user,
    get_jwt_secret,
    require_role,
)

_SECRET = "k" * 32  # 测试用密钥(>=16 字符)


# ---------- dev 模式 ----------
def test_dev_provider_returns_full_role_user() -> None:
    provider = DevAuthProvider()
    user = provider.authenticate("")
    assert user.id == "dev-user"
    assert "admin" in user.roles and "approver" in user.roles


def test_dev_current_user_without_token() -> None:
    user = get_current_user(authorization=None, provider=DevAuthProvider())
    assert user.id == "dev-user"


def test_dev_current_user_with_bearer_token() -> None:
    user = get_current_user(authorization="Bearer some-token", provider=DevAuthProvider())
    assert user.id == "dev-user"


# ---------- JWT 签发/验签 ----------
def test_create_and_decode_token() -> None:
    token = create_access_token("alice", _SECRET, 60)
    assert decode_access_token(token, _SECRET) == "alice"


def test_expired_token_rejected() -> None:
    token = create_access_token("alice", _SECRET, -1)  # 负有效期 = 立即过期
    with pytest.raises(pyjwt.ExpiredSignatureError):
        decode_access_token(token, _SECRET)


def test_tampered_token_rejected() -> None:
    token = create_access_token("alice", _SECRET, 60)
    with pytest.raises(pyjwt.InvalidSignatureError):
        decode_access_token(token + "x", _SECRET)


def test_wrong_secret_rejected() -> None:
    token = create_access_token("alice", _SECRET, 60)
    with pytest.raises(pyjwt.InvalidSignatureError):
        decode_access_token(token, "z" * 32)


# ---------- 密钥校验 ----------
def test_get_jwt_secret_requires_min_length(monkeypatch) -> None:
    monkeypatch.setattr("src.api.auth.settings.jwt_secret", "short")
    with pytest.raises(RuntimeError):
        get_jwt_secret()


# ---------- 本地 oauth2 provider ----------
def _make_provider(monkeypatch) -> OAuth2JwtAuthProvider:
    monkeypatch.setattr("src.api.auth.settings.jwt_secret", _SECRET)
    return OAuth2JwtAuthProvider()


def test_oauth2_provider_local_auth_ok(monkeypatch) -> None:
    provider = _make_provider(monkeypatch)
    token = create_access_token("alice", _SECRET, 60)

    def _load(username: str):
        return User(id="1", name=username, roles=["approver"]) if username == "alice" else None

    monkeypatch.setattr("src.api.auth._load_user_by_username", _load)
    user = provider.authenticate(token)
    assert user.name == "alice"
    assert "approver" in user.roles


def test_oauth2_provider_invalid_token_401(monkeypatch) -> None:
    provider = _make_provider(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        provider.authenticate("not-a-jwt")
    assert exc.value.status_code == 401


def test_oauth2_provider_unknown_user_401(monkeypatch) -> None:
    provider = _make_provider(monkeypatch)
    token = create_access_token("ghost", _SECRET, 60)
    monkeypatch.setattr("src.api.auth._load_user_by_username", lambda u: None)
    with pytest.raises(HTTPException) as exc:
        provider.authenticate(token)
    assert exc.value.status_code == 401


# ---------- 角色权限 ----------
def test_require_role_passes_for_dev_user() -> None:
    dep = require_role("approver")
    dep(current_user=DevAuthProvider().authenticate(""))  # 不应抛错


def test_require_role_rejects_unmatched_role() -> None:
    dep = require_role("super-admin")
    with pytest.raises(HTTPException) as exc:
        dep(current_user=User(id="u", name="x", roles=["user"]))
    assert exc.value.status_code == 403


# ---------- 登录辅助 ----------
def test_authenticate_user_ok(monkeypatch) -> None:
    from src.utils.security import hash_password

    class _FakeRow:
        id = 7
        username = "alice"
        password_hash = hash_password("right-pass")
        roles = ["user", "approver"]

    class _FakeQuery:
        def __init__(self, rows):
            self._rows = rows
            self._condition = None

        def filter(self, *conditions):
            self._condition = conditions[0]
            return self

        def first(self):
            # 解析 BinaryExpression: left=UserModel.username, right=<str>
            right = self._condition.right
            name = right.value if hasattr(right, "value") else str(right)
            for row in self._rows:
                if row.username == name:
                    return row
            return None

    class _FakeSession:
        def __init__(self):
            self._rows = [_FakeRow()]

        def query(self, *a, **k):
            return _FakeQuery(self._rows)

        def close(self):
            pass

    monkeypatch.setattr("src.api.deps.SessionLocal", lambda: _FakeSession())
    user = authenticate_user("alice", "right-pass")
    assert user is not None and user.name == "alice"
    assert authenticate_user("alice", "wrong-pass") is None
    assert authenticate_user("nobody", "right-pass") is None


def test_oauth2_provider_mode_label() -> None:
    assert AUTH_MODE_DEV == "dev"
    assert AUTH_MODE_OAUTH2 == "oauth2"
