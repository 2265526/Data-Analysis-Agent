"""数据源工具: 连接串解析/加密存取/按任务路由。

- resolve_db_url(data_source_id): 任务级数据源路由 —— 返回实际连接的 PostgreSQL 连接串
  (None/0/未找到 -> 主库 settings.database_url, 兼容历史任务)
- validate_db_url(url): 创建数据源时校验可连接(只读, 3s 超时)
"""
from __future__ import annotations

from typing import Optional

from src.utils.logger import get_logger
from src.utils.settings import get_settings

logger = get_logger(__name__)
settings = get_settings()


def resolve_db_url(data_source_id: int | str | None = None) -> str:
    """按数据源 ID 解析实际连接串; 无/停用/未找到 -> 主库 settings.database_url。"""
    if not data_source_id:
        return settings.database_url
    try:
        from src.api.deps import SessionLocal
        from src.models import DataSource
        from src.utils.security import decrypt

        db = SessionLocal()
        try:
            ds = db.get(DataSource, int(data_source_id))
            if ds is not None and ds.enabled:
                return decrypt(ds.db_url_enc)
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001 — 解析失败回退主库, 不阻塞
        logger.warning("resolve_db_url_failed", data_source_id=data_source_id, error=str(exc))
    return settings.database_url


def validate_db_url(url: str) -> tuple[bool, str]:
    """校验连接串可访问(只读连接 + 探测 public schema), 供创建数据源时使用。"""
    url = (url or "").strip()
    if not url.startswith(("postgresql://", "postgres://")):
        return False, "连接串必须以 postgresql:// 开头"
    try:
        import psycopg2

        from src.tools.schema_provider import parse_db_url

        conn = psycopg2.connect(**parse_db_url(url), connect_timeout=3)
        try:
            cur = conn.cursor()
            cur.execute("SELECT 1 FROM information_schema.tables LIMIT 1")
            cur.fetchone()
            cur.close()
        finally:
            conn.close()
        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, f"连接失败: {str(exc)[:200]}"


def test_data_source_connection(db_url: str) -> tuple[bool, str]:
    """连接性测试(与 validate 相同, 供前端"测试连接"按钮)。"""
    return validate_db_url(db_url)


def build_db_url(host: str, port: int | str, dbname: str, user: str, password: str) -> str:
    """按分字段拼接连接串(前端友好表单 -> 标准 URL)。

    host 支持 "host:port" 组合或 host 与 port 分开两种写法; 端口缺省 5432。
    """
    from urllib.parse import quote

    host = (host or "").strip()
    if ":" in host and not port:
        host, port = host.rsplit(":", 1)
    dbname = (dbname or "").strip()
    user = (user or "").strip()
    password = password or ""
    if not host or not dbname:
        raise ValueError("host 与 dbname 必填")
    return (
        f"postgresql://{quote(user, safe='')}:{quote(password, safe='')}"
        f"@{host}:{int(port or 5432)}/{quote(dbname, safe='')}"
    )


def _load_schema_dict() -> dict[tuple[str, str], str]:
    """加载数据字典(DB 表): {(table, column): cn}; column='' 表示表级。"""
    try:
        from src.api.deps import SessionLocal
        from src.models import SchemaDict

        db = SessionLocal()
        try:
            rows = db.query(SchemaDict).all()
            return {(r.table_name, r.column_name or ""): r.cn_name for r in rows}
        finally:
            db.close()
    except Exception:  # noqa: BLE001 — 字典表不可用不影响
        return {}


def _table_cn(table: str, db_comment: str | None = None, schema_dict: dict | None = None) -> str:
    """表中文名: 数据字典 > DB COMMENT > 内置映射。"""
    if schema_dict and (table, "") in schema_dict:
        return schema_dict[(table, "")]
    if db_comment:
        return db_comment
    return _TABLE_CN.get(table, "")


def _column_cn(table: str, column: str, db_comment: str | None = None, schema_dict: dict | None = None) -> str:
    """列中文名: 数据字典 > DB COMMENT > 内置映射。"""
    if schema_dict and (table, column) in schema_dict:
        return schema_dict[(table, column)]
    if db_comment:
        return db_comment
    return _COLUMN_CN.get(table, {}).get(column, "")


# 默认库(演示供应链 + 平台业务表)中文名映射 —— 依据 data/数据库设计.docx
# 优先级: 数据字典(schema_dict 表) > DB COMMENT > 本内置映射
_TABLE_CN = {
    # 演示业务(分析目标库)
    "suppliers": "供应商表",
    "products": "商品表",
    "customers": "客户表",
    "orders": "订单表",
    "order_items": "订单明细表",
    "logistics": "物流表",
    # 平台业务
    "users": "用户表",
    "tasks": "分析任务表",
    "task_node_runs": "节点运行记录表",
    "audit_logs": "审计日志表",
    "model_routes": "模型路由表",
    "cost_records": "成本记录表",
    "metric_definitions": "指标定义表",
    "query_runs": "SQL血缘记录表",
    "chat_sessions": "会话表",
    "chat_messages": "会话消息表",
    # 产品增强新增
    "data_policy_rules": "数据权限规则表",
    "data_sources": "数据源表",
    "scheduled_tasks": "定时任务表",
    "notifications": "通知表",
    "schema_dict": "数据字典表",
    # 框架支撑(LangGraph 自动维护)
    "checkpoints": "检查点表",
    "checkpoint_blobs": "检查点对象表",
    "checkpoint_writes": "检查点写通道表",
    "checkpoint_migrations": "迁移版本表",
}

_COLUMN_CN = {
    # ---------- 演示业务 ----------
    "suppliers": {
        "supplier_id": "供应商ID", "supplier_name": "供应商名称", "contact_person": "联系人",
        "contact_phone": "联系电话", "province": "省份", "city": "城市", "address": "地址",
        "credit_rating": "信用评级", "created_at": "创建时间",
    },
    "products": {
        "product_id": "商品ID", "product_name": "商品名称", "category_l1": "一级品类",
        "category_l2": "二级品类", "brand": "品牌", "unit_cost": "单位成本",
        "unit_price": "单价", "supplier_id": "供应商ID", "created_at": "创建时间",
    },
    "customers": {
        "customer_id": "客户ID", "customer_name": "客户姓名", "phone": "手机号",
        "id_card": "身份证号", "province": "省份", "city": "城市", "address": "地址",
        "customer_segment": "客户分组", "registered_at": "注册时间",
    },
    "orders": {
        "order_id": "订单号", "customer_id": "客户ID", "order_date": "下单日期",
        "total_amount": "订单金额", "payment_method": "支付方式", "order_status": "订单状态",
        "notes": "备注", "created_at": "创建时间",
    },
    "order_items": {
        "item_id": "明细ID", "order_id": "订单号", "product_id": "商品ID",
        "quantity": "数量", "unit_price": "单价", "discount": "折扣",
        "total_item_amount": "明细金额",
    },
    "logistics": {
        "logistics_id": "物流ID", "order_id": "订单号", "carrier": "承运商",
        "tracking_number": "物流单号", "from_city": "发货城市", "to_city": "到达城市",
        "ship_date": "发货日期", "delivery_date": "送达日期", "logistics_status": "物流状态",
        "transit_days": "运输天数",
    },
    # ---------- 平台业务 ----------
    "users": {
        "id": "用户ID", "username": "登录名", "password_hash": "密码哈希",
        "roles": "角色列表", "created_at": "创建时间", "updated_at": "更新时间",
    },
    "tasks": {
        "id": "任务ID", "user_query": "分析需求", "session_id": "会话ID",
        "data_source_id": "数据源ID", "created_by": "提交者", "status": "状态",
        "progress": "进度文案", "current_node": "当前节点", "progress_detail": "进度详情",
        "progress_percent": "进度百分比", "is_archived": "已归档", "result_path": "报告路径",
        "summary": "结果摘要", "error_log": "错误日志", "retry_count": "重试次数",
        "created_at": "创建时间", "updated_at": "更新时间",
    },
    "task_node_runs": {
        "id": "记录ID", "task_id": "任务ID", "run_seq": "执行序号", "node_name": "节点名",
        "model_name": "模型名", "prompt_tokens": "提示Token", "completion_tokens": "补全Token",
        "duration_ms": "耗时毫秒", "cost_amount": "成本金额", "status": "状态",
        "error": "错误信息", "created_at": "创建时间",
    },
    "audit_logs": {
        "id": "日志ID", "task_id": "任务ID", "event": "事件类型", "actor": "操作者",
        "node_name": "来源节点", "client_ip": "客户端IP", "user_agent": "用户代理",
        "detail": "详情", "created_at": "时间",
    },
    "model_routes": {
        "id": "配置ID", "node": "节点名", "model_name": "模型名",
        "price_per_1k_prompt": "提示单价", "price_per_1k_completion": "补全单价",
        "priority": "优先级", "enabled": "启停", "created_at": "创建时间",
        "updated_at": "更新时间",
    },
    "cost_records": {
        "id": "记录ID", "task_id": "任务ID", "run_id": "节点运行ID", "user_id": "用户ID",
        "node_name": "节点", "model_name": "模型", "cost_type": "成本类型",
        "cost_amount": "成本金额", "prompt_tokens": "提示Token", "completion_tokens": "补全Token",
        "created_at": "创建时间",
    },
    "metric_definitions": {
        "id": "指标ID", "name_en": "英文标识", "name_cn": "中文名", "alias": "别名",
        "description": "说明", "agg": "聚合方式", "expr": "口径表达式", "filter": "默认过滤",
        "unit": "单位", "source_tables": "来源表", "category": "分类", "status": "状态",
        "created_at": "创建时间", "updated_at": "更新时间",
    },
    "query_runs": {
        "id": "记录ID", "task_id": "任务ID", "run_order": "步骤序号", "sql_text": "SQL全文",
        "tables": "涉及表", "status": "状态", "rows_returned": "返回行数",
        "duration_ms": "耗时毫秒", "created_at": "创建时间",
    },
    "chat_sessions": {
        "id": "会话ID", "owner": "所属用户", "title": "标题", "is_pinned": "置顶",
        "created_at": "创建时间", "updated_at": "更新时间",
    },
    "chat_messages": {
        "id": "消息ID", "session_id": "会话ID", "role": "角色", "type": "类型",
        "content": "消息正文", "task_id": "关联任务", "report_snapshot": "报告快照",
        "has_pdf": "有PDF", "has_board": "有看板", "created_at": "创建时间",
    },
    # ---------- 产品增强新增 ----------
    "data_policy_rules": {
        "id": "规则ID", "target_type": "目标类型", "target_name": "目标名称",
        "table_name": "业务表", "row_filter": "行级过滤", "col_access": "列访问控制",
        "mask_expression": "脱敏表达式", "enabled": "启停", "created_by": "创建人",
        "created_at": "创建时间", "updated_at": "更新时间",
    },
    "data_sources": {
        "id": "数据源ID", "name": "名称", "db_url_enc": "加密连接串",
        "tables_whitelist": "表白名单", "description": "说明", "enabled": "启停",
        "created_by": "创建人", "created_at": "创建时间", "updated_at": "更新时间",
    },
    "scheduled_tasks": {
        "id": "任务ID", "name": "名称", "query": "分析需求", "cron": "调度表达式",
        "data_source_id": "数据源", "owner": "创建人", "schedule_type": "频率类型",
        "schedule_time": "执行时间", "schedule_weekday": "星期", "enabled": "启停",
        "last_run_at": "上次运行", "next_run_at": "下次运行", "created_at": "创建时间",
    },
    "notifications": {
        "id": "通知ID", "user": "接收人", "title": "标题", "content": "内容",
        "task_id": "任务ID", "kind": "类型", "read": "已读", "created_at": "创建时间",
    },
    "schema_dict": {
        "id": "记录ID", "table_name": "表名", "column_name": "列名", "cn_name": "中文名",
        "created_by": "创建人", "created_at": "创建时间", "updated_at": "更新时间",
    },
    # ---------- 框架支撑 ----------
    "checkpoints": {
        "thread_id": "线程ID", "checkpoint_ns": "命名空间", "checkpoint_id": "检查点ID",
        "parent_checkpoint_id": "父检查点", "type": "序列化类型", "checkpoint": "状态快照",
        "metadata": "元数据",
    },
    "checkpoint_blobs": {
        "thread_id": "线程ID", "checkpoint_ns": "命名空间", "channel": "通道名",
        "version": "版本", "type": "序列化类型", "blob": "二进制快照",
    },
    "checkpoint_writes": {
        "thread_id": "线程ID", "checkpoint_ns": "命名空间", "checkpoint_id": "检查点ID",
        "task_id": "任务ID", "idx": "序号", "channel": "通道名", "type": "类型",
        "blob": "二进制快照", "task_path": "任务路径",
    },
    "checkpoint_migrations": {"v": "迁移版本"},
}


def fetch_schema_tables(db_url: str, limit: int = 60) -> list[dict]:
    """拉取数据源 public schema 的表与列清单(供管理员选择/白名单勾选)。

    Returns: [{"name": "orders", "comment": "订单表",
               "columns": [{"name": "order_id", "data_type": "text", "comment": "订单号"}, ...]}, ...]
    表/列中文名优先 DB COMMENT(pg_description), 缺省用内置映射; 连接失败抛异常。
    """
    import psycopg2

    from src.tools.schema_provider import parse_db_url

    schema_dict = _load_schema_dict()

    conn = psycopg2.connect(**parse_db_url(db_url), connect_timeout=3)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT c.relname, a.attname, pg_catalog.format_type(a.atttypid, a.atttypmod),
                   obj_description(c.oid), col_description(c.oid, a.attnum)
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            JOIN pg_attribute a ON a.attrelid = c.oid
            WHERE n.nspname = 'public' AND c.relkind = 'r'
              AND a.attnum > 0 AND NOT a.attisdropped
            ORDER BY c.relname, a.attnum
            LIMIT %s
            """,
            (limit * 60,),
        )
        rows = cur.fetchall()
        tables: dict[str, dict] = {}
        for table, column, dtype, t_comment, c_comment in rows:
            if table not in tables:
                tables[table] = {
                    "name": table,
                    "comment": _table_cn(table, t_comment, schema_dict),
                    "columns": [],
                }
            tables[table]["columns"].append({
                "name": column,
                "data_type": (dtype or "text")[:40],
                "comment": _column_cn(table, column, c_comment, schema_dict),
            })
        cur.close()
        return list(tables.values())[:limit]
    finally:
        conn.close()
