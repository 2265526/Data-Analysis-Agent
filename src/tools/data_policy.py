"""数据级权限: 策略加载与合并 + SQL 改写引擎(执行前强制, AI-SQL 场景)。

设计来源: 业界方案调研(Apache Ranger RLEF/CLM、Superset RLS、Metabase 沙箱)
的共识 —— "集中策略中心 + 查询改写注入"。本项目为 AI 智能体生成 SQL 的场景,
应用层 AST 改写是唯一可行的细粒度强制点(Metabase 官方结论: 不解析 SQL 就无法
细粒度授权, 只能一刀切禁 native 查询), 本项目已依赖 sqlglot, 故改写零新依赖。

语义(默认允许 + 显式收紧, 与历史行为平滑兼容):
- 无任何规则命中的表 = 放行(现状不变)
- 规则是"限制性声明", 绑定到 角色 或 用户; 用户级规则覆盖角色级
- 多角色命中同一张表时:
    - 行过滤(row_filter): 所有命中规则的表达式 AND(交集, 最严格)
    - 列访问(col_access): 逐列取"最宽松"(任一规则 allow 即 allow,
      否则任一规则 mask 即 mask, 否则 deny)——保证拥有高权限角色的用户豁免
- 列 deny: 查询显式引用该列 -> 拒绝整条查询(不泄漏)
- 列 mask: 显式引用 -> 替换为脱敏表达式(默认 '***'); SELECT * -> 展开后逐列处理
- 行过滤: 注入到每个引用该表的查询层(主查询/子查询/CTE 各自安全注入)
- 无法安全改写(解析失败/列归属无法确定且命中限制) -> 拒绝, 宁可拒绝不泄漏

强制执行点:
- src/nodes/executor.py(agent 生成 SQL, 提交执行前)
- src/api/routes.py drill_task_board(下钻 SQL)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy import and_, or_

from src.utils.logger import get_logger

logger = get_logger(__name__)

# 列访问模式宽松序: allow 最宽松, deny 最严格
_COL_RANK = {"allow": 2, "mask": 1, "deny": 0}


@dataclass
class TablePolicy:
    """某张表的合并生效策略。"""

    table: str
    row_filters: list[str] = field(default_factory=list)  # 多个时执行 AND
    col_access: dict[str, str] = field(default_factory=dict)  # col -> allow|mask|deny
    mask_expr: str = "'***'"  # mask 列使用的脱敏表达式(PostgreSQL 方言)


def load_effective_policies(
    username: str | None, roles: list[str] | None
) -> dict[str, TablePolicy]:
    """从 DB 加载并合并用户生效的数据权限, 返回 {table: TablePolicy}。

    - 命中范围: 用户本身(target_type=user) + 用户所有角色(target_type=role)
    - 用户级规则整体覆盖角色级(用户显式配置优先)
    - 多角色命中: 行过滤 AND; 列访问逐列取最宽松
    """
    policies: dict[str, TablePolicy] = {}
    role_names = [r for r in (roles or []) if r]

    from src.api.deps import SessionLocal
    from src.models import DataPolicyRule
    from src.models.data_policy import DEFAULT_MASK_EXPRESSION

    db = SessionLocal()
    try:
        q = db.query(DataPolicyRule).filter(DataPolicyRule.enabled.is_(True))
        if role_names or username:
            q = q.filter(
                or_(
                    and_(
                        DataPolicyRule.target_type == "role",
                        DataPolicyRule.target_name.in_(role_names or ["__none__"]),
                    ),
                    and_(
                        DataPolicyRule.target_type == "user",
                        DataPolicyRule.target_name == (username or "__none__"),
                    ),
                )
            )
        else:
            q = q.filter(DataPolicyRule.id < 0)  # 无身份: 查不到任何规则
        rules = q.all()
    finally:
        db.close()

    # 按表分组: 角色级 + 用户级
    role_by_table: dict[str, list] = {}
    user_by_table: dict[str, list] = {}
    for r in rules:
        bucket = user_by_table if r.target_type == "user" else role_by_table
        bucket.setdefault(r.table_name, []).append(r)

    for table in set(role_by_table) | set(user_by_table):
        role_rules = role_by_table.get(table, [])
        user_rules = user_by_table.get(table, [])
        chosen = user_rules if user_rules else role_rules  # 用户级覆盖角色级

        # 列访问: 逐列取最宽松
        col_access: dict[str, str] = {}
        for r in chosen:
            for col, mode in (r.col_access or {}).items():
                if mode not in _COL_RANK:
                    continue
                if col not in col_access or _COL_RANK[mode] > _COL_RANK[col_access[col]]:
                    col_access[col] = mode

        row_filters = [r.row_filter for r in chosen if r.row_filter]
        mask_expr = next(
            (r.mask_expression for r in chosen if r.mask_expression),
            DEFAULT_MASK_EXPRESSION,
        )
        policies[table] = TablePolicy(
            table=table,
            row_filters=row_filters,
            col_access=col_access,
            mask_expr=mask_expr,
        )
    return policies


# ---------------------------------------------------------------------------
# SQL 改写引擎
# ---------------------------------------------------------------------------

class PolicyViolation(Exception):
    """数据权限违规: 携带给用户/agent 的明确原因。"""


class PolicyDeniedError(Exception):
    """数据权限拒绝(执行层抛出): 任务直接失败终止, 不回 LLM 自修复循环。"""


def get_user_roles(username: str | None) -> list[str]:
    """按用户名查本地用户角色(供权限判定); 用户不存在/未登录返回空列表。"""
    if not username:
        return []
    from src.api.deps import SessionLocal
    from src.models import User as UserModel

    db = SessionLocal()
    try:
        row = db.query(UserModel).filter(UserModel.username == username).first()
        return [str(r) for r in (row.roles or [])] if row else []
    finally:
        db.close()


def _qualify(column: str, alias: str | None, table: str) -> str:
    """生成限定列引用: 优先 alias, 其次表名; 非限定则原样。"""
    if alias:
        return f"{alias}.{column}"
    return f"{table}.{column}"


def _qualify_expr(expr_sql: str, alias: str | None, table: str) -> str:
    """把策略表达式(SQL 片段)中的裸列名限定为 alias/表名限定列。

    仅替换顶层裸列引用(如 phone), 已限定的(o.phone)保持不变;
    解析失败视为不可用(由调用方决定拒绝)。
    """
    import sqlglot
    from sqlglot import exp

    expr = sqlglot.parse_one(expr_sql, read="postgres")
    for col in list(expr.find_all(exp.Column)):
        if col.table:  # 已带限定
            continue
        col.replace(exp.column(col.name, table=alias or table))
    return expr.sql(dialect="postgres")


def _mask_expr_ast(mask_expr: str, alias: str | None, table: str) -> str:
    """构造掩码表达式字符串, 其中的裸列名限定到当前表/别名。"""
    return _qualify_expr(mask_expr, alias, table)


def _list_table_columns(table: str) -> list[str]:
    """查 information_schema 返回表全部列名; 失败返回空列表。"""
    import psycopg2

    from src.tools.schema_provider import parse_db_url
    from src.utils.settings import get_settings

    settings = get_settings()
    conn = psycopg2.connect(**parse_db_url(settings.database_url), connect_timeout=3)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = %s ORDER BY ordinal_position",
            (table,),
        )
        cols = [r[0] for r in cur.fetchall()]
        cur.close()
        return cols
    except Exception as exc:  # noqa: BLE001 — 表不存在/连接失败
        logger.warning("data_policy_list_columns_failed", table=table, error=str(exc)[:120])
        return []
    finally:
        conn.close()


def _resolve_column_table(
    col, scope, policies: dict[str, TablePolicy]
) -> tuple[str | None, str | None]:
    """解析列的所属物理表与别名。

    - 已限定(col.table): 查 scope.sources 得物理表
    - 未限定: 该 scope 只有一个物理表 source 时归属它; 多个表时返回 None(无法确定)
    Returns: (table_name, alias) 或 (None, None)
    """
    import sqlglot  # noqa: F401
    from sqlglot import exp

    tbl = col.table or ""
    if tbl:
        src = scope.sources.get(tbl)
        if isinstance(src, exp.Table):
            return src.name, src.alias_or_name
        return None, None  # 限定的是子查询/CTE 别名, 该层策略不适用
    phys = [(a, t) for a, t in scope.sources.items() if isinstance(t, exp.Table)]
    if len(phys) == 1:
        return phys[0][1].name, phys[0][0]
    return None, None


def _apply_column_policy_on_select(scope, policies: dict[str, TablePolicy]) -> None:
    """对单个查询层(scope)的投影列应用掩码/deny; * 展开。

    直接在 sqlglot AST 上原地改写; 违规抛 PolicyViolation。
    scope.walk() 只遍历本层, 不进入子查询(子查询由各自的 scope 处理)。
    """
    import sqlglot
    from sqlglot import exp

    select = scope.expression
    if not isinstance(select, exp.Select):
        return  # Union/其他, 投影处理不适用

    # ---- 处理显式列引用(含函数参数中的列) ----
    for col in list(scope.walk()):
        if not isinstance(col, exp.Column) or isinstance(col, exp.Star):
            continue
        table_name, alias = _resolve_column_table(col, scope, policies)
        if table_name is None:
            # 归属无法确定: 列名命中限制 -> 保守拒绝(防同名列误判, 提示加别名)
            for tp in policies.values():
                mode = tp.col_access.get(col.name)
                if mode in ("deny", "mask"):
                    raise PolicyViolation(
                        f"列 {col.name} 受数据权限保护且无法确定所属表, "
                        f"请用表别名限定(如 t.{col.name}); 命中表 {tp.table}"
                    )
            continue  # 无限制列, 不动

        tp = policies.get(table_name)
        if tp is None:
            continue
        mode = tp.col_access.get(col.name)
        if mode == "deny":
            raise PolicyViolation(f"无权访问表 {table_name} 的列 {col.name}(deny)")
        if mode == "mask":
            mask_sql = _mask_expr_ast(tp.mask_expr, alias, table_name)
            col.replace(sqlglot.parse_one(mask_sql, read="postgres"))

    # ---- SELECT * / t.* 展开(仅在命中列策略时) ----
    projections = select.expressions
    hit = any(
        policies.get(src.name)
        for src in scope.sources.values()
        if isinstance(src, exp.Table)
    )
    if not hit:
        return
    for star in [e for e in projections if isinstance(e, exp.Star)]:
        star_table = star.args.get("table") or ""
        # 确定参与展开的表: t.* 只展开该表; * 展开全部物理表
        candidates = [
            (a, t)
            for a, t in scope.sources.items()
            if isinstance(t, exp.Table) and (not star_table or a == star_table)
        ]
        if not candidates:
            continue
        new_cols: list = []
        for alias, t in candidates:
            tp = policies.get(t.name)
            if tp is None:
                new_cols.append(exp.column(t.name, table=alias) if alias != t.name else exp.column(t.name))
                continue
            cols = _list_table_columns(t.name)
            if not cols:
                raise PolicyViolation(
                    f"表 {t.name} 存在列级策略且无法解析列清单(SELECT * 需展开), 请显式列出列名"
                )
            exposed = 0
            for c in cols:
                mode = tp.col_access.get(c, "allow")
                if mode == "deny":
                    continue
                if mode == "mask":
                    new_cols.append(
                        sqlglot.parse_one(_mask_expr_ast(tp.mask_expr, alias, t.name), read="postgres")
                    )
                else:
                    new_cols.append(exp.column(c, table=alias) if alias != t.name else exp.column(c))
                exposed += 1
            if exposed == 0:
                raise PolicyViolation(f"表 {t.name} 的所有列均被禁止访问, 无法 SELECT *")
        idx = projections.index(star)
        projections[idx : idx + 1] = new_cols


def _apply_row_filters(ast, policies: dict[str, TablePolicy]) -> None:
    """对每个引用策略表的查询层注入行过滤(WHERE AND)。"""
    import sqlglot
    from sqlglot import exp

    for table_node in list(ast.find_all(exp.Table)):
        tp = policies.get(table_node.name)
        if tp is None or not tp.row_filters:
            continue
        # 找到该表所在查询层(父链上的 Select)
        parent = table_node.parent
        select = None
        while parent is not None:
            if isinstance(parent, exp.Select):
                select = parent
                break
            parent = parent.parent
        if select is None:
            continue  # 非查询上下文(如 DDL), 只读校验已禁止, 忽略
        alias = table_node.alias_or_name
        conds = [
            sqlglot.parse_one(
                _qualify_expr(f, alias, table_node.name), read="postgres"
            )
            for f in tp.row_filters
        ]
        for cond in conds:
            select.where(cond, append=True, copy=False)


def apply_data_policy(
    sql: str, username: str | None, roles: list[str] | None
) -> tuple[Optional[str], Optional[str]]:
    """对 SQL 应用数据权限。

    Returns:
        (改写后SQL, None) 成功(无策略时原样返回)
        (None, 拒绝原因)  违规: 解析失败 / 引用 deny 列或表 / 无法安全改写
    """
    from src.tools.sql_validator import looks_like_sql

    sql = (sql or "").strip()
    if not looks_like_sql(sql):
        return sql, None  # Python 代码或非 SQL 不处理

    policies = load_effective_policies(username, roles)
    if not policies:
        return sql, None  # 默认允许

    import sqlglot
    from sqlglot.optimizer.scope import traverse_scope

    try:
        ast = sqlglot.parse_one(sql, read="postgres")
    except Exception as exc:  # noqa: BLE001
        logger.warning("data_policy_parse_failed", error=str(exc)[:120])
        return None, "数据权限检查失败: 无法解析 SQL(仅支持单条只读 SELECT)"

    try:
        scopes = traverse_scope(ast)
        for scope in scopes:
            _apply_column_policy_on_select(scope, policies)
        _apply_row_filters(ast, policies)
        return ast.sql(dialect="postgres"), None
    except PolicyViolation as exc:
        return None, str(exc)
    except Exception as exc:  # noqa: BLE001 — 改写异常保守拒绝
        logger.warning("data_policy_rewrite_failed", error=str(exc)[:200])
        return None, f"数据权限改写失败, 已拒绝执行: {exc}"
