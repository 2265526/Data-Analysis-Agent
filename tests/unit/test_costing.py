"""阶段 2 成本与监控: 三表落库 + 成本预估 + 熔断时机 单元测试。"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.models import Base
from src.models.model_routes import ModelRoute


@pytest.fixture
def sqlite_db(monkeypatch):
    """共享连接的内存 sqlite, 替换 run_records 的 SessionLocal 以隔离测试。"""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    sm = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    monkeypatch.setattr("src.utils.run_records.SessionLocal", sm)
    return sm


# ---------- 落库: task_node_runs + cost_records ----------

def test_record_llm_run_writes_run_and_cost(sqlite_db):
    from src.models.cost_records import CostRecord
    from src.models.task_node_runs import TaskNodeRun
    from src.utils.run_records import record_llm_run

    run_id = record_llm_run("task-1", "planner", "deepseek-chat", 1000, 500)
    assert run_id is not None

    db = sqlite_db()
    try:
        run = db.get(TaskNodeRun, run_id)
        assert run.task_id == "task-1"
        assert run.node_name == "planner"
        assert run.model_name == "deepseek-chat"
        assert run.prompt_tokens == 1000
        assert run.completion_tokens == 500
        assert run.run_seq == 1

        cost = db.query(CostRecord).filter_by(run_id=run_id).one()
        assert cost.cost_type == "actual"
        # 兜底单价 0.001/0.002: 1000/1000*0.001 + 500/1000*0.002 = 0.002
        assert float(cost.cost_amount) == pytest.approx(0.002, abs=1e-6)
    finally:
        db.close()


def test_record_llm_run_run_seq_increments_on_retry(sqlite_db):
    from src.utils.run_records import record_llm_run

    record_llm_run("task-2", "coder", "deepseek-chat", 100, 50)
    record_llm_run("task-2", "coder", "deepseek-chat", 100, 50)
    record_llm_run("task-2", "reporter", "deepseek-chat", 100, 50)

    db = sqlite_db()
    try:
        from src.models.task_node_runs import TaskNodeRun

        seqs = sorted(r.run_seq for r in db.query(TaskNodeRun).filter_by(task_id="task-2", node_name="coder"))
        assert seqs == [1, 2]
        # 不同节点独立计数
        rep = db.query(TaskNodeRun).filter_by(task_id="task-2", node_name="reporter").one()
        assert rep.run_seq == 1
    finally:
        db.close()


def test_record_llm_run_without_task_id_is_noop(sqlite_db):
    from src.utils.run_records import record_llm_run

    assert record_llm_run("", "coder", "deepseek-chat", 10, 10) is None


def test_record_sandbox_run_and_estimate(sqlite_db):
    from src.models.cost_records import CostRecord
    from src.models.task_node_runs import TaskNodeRun
    from src.utils.run_records import record_estimate, record_sandbox_run

    run_id = record_sandbox_run("task-3", output_rows=42, duration_ms=1234, error_message=None)
    assert run_id is not None
    db = sqlite_db()
    try:
        run = db.get(TaskNodeRun, run_id)
        assert run.node_name == "executor"
        assert run.model_name == "sandbox"
        assert run.output_rows == 42
        assert run.duration_ms == 1234
    finally:
        db.close()

    record_estimate("task-3", "planner", "deepseek-chat", 2000, 700)
    db = sqlite_db()
    try:
        est = db.query(CostRecord).filter_by(task_id="task-3", cost_type="estimate").one()
        assert est.run_id is None
        # 2000/1000*0.001 + 700/1000*0.002 = 0.0034
        assert float(est.cost_amount) == pytest.approx(0.0034, abs=1e-6)
    finally:
        db.close()


# ---------- model_routes 单价 ----------

def test_get_price_prefers_model_routes(sqlite_db):
    from src.utils.run_records import get_price

    db = sqlite_db()
    try:
        db.add(ModelRoute(
            node="coder", model_name="deepseek-chat",
            price_per_1k_prompt=0.123, price_per_1k_completion=0.456,
            priority=1, enabled=True,
        ))
        db.commit()
    finally:
        db.close()

    prompt_p, completion_p = get_price("deepseek-chat")
    assert prompt_p == pytest.approx(0.123)
    assert completion_p == pytest.approx(0.456)


def test_get_price_falls_back_to_settings(sqlite_db):
    from src.utils.run_records import get_price

    prompt_p, completion_p = get_price("unknown-model")
    assert prompt_p == pytest.approx(0.001)
    assert completion_p == pytest.approx(0.002)


# ---------- 成本预估(熔断时机) ----------

def test_estimate_plan_cost_uses_default_tokens(monkeypatch):
    from src.utils import costing
    from src.utils.run_records import get_price

    monkeypatch.setattr(costing, "get_avg_tokens", lambda node, limit=50: (None, None))
    # 固定单价便于断言
    monkeypatch.setattr(costing, "get_price", lambda model: (0.001, 0.002))

    cost, prompt_t, completion_t = costing.estimate_plan_cost(
        [{"step": "a"}, {"step": "b"}], "查询近7天销量"
    )
    # planner: prompt=7*4+500=528, completion=200
    # exec: 2步 × (1500, 500)
    assert prompt_t == 528 + 2 * 1500
    assert completion_t == 200 + 2 * 500
    expected = 528 / 1000 * 0.001 + 200 / 1000 * 0.002 + 2 * 1500 / 1000 * 0.001 + 2 * 500 / 1000 * 0.002
    assert cost == pytest.approx(round(expected, 6), abs=1e-6)


def test_estimate_plan_cost_uses_historical_avg(monkeypatch):
    from src.utils import costing

    monkeypatch.setattr(costing, "get_avg_tokens", lambda node, limit=50: (3000.0, 800.0))
    monkeypatch.setattr(costing, "get_price", lambda model: (0.001, 0.002))

    cost, prompt_t, completion_t = costing.estimate_plan_cost([{"step": "a"}], "x")
    assert prompt_t == 504 + 3000  # len("x")=1 → 1*4+500=504
    assert completion_t == 200 + 800
    assert cost > 0


def test_should_escalate_approval():
    from src.utils.costing import should_escalate_approval
    from src.utils.settings import get_settings

    max_cost = get_settings().max_estimate_cost
    assert should_escalate_approval(max_cost + 1) is True
    assert should_escalate_approval(max_cost / 10) is False
