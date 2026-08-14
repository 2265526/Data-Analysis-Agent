"""数据库表结构提供器: 从 PostgreSQL 读取 public schema 的表结构。

用途: 注入 Coder 提示词, 让 LLM 生成 SQL 时使用真实的表名/列名/类型/方言。
用户后续导入分析数据表后, 本模块自动包含新表, 无需改代码。

上下文控制(瘦身):
- 按估算行数倒序, 只对前 MAX_TABLES_DETAILED 张表展开完整结构(列/类型/示例值);
- 其余表仅注入表名清单, 让 LLM 知道存在但提示优先使用已展开的表;
- 避免 30 张大表全量结构注入导致的 6k+ token 上下文膨胀。

PostgreSQL 不可用时返回空字符串(调用方降级, 不阻塞主流程)。
"""
from __future__ import annotations

from src.utils.logger import get_logger
from src.utils.settings import get_settings

logger = get_logger(__name__)
settings = get_settings()

# 注入上限, 防止上下文膨胀
MAX_TABLES = 30                # 表名清单上限(含未展开表)
MAX_TABLES_DETAILED = 12       # 完整展开结构的表数(按行数倒序)
MAX_COLUMNS_PER_TABLE = 40     # 每表列数上限
MAX_SAMPLE_VALUES = 4          # 每列示例值个数

# LangGraph/框架内部状态表, 不属于业务分析数据, 不注入 Coder 提示词
SYSTEM_TABLE_PREFIXES = ("checkpoint", "langgraph_", "alembic_")


def parse_db_url(url: str) -> dict:
    """解析 postgresql://user:pass@host:port/db -> psycopg2.connect 参数。"""
    from urllib.parse import urlparse

    p = urlparse(url)
    return {
        "host": p.hostname or "localhost",
        "port": p.port or 5432,
        "dbname": (p.path or "/postgres").lstrip("/"),
        "user": p.username or "",
        "password": p.password or "",
    }


def _table_row_estimates(cur) -> dict[str, int]:
    """各表估算行数(reltuples, 非精确), 用于按业务核心表优先展开。"""
    cur.execute(
        """
        SELECT c.relname, c.reltuples::bigint AS est_rows
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind = 'r'
        """
    )
    rows = cur.fetchall()
    return {
        name: int(r or 0)
        for name, r in rows
        if not name.startswith(SYSTEM_TABLE_PREFIXES)
    }


def fetch_schema_sql(db_url: str | None = None) -> str:
    """返回可注入提示词的表结构文本(大表展开 + 其余表名清单); PG 不可用返回空串。

    Args:
        db_url: 目标数据源连接串; None 用主库 settings.database_url
    """
    url = db_url or settings.database_url
    try:
        import psycopg2

        conn = psycopg2.connect(**parse_db_url(url), connect_timeout=3)
        try:
            cur = conn.cursor()
            est_rows = _table_row_estimates(cur)
            cur.execute(
                """
                SELECT c.table_name, c.column_name, c.data_type, c.is_nullable
                FROM information_schema.columns c
                JOIN information_schema.tables t
                  ON t.table_schema = c.table_schema AND t.table_name = c.table_name
                WHERE c.table_schema = 'public'
                  AND t.table_type = 'BASE TABLE'
                ORDER BY c.table_name, c.ordinal_position
                """
            )
            rows = cur.fetchall()
            tables: dict[str, list[str]] = {}
            table_sample: dict[str, dict] = {}
            for table, column, dtype, nullable in rows:
                if table.startswith(SYSTEM_TABLE_PREFIXES):
                    continue
                if len(tables) >= MAX_TABLES:
                    break
                if table not in tables:
                    tables[table] = []
                    # 每表采样 100 行, 文本列取去重示例值(帮助 LLM 用对筛选值, 如 category_l2='手机通讯')
                    try:
                        cur.execute(f'SELECT * FROM "{table}" LIMIT 100')
                        sample_rows = cur.fetchall()
                        sdesc = [d[0] for d in cur.description] if sample_rows else []
                        col_vals: dict[str, list[str]] = {}
                        for row in sample_rows:
                            for i, col in enumerate(sdesc):
                                v = row[i]
                                if v is None:
                                    continue
                                sv = str(v).strip()
                                if sv and sv not in col_vals.setdefault(col, []):
                                    col_vals[col].append(sv[:15])
                        table_sample[table] = {c: vs[:MAX_SAMPLE_VALUES] for c, vs in col_vals.items()}
                    except Exception:  # noqa: BLE001 — 采样失败不阻塞
                        table_sample[table] = {}
                if len(tables[table]) >= MAX_COLUMNS_PER_TABLE:
                    continue
                line = f"  {column} {dtype} {'NULL' if nullable == 'YES' else 'NOT NULL'}"
                # 文本/日期列附加示例值(截断防上下文膨胀)
                if dtype in (
                    "character varying", "text", "varchar",
                    "date", "timestamp without time zone", "timestamp with time zone",
                ):
                    samples = table_sample.get(table, {}).get(column, [])
                    if samples:
                        line += "  示例: " + " / ".join(samples)
                tables[table].append(line)
            if not tables:
                return ""

            # 按估算行数倒序: 大表(业务核心)优先展开完整结构
            ordered = sorted(
                tables.keys(),
                key=lambda t: (est_rows.get(t, 0), t),
                reverse=True,
            )
            detailed = ordered[:MAX_TABLES_DETAILED]
            lines = ["以下是当前数据库(PostgreSQL)中可查询的表结构, 生成 SQL 时表名/列名/筛选值必须以此为准:"]
            for table in detailed:
                lines.append(f"表 {table}(约 {est_rows.get(table, 0):,} 行):")
                lines.extend(tables[table])
            rest = [t for t in ordered[MAX_TABLES_DETAILED:] if t in tables]
            if rest:
                lines.append("其他表(仅表名, 未展开结构, 优先使用上面已展开的表):")
                lines.append("  " + ", ".join(rest))
            return "\n".join(lines)
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 — PG 不可用不阻塞主流程
        logger.debug("schema_provider_unavailable", error=str(exc))
        return ""
