"""上下文窗口管理单元测试: token 估算 / 结果集防塞入 / 筛选条件提取 / 预算裁剪 / 节点格式化。

不依赖 DB/LLM: 测纯函数与降级分支; 跨会话动态上下文在节点层有单独集成验证。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.utils.context_window import (
    _extract_filters_rules,
    _trim_to_budget,
    _truncate,
    build_context_raw,
    estimate_tokens,
    format_context,
    load_session_history,
)


# ---------- token 估算 ----------
def test_estimate_tokens_empty():
    assert estimate_tokens("") == 0
    assert estimate_tokens(None) == 0


def test_estimate_tokens_positive():
    assert estimate_tokens("销售额") > 0
    assert estimate_tokens("a" * 100) >= 1


# ---------- 结果集防塞入(截断) ----------
def test_truncate_short_unchanged():
    assert _truncate("abc", 10) == "abc"


def test_truncate_long_cut_with_marker():
    out = _truncate("x" * 100, 20)
    assert len(out) <= 20 + len("…(已截断)")
    assert "已截断" in out


# ---------- 预算裁剪 ----------
def test_trim_to_budget_under_stays():
    assert _trim_to_budget("hello", 100) == "hello"


def test_trim_to_budget_over_cuts():
    long_text = "字" * 5000
    out = _trim_to_budget(long_text, 100)  # 100 tokens ≈ 166 字符
    assert len(out) < 5000
    assert out.startswith("字")


def test_trim_to_budget_zero_returns_empty():
    assert _trim_to_budget("hello", 0) == ""


# ---------- 规则提取累积筛选条件 ----------
def test_extract_filters_rules():
    texts = [
        "统计华南区各品类销售额",
        "只看已完成的订单",
        "最近7天的数据",
    ]
    filters = _extract_filters_rules(texts)
    joined = " ".join(filters)
    assert "华南" in joined
    assert "已完成" in joined
    assert "近7天" in joined or "最近7天" in joined


def test_extract_filters_rules_empty():
    assert _extract_filters_rules(["随便聊聊", "天气不错"]) == []


# ---------- 历史加载降级 ----------
def test_load_session_history_no_session():
    # session_id=0/None 直接返回空, 不碰 DB
    assert load_session_history(0) == []
    assert load_session_history(None) == []


# ---------- 入口构建降级 ----------
def test_build_context_raw_no_session():
    assert build_context_raw(None, current_query="x") == {}


def test_build_context_raw_disabled(monkeypatch):
    from src.utils.settings import get_settings

    monkeypatch.setattr(get_settings(), "context_window_enabled", False)
    # 即使给 session_id, 关闭开关后也不构建(避免在单测里访问 DB)
    assert build_context_raw(123, current_query="x") == {}
    monkeypatch.setattr(get_settings(), "context_window_enabled", True)


# ---------- 节点格式化 ----------
def test_format_context_empty_raw():
    assert format_context(None, node="planner") == ""
    assert format_context({}, node="coder") == ""


def test_format_context_assembles_layers():
    raw = {
        "recent_user": ["统计各品类销售额"],
        "recent_assistant": ["上周销售额 1000 万"],
        "filters": ["地域=华南"],
        "summary": "更早: 分析过华东区",
    }
    out = format_context(raw, node="planner")
    assert "地域=华南" in out
    assert "统计各品类销售额" in out
    assert "更早" in out


def test_format_context_coder_includes_previous_conclusion():
    raw = {
        "recent_user": ["再按华南区筛选"],
        "recent_assistant": ["华东区销售额 888 万"],
        "filters": [],
        "summary": "",
    }
    out_coder = format_context(raw, node="coder")
    out_planner = format_context(raw, node="planner")
    assert "华东区销售额 888 万" in out_coder       # coder 带上轮结论
    assert "华东区销售额 888 万" not in out_planner  # planner 不带


def test_format_context_respects_budget(monkeypatch):
    from src.utils.settings import get_settings

    monkeypatch.setattr(get_settings(), "context_budget_planner_tokens", 50)
    raw = {
        "recent_user": ["非常长的用户消息内容" * 100],
        "recent_assistant": [],
        "filters": [],
        "summary": "",
    }
    out = format_context(raw, node="planner")
    assert estimate_tokens(out) <= 50 + 1  # 裁剪到预算内
