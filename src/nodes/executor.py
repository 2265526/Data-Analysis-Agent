"""Executor 节点: 在安全沙箱中执行 Coder 生成的代码。

- SQL 强制只读校验(危险语句直接拒绝)
- Docker 容器隔离(无网络/资源限制/超时), 不可用时降级本地模拟
- 结果行数超过阈值 -> 转入人工审批(Human-in-the-loop)

监控埋点(对应优化方案指标):
- sandbox_exec_duration_seconds: SQL/Python 实际执行耗时(指标5)
- executor_failures_total: 执行失败次数, 自修复尝试的统计源(指标1)
"""
from __future__ import annotations

import time

from src.sandbox.docker_sandbox import run_in_sandbox
from src.state import EXEC_OK, PipelineState
from src.tools.schema_retriever import get_schema_retriever
from src.tools.sql_validator import find_sensitive_tables
from src.utils.logger import get_logger
from src.utils.metrics import metrics
from src.utils.settings import get_settings

logger = get_logger(__name__)
settings = get_settings()

# 输出行数超过该阈值视为大结果集, 需人工审批后才能交给 Reporter
APPROVAL_THRESHOLD = settings.approval_threshold_rows
# exec_result 全量落盘的行数阈值: 必须小于审批阈值, 否则中等结果集分支永不触发
EXEC_RESULT_FULL_LIMIT = min(settings.exec_result_full_limit_rows, APPROVAL_THRESHOLD - 1)


def _query_has_specific_date(user_query: str) -> bool:
    """用户查询是否指定了具体日期(2026年8月7日 / 2026-08-07 等)。

    具体日期查询绝不能用相对时间窗口(NOW()/INTERVAL) —— coder 复用
    "近7天/近1月"历史代码会整错口径(回归根因)。
    """
    import re as _re

    return bool(
        _re.search(r"\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日", user_query or "")
        or _re.search(r"\d{4}-\d{2}-\d{2}", user_query or "")
    )


def _sql_uses_relative_window(sql: str) -> bool:
    """SQL 是否用相对时间窗口(NOW()/CURRENT_TIMESTAMP/INTERVAL)过滤 order_date。"""
    low = (sql or "").lower()
    if "order_date" not in low and "order_day" not in low:
        return False
    return "now()" in low or "current_timestamp" in low or "interval" in low


def _persist_exec_full(task_id: str, output: str) -> None:
    """大结果集全量输出落盘(审批通过后 reporter 读取做精确统计, 不丢结构)。

    路径按 task_id 唯一(reports_dir 根下), 不依赖"当天日期"目录 ——
    审批可经 interrupt 挂起跨午夜, reporter 若按新日期目录找会读不到全量,
    静默回退截断文本导致 KPI/图表/明细失真(回归根因)。
    """
    try:
        f = settings.reports_dir / f"{task_id}.exec_full.txt"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(output, encoding="utf-8")
    except Exception:  # noqa: BLE001 - 落盘失败不影响主流程
        logger.warning("exec_full_persist_failed", task_id=task_id)


def executor_node(state: PipelineState) -> dict:
    """执行 state["code"], 更新 exec_result / error_log。

    route: 成功->reporter / 大结果集->human_approval / 失败->coder(自修复重写)
    """
    code = state.get("code", "")
    if not code:
        return {"error_log": "Executor 收到空代码", "status": "failed", "route": "coder", "progress": "executor_empty"}

    logger.info("executor_start", code_chars=len(code))
    task_id = state.get("task_id")

    # 数据源路由: 任务指定的数据源连接串(默认主库); 非主库跳过 MCP 预检(子进程固定连主库)
    from src.tools.data_source import resolve_db_url
    from src.utils.settings import get_settings

    db_url = resolve_db_url(state.get("data_source_id"))
    is_main_db = db_url == get_settings().database_url

    # PG MCP 预检(EXPLAIN, 只读): 仅对复杂 SQL(多表 JOIN/CTE/子查询/长 SQL)预检,
    # 简单查询本地执行毫秒级报错, 预检反而增加子进程开销(量化 A/B 结论); 仅主库启用
    from src.tools.sql_validator import looks_like_sql

    if looks_like_sql(code) and is_main_db:
        from src.tools.mcp_client import explain_sql, is_complex_sql

        if is_complex_sql(code):
            plan = explain_sql(code)
            if plan is not None and plan.strip().startswith("Error"):
                msg = plan.strip().splitlines()[0][:300]
                logger.info("executor_mcp_preexplain_failed", task_id=task_id, error=msg)
                return {
                    "exec_result": "",
                    "error_log": (
                        f"SQL 预检失败(MCP EXPLAIN): {msg}\n"
                        "请修正 SQL 后重新生成(表/列名、数据类型、语法); "
                        "注意: PostgreSQL 中字符串列不可用 [x] 下标/切片访问, 字符串截取请用 LEFT/SUBSTRING。"
                    ),
                    "status": "running",  # 保持运行中, 由 coder 重试直到终态(与沙箱失败分支一致)
                    "route": "coder",
                    "progress": "executor_preexplain_failed",
                    "progress_detail": "SQL 预检未通过(表/列/语法), 正在修正...",
                }

    # 数据级权限强制(表/列/行级): 提交执行前对 SQL 应用策略。
    # deny/无法安全改写 -> 抛 PolicyDeniedError 直接终止任务(failed),
    # 不回 coder 自修复(权限不可通过重写绕过); 掩码/行过滤通过 SQL 改写注入。
    if looks_like_sql(code):
        from src.tools.data_policy import PolicyDeniedError, apply_data_policy, get_user_roles

        actor = state.get("actor") or ""
        new_sql, denied = apply_data_policy(code, actor or None, get_user_roles(actor))
        if denied:
            logger.info("executor_data_policy_denied", task_id=task_id, reason=denied)
            raise PolicyDeniedError(f"数据权限拒绝: {denied}")
        if new_sql != code:
            logger.info("executor_data_policy_rewritten", task_id=task_id)
            code = new_sql  # 掩码/行过滤已在 SQL 层强制, 用改写后 SQL 执行

        # 时间语义一致性: 用户查询指定了具体日期(如 2026年8月7日), 但 SQL 用
        # 相对窗口(NOW()/INTERVAL)过滤 order_date —— coder 复用"近7天/近1月"
        # 历史代码导致"8月7日单日查询"整成近7天/近1个月(回归根因)。确定性拦截
        # 打回 coder 重生成(带明确修正提示), 而不是执行错误数据出报告。
        if _query_has_specific_date(state.get("user_query", "")) and _sql_uses_relative_window(code):
            logger.info("executor_time_semantics_reject", task_id=task_id)
            return {
                "error_log": (
                    "用户查询指定了具体日期(如 2026年8月7日), 但 SQL 使用相对时间窗口"
                    "(NOW()/CURRENT_TIMESTAMP/INTERVAL)过滤 order_date, 与查询不符。"
                    "请改用具体日期过滤: order_date >= '2026-08-07' AND order_date < '2026-08-08'。"
                ),
                "status": "running",
                "route": "coder",
                "progress": "executor_time_semantics_reject",
                "progress_detail": "具体日期查询误用相对时间窗口, 正在修正...",
            }

    started = time.monotonic()
    result = run_in_sandbox(code, backend="auto", db_url=db_url)
    metrics.observe(
        "sandbox_exec_duration_seconds",
        time.monotonic() - started,
        labels={"backend": "auto"},
    )
    elapsed_ms = int((time.monotonic() - started) * 1000)

    if result["status"] == EXEC_OK:
        output = result["output"]
        # 真实返回行数: SQL 由沙箱返回 row_count(服务器端游标统计), Python 回退文本行数
        row_count = result.get("row_count")
        line_count = row_count if row_count is not None else len(output.strip().splitlines())
        # 落库: 沙箱执行明细(输出行数/耗时)
        from src.utils.run_records import record_sandbox_run

        record_sandbox_run(
            task_id=task_id, output_rows=line_count, duration_ms=elapsed_ms
        )
        # 血缘/溯源: 记录本次 SQL 执行(SQL 全文/涉及表/行数/耗时), 供报告溯源附录展示
        from src.tools.lineage import record_query_run

        record_query_run(
            task_id=task_id or "",
            sql_text=code,
            run_order=state.get("current_task_index", 0),
            rows_returned=line_count,
            duration_ms=elapsed_ms,
        )
        # CR-07 敏感表保护: SQL 引用敏感表(phone/身份证/密码等)即使行数不大也触发人工审批
        # 例外: 本任务已人工过审(approval_passed)或自动任务(定时任务 auto_approve)——不再反复挂起
        approved = bool(state.get("approval_passed") or state.get("auto_approve"))
        sensitive_tables = find_sensitive_tables(code)
        if sensitive_tables and not approved:
            logger.info("executor_sensitive_table_approval", tables=sensitive_tables)
            # 与"大结果集"一致: 全量输出落盘, 审批通过后 reporter 基于全量统计(截断会失真)
            _persist_exec_full(task_id, output)
            return {
                "exec_result": output[:2000],
                "error_log": "",
                "route": "human_approval",
                "status": "awaiting_approval",
                "progress": "awaiting_approval",
                "progress_detail": f"查询命中敏感表 {', '.join(sensitive_tables)}, 等待人工审批",
                "progress_percent": 70,
            }
        # 大结果集: 触发人工审批; 全量输出落盘供 reporter 精确统计, exec_result 存结构保真截断。
        # 注意: 不能用 LLM 生成摘要替代 exec_result —— 摘要模型会把 9927 行客户数据
        # 幻觉成"品类销售"结构, 导致报告正文/明细/看板全部不对题(回归根因)。
        if line_count > APPROVAL_THRESHOLD and not approved:
            _persist_exec_full(task_id, output)
            logger.info("executor_awaiting_approval", lines=line_count)
            return {
                "exec_result": output[:2000],
                "error_log": "",
                "route": "human_approval",
                "status": "awaiting_approval",
                "progress": "awaiting_approval",
                "progress_detail": f"结果 {line_count} 行, 超过阈值 {APPROVAL_THRESHOLD}, 等待人工审批",
                "progress_percent": 70,
            }

        # 中等结果集(EXEC_RESULT_FULL_LIMIT ~ 审批阈值): 沙箱已全量输出, exec_result 截断进 state/prompt,
        # 全量落盘 exec_full 供 reporter 读取做精确统计/图表(不截断则品类/客户不全)。
        # 小结果集(<= EXEC_RESULT_FULL_LIMIT 行)直接全量进 exec_result, reporter 无需读盘。
        if line_count > EXEC_RESULT_FULL_LIMIT:
            _persist_exec_full(task_id, output)
            exec_result = output[:2000]
        else:
            exec_result = output

        # OR-01: 成功代码写入向量库(status=success), 供后续任务负向过滤复用(失败静默降级)
        plan = state.get("plan", [])
        pidx = state.get("current_task_index", 0)
        plan_step = plan[pidx].get("description", "") if pidx < len(plan) else ""
        req_tables = plan[pidx].get("required_tables", []) if pidx < len(plan) else []
        try:
            retriever = get_schema_retriever()
            if retriever.health():
                retriever.upsert_success_code(
                    code, plan_step=plan_step, required_tables=req_tables, task_id=task_id or ""
                )
        except Exception:  # noqa: BLE001
            pass  # 向量库写入失败不影响主流程

        return {
            "exec_result": exec_result,
            "error_log": "",
            "status": "running",
            "route": "reporter",
            "progress": "executor_finished",
            "progress_detail": f"执行成功, 耗时 {elapsed_ms / 1000:.1f}s",
            "progress_percent": 80,
        }

    # 执行失败: 错误信息交给 Coder 修复(自修复循环)
    logger.warning("executor_failed", error=result["error"][:500])
    metrics.inc("executor_failures_total", labels={"node": "executor"})
    from src.utils.run_records import record_sandbox_run

    record_sandbox_run(
        task_id=task_id,
        duration_ms=elapsed_ms,
        error_message=(result.get("error") or "")[:2000],
    )
    return {
        "exec_result": "",
        "error_log": result["error"] or "未知执行错误",
        "status": "running",
        "route": "coder",
        "progress": "executor_failed",
        "progress_detail": f"执行失败(耗时 {elapsed_ms / 1000:.1f}s): {(result.get('error') or '')[:120]}",
    }
