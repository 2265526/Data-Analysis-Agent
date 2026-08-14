"""定时任务调度器(APScheduler): 按 cron 触发分析任务, 完成后写站内通知。

设计(PM P1 定时任务+结果推送):
- FastAPI startup 时启动 AsyncIOScheduler(单机部署内嵌, 不依赖外部 worker)
- 每次启动从 DB 加载所有 enabled 定时任务并注册; 任务创建/更新/启停后调用
  `refresh_schedule()` 热同步
- 触发时: 创建 Task 记录(actor=owner, data_source_id) -> 后台执行流水线
  (复用 run_pipeline_task, 与手动提交同一链路) -> 完成后给 owner 写通知
- 失败防护: 单次触发异常仅记日志+通知, 不中断调度器
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from src.utils.logger import get_logger

logger = get_logger(__name__)

_scheduler = None
SCHEDULER_ENABLED = True  # 生产可经 settings 关闭


def _notify(task_id: str, title: str, content: str, user: str) -> None:
    """写站内通知(独立事务, 失败不阻断)。"""
    try:
        from src.api.deps import SessionLocal
        from src.models import Notification

        db = SessionLocal()
        try:
            db.add(
                Notification(
                    user=user,
                    title=title,
                    content=content[:2000],
                    task_id=task_id,
                    kind="scheduled",
                )
            )
            db.commit()
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("notification_write_failed", task_id=task_id, error=str(exc))


def _notify_users(task_id: str, title: str, content: str, users: list[str]) -> None:
    """给一组用户写站内通知(各用户一条, 独立事务, 单条失败不影响其他)。"""
    for user in dict.fromkeys(u for u in (users or []) if u):
        _notify(task_id, title, content, user)


def _run_scheduled(scheduled_id: int) -> None:
    """调度触发: 为每个目标数据源创建任务并执行流水线(跨数据源各跑一次)。"""
    from src.api.deps import SessionLocal
    from src.models import AuditLog, ScheduledTask, Task

    owner = "admin"
    notify_users: list[str] = []
    approval_status = "pending"
    db = SessionLocal()
    try:
        st = db.get(ScheduledTask, scheduled_id)
        if st is None or not st.enabled:
            return
        owner = st.owner or owner
        notify_users = list(st.notify_users or [])
        approval_status = getattr(st, "approval_status", None) or "pending"
        ds_ids = list(st.data_source_ids or [])
        if not ds_ids:
            ds_ids = [st.data_source_id] if st.data_source_id else [None]  # 空=主库
        schedule_query = st.query
        schedule_cron = st.cron
    finally:
        db.close()

    # 数据源显示名(通知标题用): 主库或数据源名
    results: list[str] = []
    for ds_id in ds_ids:
        task_id = str(uuid.uuid4())
        ds_label = "主库" if not ds_id else _ds_label(ds_id)
        db = SessionLocal()
        try:
            task = Task(
                id=task_id,
                user_query=schedule_query,
                status="pending",
                progress="定时任务已触发, 排队中",
                created_by=owner,
                data_source_id=ds_id,
                source="scheduled",
            )
            db.add(task)
            db.add(
                AuditLog(
                    task_id=task_id,
                    event="task_submitted",
                    actor=owner,
                    detail={"source": "scheduled", "scheduled_task_id": scheduled_id, "data_source_id": ds_id},
                )
            )
            st = db.get(ScheduledTask, scheduled_id)
            if st is not None:
                st.last_run_at = datetime.now(timezone.utc)
            db.commit()
            logger.info("scheduled_task_triggered", scheduled_id=scheduled_id, task_id=task_id, ds=ds_id)
        except Exception as exc:  # noqa: BLE001
            logger.error("scheduled_task_trigger_failed", scheduled_id=scheduled_id, error=str(exc))
            results.append(f"{ds_label}: 触发失败")
            _notify_users(task_id, "定时任务触发失败", f"{ds_label}: {str(exc)[:120]}", [owner] + notify_users)
            continue
        finally:
            db.close()

        try:
            from src.api.routes import run_pipeline_task

            result = run_pipeline_task.run(task_id)
            status = result.get("status") if isinstance(result, dict) else "completed"
            results.append(f"{ds_label}: {status}")
            title = "定时任务执行完成" if status == "completed" else f"定时任务执行结束({status})"
            content = f"任务「{task_id[:8]}」({ds_label})已结束, 状态: {status}"
            if approval_status != "approved":
                content += "。提示: 该定时任务尚未永久审批, 请在审批中心「永久审批」处理(通过后不再提示)。"
            _notify_users(task_id, f"{title} · {ds_label}", content, [owner] + notify_users)
        except Exception as exc:  # noqa: BLE001
            logger.error("scheduled_execution_failed", scheduled_id=scheduled_id, task_id=task_id, error=str(exc))
            results.append(f"{ds_label}: 执行异常")
            _notify_users(task_id, f"定时任务执行失败 · {ds_label}", f"{ds_label}: {str(exc)[:120]}", [owner] + notify_users)

    logger.info("scheduled_run_finished", scheduled_id=scheduled_id, results="; ".join(results))


def _ds_label(ds_id: int) -> str:
    """数据源显示名(通知/日志用)。"""
    try:
        from src.api.deps import SessionLocal
        from src.models import DataSource

        db = SessionLocal()
        try:
            ds = db.get(DataSource, ds_id)
            return ds.name if ds else f"数据源#{ds_id}"
        finally:
            db.close()
    except Exception:  # noqa: BLE001
        return f"数据源#{ds_id}"


def build_cron(
    schedule_type: str = "daily",
    schedule_time: str = "09:00",
    schedule_weekday: str = "1",
    schedule_day: int = 1,
    custom_cron: str = "",
) -> str:
    """把非程序员友好的频率描述翻译为 cron 表达式(5 段: 分 时 日 月 周)。

    - daily:   每天 HH:MM
    - weekly:  每周 星期几(0=周日..6=周六) HH:MM
    - monthly: 每月第 N 天 HH:MM
    - custom:  直接使用自定义 cron(高级模式)
    """
    schedule_type = (schedule_type or "daily").strip().lower()
    hh, _, mm = (schedule_time or "09:00").strip().partition(":")
    hh = hh.strip() or "9"
    mm = mm.strip() or "0"
    try:
        h = int(hh) % 24
        m = int(mm) % 60
    except ValueError:
        raise ValueError(f"执行时间格式错误: {schedule_time}")
    if schedule_type == "weekly":
        w = ",".join(x.strip() for x in str(schedule_weekday or "1").split(",") if x.strip() in "0123456")
        if not w:
            w = "1"
        return f"{m} {h} * * {w}"
    if schedule_type == "monthly":
        try:
            d = int(schedule_day or 1)
        except ValueError:
            d = 1
        return f"{m} {h} {min(max(d, 1), 28)} * *"
    if schedule_type == "custom":
        return (custom_cron or "").strip() or f"{m} {h} * * *"
    return f"{m} {h} * * *"  # daily


def cron_description(cron: str) -> str:
    """把 cron 表达式翻译成业务语言, 用于列表展示(如 0 9 * * * -> 每天 09:00)。"""
    parts = (cron or "").strip().split()
    if len(parts) != 5:
        return cron or "-"
    minute, hour, day, month, dow = parts
    try:
        hh = f"{int(hour) % 24:02d}"
        mm = f"{int(minute) % 60:02d}"
    except ValueError:
        return cron or "-"
    if dow not in ("*", "?"):
        try:
            week = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"][int(dow) % 7]
            return f"每周{week} {hh}:{mm}"
        except ValueError:
            return cron or "-"
    if day != "*":
        try:
            return f"每月 {int(day)} 号 {hh}:{mm}"
        except ValueError:
            return cron or "-"
    return f"每天 {hh}:{mm}"


def _load_scheduled() -> list[dict]:
    """从 DB 加载全部启用的定时任务。"""
    from src.api.deps import SessionLocal
    from src.models import ScheduledTask

    db = SessionLocal()
    try:
        rows = db.query(ScheduledTask).filter(ScheduledTask.enabled.is_(True)).all()
        return [{"id": r.id, "cron": r.cron} for r in rows]
    finally:
        db.close()


def refresh_schedule() -> None:
    """热同步: 定时任务增删改/启停后重载调度(幂等)。"""
    if not SCHEDULER_ENABLED:
        return
    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from apscheduler.triggers.cron import CronTrigger

        global _scheduler
        if _scheduler is None:
            _scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")
        for job in list(_scheduler.get_jobs()):
            job.remove()
        for item in _load_scheduled():
            try:
                trigger = CronTrigger.from_crontab(item["cron"])
            except Exception:  # noqa: BLE001 — 非法 cron 跳过该任务
                logger.warning("scheduled_bad_cron", scheduled_id=item["id"], cron=item["cron"])
                continue
            _scheduler.add_job(
                _run_scheduled,
                trigger=trigger,
                id=f"sched-{item['id']}",
                args=[item["id"]],
                replace_existing=True,
                misfire_grace_time=3600,
            )
        if not _scheduler.running:
            _scheduler.start()
        logger.info("scheduler_refreshed", jobs=len(_scheduler.get_jobs()))
    except Exception as exc:  # noqa: BLE001 — 调度不可用不阻塞服务
        logger.warning("scheduler_unavailable", error=str(exc))


def start_scheduler() -> None:
    """应用启动时调用: 初始化并加载定时任务。"""
    refresh_schedule()


def shutdown_scheduler() -> None:
    """应用关闭时调用。"""
    global _scheduler
    try:
        if _scheduler is not None and _scheduler.running:
            _scheduler.shutdown(wait=False)
    except Exception:  # noqa: BLE001
        pass
    _scheduler = None
