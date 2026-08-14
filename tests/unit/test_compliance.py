"""阶段 1 合规审计功能单元测试: CR-05 SQL 代价预判 / CR-07 敏感表保护。"""
from __future__ import annotations

from src.tools.sql_validator import check_query_cost, find_sensitive_tables, validate_readonly


# ---------------- CR-05 代价预判 ----------------
def test_cost_ok_normal_select() -> None:
    ok, reason = check_query_cost("SELECT * FROM orders WHERE id = 1 LIMIT 10")
    assert ok, reason


def test_cost_ok_aggregate_without_limit() -> None:
    # 聚合查询无需 LIMIT(全表扫描豁免)
    ok, reason = check_query_cost("SELECT COUNT(*) FROM orders")
    assert ok, reason


def test_cost_ok_join_with_where() -> None:
    ok, reason = check_query_cost("SELECT * FROM users u JOIN orders o ON u.id = o.user_id WHERE o.amount > 100 LIMIT 5")
    assert ok, reason


def test_cost_reject_sleep_function() -> None:
    ok, reason = check_query_cost("SELECT pg_sleep(10)")
    assert not ok and "高危函数" in reason


def test_cost_reject_copy_outfile() -> None:
    assert not check_query_cost("SELECT * FROM users INTO OUTFILE '/tmp/x'")[0]
    assert not check_query_cost("COPY users TO '/tmp/x'")[0]


def test_cost_reject_cartesian_without_join_condition() -> None:
    # 裸 JOIN(无 ON)视为笛卡尔积风险, 拒绝
    ok, reason = check_query_cost("SELECT * FROM users u JOIN orders o")
    assert not ok and "连接条件" in reason
    # 逗号连接(隐式笛卡尔积)拒绝
    ok2, reason2 = check_query_cost("SELECT * FROM users, orders")
    assert not ok2 and "连接条件" in reason2


def test_cost_allow_join_with_on() -> None:
    # JOIN ... ON ... 提供连接条件, 不是笛卡尔积, 放行(带 WHERE+LIMIT 避免全表扫描检查)
    ok, reason = check_query_cost(
        "SELECT * FROM users u JOIN orders o ON u.id = o.user_id WHERE o.id > 0 LIMIT 10"
    )
    assert ok


def test_cost_reject_full_scan_without_limit() -> None:
    ok, reason = check_query_cost("SELECT * FROM orders WHERE status = 'active'")
    assert not ok and "全表扫描" in reason


def test_validate_readonly_includes_cost_check() -> None:
    # 合法只读查询但无 LIMIT -> 拒绝(代价预判)
    ok, reason = validate_readonly("SELECT * FROM orders WHERE id = 1")
    assert not ok and "LIMIT" in reason
    # 带 LIMIT -> 通过
    ok, _ = validate_readonly("SELECT * FROM orders WHERE id = 1 LIMIT 10")
    assert ok


# ---------------- CR-07 敏感表保护 ----------------
def test_sensitive_table_detected() -> None:
    hits = find_sensitive_tables("SELECT * FROM user_phone_records WHERE id = 1")
    assert "user_phone_records" in hits


def test_sensitive_table_no_false_positive() -> None:
    hits = find_sensitive_tables("SELECT * FROM orders WHERE user_id = 1 LIMIT 10")
    assert hits == []


def test_sensitive_table_schema_qualified() -> None:
    hits = find_sensitive_tables("SELECT * FROM public.id_card_info LIMIT 5")
    assert "id_card_info" in hits


def test_sensitive_table_join_detected() -> None:
    hits = find_sensitive_tables("SELECT * FROM orders o JOIN bank_card_records b ON o.id = b.order_id LIMIT 5")
    assert "bank_card_records" in hits
