"""Clarifier 节点(开发流程 162 段 / OR-02): 需求澄清, 复用 interrupt() 挂起机制。

触发: Planner 拆解 confidence < 0.6 或缺少关键指标口径(留存率分母/时间窗口/维度定义)。
机制:
- 节点执行到 interrupt(payload) 时挂起, payload 携带候选问题清单
- 审批接口(复用 /approve)通过 Command(resume={"approved": bool, "clarify_answer": "..."})
  恢复; interrupt() 返回该值, 节点据此把用户回答回填到状态并重新路由 Planner 重拆
- 用户未回答(拒绝/空): 按"用户未补充口径"继续, 由 Planner 按默认假设重拆, 不阻塞流水线
"""
from __future__ import annotations

from langgraph.types import interrupt

from src.state import PipelineState


def clarifier_node(state: PipelineState) -> dict:
    """挂起提问; resume 值 {"approved": bool, "clarify_answer": str} 决定回填内容。"""
    questions = state.get("clarify_questions") or ["请补充关键指标口径(如时间窗口、口径定义、维度)"]
    decision = interrupt(
        {
            "kind": "clarify",
            "question": "需求存在歧义, 请补充关键指标口径:",
            "task_id": state.get("task_id", "unknown"),
            "query": (state.get("user_query") or "")[:200],
            "questions": questions[:5],
        }
    )
    approved = bool(decision and decision.get("approved"))
    answer = (decision or {}).get("clarify_answer") or ""
    if approved and answer:
        return {
            "route": "planner",  # 回 Planner 重拆(带澄清口径)
            "clarify_answer": answer.strip()[:500],
            "status": "running",
            "progress": "clarify_answered",
            "progress_detail": "已收到用户澄清, 重新拆解任务",
        }
    return {
        "route": "planner",
        "clarify_answer": "",
        "status": "running",
        "error_log": "",
        "progress": "clarify_skipped",
        "progress_detail": "用户未补充口径, 按默认假设继续",
    }
