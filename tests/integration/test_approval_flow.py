"""审批流程集成测试: execute_task 挂起 -> resume_task 恢复(官方 interrupt 模式)。

- 周边节点(supervisor/planner/coder/executor/reporter)mock, human_approval 用真实节点
- SessionLocal 替换为 sqlite, get_checkpointer 替换为共享 InMemorySaver
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import src.graph as graph_mod
from src.models import AuditLog, Base, Task


@pytest.fixture
def app_env(monkeypatch, tmp_path):
    """构造隔离的测试环境: sqlite DB + 共享内存 checkpointer + mock 周边节点。"""
    engine = create_engine(f"sqlite:///{tmp_path}/approval.db")
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    saver = InMemorySaver()
    monkeypatch.setattr("src.api.deps.SessionLocal", TestSession)
    monkeypatch.setattr(graph_mod, "get_checkpointer", lambda: saver)

    # mock 周边节点, 让流水线确定性地走到 human_approval 挂起
    monkeypatch.setattr(graph_mod, "supervisor_node", lambda s: {"route": "planner"})
    monkeypatch.setattr(
        graph_mod,
        "planner_node",
        lambda s: {
            "plan": [{"description": "x", "dependencies": [], "required_tables": []}],
            "current_task_index": 0,
            "retry_count": 0,
            "error_log": "",
            "status": "running",
        },
    )
    monkeypatch.setattr(
        graph_mod, "coder_node", lambda s: {"code": "print(1)", "error_log": "", "retry_count": 1, "route": "executor"}
    )
    monkeypatch.setattr(
        graph_mod,
        "executor_node",
        lambda s: {"exec_result": "row", "error_log": "", "route": "human_approval", "status": "awaiting_approval"},
    )
    monkeypatch.setattr(
        graph_mod,
        "reporter_node",
        lambda s: {"final_report": "/static/reports/x.md", "status": "completed", "progress": "ok"},
    )
    return TestSession


def _create_task(session, task_id: str, query: str = "查询") -> Task:
    task = Task(id=task_id, user_query=query, status="pending", progress="")
    session.add(task)
    session.commit()
    return task


def test_execute_pauses_and_approve_continues(app_env) -> None:
    session = app_env()
    _create_task(session, "t-approve")

    # 第一次执行: 在 human_approval 挂起
    result = graph_mod.execute_task("t-approve")
    assert result["status"] == "awaiting_approval"

    db = app_env()
    task = db.get(Task, "t-approve")
    assert task.status == "awaiting_approval"
    assert task.current_node == "human_approval"
    db.close()

    # 审批通过: 图恢复执行到 Reporter, 任务完成
    result = graph_mod.resume_task("t-approve", approved=True, approver="张三", comment="同意")
    assert result["status"] == "completed"

    db = app_env()
    task = db.get(Task, "t-approve")
    assert task.status == "completed"
    assert task.result_path == "/static/reports/x.md"

    events = [a.event for a in db.query(AuditLog).filter(AuditLog.task_id == "t-approve").all()]
    assert "awaiting_approval" in events
    assert "approved" in events
    assert "pipeline_finished" in events
    db.close()


def test_execute_pauses_and_reject_fails(app_env) -> None:
    session = app_env()
    _create_task(session, "t-reject")

    result = graph_mod.execute_task("t-reject")
    assert result["status"] == "awaiting_approval"

    # 审批拒绝: 任务终止
    result = graph_mod.resume_task("t-reject", approved=False, approver="李四")
    assert result["status"] == "failed"

    db = app_env()
    task = db.get(Task, "t-reject")
    assert task.status == "failed"
    assert "拒绝" in (task.error_log or "")

    events = [a.event for a in db.query(AuditLog).filter(AuditLog.task_id == "t-reject").all()]
    assert "rejected" in events
    db.close()


def test_resume_requires_awaiting_status(app_env) -> None:
    session = app_env()
    _create_task(session, "t-new")
    session.query(Task).filter(Task.id == "t-new").update({"status": "pending"})
    session.commit()
    session.close()

    with pytest.raises(ValueError, match="not awaiting approval"):
        graph_mod.resume_task("t-new", approved=True)


def test_resolve_approval_timeout_rejects(app_env, monkeypatch) -> None:
    session = app_env()
    _create_task(session, "t-timeout")
    session.close()

    # 先真正执行到挂起, 再模拟超时(25 小时前更新)
    assert graph_mod.execute_task("t-timeout")["status"] == "awaiting_approval"
    db = app_env()
    db.query(Task).filter(Task.id == "t-timeout").update(
        {"updated_at": datetime.now(timezone.utc) - timedelta(hours=25)}
    )
    db.commit()
    db.close()

    monkeypatch.setattr("src.graph.settings.approval_timeout_action", "reject")
    handled = graph_mod.resolve_approval_timeouts()
    assert "t-timeout" in handled

    db = app_env()
    task = db.get(Task, "t-timeout")
    assert task.status == "failed"  # 超时按拒绝处理
    events = [a.event for a in db.query(AuditLog).filter(AuditLog.task_id == "t-timeout").all()]
    assert "approval_timeout" in events
    db.close()
