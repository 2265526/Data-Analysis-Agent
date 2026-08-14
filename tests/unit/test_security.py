"""密码哈希单元测试(PBKDF2-HMAC-SHA256)。"""
from __future__ import annotations

from src.utils.security import hash_password, verify_password


def test_hash_verify_roundtrip() -> None:
    stored = hash_password("s3cret-pass")
    assert stored.startswith("pbkdf2$")
    assert verify_password("s3cret-pass", stored)


def test_wrong_password_rejected() -> None:
    stored = hash_password("correct-pass")
    assert not verify_password("wrong-pass", stored)


def test_hash_is_salted_and_unique() -> None:
    # 相同密码两次哈希结果不同(随机盐)
    assert hash_password("same") != hash_password("same")


def test_malformed_stored_hash_rejected() -> None:
    assert not verify_password("x", "not-a-hash")
    assert not verify_password("x", "md5$1$aa$bb")  # 非 pbkdf2 算法
    assert not verify_password("x", "")
