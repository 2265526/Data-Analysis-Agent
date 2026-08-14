"""SQL 只读校验(CR-04/CR-05/CR-07)单元测试: 危险拦截 / 绕过 / 误报 / 敏感表。

部分用例用于暴露已知缺陷(字符串字面量感知缺失), 见 data/ 目录测试日志。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.tools.sql_validator import (
    check_query_cost,
    find_sensitive_tables,
    is_readonly,
    looks_like_sql,
    strip_comments,
    validate_readonly,
)


# ---------- 基本只读判定 ----------
def test_readonly_accepts_plain_select():
    assert is_readonly("SELECT * FROM orders LIMIT 10") is True
    assert is_readonly("  select * from orders limit 10  ") is True
    assert is_readonly("WITH x AS (SELECT 1) SELECT * FROM x LIMIT 1") is True


def test_readonly_rejects_leading_whitespace_then_drop():
    assert is_readonly("DROP TABLE users") is False
    assert is_readonly("UPDATE orders SET x=1") is False
    assert is_readonly("DELETE FROM orders") is False
    assert is_readonly("INSERT INTO t VALUES (1)") is False


def test_readonly_rejects_non_select_prefix():
    assert is_readonly("SHOW TABLES") is False
    assert is_readonly("EXPLAIN SELECT 1") is False
    assert is_readonly("") is False


# ---------- 注释剥离 ----------
def test_strip_comments_removes_block_and_line():
    assert "DROP" not in strip_comments("/* DROP TABLE x */")
    assert "DROP" not in strip_comments("SELECT 1 -- DROP\n")
    assert "DROP" not in strip_comments("SELECT 1 # DROP")


def test_strip_comments_preserves_string_literal():
    """字符串字面量中的 -- / /* 不应被当作注释剥离(已知缺陷: 会被误删)。"""
    out = strip_comments("SELECT 'a--b' AS t FROM orders LIMIT 1")
    assert "a--b" in out, f"字符串字面量被注释剥离破坏: {out!r}"


def test_strip_comments_preserves_block_in_string():
    out = strip_comments("SELECT '/* x */' AS t FROM t1 LIMIT 1")
    assert "/* x */" in out, f"字符串字面量被注释剥离破坏: {out!r}"


# ---------- 关键字黑名单(字符串字面量误报) ----------
def test_validate_readonly_rejects_real_drop():
    ok, reason = validate_readonly("SELECT * FROM orders; DROP TABLE users")
    assert ok is False
    assert "DROP" in reason


def test_validate_readonly_allows_keyword_in_string_literal():
    """字符串字面量中的普通单词不应触发危险关键字(已知缺陷: 被误拦)。"""
    ok, reason = validate_readonly("SELECT 'delete' AS action FROM orders LIMIT 1")
    assert ok is True, f"合法只读查询被误拦: {reason}"


def test_validate_readonly_allows_drop_text_in_string():
    ok, reason = validate_readonly("SELECT 'drop table' AS note FROM orders LIMIT 1")
    assert ok is True, f"合法只读查询被误拦: {reason}"


# ---------- 多语句 / 全表扫描 ----------
def test_validate_readonly_rejects_multi_statement():
    ok, reason = validate_readonly("SELECT * FROM a LIMIT 1; SELECT * FROM b LIMIT 1")
    assert ok is False
    assert "多条" in reason or "多语句" in reason


def test_check_query_cost_rejects_full_scan_without_limit():
    ok, reason = check_query_cost("SELECT * FROM orders")
    assert ok is False


def test_check_query_cost_allows_aggregate_without_limit():
    ok, _ = check_query_cost("SELECT count(*) FROM orders")
    assert ok is True


def test_check_query_cost_rejects_cross_join():
    ok, reason = check_query_cost("SELECT * FROM a, b")
    assert ok is False


def test_check_query_cost_rejects_join_without_on():
    ok, _ = check_query_cost("SELECT * FROM a JOIN b LIMIT 5")
    assert ok is False


def test_check_query_cost_allows_join_with_on():
    ok, _ = check_query_cost("SELECT * FROM a JOIN b ON a.id=b.id LIMIT 5")
    assert ok is True


def test_check_query_cost_rejects_costly_function():
    ok, reason = check_query_cost("SELECT pg_sleep(10)")
    assert ok is False
    assert "pg_sleep" in reason.lower()


# ---------- 敏感表(CR-07) ----------
def test_find_sensitive_tables_by_pattern():
    assert "user_phone_records" in find_sensitive_tables("SELECT * FROM user_phone_records LIMIT 1")


def test_find_sensitive_tables_by_name():
    assert "customers" in find_sensitive_tables("SELECT * FROM customers LIMIT 1")


def test_find_sensitive_tables_no_hit():
    assert find_sensitive_tables("SELECT * FROM orders LIMIT 1") == []


def test_find_sensitive_tables_with_join():
    hits = find_sensitive_tables("SELECT * FROM orders o JOIN customers c ON o.id=c.id LIMIT 1")
    assert "customers" in hits


# ---------- looks_like_sql ----------
def test_looks_like_sql():
    assert looks_like_sql("SELECT 1") is True
    assert looks_like_sql("  -- comment\nSELECT 1") is True
    assert looks_like_sql("/* x */ WITH t AS (SELECT 1) SELECT * FROM t") is True
    assert looks_like_sql("import os") is False
    assert looks_like_sql("print('hi')") is False
    assert looks_like_sql("") is False
