"""安全工具单元测试: 脱敏 / 加密 / 密码哈希(扩展边界用例)。

已知缺陷: mask_id_card 正则吞位(18位->17位)、mask_sensitive 先 phone 后 id 导致
身份证被手机号正则误伤。相关失败用例见 data/ 目录测试日志。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.utils.security import (
    decrypt,
    encrypt,
    hash_identifier,
    hash_password,
    mask_id_card,
    mask_phone,
    mask_sensitive,
    verify_password,
)


# ---------- 手机号脱敏 ----------
def test_mask_phone():
    assert mask_phone("13812345678") == "138****5678"


def test_mask_phone_in_text():
    assert "138****5678" in mask_sensitive("手机号 13812345678 已登记")


# ---------- 身份证脱敏 ----------
def test_mask_id_card_keeps_length():
    """18 位身份证脱敏后应保持 18 位(已知缺陷: 实际 17 位)。"""
    idc = "110101199001011234"
    out = mask_id_card(idc)
    assert len(out) == len(idc), f"脱敏后长度 {len(out)} != {len(idc)}: {out}"


def test_mask_id_card_masks_middle():
    idc = "110101199001011234"
    out = mask_id_card(idc)
    assert "19900101" not in out  # 出生日期必须被掩码


def test_mask_id_card_preserves_suffix4():
    """标准脱敏"前 6 后 4": 后 4 位应保留(已知缺陷: 只保留后 3 位)。"""
    idc = "110101199001011234"
    out = mask_id_card(idc)
    assert out.endswith("1234"), f"后 4 位应保留, 实际: {out}"


def test_mask_id_card_15_digit_keeps_length():
    idc = "110101900101123"  # 15 位旧身份证
    out = mask_id_card(idc)
    assert len(out) == len(idc), f"15 位脱敏后长度 {len(out)} != {len(idc)}: {out}"


def test_mask_sensitive_id_card_not_corrupted():
    """mask_sensitive 对纯身份证不应先被 phone 正则误伤(已知缺陷: 格式错乱)。"""
    idc = "110101199001011234"
    out = mask_sensitive(idc)
    # 正确结果应形如 110101********1234(前6后4), 长度 18
    assert len(out) == len(idc), f"脱敏后长度 {len(out)} != 18: {out}"
    assert out.startswith("110101"), f"前 6 位应保留: {out}"


# ---------- 密码哈希 ----------
def test_hash_and_verify_password():
    stored = hash_password("s3cret")
    assert stored.startswith("pbkdf2$")
    assert verify_password("s3cret", stored) is True
    assert verify_password("wrong", stored) is False


def test_verify_password_bad_format():
    assert verify_password("x", "not-a-hash") is False
    assert verify_password("x", "") is False


# ---------- AES-GCM 加密往返 ----------
def test_encrypt_decrypt_roundtrip():
    plaintext = "敏感数据 13812345678"
    payload = encrypt(plaintext)
    assert payload != plaintext
    assert decrypt(payload) == plaintext


def test_encrypt_produces_hex():
    import re

    payload = encrypt("hello")
    assert re.fullmatch(r"[0-9a-f]+", payload)


# ---------- 标识符哈希 ----------
def test_hash_identifier():
    assert hash_identifier("user1") == hash_identifier("user1")
    assert hash_identifier("user1") != hash_identifier("user2")
    assert hash_identifier("u", salt="s") != hash_identifier("u", salt="t")
    assert hash_identifier("u") != hash_identifier("u", salt="s")
