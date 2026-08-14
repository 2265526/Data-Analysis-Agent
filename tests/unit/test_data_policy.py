"""数据级权限单元测试: 策略合并 / SQL 改写引擎 / executor 强制 / 管理 API。

- 策略加载与合并: 角色+用户(用户优先)、多角色列取最宽松、行过滤 AND
- SQL 改写: 行过滤注入(主查询/子查询)、列掩码、deny 拒绝、SELECT * 展开
- executor: 权限拒绝抛 PolicyDeniedError(任务直接 failed, 不回 coder)
- 管理 API: admin CRUD + 非 admin 403
"""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.models import Base, DataPolicyRule
from src.tools import data_policy as dp
from src.tools.data_policy import TablePolicy


@pytest.fixture
def dp_env(monkeypatch):
    """sqlite 环境: 替换 SessionLocal, 提供建规则辅助。"""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    sm = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    monkeypatch.setattr("src.api.deps.SessionLocal", sm)

    def add_rule(target_type, target_name, table, row_filter=None, col_access=None,
                 mask_expression=None, enabled=True) -> DataPolicyRule:
        db = sm()
        try:
            r = DataPolicyRule(
                target_type=target_type, target_name=target_name, table_name=table,
                row_filter=row_filter, col_access=col_access or {},
                mask_expression=mask_expression, enabled=enabled,
            )
            db.add(r)
            db.commit()
            return r
        finally:
            db.close()

    return sm, add_rule


# ---------- 策略加载与合并 ----------

def test_no_rules_means_default_allow(dp_env):
    sm, add_rule = dp_env
    pols = dp.load_effective_policies("alice", ["user"])
    assert pols == {}


def test_user_policy_overrides_role(dp_env):
    sm, add_rule = dp_env
    add_rule("role", "user", "customers", col_access={"phone": "mask"})
    add_rule("user", "alice", "customers", col_access={"phone": "allow"})
    pols = dp.load_effective_policies("alice", ["user"])
    assert pols["customers"].col_access == {"phone": "allow"}  # 用户级覆盖角色级


def test_multi_role_loosest_column_merge(dp_env):
    sm, add_rule = dp_env
    # user 角色 mask, approver 角色 allow -> 合并结果 allow(任一 allow 即 allow)
    add_rule("role", "user", "customers", col_access={"phone": "mask", "id_card": "mask"})
    add_rule("role", "approver", "customers", col_access={"phone": "allow"})
    pols = dp.load_effective_policies("alice", ["user", "approver"])
    ca = pols["customers"].col_access
    assert ca["phone"] == "allow"   # 宽松豁免
    assert ca["id_card"] == "mask"  # 只有 user 规则覆盖


def test_row_filters_anded_across_roles(dp_env):
    sm, add_rule = dp_env
    add_rule("role", "user", "orders", row_filter="order_date >= now() - interval '90 days'")
    add_rule("role", "approver", "orders", row_filter="total_amount > 0")
    pols = dp.load_effective_policies("alice", ["user", "approver"])
    assert len(pols["orders"].row_filters) == 2  # AND 语义(交集最严格)


def test_disabled_rule_ignored(dp_env):
    sm, add_rule = dp_env
    add_rule("role", "user", "customers", col_access={"phone": "mask"}, enabled=False)
    pols = dp.load_effective_policies("alice", ["user"])
    assert "customers" not in pols


# ---------- SQL 改写引擎 ----------

def _rewrite(pols: dict, sql: str) -> tuple[str | None, str | None]:
    import sqlglot
    from sqlglot.optimizer.scope import traverse_scope

    ast = sqlglot.parse_one(sql, read="postgres")
    for scope in traverse_scope(ast):
        dp._apply_column_policy_on_select(scope, pols)
    dp._apply_row_filters(ast, pols)
    return ast.sql(dialect="postgres"), None


def test_row_filter_injected_main_and_subquery():
    pols = {"orders": TablePolicy("orders", row_filters=["order_date >= now() - interval '90 days'"])}
    out, _ = _rewrite(pols, "SELECT o.order_id FROM orders o WHERE o.total_amount > 100")
    assert "o.order_date >= CURRENT_TIMESTAMP" in out
    out2, _ = _rewrite(
        pols,
        "SELECT o.order_id FROM orders o WHERE o.total_amount > (SELECT avg(total_amount) FROM orders)",
    )
    assert out2.count("CURRENT_TIMESTAMP") == 2  # 主查询 + 子查询都注入


def test_column_mask_replaced():
    pols = {"customers": TablePolicy("customers", col_access={"phone": "mask", "id_card": "mask"})}
    out, _ = _rewrite(pols, "SELECT c.phone, c.id_card, c.customer_name FROM customers c")
    assert "SELECT '***', '***', c.customer_name FROM customers AS c" in out


def test_custom_mask_expression():
    pols = {"customers": TablePolicy(
        "customers", col_access={"phone": "mask"},
        mask_expr="substr(phone,1,3) || '****' || substr(phone,8,4)",
    )}
    out, _ = _rewrite(pols, "SELECT c.phone FROM customers c")
    # sqlglot 将 substr(x,1,3) 规范化为 SUBSTRING(x FROM 1 FOR 3)
    assert "c.phone" in out and "'****'" in out


def test_deny_column_rejected():
    pols = {"customers": TablePolicy("customers", col_access={"id_card": "deny"})}
    with pytest.raises(dp.PolicyViolation, match="deny"):
        _rewrite(pols, "SELECT customer_id, id_card FROM customers")


def test_ambiguous_column_rejected():
    pols = {"order_items": TablePolicy("order_items", col_access={"unit_price": "deny"})}
    with pytest.raises(dp.PolicyViolation, match="无法确定所属表"):
        _rewrite(pols, "SELECT unit_price FROM order_items oi JOIN products p ON p.product_id = oi.product_id")


def test_single_table_unqualified_column_masked():
    pols = {"customers": TablePolicy("customers", col_access={"phone": "mask"})}
    out, _ = _rewrite(pols, "SELECT phone FROM customers")
    assert "SELECT '***' FROM customers" in out


def test_star_expansion_masks_and_drops_deny(monkeypatch):
    pols = {"customers": TablePolicy(
        "customers", col_access={"phone": "mask", "id_card": "deny"},
    )}
    monkeypatch.setattr(
        dp, "_list_table_columns",
        lambda t: ["customer_id", "customer_name", "phone", "id_card", "address"],
    )
    out, _ = _rewrite(pols, "SELECT * FROM customers")
    assert "id_card" not in out
    assert "'***'" in out
    assert "customer_id" in out and "address" in out


def test_star_all_columns_denied_rejected(monkeypatch):
    pols = {"customers": TablePolicy("customers", col_access={"id_card": "deny"})}
    monkeypatch.setattr(dp, "_list_table_columns", lambda t: ["id_card"])
    with pytest.raises(dp.PolicyViolation, match="所有列均被禁止"):
        _rewrite(pols, "SELECT * FROM customers")


# ---------- apply_data_policy 端到端(走策略加载) ----------

def test_apply_data_policy_end_to_end(dp_env):
    sm, add_rule = dp_env
    add_rule("role", "user", "orders", row_filter="order_date >= now() - interval '90 days'")
    add_rule("role", "user", "customers", col_access={"phone": "mask"})

    sql, denied = dp.apply_data_policy(
        "SELECT o.order_id, c.phone FROM orders o JOIN customers c ON c.customer_id = o.customer_id",
        "alice", ["user"],
    )
    assert denied is None
    assert "o.order_date >= CURRENT_TIMESTAMP" in sql
    assert "'***'" in sql

    # approver 单角色(无规则) -> 默认允许, 不掩码
    sql2, denied2 = dp.apply_data_policy("SELECT phone FROM customers", "bob", ["approver"])
    assert denied2 is None and sql2 is not None
    assert "'***'" not in sql2


# ---------- executor 强制 ----------

def test_executor_denied_raises_policy_denied(dp_env, monkeypatch):
    sm, add_rule = dp_env
    add_rule("role", "user", "customers", col_access={"id_card": "deny"})
    monkeypatch.setattr(dp, "get_user_roles", lambda u: ["user"])

    from src.nodes.executor import executor_node

    with pytest.raises(dp.PolicyDeniedError, match="数据权限拒绝"):
        executor_node({
            "code": "SELECT customer_id, id_card FROM customers",
            "task_id": f"t-{uuid.uuid4()}",
            "actor": "alice",
        })


def test_executor_rewrites_and_passes(dp_env, monkeypatch):
    sm, add_rule = dp_env
    add_rule("role", "user", "orders", col_access={"customer_id": "mask"})
    monkeypatch.setattr(dp, "get_user_roles", lambda u: ["user"])
    captured = {}

    import src.nodes.executor as ex

    def fake_run(code, backend="auto", db_url=None):
        captured["sql"] = code
        return {"status": "success", "output": "rows=1\norder_id\n('1',)", "error": "", "row_count": 1}

    monkeypatch.setattr(ex, "run_in_sandbox", fake_run)
    from src.nodes.executor import executor_node

    res = executor_node({
        "code": "SELECT order_id, customer_id FROM orders",
        "task_id": f"t-{uuid.uuid4()}",
        "actor": "alice",
    })
    assert "SELECT order_id, '***' FROM orders" in captured["sql"]  # 执行的是改写后 SQL


# ---------- 管理 API ----------

@pytest.fixture
def api_env(monkeypatch):
    """TestClient + admin 身份 + sqlite DB。"""
    from src.api.auth import User, get_current_user
    from src.api.deps import get_db

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    sm = sessionmaker(bind=engine, autocommit=False, autoflush=False)

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

    def _user():
        return User(id="2", name="alice", roles=["user"])

    app.dependency_overrides[get_current_user] = _admin
    client = TestClient(app)
    yield client, sm, _user
    app.dependency_overrides.clear()
    client.close()


def test_data_policy_crud(api_env):
    client, sm, _ = api_env
    # 创建
    resp = client.post("/api/v1/admin/data-policies", json={
        "target_type": "role", "target_name": "user", "table_name": "orders",
        "row_filter": "order_date >= now() - interval '90 days'",
        "col_access": {"customer_id": "mask"},
    })
    assert resp.status_code == 200, resp.text
    pid = resp.json()["id"]

    # 列表
    lst = client.get("/api/v1/admin/data-policies")
    assert lst.status_code == 200
    assert lst.json()["total"] == 1

    # 更新
    upd = client.put(f"/api/v1/admin/data-policies/{pid}", json={"enabled": False})
    assert upd.status_code == 200
    assert upd.json()["enabled"] is False

    # 非法列模式 -> 422
    bad = client.post("/api/v1/admin/data-policies", json={
        "target_type": "role", "target_name": "user", "table_name": "x",
        "col_access": {"a": "evil"},
    })
    assert bad.status_code == 422

    # 非法表达式(分号) -> 422
    bad2 = client.post("/api/v1/admin/data-policies", json={
        "target_type": "role", "target_name": "user", "table_name": "x",
        "row_filter": "1=1; DROP TABLE x",
    })
    assert bad2.status_code == 422

    # 重复创建 -> 409
    dup = client.post("/api/v1/admin/data-policies", json={
        "target_type": "role", "target_name": "user", "table_name": "orders",
    })
    assert dup.status_code == 409

    # 删除
    dele = client.delete(f"/api/v1/admin/data-policies/{pid}")
    assert dele.status_code == 200
    assert client.get("/api/v1/admin/data-policies").json()["total"] == 0


def test_data_policy_api_forbidden_for_regular_user(api_env):
    client, sm, _user = api_env
    app = client.app
    from src.api.auth import get_current_user

    app.dependency_overrides[get_current_user] = _user
    resp = client.get("/api/v1/admin/data-policies")
    assert resp.status_code == 403
