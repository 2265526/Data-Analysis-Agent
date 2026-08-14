"""P0/P1 产品增强功能测试: 数据源 / 审计导出 / 指标管理 / 报告溯源 / 定时任务+通知。

- 数据源: resolve_db_url 主库回退、CRUD(连接串加密落库、列表不回显)、校验注入
- 审计: export CSV 内容与筛选
- 指标: CRUD + 下线(deprecated)+ 热重载调用
- 溯源: lineage 列表 + rerun 权限约束
- 定时任务: CRUD + cron 校验 + 通知列表/已读
"""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.models import (
    AuditLog,
    Base,
    DataPolicyRule,
    DataSource,
    MetricDefinition,
    Notification,
    QueryRun,
    ScheduledTask,
    Task,
)


@pytest.fixture
def api_env(monkeypatch):
    """TestClient + admin 身份 + sqlite DB。"""
    from src.api.auth import User, get_current_user
    from src.api.deps import get_db

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    sm = sessionmaker(bind=engine, autocommit=False, autoflush=False, expire_on_commit=False)

    # 让业务代码内的 SessionLocal 也指向 sqlite(如 resolve_db_url / scheduler)
    monkeypatch.setattr("src.api.deps.SessionLocal", sm)

    def _get_db():
        db = sm()
        try:
            yield db
        finally:
            db.close()

    from main import app

    app.dependency_overrides[get_db] = _get_db

    def _admin():
        return User(id="1", name="admin", roles=["user", "approver", "admin"])

    app.dependency_overrides[get_current_user] = _admin
    client = TestClient(app)
    yield client, sm
    app.dependency_overrides.clear()
    client.close()


def _add_task(sm, query="统计最近7天销售额", created_by="admin", status="completed", **kw) -> Task:
    db = sm()
    try:
        t = Task(id=str(uuid.uuid4()), user_query=query, status=status, created_by=created_by, **kw)
        db.add(t)
        db.commit()
        return t
    finally:
        db.close()


# ---------- 数据源 ----------

def test_data_source_crud_encrypts_url(api_env, monkeypatch):
    client, sm = api_env
    # 校验连接串: 打桩为成功(避免依赖真实 PG)
    monkeypatch.setattr(
        "src.tools.data_source.validate_db_url", lambda url: (True, "")
    )
    resp = client.post("/api/v1/data-sources", json={
        "name": "测试库", "db_url": "postgresql://u:p@h:5432/db",
        "tables_whitelist": ["orders", "customers"], "description": "d", "enabled": True,
    })
    assert resp.status_code == 200, resp.text
    sid = resp.json()["id"]
    # 列表不回显连接串
    lst = client.get("/api/v1/data-sources").json()
    assert lst["total"] == 1
    assert "db_url" not in lst["sources"][0]
    # 落库为密文
    db = sm()
    try:
        ds = db.get(DataSource, sid)
        assert ds.db_url_enc != "postgresql://u:p@h:5432/db"
    finally:
        db.close()
    # 更新 + 删除
    assert client.put(f"/api/v1/data-sources/{sid}", json={"enabled": False}).status_code == 200
    assert client.delete(f"/api/v1/data-sources/{sid}").status_code == 200


def test_resolve_db_url_fallback(api_env, monkeypatch):
    from src.tools.data_source import resolve_db_url
    from src.utils.settings import get_settings

    # 无 id -> 主库; 未知 id -> 主库
    assert resolve_db_url(None) == get_settings().database_url
    assert resolve_db_url(9999) == get_settings().database_url

    # 存在启用的数据源 -> 解密连接串
    db = api_env[1]()
    try:
        from src.utils.security import encrypt

        ds = DataSource(name="ds1", db_url_enc=encrypt("postgresql://x:x@h:1/db"), enabled=True)
        db.add(ds)
        db.commit()
        sid = ds.id
    finally:
        db.close()
    assert resolve_db_url(sid) == "postgresql://x:x@h:1/db"


# ---------- 审计导出 ----------

def test_audit_export_csv(api_env):
    client, sm = api_env
    t = _add_task(sm, query="查询1", status="completed")
    db = sm()
    try:
        db.add(AuditLog(task_id=t.id, event="task_submitted", actor="admin"))
        db.commit()
    finally:
        db.close()
    resp = client.get("/api/v1/admin/audit-logs/export")
    assert resp.status_code == 200, resp.text
    body = resp.text
    assert "查询1" in body
    assert resp.headers["content-type"].startswith("text/csv")
    # 普通用户 403
    from src.api.auth import get_current_user

    client.app.dependency_overrides[get_current_user] = lambda: __import__(
        "src.api.auth", fromlist=["User"]
    ).User(id="2", name="alice", roles=["user"])
    assert client.get("/api/v1/admin/audit-logs/export").status_code == 403


# ---------- 指标管理 ----------

def test_metric_crud_and_deprecate(api_env):
    client, sm = api_env
    resp = client.post("/api/v1/admin/metric-definitions", json={
        "name_en": "new_metric", "name_cn": "新指标", "agg": "sum", "expr": "oi.total_item_amount",
        "alias": ["新销售额"], "unit": "元", "source_tables": ["orders"],
    })
    assert resp.status_code == 200, resp.text
    mid = resp.json()["id"]
    # 更新
    upd = client.put(f"/api/v1/admin/metric-definitions/{mid}", json={"name_cn": "新指标2"})
    assert upd.status_code == 200 and upd.json()["name_cn"] == "新指标2"
    # 下线
    dele = client.delete(f"/api/v1/admin/metric-definitions/{mid}")
    assert dele.status_code == 200
    db = sm()
    try:
        assert db.get(MetricDefinition, mid).status == "deprecated"
    finally:
        db.close()
    # 非法 agg -> 422
    bad = client.post("/api/v1/admin/metric-definitions", json={
        "name_en": "bad", "name_cn": "坏", "agg": "evil", "expr": "x",
    })
    assert bad.status_code == 422


# ---------- 报告溯源 ----------

def test_lineage_and_rerun(api_env, monkeypatch):
    client, sm = api_env
    t = _add_task(sm, status="completed")
    db = sm()
    try:
        db.add(QueryRun(task_id=t.id, run_order=1, sql_text="SELECT * FROM orders", tables=["orders"], rows_returned=3))
        db.commit()
    finally:
        db.close()
    # 血缘列表
    lin = client.get(f"/api/v1/tasks/{t.id}/lineage")
    assert lin.status_code == 200 and lin.json()["total"] == 1

    # rerun: 打桩沙箱执行 + 数据权限放行(无规则)
    monkeypatch.setattr(
        "src.sandbox.docker_sandbox.run_in_sandbox",
        lambda sql, backend="auto", db_url=None: {"status": "success", "output": "rows=3\norder_id\n('1',)", "error": "", "row_count": 3},
    )
    rr = client.post(f"/api/v1/tasks/{t.id}/query-runs/1/rerun")
    assert rr.status_code == 200, rr.text
    assert rr.json()["ok"] is True and rr.json()["row_count"] == 3

    # 数据权限 deny -> 403
    db = sm()
    try:
        db.add(DataPolicyRule(target_type="role", target_name="user", table_name="orders",
                              col_access={"total_amount": "deny"}))
        db.commit()
    finally:
        db.close()
    db2 = sm()
    try:
        db2.add(QueryRun(task_id=t.id, run_order=2, sql_text="SELECT total_amount FROM orders"))
        db2.commit()
        run2 = db2.query(QueryRun).filter(QueryRun.run_order == 2).first().id
    finally:
        db2.close()
    rr2 = client.post(f"/api/v1/tasks/{t.id}/query-runs/{run2}/rerun")
    assert rr2.status_code == 403


# ---------- 定时任务 + 通知 ----------

def test_scheduled_task_crud_and_notifications(api_env, monkeypatch):
    client, sm = api_env
    # 热同步打桩(避免启动真实调度器)
    monkeypatch.setattr("src.tools.scheduler.refresh_schedule", lambda: None)
    resp = client.post("/api/v1/scheduled-tasks", json={
        "name": "每日日报", "query": "统计最近7天销售额", "cron": "0 9 * * *",
        "notify_users": ["zhangsan"],  # admin 可设置推送范围
        "data_source_ids": [1, 2],     # admin 可跨多数据源
    })
    assert resp.status_code == 200, resp.text
    sid = resp.json()["id"]
    assert resp.json()["owner"] == "admin"
    assert resp.json()["notify_users"] == ["zhangsan"]
    assert resp.json()["data_source_ids"] == [1, 2]
    # 非法 cron -> 422
    bad = client.post("/api/v1/scheduled-tasks", json={
        "name": "x", "query": "yy", "cron": "not-a-cron",
    })
    assert bad.status_code == 422
    # 更新启停
    upd = client.put(f"/api/v1/scheduled-tasks/{sid}", json={"enabled": False})
    assert upd.status_code == 200 and upd.json()["enabled"] is False
    # 通知: 给 admin 写一条
    db = sm()
    try:
        db.add(Notification(user="admin", title="定时任务完成", content="ok", task_id="t1"))
        db.commit()
    finally:
        db.close()
    nlist = client.get("/api/v1/notifications")
    assert nlist.status_code == 200 and nlist.json()["unread"] == 1
    nid = nlist.json()["notifications"][0]["id"]
    read = client.post("/api/v1/notifications/read", json={"id": nid})
    assert read.status_code == 200
    assert client.get("/api/v1/notifications").json()["unread"] == 0


def test_scheduled_task_regular_user_scope(api_env, monkeypatch):
    """普通用户: 只能看/改自己的任务; 不能设置推送范围(被忽略); 不能改别人的(403)。"""
    from src.api.auth import User, get_current_user

    client, sm = api_env
    monkeypatch.setattr("src.tools.scheduler.refresh_schedule", lambda: None)
    # 管理员创建一个任务(owner=admin)
    admin_task = client.post("/api/v1/scheduled-tasks", json={
        "name": "admin日报", "query": "统计", "schedule_type": "daily",
    }).json()["id"]
    # 切到 alice(普通用户)
    client.app.dependency_overrides[get_current_user] = lambda: User(id="2", name="alice", roles=["user"])
    # alice 创建: 传 notify_users/数据源被忽略; owner=自己
    resp = client.post("/api/v1/scheduled-tasks", json={
        "name": "alice自建", "query": "统计", "schedule_type": "daily",
        "notify_users": ["bob"], "data_source_ids": [1, 2],
    })
    assert resp.status_code == 200
    own = resp.json()["id"]
    assert resp.json()["owner"] == "alice"
    assert resp.json()["notify_users"] == []
    assert resp.json()["data_source_ids"] == []
    # 列表只看自己的
    lst = client.get("/api/v1/scheduled-tasks").json()
    assert lst["total"] == 1 and lst["tasks"][0]["name"] == "alice自建"
    # 改/删别人的任务 -> 403
    assert client.put(f"/api/v1/scheduled-tasks/{admin_task}", json={"name": "hack"}).status_code == 403
    assert client.delete(f"/api/v1/scheduled-tasks/{admin_task}").status_code == 403
    # 改自己的 -> 200
    assert client.put(f"/api/v1/scheduled-tasks/{own}", json={"name": "alice日报改"}).status_code == 200


# ---------- 非程序员视角改造: cron 翻译 / 分字段连接 / schema 表清单 ----------

def test_build_cron_translation():
    from src.tools.scheduler import build_cron, cron_description

    assert build_cron("daily", "09:30") == "30 9 * * *"
    assert build_cron("weekly", "08:00", "1") == "0 8 * * 1"          # 每周一 08:00
    assert build_cron("monthly", "10:15", "1", 5) == "15 10 5 * *"    # 每月5号
    assert build_cron("custom", "09:00", "1", 1, "0 8 * * 1") == "0 8 * * 1"
    assert cron_description("30 9 * * *") == "每天 09:30"
    assert cron_description("0 8 * * 1") == "每周周一 08:00"
    assert cron_description("15 10 5 * *") == "每月 5 号 10:15"


def test_data_source_conn_fields_creation(api_env, monkeypatch):
    """分字段表单(host/port/dbname/user/password)创建数据源, 后端拼接加密。"""
    client, sm = api_env
    monkeypatch.setattr(
        "src.tools.data_source.validate_db_url", lambda url: (True, "")
    )
    resp = client.post("/api/v1/data-sources", json={
        "name": "分字段库",
        "conn_fields": {"host": "db.example.com", "port": 5432, "dbname": "mydb",
                        "user": "readonly", "password": "secret"},
        "enabled": True,
    })
    assert resp.status_code == 200, resp.text
    item = resp.json()
    assert item["conn_fields"]["host"] == "db.example.com"
    assert "password" not in item["conn_fields"]  # 不回显密码
    # 落库为密文且包含拼接后的 URL
    db = sm()
    try:
        from src.utils.security import decrypt

        ds = db.query(DataSource).filter(DataSource.name == "分字段库").first()
        assert "db.example.com" in decrypt(ds.db_url_enc)
    finally:
        db.close()


def test_schema_tables_endpoint(api_env, monkeypatch):
    client, _ = api_env
    monkeypatch.setattr(
        "src.tools.data_source.fetch_schema_tables",
        lambda url: [{"name": "orders", "columns": [{"name": "order_id", "data_type": "text"}]}],
    )
    monkeypatch.setattr("src.tools.data_source.validate_db_url", lambda url: (True, ""))
    resp = client.post("/api/v1/admin/schema-tables", json={
        "conn_fields": {"host": "h", "port": 5432, "dbname": "d", "user": "u", "password": ""},
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["tables"][0]["name"] == "orders"


def test_scheduled_task_weekly_translation(api_env, monkeypatch):
    """每周一 08:00 -> cron 0 8 * * 1; 列表返回业务描述。"""
    client, sm = api_env
    monkeypatch.setattr("src.tools.scheduler.refresh_schedule", lambda: None)
    resp = client.post("/api/v1/scheduled-tasks", json={
        "name": "周报", "query": "统计上周销售", "schedule_type": "weekly",
        "schedule_time": "08:00", "schedule_weekday": "1",
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["cron"] == "0 8 * * 1"
    assert resp.json()["cron_desc"] == "每周周一 08:00"


# ---------- 数据字典 ----------

def test_schema_dict_crud_and_priority(api_env, monkeypatch):
    """数据字典: 中文名优先级 字典 > DB comment > 内置映射。"""
    from src.models import SchemaDict

    client, sm = api_env
    # 创建: 覆盖内置映射(orders -> 自定义中文名)
    resp = client.post("/api/v1/admin/schema-dict", json={
        "table_name": "orders", "column_name": "", "cn_name": "我的订单表",
    })
    assert resp.status_code == 200, resp.text
    sid = resp.json()["id"]
    # 列表搜索
    lst = client.get("/api/v1/admin/schema-dict", params={"keyword": "我的"})
    assert lst.status_code == 200 and lst.json()["total"] == 1
    # 更新
    upd = client.put(f"/api/v1/admin/schema-dict/{sid}", json={"cn_name": "订单主表"})
    assert upd.status_code == 200 and upd.json()["cn_name"] == "订单主表"
    # 重复创建 -> 409
    dup = client.post("/api/v1/admin/schema-dict", json={
        "table_name": "orders", "column_name": "", "cn_name": "x",
    })
    assert dup.status_code == 409
    # fetch_schema_tables 合并优先级: 字典 > 内置映射
    db = sm()
    try:
        db.add(SchemaDict(table_name="orders", column_name="total_amount", cn_name="销售总额", created_by="admin"))
        db.commit()
    finally:
        db.close()
    monkeypatch.setattr(
        "src.tools.data_source._load_schema_dict",
        lambda: {("orders", ""): "订单主表", ("orders", "total_amount"): "销售总额"},
    )
    ts = dp_fetch_schema_tables()
    orders = next(t for t in ts if t["name"] == "orders")
    assert orders["comment"] == "订单主表"  # 字典覆盖内置"订单表"
    amt = next(c for c in orders["columns"] if c["name"] == "total_amount")
    assert amt["comment"] == "销售总额"
    # 删除
    dele = client.delete(f"/api/v1/admin/schema-dict/{sid}")
    assert dele.status_code == 200


def dp_fetch_schema_tables():
    """复用 fetch_schema_tables 真实主库(数据字典加载被打桩)。"""
    from src.tools.data_source import fetch_schema_tables
    from src.utils.settings import get_settings

    return fetch_schema_tables(get_settings().database_url)


def test_permanent_approval_api(api_env, monkeypatch):
    """定时任务永久审批: 批准留痕审计; 拒绝停用+通知创建人。"""
    from src.models import Notification

    client, sm = api_env
    monkeypatch.setattr("src.tools.scheduler.refresh_schedule", lambda: None)
    s = client.post("/api/v1/scheduled-tasks", json={
        "name": "敏感日报", "query": "统计客户消费", "schedule_type": "daily",
    }).json()
    assert s["approval_status"] == "pending"
    # 拒绝 -> 停用 + 通知
    rej = client.post(f"/api/v1/admin/scheduled-tasks/{s['id']}/permanent-approval", json={"approved": False})
    assert rej.status_code == 200
    assert rej.json()["approval_status"] == "rejected"
    assert rej.json()["enabled"] is False
    # 再批准 -> approved + 审计
    appr = client.post(f"/api/v1/admin/scheduled-tasks/{s['id']}/permanent-approval", json={"approved": True})
    assert appr.status_code == 200
    assert appr.json()["approval_status"] == "approved"
    assert appr.json()["enabled"] is True
    db = sm()
    try:
        from src.models import AuditLog

        evs = db.query(AuditLog).filter(AuditLog.event.like("scheduled_permanent_%")).all()
        assert len(evs) == 2
        notifs = db.query(Notification).filter(Notification.title == "定时任务被拒绝").count()
        assert notifs >= 1
    finally:
        db.close()


def test_chat_message_output_flags_dynamic(api_env, monkeypatch):
    """回归: 历史会话消息的 has_pdf/has_board 必须按任务当前状态动态计算,
    不能依赖落库快照(旧前端未传 has_pdf 时快照失真 -> 刷新后下载/看板按钮丢失)。"""
    import psycopg2

    from src.tools.schema_provider import parse_db_url
    from src.utils.settings import get_settings

    client, sm = api_env
    monkeypatch.setattr("src.tools.scheduler.refresh_schedule", lambda: None)
    # 建一个真实任务(沙箱 SQL 执行, 产生 result_path + board.json)
    tid = client.post("/api/v1/tasks", json={
        "query": "统计各品类上周的销售额是多少",
    }).json()["task_id"]
    for _ in range(60):
        st = client.get(f"/api/v1/tasks/{tid}/status").json()
        if st["status"] in ("completed", "failed", "awaiting_approval"):
            break
    assert st["status"] == "completed"
    # 模拟旧前端落库失真: 手动造一条 task 消息, 快照 has_pdf/has_board = false
    db = sm()
    try:
        from src.models import ChatMessage, ChatSession

        sess = ChatSession(title="测试", owner="admin")
        db.add(sess)
        db.commit()
        db.refresh(sess)
        msg = ChatMessage(
            session_id=sess.id, role="assistant", type="task",
            content="snapshot", task_id=tid, has_pdf=False, has_board=False,
        )
        db.add(msg)
        db.commit()
    finally:
        db.close()
    # 历史消息接口应动态恢复
    from src.api.routes import _msg_has_pdf, _msg_has_board

    db = sm()
    try:
        assert _msg_has_pdf(tid, db, fallback=False) is True
        assert _msg_has_board(tid, db, fallback=False) is True
    finally:
        db.close()
