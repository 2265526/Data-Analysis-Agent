"""人机协同节点: 使用 LangGraph 官方 interrupt() 机制挂起, 等待人工审批后恢复。

机制:
- 节点执行到 interrupt(payload) 时, 图挂起并返回 __interrupt__ 给调用方
  (payload 含审批说明, 供前端展示)
- 审批接口通过 Command(resume={"approved": bool}) 恢复执行,
  interrupt() 调用返回该值, 节点据此决定继续(Reporter)或终止
- 依赖持久化 Checkpointer(PostgresSaver) 跨进程保存挂起状态

触发点: Executor 结果行数超阈值 / Coder 重试超限 / LLM 结构化输出连续失败
"""
from __future__ import annotations

from langgraph.types import interrupt

from src.state import PipelineState


def human_approval_node(state: PipelineState) -> dict:
    """挂起等待审批; resume 值 {"approved": bool} 决定继续/拒绝。"""
    decision = interrupt(
        {
            "question": "是否需要人工审批?",
            "task_id": state.get("task_id", "unknown"),
            "query": (state.get("user_query") or "")[:200],
            "reason": (state.get("error_log") or state.get("approval_reason") or "")[:500],
            "estimated_cost": state.get("estimated_cost"),
        }
    )
    approved = bool(decision and decision.get("approved"))
    if approved:
        # 标记"本任务已人工过审": 恢复后若重跑 SQL 再次命中敏感表/大结果集, 不再反复挂起
        return {
            "human_approval": True,
            "status": "running",
            "error_log": "",
            "retry_count": 0,
            "approval_passed": True,
        }
    return {"human_approval": False, "status": "failed", "error_log": "人工审批拒绝执行"}
