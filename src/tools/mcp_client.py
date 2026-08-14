"""PostgreSQL MCP Server 客户端适配层(只读)。

通过 mcp Python SDK 以 stdio 方式连接 postgres-mcp(RESTRICTED 只读模式),
向流水线暴露标准 MCP 工具:
- list_objects / get_object_details: 按需 schema 检索(替代全量 fetch_schema_sql 的上下文膨胀)
- explain_query: SQL EXPLAIN 预检(提交沙箱前快速发现 语法错/表列不存在/性能问题,
  提前触发 coder 重写, 减少 executor 的 30s 沙箱试错轮)

设计:
- 同步接口(async 内层封装), 供 LangGraph 同步节点直接调用
- 每次调用建立独立 stdio 会话(进程内连接池不可跨线程复用), 用完即关
- 连接失败/超时优雅降级: 返回 None/空, 不阻塞主流程(现有 fetch_schema_sql / 沙箱兜底)
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from src.utils.settings import get_settings

logger = logging.getLogger(__name__)

# 每次工具调用的总超时(含 stdio 子进程启动), 超过视为 MCP 不可用
_CALL_TIMEOUT = 20.0

# postgres-mcp 可执行文件(venv bin), 子进程 PATH 不含 venv 需用绝对路径
_POSTGRES_MCP_BIN = str(
    Path(__file__).resolve().parent.parent.parent / ".venv" / "bin" / "postgres-mcp"
)


def _enabled() -> bool:
    """MCP 开关(环境变量 PG_MCP_ENABLED, 默认开启; 0/false 关闭)。"""
    return os.getenv("PG_MCP_ENABLED", "1").lower() not in ("0", "false", "off")


def _call_tool(tool_name: str, args: Dict[str, Any]) -> Optional[str]:
    """同步调用单个 MCP 工具, 返回文本结果; 失败返回 None(调用方降级)。"""
    results = _call_tools([(tool_name, args)])
    return results[0] if results else None


def _call_tools(calls: List[Tuple[str, Dict[str, Any]]]) -> List[Optional[str]]:
    """一次 stdio 会话内批量调用多个 MCP 工具(减少子进程启动开销)。

    返回与 calls 等长的结果列表(单个失败不影响其他; 会话失败全部返回 None)。
    """
    if not _enabled() or not calls:
        return [None] * len(calls)
    db_url = get_settings().database_url

    async def _run() -> List[Optional[str]]:
        params = StdioServerParameters(
            command=_POSTGRES_MCP_BIN,
            args=["--access-mode=restricted", db_url],
            env={**os.environ.copy()},
        )
        out: List[Optional[str]] = []
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                for tool_name, args in calls:
                    try:
                        res = await session.call_tool(tool_name, args)
                        out.append(
                            "\n".join(c.text if hasattr(c, "text") else str(c) for c in res.content)
                            if res.content
                            else ""
                        )
                    except Exception:  # noqa: BLE001 — 单个工具失败不影响后续
                        out.append(None)
        return out

    try:
        return asyncio.run(asyncio.wait_for(_run(), timeout=_CALL_TIMEOUT))
    except Exception as exc:  # noqa: BLE001 — MCP 不可用静默降级
        logger.warning("mcp_call_failed", calls=[c[0] for c in calls], error=str(exc)[:120])
        return [None] * len(calls)


def mcp_available() -> bool:
    """探测 MCP server 是否可用(连接一次并列出工具)。"""
    return _call_tool("list_schemas", {}) is not None


def list_tables(schema: str = "public") -> Optional[List[Dict[str, str]]]:
    """列出 public schema 的业务表(替代 get_tables)。"""
    text = _call_tool("list_objects", {"schema_name": schema, "object_type": "table"})
    if text is None:
        return None
    try:
        import ast

        items = ast.literal_eval(text)  # postgres-mcp 返回 Python 字面量
        return [i for i in items if isinstance(i, dict)]
    except Exception:  # noqa: BLE001
        return None


def get_table_details(table: str, schema: str = "public") -> Optional[str]:
    """获取单表完整结构(列名/类型/约束/示例), 供 coder 按需注入。"""
    return _call_tool(
        "get_object_details",
        {"schema_name": schema, "object_name": table, "object_type": "table"},
    )


def get_table_details_batch(tables: List[str], schema: str = "public") -> List[Optional[str]]:
    """一次 stdio 会话内批量取多表结构(减少子进程启动开销, 返回与 tables 等长)。"""
    return _call_tools(
        [
            (
                "get_object_details",
                {"schema_name": schema, "object_name": t, "object_type": "table"},
            )
            for t in tables
        ]
    )


def is_complex_sql(sql: str) -> bool:
    """启发式判定 SQL 是否复杂(值得 EXPLAIN 预检)。

    简单单表查询本地执行毫秒级报错, 预检反而是净开销(量化 A/B 结论);
    多表 JOIN / CTE / 子查询 / 长 SQL 首轮出错概率高, 预检价值大。
    """
    s = sql.strip().lower()
    if not s.startswith(("select", "with")):
        return False
    if s.startswith("with"):
        return True  # CTE
    if s.count(" join ") >= 2:
        return True  # 多表 JOIN
    # 子查询: 括号内出现 select
    if "(" in s and "select" in s[s.find("("):]:
        return True
    if len(sql) > 600:
        return True
    return False


def explain_sql(sql: str, analyze: bool = False) -> Optional[str]:
    """EXPLAIN 预检 SQL: 返回计划文本; SQL 有错返回错误信息; MCP 不可用返回 None。"""
    if not sql.strip().lower().startswith(("select", "with")):
        return None
    return _call_tool("explain_query", {"sql": sql, "analyze": analyze})
