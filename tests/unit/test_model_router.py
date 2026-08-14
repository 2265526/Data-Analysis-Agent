"""开发流程 2.2 模型分级选型: ModelRouter 表驱动 + 主备切换 + 服务商路由 单元测试。"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.models import Base
from src.models.model_routes import ModelRoute
from src.utils.settings import get_settings

settings = get_settings()


@pytest.fixture
def router_db(monkeypatch):
    """sqlite 隔离 model_routes + 重置 ModelRouter 单例。"""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    sm = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    monkeypatch.setattr("src.utils.model_router.SessionLocal", sm)
    monkeypatch.setattr("src.utils.model_router._router", None)

    from src.utils.model_router import get_model_router

    router = get_model_router()
    yield router, sm
    router._state.clear()


def _add_route(sm, node, model, priority=1, enabled=True):
    db = sm()
    try:
        db.add(ModelRoute(
            node=node, model_name=model,
            price_per_1k_prompt=0.001, price_per_1k_completion=0.002,
            priority=priority, enabled=enabled,
        ))
        db.commit()
    finally:
        db.close()


def test_resolve_from_table(router_db):
    router, sm = router_db
    _add_route(sm, "supervisor", "qwen-flash")
    assert router.resolve("supervisor", "deepseek-chat") == "qwen-flash"


def test_resolve_fallback_when_no_route(router_db):
    router, _ = router_db
    assert router.resolve("unknown-node", "deepseek-chat") == "deepseek-chat"


def test_resolve_disabled_route_ignored(router_db):
    router, sm = router_db
    _add_route(sm, "coder", "qwen-flash", enabled=False)
    assert router.resolve("coder", "deepseek-chat") == "deepseek-chat"


def test_switch_and_rollback(router_db, monkeypatch):
    router, sm = router_db
    _add_route(sm, "coder", "deepseek-chat", priority=1)
    _add_route(sm, "coder", "qwen-flash", priority=2)
    fake_breaker = type("Fake", (), {"state": "closed"})()
    monkeypatch.setattr("src.nodes._llm_breaker", fake_breaker)

    assert router.resolve("coder", "x") == "deepseek-chat"
    # 连续 2 次失败 -> 切换备用模型
    router.record_failure("coder")
    router.record_failure("coder")
    assert router.resolve("coder", "x") == "qwen-flash"
    assert not router.is_degraded("coder")
    # 备用模型连续 2 次失败 -> 回滚主模型并转降级
    router.record_failure("coder")
    router.record_failure("coder")
    assert router.resolve("coder", "x") == "deepseek-chat"
    assert router.is_degraded("coder")


def test_no_switch_when_breaker_open(router_db, monkeypatch):
    router, sm = router_db
    _add_route(sm, "coder", "deepseek-chat", priority=1)
    _add_route(sm, "coder", "qwen-flash", priority=2)
    fake_breaker = type("Fake", (), {"state": "open"})()
    monkeypatch.setattr("src.nodes._llm_breaker", fake_breaker)

    router.record_failure("coder")
    router.record_failure("coder")
    # 熔断打开期间不做切换尝试
    assert router.resolve("coder", "x") == "deepseek-chat"


def test_record_success_resets_failures(router_db, monkeypatch):
    router, sm = router_db
    _add_route(sm, "coder", "deepseek-chat", priority=1)
    _add_route(sm, "coder", "qwen-flash", priority=2)
    monkeypatch.setattr("src.nodes._llm_breaker", type("F", (), {"state": "closed"})())

    router.record_failure("coder")
    router.record_success("coder")
    router.record_failure("coder")
    # 只有 1 次连续失败 -> 不切换
    assert router.resolve("coder", "x") == "deepseek-chat"


def test_resolve_llm_config_service_routing():
    from src.nodes import _resolve_llm_config

    qwen_cfg = _resolve_llm_config("qwen-flash")
    assert qwen_cfg["base_url"] == settings.dashscope_base_url
    assert qwen_cfg["api_key"] == settings.dashscope_api_key

    deepseek_cfg = _resolve_llm_config("deepseek-chat")
    assert deepseek_cfg["base_url"] == settings.deepseek_base_url
    assert deepseek_cfg["api_key"] == settings.deepseek_api_key


def test_make_llm_routes_node_to_service(router_db):
    """表驱动: supervisor -> qwen-flash(百炼端点), coder -> deepseek(官方端点)。"""
    from src.nodes import make_llm

    router, sm = router_db
    _add_route(sm, "supervisor", "qwen-flash")
    _add_route(sm, "coder", "deepseek-chat")

    supervisor_llm = make_llm("deepseek-chat", node="supervisor")
    assert "dashscope" in supervisor_llm._chat.openai_api_base
    assert supervisor_llm._chat.model_name == "qwen-flash"

    coder_llm = make_llm("deepseek-chat", node="coder")
    assert "deepseek.com" in coder_llm._chat.openai_api_base
    assert coder_llm._chat.model_name == "deepseek-chat"
