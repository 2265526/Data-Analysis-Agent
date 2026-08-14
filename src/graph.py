"""LangGraph 状态图编排与 Checkpointer 初始化。

流水线: START -> supervisor -> planner -> coder -> executor -> (human_approval) -> reporter -> END
- 自修复循环: executor 失败 -> coder 依据错误信息重写(最多 MAX_RETRY 次)
- 逻辑错误: coder 路由回 planner 重新规划
- 人机协同: 大结果集/重试超限 -> human_approval 节点调用 interrupt() 挂起,
  审批接口通过 Command(resume=...) 恢复(官方 HITL 模式, 依赖持久化 Checkpointer)
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from src.nodes import (
    clarifier_node,
    coder_node,
    executor_node,
    human_approval_node,
    planner_node,
    reporter_node,
    supervisor_node,
)
from src.state import PipelineState
from src.utils.logger import bind_run_context, get_logger
from src.utils.metrics import metrics
from src.utils.notifier import get_notifier
from src.utils.settings import get_settings

logger = get_logger(__name__)
settings = get_settings()


def is_task_canceled(task_id: str) -> bool:
    """OR-08: 检查 Redis 取消标志 cancel:{task_id} 是否存在。

    Redis 不可用时返回 False(取消检查是旁路, 不能因它阻断任务)。
    """
    try:
        from src.api.deps import get_redis

        redis = next(get_redis())
        return redis.get(f"cancel:{task_id}") is not None
    except Exception as exc:  # noqa: BLE001
        logger.warning("cancel_check_failed", task_id=task_id, error=str(exc))
        return False


def _persist_node_progress(task, node_name: str, state: dict, db) -> None:
    """节点执行完后, 把实时进度写回 tasks 表(前端轮询实时展示进度条)。

    state 为 LangGraph 节点输出(PipelineState 片段), 含 progress/progress_detail/
    progress_percent/status/route; 只更新非空字段, 避免覆盖已有值。
    """
    task.current_node = state.get("route") or node_name
    if state.get("status"):
        task.status = state["status"]
    if state.get("progress"):
        task.progress = state["progress"]
    if state.get("progress_detail"):
        task.progress_detail = str(state["progress_detail"])[:500]
    if state.get("progress_percent") is not None:
        task.progress_percent = int(state["progress_percent"])
    db.commit()


# ---------------------------------------------------------------------------
# 条件路由函数: 节点通过返回 {"route": ...} 驱动分支
# ---------------------------------------------------------------------------
def _route_after_supervisor(state: PipelineState) -> str:
    return state.get("route", "planner")


def _route_after_planner(state: PipelineState):
    """planner 路由: 缺口径->clarifier(澄清挂起) / 成本超限->human_approval / 多步无依赖->Send 并行 / 单步->coder"""
    if state.get("route") == "clarifier":
        return "clarifier"
    if state.get("status") == "awaiting_approval":
        return "human_approval"
    steps = state.get("plan") or []
    if len(steps) > 1:
        # OR-03: 无依赖步骤 Send 并发派发(每步一个子任务, sub_task_id 防乱序)
        from langgraph.types import Send

        return [
            Send(
                "step_exec",
                {
                    "task_id": state.get("task_id", ""),
                    "user_query": state.get("user_query", ""),
                    "plan_step": step,
                    "sub_task_id": i,
                    # 多轮上下文透传: 并行子任务的 coder 也要能读到累积筛选条件/上文
                    "session_id": state.get("session_id"),
                    "conversation_context": state.get("conversation_context", {}),
                },
            )
            for i, step in enumerate(steps)
        ]
    return "coder"


# ---------------------------------------------------------------------------
# OR-03 并行执行(Send API): 无依赖步骤并发派发, sub_task_id 防乱序
# ---------------------------------------------------------------------------
def _step_exec(state: PipelineState) -> dict:
    """并行子任务: 单个 plan 步骤 = coder 生成 -> executor 执行。

    - 子任务不重复成本预估/澄清/审批挂起; executor 触发审批条件时标记 needs_approval,
      由 aggregate 汇总后统一走一次人工审批
    - 并行场景不做多轮自修复(失败即记录, 由汇总结果呈现)
    """
    sub_state: PipelineState = {
        "user_query": state.get("user_query", ""),
        "task_id": state.get("task_id", ""),
        "plan": [state.get("plan_step")],
        "current_task_index": 0,
        "error_log": state.get("error_log", ""),
        "retry_count": 0,
        "status": "running",
        "session_id": state.get("session_id"),
        "conversation_context": state.get("conversation_context", {}),
    }
    sub_task_id = state.get("sub_task_id", 0)

    coder_out = coder_node(sub_state)
    if coder_out.get("route") in ("coder", "planner", "human_approval") or coder_out.get("error_log"):
        return {
            "sub_results": [{
                "sub_task_id": sub_task_id,
                "needs_approval": False,
                "error": coder_out.get("error_log") or "代码生成失败",
                "result": "",
            }]
        }
    sub_state.update(coder_out)

    exec_out = executor_node(sub_state)
    if exec_out.get("status") == "awaiting_approval":
        # 大结果集/敏感表: 不挂起, 标记后由 aggregate 统一审批
        return {
            "sub_results": [{
                "sub_task_id": sub_task_id,
                "needs_approval": True,
                "reason": exec_out.get("progress_detail") or "需人工审批",
                "result": exec_out.get("exec_result", ""),
                "error": "",
            }]
        }
    if exec_out.get("route") == "coder" or exec_out.get("error_log"):
        return {
            "sub_results": [{
                "sub_task_id": sub_task_id,
                "needs_approval": False,
                "error": exec_out.get("error_log") or "执行失败",
                "result": "",
            }]
        }
    return {
        "sub_results": [{
            "sub_task_id": sub_task_id,
            "needs_approval": False,
            "error": "",
            "result": exec_out.get("exec_result", ""),
        }]
    }


def _aggregate(state: PipelineState) -> dict:
    """收集并行子结果: 按 sub_task_id 排序合并; 存在需审批步骤则统一挂起。"""
    subs = sorted(state.get("sub_results") or [], key=lambda s: s.get("sub_task_id", 0))
    if not subs:
        return {"route": "reporter", "status": "running", "exec_result": ""}

    needs = [s for s in subs if s.get("needs_approval")]
    if needs:
        reasons = "; ".join(s.get("reason", "需人工审批") for s in needs)[:500]
        return {
            "route": "human_approval",
            "status": "awaiting_approval",
            "error_log": f"并行执行部分步骤需人工审批: {reasons}",
            "progress": "awaiting_approval",
            "progress_detail": reasons,
            "progress_percent": 70,
        }

    parts = []
    for s in subs:
        idx = s.get("sub_task_id", 0)
        if s.get("error"):
            parts.append(f"[步骤{idx + 1} 失败] {s['error']}")
        else:
            parts.append(f"[步骤{idx + 1}]\n{s.get('result', '')}")
    merged = "\n".join(parts)
    return {
        "exec_result": merged,
        "route": "reporter",
        "status": "running",
        "progress": "parallel_finished",
        "progress_detail": f"并行执行 {len(subs)} 个步骤完成",
        "progress_percent": 80,
    }


def _route_after_aggregate(state: PipelineState) -> str:
    return state.get("route", "reporter")


def _route_after_coder(state: PipelineState) -> str:
    """coder 可能路由: executor(默认) / planner(逻辑错误重规划) / failed(重试超限, 直接失败)"""
    return state.get("route", "executor")


def _route_after_executor(state: PipelineState) -> str:
    """executor 可能路由: reporter(默认) / human_approval(大结果集)"""
    return state.get("route", "reporter")


def _route_after_approval(state: PipelineState) -> str:
    """审批通过后的路由:
    - 拒绝 -> END(终止)
    - 已产生执行结果(executor 后审批: 大结果集/敏感表)-> reporter 出报告
    - 执行前审批(成本超限/coder 重试超限, exec_result 为空且 code 为空)
      -> coder 重新生成代码并继续执行, 避免产出空报告
    """
    if not state.get("human_approval"):
        return "finish"
    if state.get("exec_result") or state.get("code"):
        return "reporter"
    if state.get("plan"):
        return "coder"
    return "reporter"  # 兜底: 无计划也进报告(报告会标注无数据)


# ---------------------------------------------------------------------------
# Checkpointer: 断点恢复(生产 Postgres / 开发 Sqlite / 兜底 Memory)
# ---------------------------------------------------------------------------
def get_checkpointer():
    """初始化 Checkpointer。

    - 生产默认 postgres(状态快照落库, interrupt 恢复依赖它跨进程共享)
    - 依赖缺失时降级 InMemorySaver, 但 interrupt 挂起后无法跨进程恢复
    """
    backend = settings.checkpointer_backend
    try:
        if backend == "postgres":
            from langgraph.checkpoint.postgres import PostgresSaver
            from psycopg import Connection

            # setup() 会执行 CREATE INDEX CONCURRENTLY, 必须 autocommit 连接
            conn = Connection.connect(settings.database_url, autocommit=True)
            checkpointer = PostgresSaver(conn)
            checkpointer.setup()  # 创建检查点表
            return checkpointer
        if backend == "sqlite":
            from langgraph.checkpoint.sqlite import SqliteSaver

            return SqliteSaver.from_conn_string(str(settings.project_root / "checkpoints.sqlite"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("checkpointer_unavailable", backend=backend, error=str(exc))

    from langgraph.checkpoint.memory import InMemorySaver

    logger.info("checkpointer_fallback_memory")
    return InMemorySaver()


# ---------------------------------------------------------------------------
# 状态图构建
# ---------------------------------------------------------------------------
def build_graph(checkpointer=None) -> StateGraph:
    """组装状态图。human_approval 节点内部用 interrupt() 挂起, 无需 interrupt_before。"""
    builder = StateGraph(PipelineState)

    builder.add_node("supervisor", supervisor_node)
    builder.add_node("planner", planner_node)
    builder.add_node("clarifier", clarifier_node)
    builder.add_node("coder", coder_node)
    builder.add_node("executor", executor_node)
    builder.add_node("human_approval", human_approval_node)
    builder.add_node("reporter", reporter_node)

    # 入口
    builder.add_edge(START, "supervisor")

    # Supervisor 动态路由
    builder.add_conditional_edges(
        "supervisor",
        _route_after_supervisor,
        {"planner": "planner", "reporter": "reporter", "FINISH": END},
    )

    # 拆解后进入编码; 成本预估超限转人工审批(熔断时机), 需求缺口径转 Clarifier 澄清(OR-02),
    # 多步骤且无依赖时 Send API 并行派发(OR-03), 单步骤走串行
    builder.add_conditional_edges("planner", _route_after_planner)

    # Clarifier 澄清后回 Planner 重拆(带用户补充的口径)
    builder.add_edge("clarifier", "planner")

    # 并行子任务(OR-03): 各 Send 实例执行后汇聚到 aggregate
    builder.add_node("step_exec", _step_exec)
    builder.add_node("aggregate", _aggregate)
    builder.add_edge("step_exec", "aggregate")
    builder.add_conditional_edges(
        "aggregate",
        _route_after_aggregate,
        {"reporter": "reporter", "human_approval": "human_approval"},
    )

    # Coder -> Executor / Planner(逻辑错误) / Failed(重试超限, 直接失败) / Coder(LLM 失败自循环重试)
    builder.add_conditional_edges(
        "coder",
        _route_after_coder,
        {"executor": "executor", "planner": "planner", "failed": END, "coder": "coder"},
    )

    # Executor -> Reporter / HumanApproval(大结果集) / Coder(执行失败自修复重写)
    builder.add_conditional_edges(
        "executor",
        _route_after_executor,
        {"reporter": "reporter", "human_approval": "human_approval", "coder": "coder"},
    )

    # HumanApproval -> Reporter / Coder(执行前审批恢复后继续执行) / END
    builder.add_conditional_edges(
        "human_approval",
        _route_after_approval,
        {"reporter": "reporter", "coder": "coder", "finish": END},
    )

    # 汇总产出报告
    builder.add_edge("reporter", END)

    return builder.compile(checkpointer=checkpointer)


# ---------------------------------------------------------------------------
# 任务执行 / 审批恢复 / 超时处理
# ---------------------------------------------------------------------------
def _make_config(task_id: str) -> Dict[str, Any]:
    """LangGraph 运行配置: thread_id 即 task_id, 检查点按任务隔离。"""
    return {"configurable": {"thread_id": task_id}}


def _write_chat_reply_md(task, reply: str) -> str | None:
    """闲聊回复写入报告产物 md, 返回相对路径(供前端渲染/下载); 失败返回 None。"""
    try:
        from datetime import datetime
        from pathlib import Path

        today = datetime.now().strftime("%Y/%m/%d")
        out_dir = settings.reports_dir / today
        out_dir.mkdir(parents=True, exist_ok=True)
        md_file = out_dir / f"{task.id}.md"
        md_file.write_text(f"# 助手回复\n\n{reply}\n", encoding="utf-8")
        logger.info("chat_reply_saved", task_id=task.id)
        return f"/static/reports/{today}/{task.id}.md"
    except Exception as exc:  # noqa: BLE001
        logger.warning("chat_reply_save_failed", task_id=task.id, error=str(exc)[:200])
        return None


def _finalize_task(task, final_state: PipelineState, db) -> None:
    """回写任务最终状态 + 监控指标 + 完成审计(execute_task 与 resume_task 共用)。"""
    from src.models import AuditLog

    # 进度回写(节点级进度事件, BR-06)
    task.current_node = final_state.get("route") or task.current_node
    task.progress = final_state.get("progress") or task.progress
    task.progress_detail = (final_state.get("progress_detail") or "")[:500]
    task.progress_percent = final_state.get("progress_percent") or task.progress_percent

    final_status = final_state.get("status", "completed")
    if final_status == "completed":
        task.status = "completed"
        task.progress = "任务完成"
        task.result_path = final_state.get("final_report")
        task.summary = (final_state.get("exec_result") or "")[:500]
        # 闲聊等无分析产物的完成: 把回复写入报告产物, 前端可正常渲染/下载
        if not task.result_path:
            reply = final_state.get("chat_reply")
            if reply:
                task.result_path = _write_chat_reply_md(task, reply)
    else:
        task.status = "failed"
        task.error_log = final_state.get("error_log") or "流水线执行失败"

    # 监控指标: 任务结果 / 自修复成功率 / 重试次数分布(优化方案指标1/2)
    retry_count = final_state.get("retry_count", 0) or 0
    metrics.inc("task_executed_total", labels={"status": final_status})
    metrics.observe("task_retry_count", retry_count, buckets=(0, 1, 2, 3, 5))
    if final_status == "completed":
        if retry_count > 0:
            metrics.inc("self_heal_successes_total")
    elif final_state.get("error_log"):
        metrics.inc("self_heal_failures_total")

    db.add(AuditLog(task_id=task.id, event="pipeline_finished", node_name="graph", detail={"status": task.status}))
    db.commit()


def execute_task(task_id: str) -> Dict[str, Any]:
    """执行一次完整流水线; 若在 human_approval 挂起, 标记 awaiting_approval 并返回。

    审批通过后由 resume_task 从挂起点继续, 不再重复执行。
    """
    bind_run_context(run_id=task_id)

    from src.api.deps import SessionLocal
    from src.models import AuditLog, Task

    db = SessionLocal()
    try:
        task = db.get(Task, task_id)
        if task is None:
            raise ValueError(f"task not found: {task_id}")

        # 幂等性: 已完成任务不重复执行
        if task.status == "completed":
            logger.info("task_already_completed", task_id=task_id)
            return {"task_id": task_id, "status": "completed"}

        # OR-08 取消检查: 提交后执行前被取消则直接结束
        if is_task_canceled(task_id):
            task.status = "canceled"
            task.progress = "任务已取消"
            db.add(AuditLog(task_id=task_id, event="task_canceled", node_name="graph"))
            db.commit()
            return {"task_id": task_id, "status": "canceled"}

        task.status = "running"
        task.progress = "流水线启动: supervisor"
        task.current_node = "supervisor"
        db.add(AuditLog(task_id=task_id, event="pipeline_started", node_name="graph"))
        db.commit()

        graph = build_graph(checkpointer=get_checkpointer())

        # 多轮上下文: 有会话关联时, 在入口构建一次(读历史+分层+提取), 各节点按预算消费
        session_id = getattr(task, "session_id", None)
        from src.utils.context_window import build_context_raw

        conversation_context = build_context_raw(
            session_id, task.user_query, task_id=task_id
        )

        initial_state: PipelineState = {
            "user_query": task.user_query,
            "task_id": task_id,
            "session_id": session_id,
            "conversation_context": conversation_context,
            "status": "running",
            "human_approval": False,
            "route": "",
            "actor": task.created_by or "",
            "data_source_id": getattr(task, "data_source_id", None),
            # 定时任务触发的分析自动放行敏感表/大结果集审批(创建定时任务本身已含人工确认意图)
            "auto_approve": getattr(task, "source", "manual") == "scheduled",
        }

        # 用 stream 迭代执行: 每个节点结束后把进度/百分比实时写回 task 表,
        # 否则 graph.invoke 同步阻塞期间前端轮询看不到任何进度变化(进度条一直 5%)
        result: dict = {}
        try:
            for chunk in graph.stream(initial_state, config=_make_config(task_id)):
                for node_name, node_state in chunk.items():
                    if node_name == "__interrupt__":
                        result.setdefault("__interrupt__", node_state)
                        continue
                    if isinstance(node_state, dict):
                        _persist_node_progress(task, node_name, node_state, db)
                        result.update(node_state)
        finally:
            db.commit()

        # 挂起: human_approval 节点 interrupt(), 等待审批
        if "__interrupt__" in result:
            task.status = "awaiting_approval"
            task.progress = "等待人工审批"
            task.current_node = "human_approval"
            db.add(AuditLog(task_id=task_id, event="awaiting_approval", node_name="human_approval"))
            db.commit()
            # 发送审批通知(默认 console 打日志; 企业微信/钉钉为预留接口)
            get_notifier().send_approval(task_id=task_id, query=task.user_query[:200])
            return {"task_id": task_id, "status": "awaiting_approval"}

        # OR-08 取消检查: 执行中被取消则不落 completed
        if is_task_canceled(task_id):
            task.status = "canceled"
            task.progress = "任务已取消"
            db.add(AuditLog(task_id=task_id, event="task_canceled", node_name="graph"))
            db.commit()
            return {"task_id": task_id, "status": "canceled"}

        _finalize_task(task, result, db)
        logger.info("task_finished", task_id=task_id, status=task.status)
        return {"task_id": task_id, "status": task.status}

    except Exception as exc:  # noqa: BLE001
        logger.error("task_execution_failed", task_id=task_id, error=str(exc))
        task = db.get(Task, task_id)
        if task is not None:
            task.status = "failed"
            task.error_log = str(exc)[:2000]
            db.commit()
        raise
    finally:
        db.close()


def resume_task(
    task_id: str,
    approved: bool,
    approver: str = "",
    comment: str = "",
    client_ip: str = "",
    user_agent: str = "",
) -> Dict[str, Any]:
    """审批恢复: 通过 Command(resume={"approved": ...}) 从挂起点继续执行流水线。"""
    bind_run_context(run_id=task_id)

    from src.api.deps import SessionLocal
    from src.models import AuditLog, Task

    db = SessionLocal()
    try:
        task = db.get(Task, task_id)
        if task is None:
            raise ValueError(f"task not found: {task_id}")
        if task.status != "awaiting_approval":
            raise ValueError(f"task not awaiting approval, current={task.status}")

        # OR-08 取消检查: 挂起期间被取消则不恢复执行
        if is_task_canceled(task_id):
            task.status = "canceled"
            task.progress = "任务已取消"
            db.add(AuditLog(task_id=task_id, event="task_canceled", node_name="graph"))
            db.commit()
            return {"task_id": task_id, "status": "canceled"}

        state_before = {"status": task.status, "current_node": task.current_node}
        task.status = "running"
        task.progress = "审批通过, 流水线恢复执行" if approved else "审批拒绝, 任务终止"
        task.current_node = "human_approval"
        db.commit()

        graph = build_graph(checkpointer=get_checkpointer())
        result: dict = {}
        try:
            for chunk in graph.stream(
                Command(resume={"approved": approved, "clarify_answer": comment}),
                config=_make_config(task_id),
            ):
                for node_name, node_state in chunk.items():
                    if node_name == "__interrupt__":
                        result.setdefault("__interrupt__", node_state)
                        continue
                    if isinstance(node_state, dict):
                        _persist_node_progress(task, node_name, node_state, db)
                        result.update(node_state)
        finally:
            db.commit()

        # 防御: 理论上审批节点只挂起一次; 若再次挂起则维持等待
        if "__interrupt__" in result:
            task.status = "awaiting_approval"
            task.progress = "等待人工审批"
            db.commit()
            return {"task_id": task_id, "status": "awaiting_approval"}

        _finalize_task(task, result, db)
        db.add(
            AuditLog(
                task_id=task_id,
                event="approved" if approved else "rejected",
                actor=approver or "system",
                node_name="human_approval",
                client_ip=client_ip or None,
                user_agent=user_agent or None,
                state_before=state_before,
                state_after={"status": task.status, "current_node": task.current_node},
                detail={"comment": comment},
            )
        )
        db.commit()
        logger.info("task_resumed", task_id=task_id, approved=approved, status=task.status)
        return {"task_id": task_id, "status": task.status, "approved": approved}

    except Exception as exc:  # noqa: BLE001
        logger.error("task_resume_failed", task_id=task_id, error=str(exc))
        raise
    finally:
        db.close()


def resolve_approval_timeouts() -> list[str]:
    """扫描超时未审批的任务, 按 approval_timeout_action 自动处理(供 Celery Beat 定时调用)。

    - reject   : 超时按拒绝处理, 任务终止
    - continue : 超时按继续处理, 恢复执行
    - 超时事件写入审计日志(event=approval_timeout)
    """
    from src.api.deps import SessionLocal
    from src.models import AuditLog, Task

    db = SessionLocal()
    handled: list[str] = []
    try:
        threshold = datetime.now(timezone.utc) - timedelta(hours=settings.approval_timeout_hours)
        rows = (
            db.query(Task)
            .filter(Task.status == "awaiting_approval", Task.updated_at < threshold)
            .all()
        )
        for task in rows:
            approved = settings.approval_timeout_action == "continue"
            db.commit()  # 释放会话后交给 resume_task 独立处理
            try:
                resume_task(task.id, approved=approved, approver="system-timeout")
                db.add(
                    AuditLog(
                        task_id=task.id,
                        event="approval_timeout",
                        actor="system",
                        node_name="human_approval",
                        detail={"action": settings.approval_timeout_action},
                    )
                )
                db.commit()
                handled.append(task.id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("approval_timeout_resolve_failed", task_id=task.id, error=str(exc))
    finally:
        db.close()
    return handled
