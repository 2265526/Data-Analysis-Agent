"""节点运行与成本落库: task_node_runs / cost_records / model_routes 的读写封装。

设计:
- 独立短会话(SessionLocal), 调用方无 DB 上下文也能落库(节点/图执行层直接用)
- 任何写库失败只记日志, 绝不抛异常影响主流程(监控/成本是旁路)
- 单价优先取 model_routes(enabled=true), 无配置时回退 settings 兜底单价
"""
from __future__ import annotations

from typing import Optional, Tuple

from sqlalchemy import func, select

from src.api.deps import SessionLocal
from src.models.cost_records import CostRecord
from src.models.model_routes import ModelRoute
from src.models.task_node_runs import TaskNodeRun
from src.utils.logger import get_logger
from src.utils.settings import get_settings

logger = get_logger(__name__)
settings = get_settings()


def get_price(model: str) -> Tuple[float, float]:
    """返回 (prompt单价, completion单价), 单位: 元/1k tokens。

    优先查 model_routes(按 model_name 精确匹配, enabled=true, priority 最小);
    无记录时回退 settings 默认单价。
    """
    db = SessionLocal()
    try:
        row = db.execute(
            select(ModelRoute)
            .where(ModelRoute.model_name == model, ModelRoute.enabled.is_(True))
            .order_by(ModelRoute.priority.asc())
            .limit(1)
        ).scalar_one_or_none()
        if row is not None:
            return float(row.price_per_1k_prompt), float(row.price_per_1k_completion)
    except Exception as exc:  # noqa: BLE001
        logger.warning("model_route_query_failed", model=model, error=str(exc))
    finally:
        db.close()
    return (
        float(settings.default_price_per_1k_prompt),
        float(settings.default_price_per_1k_completion),
    )


def get_avg_tokens(node: str, limit: int = 50) -> Tuple[Optional[float], Optional[float]]:
    """该节点近 limit 条运行记录的平均 token 数, 用于成本预估校准。

    无历史数据时返回 (None, None), 调用方用默认值兜底。
    """
    db = SessionLocal()
    try:
        rows = db.execute(
            select(
                func.avg(TaskNodeRun.prompt_tokens),
                func.avg(TaskNodeRun.completion_tokens),
            )
            .where(TaskNodeRun.node_name == node)
            .limit(limit)
        ).one()
        avg_p, avg_c = rows[0], rows[1]
        return (float(avg_p) if avg_p is not None else None,
                float(avg_c) if avg_c is not None else None)
    except Exception as exc:  # noqa: BLE001
        logger.warning("avg_tokens_query_failed", node=node, error=str(exc))
        return None, None
    finally:
        db.close()


def _next_run_seq(db, task_id: str, node: str) -> int:
    """同任务同节点的执行序号(重试递增)。"""
    count = db.execute(
        select(func.count()).select_from(TaskNodeRun).where(
            TaskNodeRun.task_id == task_id, TaskNodeRun.node_name == node
        )
    ).scalar_one()
    return int(count) + 1


def _cost_amount(prompt_tokens: int, completion_tokens: int, price: Tuple[float, float]) -> float:
    """金额 = prompt/1k*单价 + completion/1k*单价。"""
    return round(
        prompt_tokens / 1000 * price[0] + completion_tokens / 1000 * price[1],
        6,
    )


def record_llm_run(
    task_id: str,
    node: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    duration_ms: int = 0,
    error_message: Optional[str] = None,
) -> Optional[int]:
    """记录一次 LLM 调用: task_node_runs 一行 + cost_records(actual) 一条。返回 run_id。"""
    if not task_id:
        return None
    try:
        db = SessionLocal()
        try:
            price = get_price(model)  # flush 前查询, 避免共享连接事务冲突
            run = TaskNodeRun(
                task_id=task_id,
                run_seq=_next_run_seq(db, task_id, node),
                node_name=node,
                model_name=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                duration_ms=duration_ms,
                error_message=error_message,
            )
            db.add(run)
            db.flush()  # 取 run.id
            db.add(
                CostRecord(
                    task_id=task_id,
                    run_id=run.id,
                    node_name=node,
                    model_name=model,
                    cost_type="actual",
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    cost_amount=_cost_amount(prompt_tokens, completion_tokens, price),
                )
            )
            db.commit()
            return run.id
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("record_llm_run_failed", task_id=task_id, node=node, error=str(exc))
        return None


def record_sandbox_run(
    task_id: str,
    output_rows: int = 0,
    duration_ms: int = 0,
    error_message: Optional[str] = None,
    node: str = "executor",
) -> Optional[int]:
    """记录一次沙箱执行(executor): task_node_runs 一行, 无成本。"""
    if not task_id:
        return None
    try:
        db = SessionLocal()
        try:
            run = TaskNodeRun(
                task_id=task_id,
                run_seq=_next_run_seq(db, task_id, node),
                node_name=node,
                model_name="sandbox",
                output_rows=output_rows,
                duration_ms=duration_ms,
                error_message=error_message,
            )
            db.add(run)
            db.commit()
            return run.id
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("record_sandbox_run_failed", task_id=task_id, error=str(exc))
        return None


def record_estimate(
    task_id: str,
    node: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
) -> None:
    """记录一条预估成本(Planner 后熔断时机的依据), cost_type=estimate。"""
    if not task_id:
        return
    try:
        db = SessionLocal()
        try:
            db.add(
                CostRecord(
                    task_id=task_id,
                    run_id=None,
                    node_name=node,
                    model_name=model,
                    cost_type="estimate",
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    cost_amount=_cost_amount(
                        prompt_tokens, completion_tokens, get_price(model)
                    ),
                )
            )
            db.commit()
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("record_estimate_failed", task_id=task_id, error=str(exc))
