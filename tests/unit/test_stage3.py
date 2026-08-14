"""阶段 3 可靠性与性能: 限流(OR-11) / 缓存(OR-06) / 任务取消(OR-08) 单元测试。"""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.models import AuditLog, Base, Task


def _redis_available() -> bool:
    try:
        from src.api.deps import get_redis

        next(get_redis()).ping()
        return True
    except Exception:  # noqa: BLE001
        return False


NEEDS_REDIS = pytest.mark.skipif(not _redis_available(), reason="Redis 不可用")


# ---------- OR-06 缓存 ----------

@NEEDS_REDIS
def test_cache_roundtrip_and_miss():
    from src.utils.cache import cache_get, cache_set

    key = f"test:{uuid.uuid4()}"
    try:
        assert cache_get(key) is None  # 未命中
        cache_set(key, "hello", ttl=60)
        assert cache_get(key) == "hello"
    finally:
        from src.api.deps import get_redis

        next(get_redis()).delete(key)


@NEEDS_REDIS
def test_cache_json_roundtrip():
    from src.utils.cache import cache_get_json, cache_set_json

    key = f"test:json:{uuid.uuid4()}"
    try:
        payload = [{"step": "a", "dependencies": []}]
        cache_set_json(key, payload, ttl=60)
        assert cache_get_json(key) == payload
    finally:
        from src.api.deps import get_redis

        next(get_redis()).delete(key)


# ---------- OR-11 限流 ----------

@NEEDS_REDIS
def test_rate_limiter_bucket_exhausts():
    from src.utils.rate_limit import RateLimiter

    limiter = RateLimiter(rate_per_min=1, burst=2)  # 每秒补充 1/60, 桶容量 2
    key = f"rl:{uuid.uuid4()}"
    try:
        assert limiter.allow(key) is True
        assert limiter.allow(key) is True
        assert limiter.allow(key) is False  # 桶空, 拒绝
    finally:
        from src.api.deps import get_redis

        next(get_redis()).delete(f"rate_limit:{key}")


# ---------- OR-08 任务取消 ----------

@NEEDS_REDIS
def test_is_task_canceled_flag():
    from src.api.deps import get_redis
    from src.graph import is_task_canceled

    tid = f"cancel-{uuid.uuid4()}"
    redis = next(get_redis())
    try:
        assert is_task_canceled(tid) is False
        redis.set(f"cancel:{tid}", "1", ex=60)
        assert is_task_canceled(tid) is True
    finally:
        redis.delete(f"cancel:{tid}")


@pytest.fixture
def cancel_client(monkeypatch):
    """替换 DB 为 sqlite 的 TestClient; 取消检查打桩避免依赖 Redis 标志。

    认证: 覆盖 get_current_user 返回 admin(认证本身的测试见 test_auth.py)。
    """
    from src.api.auth import User, get_current_user
    from src.api.deps import get_db
    from src import graph

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

    def _fake_current_user() -> User:
        return User(id="1", name="admin", roles=["user", "approver", "admin"])

    from main import app

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_current_user] = _fake_current_user
    monkeypatch.setattr(graph, "is_task_canceled", lambda tid: False)
    with TestClient(app) as client:
        yield client, sm
    app.dependency_overrides.clear()


def test_cancel_pending_task(cancel_client):
    client, sm = cancel_client
    db = sm()
    try:
        task = Task(id=f"t-{uuid.uuid4()}", user_query="测试", status="pending")
        db.add(task)
        db.commit()
        tid = task.id
    finally:
        db.close()

    resp = client.post(f"/api/v1/tasks/{tid}/cancel")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "canceled"

    # 再次取消 -> 409
    resp2 = client.post(f"/api/v1/tasks/{tid}/cancel")
    assert resp2.status_code == 409


def test_cancel_awaiting_approval_task(cancel_client):
    client, sm = cancel_client
    db = sm()
    try:
        task = Task(id=f"t-{uuid.uuid4()}", user_query="测试", status="awaiting_approval")
        db.add(task)
        db.commit()
        tid = task.id
    finally:
        db.close()

    resp = client.post(f"/api/v1/tasks/{tid}/cancel")
    assert resp.status_code == 200
    assert resp.json()["status"] == "canceled"

    # 审计日志落库
    db = sm()
    try:
        log = db.query(AuditLog).filter_by(task_id=tid, event="task_canceled").first()
        assert log is not None
    finally:
        db.close()


def test_cancel_completed_task_conflict(cancel_client):
    client, sm = cancel_client
    db = sm()
    try:
        task = Task(id=f"t-{uuid.uuid4()}", user_query="测试", status="completed")
        db.add(task)
        db.commit()
        tid = task.id
    finally:
        db.close()

    resp = client.post(f"/api/v1/tasks/{tid}/cancel")
    assert resp.status_code == 409


def test_cancel_nonexistent_task_404(cancel_client):
    client, _ = cancel_client
    resp = client.post("/api/v1/tasks/does-not-exist/cancel")
    assert resp.status_code == 404


# ---------- 权限回归: 取消归属 / dashboard 管理员 / 审批禁自审 ----------

@pytest.fixture
def auth_client(monkeypatch):
    """以指定用户身份访问的 TestClient 工厂(内存 sqlite, 不触发 startup)。"""
    from src.api.auth import get_current_user
    from src.api.deps import get_db
    from src import graph

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
    monkeypatch.setattr(graph, "is_task_canceled", lambda tid: False)

    def _factory(user) -> tuple[TestClient, sessionmaker]:
        app.dependency_overrides[get_current_user] = lambda: user
        return TestClient(app), sm

    yield _factory
    app.dependency_overrides.clear()


def test_cancel_other_user_task_forbidden(auth_client):
    """权限: 普通用户不能取消他人任务 -> 403。"""
    from src.api.auth import User

    client, sm = auth_client(User(id="2", name="alice", roles=["user"]))
    db = sm()
    try:
        tid = f"t-{uuid.uuid4()}"
        db.add(Task(id=tid, user_query="测试", status="pending", created_by="bob"))
        db.commit()
    finally:
        db.close()

    resp = client.post(f"/api/v1/tasks/{tid}/cancel")
    assert resp.status_code == 403, resp.text
    client.close()


def test_cancel_own_task_by_regular_user(auth_client):
    """权限: 普通用户可取消自己的任务 -> 200。"""
    from src.api.auth import User

    client, sm = auth_client(User(id="2", name="alice", roles=["user"]))
    db = sm()
    try:
        tid = f"t-{uuid.uuid4()}"
        db.add(Task(id=tid, user_query="测试", status="pending", created_by="alice"))
        db.commit()
    finally:
        db.close()

    resp = client.post(f"/api/v1/tasks/{tid}/cancel")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "canceled"
    client.close()


def test_dashboard_requires_admin(auth_client):
    """权限: /dashboard 仅管理员可见, 普通用户 -> 403。"""
    from src.api.auth import User

    client, _ = auth_client(User(id="2", name="alice", roles=["user"]))
    resp = client.get("/api/v1/dashboard")
    assert resp.status_code == 403, resp.text
    client.close()


def test_dashboard_allowed_for_admin(auth_client):
    """权限: admin 访问 /dashboard -> 200。"""
    from src.api.auth import User

    client, _ = auth_client(User(id="1", name="admin", roles=["user", "approver", "admin"]))
    resp = client.get("/api/v1/dashboard")
    assert resp.status_code == 200, resp.text
    client.close()


def test_approve_own_task_forbidden(auth_client):
    """权限: 职责分离——审批人不能审批自己提交的任务 -> 403。"""
    from src.api.auth import User

    client, sm = auth_client(
        User(id="3", name="approver1", roles=["user", "approver"])
    )
    db = sm()
    try:
        tid = f"t-{uuid.uuid4()}"
        db.add(
            Task(id=tid, user_query="测试", status="awaiting_approval", created_by="approver1")
        )
        db.commit()
    finally:
        db.close()

    resp = client.post(
        f"/api/v1/tasks/{tid}/approve",
        json={"approved": True, "approver": "approver1", "comment": ""},
    )
    assert resp.status_code == 403, resp.text
    client.close()


def test_approve_other_user_task_ok(auth_client, monkeypatch):
    """权限: 审批人可以审批他人任务, 且审计 actor 取登录用户而非请求体。"""
    from src.api.auth import User
    from src import graph

    client, sm = auth_client(
        User(id="3", name="approver1", roles=["user", "approver"])
    )
    # 打桩 resume_task, 断言 approver 参数绑定登录用户(忽略 body.approver)
    captured = {}

    def _fake_resume_task(task_id, approved, approver, comment, client_ip, user_agent):
        captured["approver"] = approver
        return {"status": "completed"}

    monkeypatch.setattr(graph, "resume_task", _fake_resume_task)
    db = sm()
    try:
        tid = f"t-{uuid.uuid4()}"
        db.add(
            Task(id=tid, user_query="测试", status="awaiting_approval", created_by="bob")
        )
        db.commit()
    finally:
        db.close()

    resp = client.post(
        f"/api/v1/tasks/{tid}/approve",
        json={"approved": True, "approver": "伪造的名字", "comment": ""},
    )
    assert resp.status_code == 200, resp.text
    assert captured["approver"] == "approver1"  # 以登录用户为准, 不信任请求体
    client.close()
