"""Coder 节点: 生成/修复安全的数据分析代码(SQL 或 Python)。

- 通过 Chroma 检索历史相似成功代码段, 提升首次生成质量(向量库不可用则降级)
- 错误分类: 语法错误 -> 原地重写重试; 逻辑错误 -> 路由回 Planner 重新规划
"""
from __future__ import annotations

import json
from typing import List, Optional

from src.nodes import make_llm
from src.state import MAX_RETRY, PipelineState
from src.tools.schema_retriever import get_schema_retriever
from src.tools.schema_provider import fetch_schema_sql
from src.utils.logger import get_logger
from src.utils.settings import get_settings

logger = get_logger(__name__)
settings = get_settings()

_SYSTEM_PROMPT = """你只生成安全的 SQL/Python 数据分析代码, 符合以下规范:
- 数据库查询必须用 SQL(SELECT / WITH 只读查询, PostgreSQL 方言), 禁止任何写操作(DROP/DELETE/INSERT/UPDATE/...)
- SQL 代码必须以 SELECT 或 WITH 开头(不要把注释写在代码第一行, 不要用 `--` 开头); 禁止使用任何全角/中文标点符号(括号、引号、逗号), 只用英文半角
- 时间范围: **优先按用户消息中给定的时间范围**——具体日期/区间用绝对日期字面量(如 order_date >= '2026-08-05' AND order_date < '2026-08-12'); 用户只说相对窗口(近7天/上周)时可用 NOW()/INTERVAL 或锚定后写绝对日期; 若用户消息带"本次查询时间范围(系统已解析)"则以该范围为准, 禁止再自由发挥
- 多表关联必须使用 "JOIN ... ON ..." 显式连接条件(例如 FROM a JOIN b ON a.id = b.a_id); 禁止用逗号(,)连接多个表, 也禁止不带 ON 的 JOIN
- 引用别名时必须使用正确的别名(如 r.sales 必须对应 r 别名定义过的列, 不要引用未定义的别名)
- **指标口径必须遵循用户消息中的'平台锁定指标口径'目录**: 涉及销售额/订单量/客单价等业务指标时, 聚合表达式、过滤条件、涉及表必须与目录一致, 禁止自行定义或改写口径**
- 禁止用 Python 代码连接数据库(pandas.read_sql / sqlalchemy / psycopg2 / os.environ['DATABASE_URL'] 等都不允许)——查询数据一律用 SQL
- Python 仅用于对 SQL 查询结果做图表/可视化(matplotlib/pandas/numpy 处理已返回的数据), 禁止文件系统/网络/子进程操作
- 代码必须可直接运行, 输出最终结果
- 只输出代码本身, 不要 Markdown 代码块标记, 不要解释"""


def _retrieve_history(user_query: str, plan_step: str, required_tables: List[str] | None = None) -> str:
    """从 Chroma 检索历史相似代码段(失败静默降级)。

    OR-01 负向过滤: 仅取 metadata.status=success 的历史片段(排除历史错误代码),
    并按 required_tables 表结构匹配度过滤(无交集片段剔除)。
    """
    try:
        retriever = get_schema_retriever()
        if not retriever.health():
            return ""
        hits = retriever.query(
            f"{user_query} {plan_step}", top_k=2, status="success", required_tables=required_tables
        )
        return "\n---\n".join(h.get("document", "") for h in hits)[:2000]
    except Exception as exc:  # noqa: BLE001
        logger.debug("schema_retriever_unavailable", error=str(exc))
        return ""


def classify_error(error_log: str) -> str:
    """错误分类: syntax(语法错误, 原地重试) / logic(逻辑错误, 重新规划)。

    规则引擎兜底: 关键字匹配; 可扩展为 qwen-flash 三分类。
    """
    low = error_log.lower()
    syntax_hints = ("syntaxerror", "nameerror", "typeerror", "undefined column", "syntax error")
    logic_hints = ("does not exist", "no such table", "relation", "ambiguous", "division by zero")

    if any(h in low for h in syntax_hints):
        return "syntax"
    if any(h in low for h in logic_hints):
        return "logic"
    return "other"  # 规则未命中 -> 交给 qwen-flash 三分类兜底(开发流程 2.2 表1)


def coder_node(state: PipelineState) -> dict:
    """生成代码。若此前执行失败, 携带错误信息让 LLM 修复代码。"""
    user_query = state.get("user_query", "")
    plan = state.get("plan", [])
    idx = state.get("current_task_index", 0)
    plan_step = plan[idx]["description"] if idx < len(plan) else user_query
    required_tables = plan[idx].get("required_tables", []) if idx < len(plan) else []
    error_log = state.get("error_log", "")
    retry_count = state.get("retry_count", 0)
    # 多轮上下文(上下文窗口管理): 有历史上下文时跳过代码缓存(上下文跨会话动态, 缓存会串)
    conversation_context = state.get("conversation_context") or {}
    has_context = bool(conversation_context)

    # 错误分类(开发流程 2.2 表1): 规则引擎优先(快), 未命中时 qwen-flash 三分类
    if error_log:
        category = classify_error(error_log)
        if category == "other" and error_log:
            from src.utils.aux_llm import classify_error_llm

            category = classify_error_llm(error_log, task_id=state.get("task_id"))
            logger.info("classify_llm_result", category=category)
        if category == "logic":
            logger.info("coder_reroute_to_planner", retry_count=retry_count)
            return {"route": "planner", "error_log": error_log, "progress": "coder_rerouting"}

    if retry_count >= MAX_RETRY:
        logger.warning("coder_max_retry_exceeded", retry_count=retry_count)
        # LLM 故障/业务重试超限直接判失败, 不再挂起人工审批 —— 审批批准后回 coder 仍会失败,
        # 只会造成"批准后再次挂起"的无限循环; 失败原因(含 LLM/代理/网络等)直接呈现给用户
        return {
            "route": "failed",
            "status": "failed",
            "error": f"代码生成失败(已重试 {MAX_RETRY} 次): {error_log}",
            "error_log": error_log,
            "progress": "coder_max_retry",
        }

    # 向量检索历史代码(降级容忍)
    history = _retrieve_history(user_query, plan_step, required_tables)
    progress_event = f"coder_retry_{retry_count + 1}" if error_log else "coder_starting"

    # 从 PostgreSQL 读取真实表结构(按任务数据源; 用户导入分析表后自动生效)
    from src.tools.data_source import resolve_db_url

    db_url = resolve_db_url(state.get("data_source_id"))
    schema_text = fetch_schema_sql(db_url)

    # PG MCP 按需补充: 仅主库(子进程固定连主库); 非主库数据源由 fetch_schema_sql 全量覆盖
    mcp_schema_block = ""
    if required_tables:
        from src.utils.settings import get_settings

        is_main_db = db_url == get_settings().database_url
        if is_main_db:
            try:
                from src.tools.mcp_client import get_table_details_batch

                details_list = get_table_details_batch(required_tables)
                parts = [d[:600] for d in details_list if d and len(d) > 40]
                if parts:
                    mcp_schema_block = "关键表精确结构(MCP):\n" + "\n---\n".join(parts)
            except Exception:  # noqa: BLE001 — MCP 不可用降级
                mcp_schema_block = ""

    # 指标/语义层: 注入平台锁定口径目录, 约束聚合逻辑遵循指标定义(防口径漂移)
    from src.tools.metric_registry import get_metric_registry

    metric_catalog = get_metric_registry().catalog_prompt(f"{user_query} {plan_step}")

    # 统一时间范围注入: 系统已从用户查询解析出绝对区间(显式日期/相对窗口锚定),
    # 要求 LLM 用此区间过滤 order_date, 禁止 NOW()/INTERVAL/CAST 相对写法自由发挥
    # (否则"2026年8月5日到8月11日"会被复用成近7天/近1月 —— 回归根因)
    time_hint = ""
    try:
        from src.utils.intent import parse_intent

        _tr = parse_intent(user_query).get("time_range")
        if _tr and _tr.get("start") and _tr.get("end"):
            time_hint = (
                f"\n本次查询时间范围(系统已解析, 必须以此为准; SQL 用绝对日期过滤 "
                f"order_date, 如 order_date >= '{_tr['start']}' AND order_date < '{_tr['end']}', "
                f"禁止用 NOW()/CURRENT_TIMESTAMP/INTERVAL/CAST 相对写法): {_tr['desc']}\n"
            )
    except Exception:  # noqa: BLE001 — 时间提示注入失败不阻塞
        pass

    # OR-06 结果缓存: 同需求+同历史schema(且非修复场景、无多轮上下文)复用生成代码
    code: str | None = None
    cache_key: str | None = None
    if not error_log and not has_context:
        import hashlib

        from src.utils.cache import cache_get, cache_set

        # 缓存 key 纳入指标目录, 口径变更后缓存自动失效; 也纳入 time_hint(绝对日期锚定),
        # 跨天复用旧绝对区间会少一天数据(回归根因)
        schema_hash = hashlib.md5(f"{history}|{schema_text}|{metric_catalog}|{time_hint}".encode("utf-8")).hexdigest()[:8]
        task_hash = hashlib.md5(f"{user_query}|{plan_step}".encode("utf-8")).hexdigest()
        cache_key = f"coder:{schema_hash}:{task_hash}"
        cached_code = cache_get(cache_key)
        if cached_code:
            code = cached_code.strip()
            logger.info("coder_cache_hit", step=plan_step)

    user_content = (
        f"需求: {user_query}\n当前步骤: {plan_step}\n"
        + (f"数据库表结构(生成 SQL 时必须使用这些真实表名/列名):\n{schema_text}\n" if schema_text else "")
        + (f"{mcp_schema_block}\n" if mcp_schema_block else "")
        + (f"{metric_catalog}\n" if metric_catalog else "")
        + time_hint
        + (f"历史相似代码参考:\n{history}\n" if history else "")
        + (f"上次执行错误:\n{error_log}\n请修复代码。" if error_log else "请生成代码。")
    )
    # 多轮上下文: 注入累积筛选条件 + 最近上文 + 上轮结论(理解追问, 沿用筛选/口径)
    if has_context:
        from src.utils.context_window import format_context

        ctx_text = format_context(conversation_context, node="coder")
        if ctx_text:
            user_content += f"\n\n{ctx_text}"

    try:
        llm = make_llm(settings.model_coder, temperature=0.2, node="coder")
        if code is None:
            code = llm.invoke(
                [{"role": "system", "content": _SYSTEM_PROMPT}, {"role": "user", "content": user_content}],
                task_id=state.get("task_id"),
            ).content.strip()
            # 去除可能的 markdown 代码块包裹
            if code.startswith("```"):
                code = code.strip("`")
                code = code.split("\n", 1)[-1] if "\n" in code else code
            # 写缓存(仅首次生成且非修复; 多轮上下文场景 cache_key 未定义, 不写缓存)
            if not error_log and cache_key is not None:
                cache_set(cache_key, code)
                logger.info("coder_cache_set", step=plan_step)
        logger.info("coder_generated", step=plan_step, chars=len(code))
    except Exception as exc:  # noqa: BLE001
        logger.error("coder_llm_failed", error=str(exc))
        return {
            "error_log": f"LLM 生成代码失败: {exc}",
            "retry_count": retry_count + 1,
            "route": "coder",
            "progress": progress_event,
        }

    return {
        "code": code,
        "error_log": "",
        "retry_count": retry_count + 1,
        "route": "executor",
        "progress": progress_event,
        "progress_detail": f"已生成代码 {len(code)} 字符",
    }
