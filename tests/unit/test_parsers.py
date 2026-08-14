"""解析/契约类单元测试: 结构化 JSON 解析 / SQL 血缘提取 / DB URL 解析 / 闲聊检测边界。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.tools.lineage import extract_tables
from src.tools.schema_provider import parse_db_url
from src.utils.chat_gate import is_chitchat
from src.utils.structured_json import parse_json_content


# ---------- structured_json.parse_json_content ----------
def test_parse_plain_object():
    assert parse_json_content('{"a": 1}') == {"a": 1}


def test_parse_fenced_json():
    assert parse_json_content('```json\n{"a": 1}\n```') == {"a": 1}


def test_parse_with_surrounding_text():
    assert parse_json_content('说明文字 {"a": 1} 结尾') == {"a": 1}


def test_parse_array():
    assert parse_json_content('[{"a": 1}, {"a": 2}]') == [{"a": 1}, {"a": 2}]


def test_parse_empty_raises():
    with pytest.raises(ValueError):
        parse_json_content("")


def test_parse_non_json_raises():
    with pytest.raises(ValueError):
        parse_json_content("not json at all")


# ---------- lineage.extract_tables ----------
def test_extract_tables_simple_join():
    sql = "SELECT * FROM orders JOIN order_items oi ON oi.order_id = orders.id"
    assert extract_tables(sql) == ["orders", "order_items"]


def test_extract_tables_excludes_cte_name():
    sql = "WITH t AS (SELECT * FROM users) SELECT * FROM t JOIN orders ON 1=1"
    tables = extract_tables(sql)
    assert "users" in tables and "orders" in tables
    assert "t" not in tables  # CTE 别名不是物理表


def test_extract_tables_comma_join():
    sql = "SELECT a.x, b.y FROM a, b WHERE a.id = b.id"
    assert extract_tables(sql) == ["a", "b"]


def test_extract_tables_quoted_identifier():
    assert extract_tables('SELECT * FROM "Order Items"') == ["Order Items"]


def test_extract_tables_empty_or_non_sql():
    assert extract_tables("") == []
    assert extract_tables("not sql") == []


# ---------- schema_provider.parse_db_url ----------
def test_parse_db_url_full():
    d = parse_db_url("postgresql://postgres:236591@localhost:5433/data_agent")
    assert d["host"] == "localhost"
    assert d["port"] == 5433
    assert d["dbname"] == "data_agent"
    assert d["user"] == "postgres"
    assert d["password"] == "236591"


def test_parse_db_url_no_port_defaults_5432():
    d = parse_db_url("postgresql://user@host/db")
    assert d["port"] == 5432
    assert d["dbname"] == "db"
    assert d["password"] == ""


def test_parse_db_url_password_with_at():
    d = parse_db_url("postgresql://user:p@ss@host:5432/db")
    assert d["password"] == "p@ss"


# ---------- chat_gate.is_chitchat 边界 ----------
def test_chitchat_greeting():
    assert is_chitchat("你好") is True
    assert is_chitchat("谢谢") is True
    assert is_chitchat("再见") is True


def test_chitchat_analysis_wins():
    assert is_chitchat("统计销售额") is False
    assert is_chitchat("查询数据") is False
    assert is_chitchat("统计销售额, 谢谢你") is False  # 含分析词优先


def test_chitchat_empty_and_short():
    assert is_chitchat("") is True
    assert is_chitchat("嗯") is True


def test_chitchat_analysis_hint_not_chitchat():
    assert is_chitchat("帮我分析一下订单") is False
