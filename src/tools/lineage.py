"""数据血缘工具: 从 SQL 提取涉及的表, 并落库 query_runs(溯源)。

设计(参考 OpenLineage Run 模型简化版):
- executor 执行 SQL 成功后调用 record_query_run 写一条执行记录
- 表提取优先 sqlglot(准确处理 JOIN/子查询/CTE), 解析失败降级正则
- 落库是旁路: 任何失败只记日志, 不影响主流程(与 run_records 一致)
"""
from __future__ import annotations

import re
from typing import List

from src.utils.logger import get_logger

logger = get_logger(__name__)

# 正则兜底: 简单提取 FROM/JOIN 后的表名(无法处理子查询/CTE 嵌套, 仅 fallback)
_TABLE_RE = re.compile(r"\b(?:FROM|JOIN)\s+([a-zA-Z_][\w.]*)", re.IGNORECASE)


def extract_tables(sql: str) -> List[str]:
    """从 SQL 提取涉及的表名列表(去重保序)。

    优先 sqlglot 解析(FROM/JOIN/子查询/CTE 均可正确识别);
    解析失败或 sqlglot 不可用时降级正则。
    """
    sql = (sql or "").strip()
    if not sql:
        return []
    try:
        import sqlglot
        from sqlglot import exp

        tree = sqlglot.parse_one(sql, dialect="postgres")
        # CTE 名不是物理表, 需剔除(如 WITH recent_sales AS (...))
        cte_names = set()
        for cte in tree.find_all(exp.CTE):
            alias = cte.alias_or_name
            if alias:
                cte_names.add(alias)
        tables = []
        for t in tree.find_all(exp.Table):
            name = t.name or ""
            if name and name not in cte_names and name not in tables:
                tables.append(name)
        if tables:
            return tables
    except Exception as exc:  # noqa: BLE001 — sqlglot 解析失败降级正则
        logger.debug("sqlglot_parse_failed", error=str(exc)[:120])
    # 正则兜底
    return list(dict.fromkeys(m.group(1) for m in _TABLE_RE.finditer(sql)))


def record_query_run(
    task_id: str,
    sql_text: str,
    *,
    run_order: int = 0,
    status: str = "success",
    rows_returned: int = 0,
    duration_ms: int = 0,
) -> None:
    """落库一条查询执行记录(血缘/溯源)。旁路写库, 失败不抛异常。"""
    try:
        from src.api.deps import SessionLocal
        from src.models.query_run import QueryRun

        tables = extract_tables(sql_text)
        db = SessionLocal()
        try:
            db.add(
                QueryRun(
                    task_id=task_id,
                    run_order=run_order,
                    sql_text=(sql_text or "")[:4000],
                    tables=tables,
                    status=status,
                    rows_returned=rows_returned,
                    duration_ms=duration_ms,
                )
            )
            db.commit()
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001 — 血缘是旁路, 失败不阻塞主流程
        logger.warning("query_run_record_failed", task_id=task_id, error=str(exc)[:200])


def get_task_runs(task_id: str, limit: int = 20) -> list[dict]:
    """查询某任务的所有 SQL 执行记录(报告溯源附录用)。失败返回空列表。"""
    try:
        from src.api.deps import SessionLocal
        from src.models.query_run import QueryRun

        db = SessionLocal()
        try:
            rows = (
                db.query(QueryRun)
                .filter(QueryRun.task_id == task_id)
                .order_by(QueryRun.run_order.asc(), QueryRun.id.asc())
                .limit(limit)
                .all()
            )
            return [
                {
                    "run_order": r.run_order,
                    "sql_text": r.sql_text,
                    "tables": r.tables or [],
                    "status": r.status,
                    "rows_returned": r.rows_returned,
                    "duration_ms": r.duration_ms,
                    "created_at": r.created_at,
                }
                for r in rows
            ]
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("query_run_query_failed", task_id=task_id, error=str(exc)[:200])
        return []
