"""LangGraph 官方 interrupt 模式测试: 机制 + human_approval 节点逻辑。"""
from __future__ import annotations

import importlib.util
from typing import TypedDict
from unittest.mock import patch

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt


class _S(TypedDict, total=False):
    v: int
    approved: bool


def _wait_node(state: _S) -> dict:
    decision = interrupt({"question": "审批?", "task_id": "t1"})
    return {"approved": bool(decision.get("approved")), "v": state.get("v", 0) + 1}


def _tail_node(state: _S) -> dict:
    return {"v": state.get("v", 0) + 100}


def _build_app(saver=None):
    g = StateGraph(_S)
    g.add_node("wait", _wait_node)
    g.add_node("tail", _tail_node)
    g.add_edge(START, "wait")
    g.add_edge("wait", "tail")
    g.add_edge("tail", END)
    return g.compile(checkpointer=saver or InMemorySaver())


def test_interrupt_pauses_and_resumes() -> None:
    app = _build_app()
    cfg = {"configurable": {"thread_id": "t1"}}

    # 挂起: 返回 __interrupt__, payload 可读
    r1 = app.invoke({"v": 1}, cfg)
    assert "__interrupt__" in r1
    assert r1["__interrupt__"][0].value == {"question": "审批?", "task_id": "t1"}

    # 恢复: Command(resume=...) 的值作为 interrupt() 返回值
    r2 = app.invoke(Command(resume={"approved": True}), cfg)
    assert r2["v"] == 102
    assert r2["approved"] is True


def test_interrupt_state_persisted_in_checkpointer() -> None:
    saver = InMemorySaver()
    app = _build_app(saver)
    cfg = {"configurable": {"thread_id": "t2"}}
    r1 = app.invoke({"v": 5}, cfg)
    assert "__interrupt__" in r1
    # 用同一 checkpointer 的新图实例也能恢复(验证状态在检查点中)
    app2 = _build_app(saver)
    r2 = app2.invoke(Command(resume={"approved": False}), cfg)
    assert r2["v"] == 106
    assert r2["approved"] is False


# ---------------- human_approval_node 逻辑 ----------------
def _load_human_approval_module():
    """直接加载节点模块(绕过 src.nodes 包 __init__ 的完整依赖链)。"""
    spec = importlib.util.spec_from_file_location(
        "ha_node_test", "src/nodes/human_approval.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_human_approval_approved() -> None:
    mod = _load_human_approval_module()
    with patch.object(mod, "interrupt", return_value={"approved": True}):
        out = mod.human_approval_node({"task_id": "t1", "user_query": "q", "error_log": ""})
    # 审批通过: 重置重试计数(执行前审批恢复后回 coder 继续执行, 不再因重试已满再次挂起)
    assert out["human_approval"] is True
    assert out["status"] == "running"
    assert out["error_log"] == ""
    assert out["retry_count"] == 0
    assert out["approval_passed"] is True  # 已人工过审标记: 重跑不再反复挂起


def test_human_approval_rejected() -> None:
    mod = _load_human_approval_module()
    with patch.object(mod, "interrupt", return_value={"approved": False}):
        out = mod.human_approval_node({"task_id": "t1"})
    assert out == {"human_approval": False, "status": "failed", "error_log": "人工审批拒绝执行"}


def test_human_approval_payload_contains_context() -> None:
    mod = _load_human_approval_module()
    captured = {}

    def _fake_interrupt(payload):
        captured["payload"] = payload
        return {"approved": True}

    with patch.object(mod, "interrupt", side_effect=_fake_interrupt):
        mod.human_approval_node({"task_id": "abc", "user_query": "计算留存率", "error_log": "err"})
    assert captured["payload"]["task_id"] == "abc"
    assert captured["payload"]["query"] == "计算留存率"
    assert captured["payload"]["reason"] == "err"
