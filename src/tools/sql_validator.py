"""SQL 只读校验: 在 Executor 层强制仅允许 SELECT, 匹配到危险语句直接拒绝。

监控埋点(对应优化方案指标6): tool_param_rejections_total —— 非法 SQL 参数拒绝次数。
"""
from __future__ import annotations

import re

from src.utils.metrics import metrics

# 高危 SQL 关键字(黑名单): 命中即拒绝执行
FORBIDDEN_PATTERNS = [
    r"\bDROP\b",
    r"\bDELETE\b",
    r"\bINSERT\b",
    r"\bUPDATE\b",
    r"\bALTER\b",
    r"\bCREATE\b",
    r"\bTRUNCATE\b",
    r"\bGRANT\b",
    r"\bREVOKE\b",
    r"\bEXEC(?:UTE)?\b",
    r"\bMERGE\b",
    r"\bREPLACE\b",
    r"\bATTACH\b",
    r"\bDETACH\b",
    r"\bVACUUM\b",
    r"\bPRAGMA\b",
]

# 注释绕过(如 /* DROP */ 或行内注释拼接)
COMMENT_PATTERNS = [r"/\*.*?\*/", r"--.*?$", r"#.*?$"]

_FORBIDDEN_RE = re.compile("|".join(FORBIDDEN_PATTERNS), re.IGNORECASE)

# 多条语句(; 分隔)视为可疑 —— 沙箱只允许单条 SELECT
_MULTI_STMT_RE = re.compile(r";\s*(SELECT|WITH)", re.IGNORECASE)

# 美元引号字符串: $$...$$ 或 $tag$...$tag$(PostgreSQL 方言)
_DOLLAR_RE = re.compile(r"\$\$|\$[A-Za-z_][A-Za-z0-9_]*\$")


def strip_comments(sql: str) -> str:
    """去除 SQL 注释, 防止绕过黑名单(感知字符串字面量, 不破坏引号内内容)。

    处理: 单引号字符串('' 转义) / 双引号标识符("" 转义) / 美元引号($$..$$, $tag$..$tag$)
    / 行注释 -- / 块注释 /* */ / # 注释(MySQL 兼容)。
    """
    if not sql:
        return ""
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        # 单引号字符串
        if c == "'":
            j = i + 1
            while j < n:
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":  # '' 转义
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            out.append(sql[i:j])
            i = j
            continue
        # 双引号标识符
        if c == '"':
            j = i + 1
            while j < n:
                if sql[j] == '"':
                    if j + 1 < n and sql[j + 1] == '"':  # "" 转义
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            out.append(sql[i:j])
            i = j
            continue
        # 美元引号字符串
        if c == "$":
            m = _DOLLAR_RE.match(sql, i)
            if m:
                tag = m.group(0)
                end = sql.find(tag, m.end())
                if end != -1:
                    end += len(tag)
                    out.append(sql[i:end])
                    i = end
                    continue
            out.append(c)
            i += 1
            continue
        # 行注释 --
        if c == "-" and i + 1 < n and sql[i + 1] == "-":
            out.append(" ")
            while i < n and sql[i] != "\n":
                i += 1
            continue
        # 块注释 /* */
        if c == "/" and i + 1 < n and sql[i + 1] == "*":
            out.append(" ")
            i += 2
            while i + 1 < n and not (sql[i] == "*" and sql[i + 1] == "/"):
                i += 1
            i += 2
            continue
        # # 注释(MySQL 兼容)
        if c == "#":
            out.append(" ")
            while i < n and sql[i] != "\n":
                i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _mask_strings(sql: str) -> str:
    """把字符串字面量/标识符内容替换为空占位(保留引号结构), 避免关键字误判。

    例如 SELECT 'delete' FROM t -> SELECT '' FROM t, 使黑名单/多语句/代价检查
    不会被字符串内容干扰。
    """
    if not sql:
        return ""
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if c == "'":
            j = i + 1
            while j < n:
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            out.append("''")
            i = j
            continue
        if c == '"':
            j = i + 1
            while j < n:
                if sql[j] == '"':
                    if j + 1 < n and sql[j + 1] == '"':
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            out.append('""')
            i = j
            continue
        if c == "$":
            m = _DOLLAR_RE.match(sql, i)
            if m:
                tag = m.group(0)
                end = sql.find(tag, m.end())
                if end != -1:
                    out.append("''")
                    i = end + len(tag)
                    continue
            out.append(c)
            i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def is_readonly(sql: str) -> bool:
    """校验 SQL 是否为只读 SELECT。

    - 去除注释后(感知字符串)检查黑名单关键字
    - 必须以 SELECT / WITH 开头(允许前导空白)
    - 拒绝多条语句拼接
    - 字符串字面量内容先 mask, 避免关键字误判
    """
    cleaned = strip_comments(sql).strip()
    if not cleaned:
        return False
    masked = _mask_strings(cleaned).strip()

    # 必须以 SELECT/WITH 开头(只读入口)
    if not re.match(r"^(SELECT|WITH)\b", masked, re.IGNORECASE):
        return False

    # 禁止多语句拼接(避免 SELECT ...; DROP ...)
    if _MULTI_STMT_RE.search(masked):
        return False

    # 黑名单关键字
    if _FORBIDDEN_RE.search(masked):
        return False

    return True


def validate_readonly(sql: str) -> tuple[bool, str]:
    """校验入口: 返回 (是否通过, 原因)。

    >>> validate_readonly("SELECT * FROM users")
    (True, '')
    >>> validate_readonly("DROP TABLE users")
    (False, '包含危险关键字: DROP')
    """
    if not sql or not sql.strip():
        metrics.inc("tool_param_rejections_total", labels={"tool": "sql_validator", "reason": "empty"})
        return False, "SQL 为空"

    cleaned = strip_comments(sql)
    masked = _mask_strings(cleaned)  # 字符串字面量内容不参与关键字/结构判定
    match = _FORBIDDEN_RE.search(masked)
    if match:
        metrics.inc(
            "tool_param_rejections_total",
            labels={"tool": "sql_validator", "reason": "forbidden_keyword"},
        )
        return False, f"包含危险关键字: {match.group(0).strip().upper()}"

    if not re.match(r"^(SELECT|WITH)\b", masked.strip(), re.IGNORECASE):
        metrics.inc(
            "tool_param_rejections_total",
            labels={"tool": "sql_validator", "reason": "not_readonly_query"},
        )
        return False, "仅允许 SELECT / WITH 开头的只读查询"

    if _MULTI_STMT_RE.search(masked):
        metrics.inc(
            "tool_param_rejections_total",
            labels={"tool": "sql_validator", "reason": "multi_statement"},
        )
        return False, "禁止多条语句拼接"

    # CR-05 代价预判: 高危函数 / 笛卡尔积 / 全表扫描
    cost_ok, cost_reason = check_query_cost(masked)
    if not cost_ok:
        metrics.inc(
            "tool_param_rejections_total",
            labels={"tool": "sql_validator", "reason": "cost_estimation"},
        )
        return False, cost_reason

    return True, ""


# 慢查询 / 外联高危函数(CR-05): 拒绝执行
_COSTLY_FUNC_RE = __import__("re").compile(
    r"\b(pg_sleep|sleep|benchmark|copy|load_file|into\s+outfile|into\s+dumpfile|"
    r"updatexml|extractvalue|waitfor|delay)\b",
    __import__("re").IGNORECASE,
)


def check_query_cost(sql: str) -> tuple[bool, str]:
    """SQL 执行代价预判(CR-05): 返回 (是否通过, 原因)。

    拦截三类隐式高危模式:
    1. 慢查询/外联函数(pg_sleep、COPY、INTO OUTFILE、LOAD_FILE 等)
    2. 多表连接无 WHERE(笛卡尔积风险)
    3. 非聚合查询无 LIMIT(全表扫描风险)
    """
    cleaned = _mask_strings(strip_comments(sql)).strip()
    lower = cleaned.lower()

    # 1) 高危函数
    match = _COSTLY_FUNC_RE.search(cleaned)
    if match:
        return False, f"包含慢查询/外联高危函数: {match.group(0).strip().upper()}"

    # 2) 笛卡尔积: FROM 之后出现多个表且无 WHERE, 且无 JOIN...ON 连接条件
    from_part = re.split(r"\bwhere\b", lower, maxsplit=1)[0]
    from_m = re.search(r"\bfrom\b", from_part)
    if from_m:
        tables_part = from_part[from_m.end():]
        # 逗号检测只针对表列表区域: 截断到 GROUP BY/ORDER BY/LIMIT/HAVING 之前,
        # 否则 GROUP BY c.customer_id, c.customer_name 的逗号会被误判为逗号连接表(回归根因)
        tables_part = re.split(r"\b(group\s+by|order\s+by|limit|having)\b", tables_part, maxsplit=1)[0]
        has_multi_table = re.search(r"\bjoin\b", tables_part) or re.search(r",\s*\w+", tables_part)
        if has_multi_table and "where" not in lower:
            # 有连接条件(JOIN ... ON ...)的多表查询不算笛卡尔积; 仍拦截:
            #   逗号连接(隐式笛卡尔) 与 裸 JOIN(无 ON)
            joins = re.findall(r"\bjoin\b", tables_part)
            join_with_on = len(re.findall(r"\bjoin\b[^;]*?\bon\b", tables_part, re.DOTALL))
            comma_join = bool(re.search(r",\s*\w+", tables_part))
            has_safe_join = join_with_on >= len(joins) and not comma_join
            if not has_safe_join:
                return False, "多表连接缺少连接条件(笛卡尔积风险, 请使用 JOIN ... ON ... 或添加 WHERE)"

    # 3) 全表扫描: 非聚合查询且无 LIMIT
    is_aggregate = bool(re.search(r"\b(group\s+by|count\(|sum\(|avg\(|max\(|min\()", lower))
    if not is_aggregate and "limit" not in lower:
        return False, "非聚合查询无 LIMIT(全表扫描风险, 请添加 LIMIT)"

    return True, ""


# 敏感表规则(CR-07): 命中即触发人工审批(用 _ 或边界包裹敏感词, 兼容 user_phone_records 等命名)
SENSITIVE_TABLE_PATTERNS = __import__("re").compile(
    r"(?:_|^)(phone|mobile|id_?card|password|passwd|bank_card|credit_card|"
    r"account_balance|api_?key|secret)(?:_|$)",
    __import__("re").IGNORECASE,
)

# 精确敏感表清单(表名本身不含敏感词, 但含脱敏后的手机号/身份证等个人敏感信息)
# 垂直赛道: customers(客户表, phone/id_card 字段)—— 查询即触发人工审批
SENSITIVE_TABLE_NAMES: set[str] = {"customers"}


def looks_like_sql(code: str) -> bool:
    """判断代码是否为 SQL: 剥离注释与空白后以 SELECT/WITH 开头。

    兼容 `-- 注释` / `/* */` 开头的 SQL(防止被误判为 Python 执行)。
    """
    return strip_comments(code or "").strip().upper().startswith(("SELECT", "WITH"))


def find_sensitive_tables(sql: str) -> list[str]:
    """检测 SQL 引用的表名是否命中敏感表规则(CR-07)。

    命中示例: SELECT * FROM user_phone_records → ['user_phone_records']
    返回命中的表名列表(去重); 未命中返回空列表。
    """
    cleaned = strip_comments(sql).strip()
    # 提取 FROM / JOIN 之后的表名(忽略别名与库名前缀)
    tables = re.findall(r"(?:from|join)\s+([a-zA-Z_][\w.]*)", cleaned, re.IGNORECASE)
    hits: list[str] = []
    for t in tables:
        bare = t.split(".")[-1]
        if bare in SENSITIVE_TABLE_NAMES and bare not in hits:
            hits.append(bare)
            continue
        if SENSITIVE_TABLE_PATTERNS.search(bare) and bare not in hits:
            hits.append(bare)
    return hits
