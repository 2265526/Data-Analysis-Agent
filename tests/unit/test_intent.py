"""意图解析单元测试: 验证"意图感知章节装配"的触发规则。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.utils.intent import parse_intent


def test_7d_compare_week_no_extension():
    """只要 7 天快照 + 上周环比: 不触发趋势/同比跨窗口扩展。"""
    it = parse_intent("统计最近7天各品类销售额,对比上周变化")
    assert it["time_window"] == "7d"
    assert it["baseline"] == "last_week"
    assert it["want_compare"] is True
    assert it["want_trend"] is False
    assert it["want_yoy"] is False
    assert it["strict_only"] is False


def test_plain_7d():
    it = parse_intent("统计最近7天各品类销售额")
    assert it["time_window"] == "7d"
    assert it["want_trend"] is False
    assert it["want_compare"] is False


def test_trend_keyword_triggers():
    it = parse_intent("分析近30天各品类销售趋势")
    assert it["time_window"] == "30d"
    assert it["want_trend"] is True


def test_yoy_triggers():
    it = parse_intent("统计各品类销售额,同比去年同期")
    assert it["baseline"] == "yoy"
    assert it["want_yoy"] is True


def test_strict_only_suppresses_extension():
    it = parse_intent("只要最近7天各品类的销售额,其他不用")
    assert it["strict_only"] is True
    assert it["want_trend"] is False
    assert it["want_yoy"] is False


def test_empty_query():
    it = parse_intent("")
    assert it["time_window"] is None
    assert it["want_trend"] is False
    assert it["want_yoy"] is False
