"""安全工具: AES-256-GCM 加密 / 数据脱敏 / 密码哈希。

- AES-256-GCM: 用于加密检查点快照等敏感落库数据(密钥来自环境变量)
- 脱敏: 手机号掩码、身份证掩码、哈希(用户 ID)
- 密码哈希: PBKDF2-HMAC-SHA256(带随机盐, 标准库实现, 用于本地 JWT 认证)
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from typing import Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from src.utils.settings import get_settings

# 密钥优先级: 环境变量 SECRET_KEY > 默认开发密钥(生产必须替换)
_SECRET_ENV = "SECRET_KEY"
_DEFAULT_SECRET = "dev-only-secret-key-change-me-0123456789abcdef"  # 32 字节

# 手机号: 138****1234
_PHONE_RE = __import__("re").compile(r"(?<=\d{3})\d{4}(?=\d{4})")
# 身份证脱敏: 保留前 6 位, 中间打码, 保留后 4 位(18位) / 后 3 位(15位), 长度不变
# 18位: 6(地址) + 8(生日) + 4(顺序码+校验码)
_ID18_RE = __import__("re").compile(r"(?<=\d{6})\d{8}(?=\d{3}[\dXx](?!\d))")
# 15位: 6(地址) + 6(生日) + 3(顺序码)
_ID15_RE = __import__("re").compile(r"(?<=\d{6})\d{6}(?=\d{3}(?!\d))")

# 密码哈希: PBKDF2-HMAC-SHA256 参数
_PBKDF2_ALGO = "pbkdf2"
_PBKDF2_ITERATIONS = 260_000  # OWASP 建议量级; 本地认证可调
_SALT_BYTES = 16


def hash_password(password: str) -> str:
    """PBKDF2-HMAC-SHA256 哈希, 返回 pbkdf2$iterations$salt_hex$hash_hex。"""
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return f"{_PBKDF2_ALGO}${_PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """校验密码与存储哈希是否匹配(恒定时间比较)。"""
    try:
        algo, iterations, salt_hex, hash_hex = stored.split("$")
        if algo != _PBKDF2_ALGO:
            return False
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except (ValueError, TypeError):
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations))
    return hmac.compare_digest(digest, expected)


def _get_secret_key() -> bytes:
    key = os.getenv(_SECRET_ENV, _DEFAULT_SECRET)
    if len(key.encode()) < 32:
        key = key + "x" * (32 - len(key.encode()))
    return key.encode()[:32]


def encrypt(plaintext: str) -> str:
    """AES-256-GCM 加密, 返回 hex(iv + ciphertext + tag)。"""
    key = _get_secret_key()
    nonce = os.urandom(12)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), None)
    return (nonce + ciphertext).hex()


def decrypt(payload: str) -> str:
    """解密 encrypt 产物。"""
    key = _get_secret_key()
    raw = bytes.fromhex(payload)
    nonce, ciphertext = raw[:12], raw[12:]
    return AESGCM(key).decrypt(nonce, ciphertext, None).decode("utf-8")


def mask_phone(phone: str) -> str:
    """手机号脱敏: 138****1234。"""
    return _PHONE_RE.sub("****", phone)


def mask_id_card(id_card: str) -> str:
    """身份证脱敏: 18 位前 6 后 4 / 15 位前 6 后 3, 中间打码且长度不变。"""
    out = _ID18_RE.sub("********", id_card)
    return _ID15_RE.sub("******", out)


def hash_identifier(value: str, salt: Optional[str] = None) -> str:
    """用户 ID 等标识符哈希(SHA-256), 支持加盐。"""
    payload = f"{salt or ''}:{value}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def mask_sensitive(text: str) -> str:
    """通用文本脱敏: 先脱敏身份证, 再脱敏手机号。

    顺序关键: 先处理身份证(把中间打码成 *), 避免手机号正则误伤身份证里的数字段。
    """
    text = mask_id_card(text)
    return mask_phone(text)


# 便捷单例(避免重复读环境变量)
def get_security_module() -> str:
    return "AES-256-GCM"
