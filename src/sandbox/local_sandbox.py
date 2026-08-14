"""本地模拟执行器(开发/无 Docker 环境兜底)。

- 基于 subprocess + 超时控制
- 不做完整隔离, 仅用于本地调试; 生产环境请使用 DockerSandbox
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict

from src.utils.logger import get_logger
from src.utils.settings import get_settings

logger = get_logger(__name__)
settings = get_settings()

# 本地模拟禁用数据库写操作的校验(与沙箱保持一致)
from src.tools.sql_validator import looks_like_sql, validate_readonly  # noqa: E402

# 结果集行数 <= 该阈值时直接全量输出(不截断前 10 行样本)。
# 多维结果(如 8 品类 × 5 天 = 39 行)按 ORDER BY 排序后前 10 行可能只覆盖 2 个品类,
# reporter 基于样本构建图表/看板/明细会导致"只剩两个品类"(回归根因);
# 超过阈值(即需要人工审批的大结果集)保持 前 10 行样本 + STATS 全量聚合。
# 与 executor 的 exec_result_full_limit_rows(500) 配合: 500 ~ 审批阈值 之间由
# executor 落盘全量 exec_full 供 reporter 精确统计。
_FULL_OUTPUT_MAX_ROWS = settings.approval_threshold_rows


class LocalSandbox:
    """本地执行 Python 代码(模拟沙箱语义)。"""

    name = "local"

    def __init__(self, timeout: int | None = None) -> None:
        self.timeout = timeout or settings.sandbox_timeout

    def execute(self, code: str, db_url: str | None = None) -> Dict[str, Any]:
        """执行代码, 返回统一结果结构 {"status", "output", "error"}。

        若代码为 SQL, 先做只读校验; 若为 Python, 直接用临时文件跑解释器。
        db_url: 数据源连接串(SQL 执行目标库; 默认主库 settings.database_url)。
        """
        code = code.strip()
        if not code:
            return {"status": "error", "output": "", "error": "代码为空"}

        # SQL 代码: 强制只读
        if looks_like_sql(code):
            ok, reason = validate_readonly(code)
            if not ok:
                return {"status": "error", "output": "", "error": f"SQL 只读校验失败: {reason}"}
            return self._execute_sql(code, db_url=db_url)

        return self._execute_python(code)

    # ------------------------------------------------------------------
    def _execute_sql(self, sql: str, db_url: str | None = None) -> Dict[str, Any]:
        """本地执行只读 SQL: 连接 PostgreSQL(会话级只读事务 + 回滚), 查询真实数据。

        db_url: 数据源连接串(默认主库); 失败直接返回真实错误, 便于错误分类与自修复。
        """
        return self._execute_sql_postgres(sql, db_url=db_url)

    def _execute_sql_postgres(self, sql: str, db_url: str | None = None) -> Dict[str, Any]:
        """连接 PostgreSQL 执行只读 SQL: SET TRANSACTION READ ONLY + 结束回滚。

        服务器端游标统计真实返回行数(row_count); db_url 为数据源连接串(默认主库)。
        """
        import uuid

        import psycopg2

        from src.tools.schema_provider import parse_db_url

        conn = psycopg2.connect(
            **parse_db_url(db_url or settings.database_url),
            connect_timeout=3,
            options="-c statement_timeout=30000",  # 30s 超时保护
        )
        try:
            # 会话级只读: 即使 SQL 绕过上层校验也无法写库(双保险)
            conn.set_session(readonly=True, autocommit=False)
            # 服务器端游标(事务内): 逐批 fetch, 内存占用恒定, 能统计真实行数
            cur = conn.cursor(name=f"ro_{uuid.uuid4().hex[:12]}")
            cur.itersize = 1000
            cur.execute(sql)
            # 服务器端游标(游标名)在 fetch 前 description 为 None —— 必须拿到第一批数据
            # 后再取列名, 否则表头恒为空, 报告表格/图表全部退化(回归根因)。
            sample: list = []
            total = 0
            col_stats: dict[str, dict] = {}  # 列名 -> {sum, max, top: [(label, value)]}
            while True:
                batch = cur.fetchmany(1000)
                if not batch:
                    break
                if not sample:
                    # 首次取数: 现在 description 已就绪(列名 -> 表头)
                    cols = [d[0] for d in cur.description] if cur.description else []
                for row in batch:
                    total += 1
                    # 多列统计: 每个能转数值的列(>=1)独立累计, 解决 3 列(客户/名称/金额)
                    # 场景下"第2列是字符串"导致 sum=0 的失真; 单列结果(COUNT/AVG 等
                    # KPI 查询)统计第 1 列, 否则 cols 恒为空 -> 报告"数据不足"(回归根因)
                    range_start = 0 if len(row) <= 1 else 1
                    for ci in range(range_start, len(row)):
                        try:
                            v = float(row[ci])
                        except (TypeError, ValueError):
                            continue
                        cname = cols[ci] if ci < len(cols) else f"col{ci}"
                        st = col_stats.setdefault(cname, {"sum": 0.0, "max": None, "top": []})
                        st["sum"] += v
                        if st["max"] is None or v > st["max"]:
                            st["max"] = v
                        # label = 该行第一个"非数值且非日期语义"列(品类/客户名), 而非日期
                        # (如 order_day), 否则 TOP 显示"2026-08-10"而不是"服饰鞋包"(回归根因)
                        label = None
                        _date_like = {"order_day", "order_date", "date", "day", "week",
                                      "week_start", "month", "created_at", "updated_at"}
                        for j in range(len(row)):
                            try:
                                float(row[j])
                                continue
                            except (TypeError, ValueError):
                                pass
                            cname_j = (cols[j] if j < len(cols) else "").lower()
                            if cname_j in _date_like:
                                continue
                            label = str(row[j])
                            break
                        if label is None:
                            label = str(row[0])
                        st["top"].append((label, v))
                        st["top"].sort(key=lambda x: x[1], reverse=True)
                        del st["top"][5:]
                    if len(sample) < _FULL_OUTPUT_MAX_ROWS:
                        sample.append(row)
            header = ", ".join(cols) if sample else ""
            # 小结果集直接全量输出(图表/看板/明细需覆盖所有品类/行);
            # 大结果集仍只显示前 10 行样本, 全量统计走 STATS/exec_full 落盘
            shown_rows = sample if total <= _FULL_OUTPUT_MAX_ROWS else sample[:10]
            shown = "\n".join(str(r) for r in shown_rows)
            output = f"rows={total}\n{header}\n{shown}"
            if total > _FULL_OUTPUT_MAX_ROWS:
                output += f"\n...(共 {total} 行, 仅显示前 10 行)"
            # 全量聚合统计(规则计算, 不依赖 LLM; reporter 用其校正 KPI/正文数字)
            stats_payload = {"count": total, "cols": {}}
            for cname, st in col_stats.items():
                stats_payload["cols"][cname] = {
                    "sum": st["sum"],
                    "max": st["max"],
                    "top": "; ".join(f"{k}:{v:.2f}" for k, v in st["top"]),
                }
            output += f"\nSTATS: {json.dumps(stats_payload, ensure_ascii=False)}"
            conn.rollback()  # 只读事务结束回滚, 不留任何写痕迹
            return {"status": "success", "output": output, "error": "", "row_count": total}
        except Exception as exc:  # noqa: BLE001 — 透传真实错误(表/列不存在等), 供错误分类
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                pass
            return {"status": "error", "output": "", "error": f"SQL 执行失败: {exc}"}
        finally:
            conn.close()

    def _execute_sql_sqlite(self, sql: str) -> Dict[str, Any]:
        """回退方案: sqlite3 内存空库, 仅验证 SQL 可执行性(无真实数据)。"""
        try:
            import sqlite3

            conn = sqlite3.connect(":memory:")
            cur = conn.execute(sql)
            rows = cur.fetchmany(100)
            output = f"rows={len(rows)}\n" + "\n".join(str(r) for r in rows[:10])
            conn.close()
            return {"status": "success", "output": output, "error": ""}
        except Exception as exc:  # noqa: BLE001
            return {"status": "error", "output": "", "error": f"SQL 执行失败: {exc}"}

    def _execute_python(self, code: str) -> Dict[str, Any]:
        """本地执行 Python 代码(受超时约束)。"""
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
            f.write(code)
            script = f.name
        try:
            proc = subprocess.run(
                [sys.executable, script],
                capture_output=True,
                text=True,
                timeout=self.timeout,
                cwd=Path(script).parent,
            )
            if proc.returncode == 0:
                return {"status": "success", "output": proc.stdout[-4000:], "error": ""}
            return {"status": "error", "output": proc.stdout[-2000:], "error": proc.stderr[-4000:]}
        except subprocess.TimeoutExpired:
            return {"status": "error", "output": "", "error": f"执行超时(>{self.timeout}s)"}
        except Exception as exc:  # noqa: BLE001
            return {"status": "error", "output": "", "error": f"本地执行异常: {exc}"}
        finally:
            Path(script).unlink(missing_ok=True)
