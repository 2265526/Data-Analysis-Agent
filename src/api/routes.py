"""业务路由(/api/v1): 认证登录、任务提交、状态轮询、审批回调。

- POST   /auth/login                    登录, 返回 JWT(本地认证)
- GET    /auth/me                       查询当前登录用户
- POST   /tasks                      提交分析任务(自然语言需求)
- GET    /tasks/{task_id}/status     轮询任务状态(前端每 2s)
- POST   /tasks/{task_id}/approve    人机协同审批回调(需 approver/admin 角色)

认证说明: auth_mode=dev(默认)时所有依赖自动放行, 单机免登录;
auth_mode=oauth2 时按本地 JWT 校验(见 src/api/auth.py)。
审批说明: 通过 Command(resume=...) 恢复 LangGraph 挂起的任务(官方 interrupt 模式)。
"""
from __future__ import annotations

import json
import uuid
from typing import Optional

from celery import Celery
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from src.api.auth import (
    User,
    authenticate_user,
    create_access_token,
    get_current_user,
    get_jwt_secret,
    require_role,
)
from src.api.deps import get_db
from src.models import AuditLog, Task
from src.utils.logger import get_logger
from src.utils.security import mask_sensitive
from src.utils.settings import get_settings

settings = get_settings()
logger = get_logger(__name__)

router = APIRouter(tags=["任务"])

# ---------------------------------------------------------------------------
# Celery 任务队列(异步执行 LangGraph 流水线)
#   worker 启动: celery -A src.api.routes:celery_app worker -l info
# ---------------------------------------------------------------------------
celery_app = Celery(
    "data_pipeline_agent",
    broker=settings.redis_url,
    backend=settings.redis_url,
)
celery_app.conf.update(task_serializer="json", result_serializer="json", accept_content=["json"])


@celery_app.task(name="pipeline.run_task", bind=True, max_retries=3)
def run_pipeline_task(self, task_id: str) -> dict:
    """Celery 任务: 执行完整 LangGraph 流水线(延迟导入避免循环依赖)。"""
    from src.graph import execute_task

    return execute_task(task_id)


# ---------------------------------------------------------------------------
# 请求/响应模型
# ---------------------------------------------------------------------------
class TaskSubmitRequest(BaseModel):
    query: str = Field(..., min_length=2, max_length=5000, description="自然语言分析需求")
    session_id: Optional[int] = Field(
        None, description="所属会话 id(多轮上下文关联; 单轮提交为空)"
    )
    data_source_id: Optional[int] = Field(
        None, description="数据源 id(空=主库; 见 /admin/data-sources)"
    )


class TaskSubmitResponse(BaseModel):
    task_id: str
    status: str = "pending"


class TaskStatusResponse(BaseModel):
    task_id: str
    status: str
    progress: str = ""
    progress_detail: str = ""
    progress_percent: int | None = None
    result_path: Optional[str] = None
    summary: Optional[str] = None
    report_content: Optional[str] = None   # 任务完成时: 报告 Markdown 正文(自然语言结论)
    error_log: Optional[str] = None
    has_pdf: bool = False                  # 生成了 PDF(可下载)
    has_board: bool = False                # 生成了看板数据(board.json 存在)


class PendingApprovalItem(BaseModel):
    task_id: str
    query: str
    progress_detail: str = ""
    created_at: Optional[str] = None


class PendingApprovalResponse(BaseModel):
    tasks: list[PendingApprovalItem]
    total: int


class TaskListItem(BaseModel):
    task_id: str
    query: str
    status: str
    submitted_by: Optional[str] = None
    progress: str = ""
    progress_detail: str = ""
    result_path: Optional[str] = None
    has_pdf: bool = False          # 生成了 PDF(可下载)
    has_board: bool = False        # 生成了看板数据(board.json 存在)
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class TaskListResponse(BaseModel):
    tasks: list[TaskListItem]
    total: int
    page: int
    page_size: int


class AuditLogItem(BaseModel):
    """任务操作记录(每任务一行, 聚合提交/审批/执行结果)。"""

    task_id: str
    query: str = ""
    submitted_by: Optional[str] = None
    submitted_at: Optional[str] = None
    approver: Optional[str] = None
    approval_result: Optional[str] = None  # approved/rejected/pending/none
    approval_comment: Optional[str] = None
    result: Optional[str] = None  # completed/failed/canceled/running
    error: Optional[str] = None
    updated_at: Optional[str] = None


class AuditLogResponse(BaseModel):
    logs: list[AuditLogItem]
    total: int
    page: int
    page_size: int


class AuditEventItem(BaseModel):
    """单条审计事件(详情页时间线)。"""

    id: int
    event: str
    actor: Optional[str] = None
    node_name: Optional[str] = None
    detail: Optional[str] = None
    created_at: Optional[str] = None


class ApproveRequest(BaseModel):
    approved: bool = Field(..., description="True=通过, False=拒绝")
    approver: str = Field(..., min_length=1, description="审批人标识")
    comment: str = Field(default="", max_length=1000, description="审批意见")


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=64, description="登录用户名")
    password: str = Field(..., min_length=1, max_length=128, description="密码")


class UserOut(BaseModel):
    id: str
    name: str
    roles: list[str] = []
    created_at: str | None = None
    updated_at: str | None = None
    password_hash: str | None = None


class UserPasswordUpdateRequest(BaseModel):
    """管理员重置用户密码请求体。"""

    new_password: str = Field(
        ..., min_length=6, max_length=128, description="新密码(至少 6 位)"
    )


class UserCreateRequest(BaseModel):
    """管理员添加用户请求体。"""

    username: str = Field(
        ..., min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_]+$",
        description="登录用户名(字母/数字/下划线)",
    )
    password: str = Field(
        ..., min_length=6, max_length=128, description="密码(至少 6 位)"
    )
    roles: list[str] = Field(
        default_factory=lambda: ["user"],
        description="角色列表, 可选 user / approver / admin",
    )


class UserListResponse(BaseModel):
    users: list[UserOut]
    total: int


_ALLOWED_ROLES = {"user", "approver", "admin"}


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------
def _create_task(
    db: Session,
    query: str,
    actor: str | None = None,
    client_ip: str = "",
    user_agent: str = "",
    session_id: int | None = None,
    data_source_id: int | None = None,
) -> Task:
    task_id = str(uuid.uuid4())
    task = Task(
        id=task_id,
        user_query=query,
        status="pending",
        progress="任务已提交, 排队中",
        created_by=actor,
        session_id=session_id,
        data_source_id=data_source_id,
    )
    db.add(task)
    db.add(
        AuditLog(
            task_id=task_id,
            event="task_submitted",
            actor=actor or "system",
            client_ip=client_ip or None,
            user_agent=user_agent or None,
            detail={"query": mask_sensitive(query)},
        )
    )
    db.commit()
    db.refresh(task)
    return task


@router.post("/auth/login", response_model=LoginResponse, summary="登录获取 JWT")
def login(body: LoginRequest) -> LoginResponse:
    """本地认证登录: 校验用户名/密码, 签发 JWT(HS256, 有效期 settings.jwt_expire_minutes)。"""
    user = authenticate_user(body.username, body.password)
    if user is None:
        logger.warning("login_failed", username=body.username)
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    token = create_access_token(
        username=user.name,
        secret=get_jwt_secret(),
        expires_minutes=settings.jwt_expire_minutes,
    )
    logger.info("login_succeeded", username=user.name)
    return LoginResponse(
        access_token=token,
        user=UserOut(id=user.id, name=user.name, roles=user.roles),
    )


@router.get("/auth/me", response_model=UserOut, summary="查询当前登录用户")
def me(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> UserOut:
    """返回当前登录用户信息(前端可用于展示/控制按钮)。"""
    from src.models import User as UserModel

    row = db.query(UserModel).filter(UserModel.username == current_user.name).first()
    return UserOut(
        id=current_user.id,
        name=current_user.name,
        roles=current_user.roles,
        created_at=row.created_at.isoformat() if row and row.created_at else None,
        updated_at=row.updated_at.isoformat() if row and row.updated_at else None,
    )


@router.post("/users", response_model=UserOut, summary="添加用户(仅管理员)")
def create_user(
    body: UserCreateRequest,
    db: Session = Depends(get_db),
    _: User = Depends(require_role("admin")),
) -> UserOut:
    """管理员添加用户: 用户名查重, 密码 PBKDF2 哈希存储。"""
    from src.models import User as UserModel

    from src.utils.security import hash_password

    roles = list(dict.fromkeys(body.roles))  # 去重保序
    for r in roles:
        if r not in _ALLOWED_ROLES:
            raise HTTPException(
                status_code=422, detail=f"非法角色: {r}(可选 user/approver/admin)"
            )
    exists = db.query(UserModel).filter(UserModel.username == body.username).first()
    if exists:
        raise HTTPException(status_code=409, detail="用户名已存在")

    user = UserModel(
        username=body.username,
        password_hash=hash_password(body.password),
        roles=roles,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    logger.info("user_created", username=user.username, roles=roles)
    return UserOut(id=str(user.id), name=user.username, roles=[str(r) for r in user.roles])


@router.get("/users", response_model=UserListResponse, summary="用户列表(仅管理员)")
def list_users(
    db: Session = Depends(get_db),
    _: User = Depends(require_role("admin")),
) -> UserListResponse:
    """管理员查看全部用户。"""
    from src.models import User as UserModel

    rows = db.query(UserModel).order_by(UserModel.id).all()
    return UserListResponse(
        users=[
            UserOut(
                id=str(u.id),
                name=u.username,
                roles=[str(r) for r in (u.roles or [])],
                created_at=u.created_at.isoformat() if u.created_at else None,
                updated_at=u.updated_at.isoformat() if u.updated_at else None,
                password_hash=u.password_hash,
            )
            for u in rows
        ],
        total=len(rows),
    )


@router.put("/users/{user_id}/password", summary="重置用户密码(仅管理员)")
def reset_user_password(
    user_id: int,
    body: UserPasswordUpdateRequest,
    db: Session = Depends(get_db),
    _: User = Depends(require_role("admin")),
) -> dict:
    """管理员重置任意用户密码(用户本人无自助改密接口, 符合内部项目管控)。"""
    from src.models import User as UserModel

    from src.utils.security import hash_password

    user = db.query(UserModel).filter(UserModel.id == user_id).first()
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    user.password_hash = hash_password(body.new_password)
    db.commit()
    logger.info("user_password_reset", username=user.username, operator="admin")
    return {"ok": True, "username": user.username}


@router.get("/admin/metrics", summary="运行指标看板数据(仅管理员)")
def admin_metrics(
    db: Session = Depends(get_db),
    _: User = Depends(require_role("admin")),
) -> dict:
    """聚合 Prometheus 进程内指标 + 数据库统计, 供管理员界面可视化。

    进程内指标(metrics.snapshot_dict): task_executed_total / self_heal_* /
    llm_tokens_total / sandbox_exec_duration_seconds / circuit_breaker_trips_total 等
    数据库统计: 任务状态分布 / 节点执行 / token 与成本 / 用户数 / 近 7 天趋势
    """
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import func

    from src.models import CostRecord, Task, TaskNodeRun
    from src.models import User as UserModel
    from src.utils.metrics import metrics as registry

    prom = registry.snapshot_dict()

    # 任务状态分布
    status_rows = (
        db.query(Task.status, func.count(Task.id)).group_by(Task.status).all()
    )
    task_status = {status: count for status, count in status_rows}
    task_total = sum(task_status.values())

    # 节点执行统计(运行次数/总耗时/token)
    node_rows = (
        db.query(
            TaskNodeRun.node_name,
            func.count(TaskNodeRun.id),
            func.sum(TaskNodeRun.duration_ms),
            func.sum(TaskNodeRun.prompt_tokens + TaskNodeRun.completion_tokens),
        )
        .group_by(TaskNodeRun.node_name)
        .order_by(func.count(TaskNodeRun.id).desc())
        .all()
    )
    node_stats = [
        {
            "node": node,
            "runs": runs,
            "duration_ms": int(duration or 0),
            "tokens": int(tokens or 0),
        }
        for node, runs, duration, tokens in node_rows
    ]

    # 自修复重试次数(run_seq > 1 的节点执行)
    retried = (
        db.query(func.count(TaskNodeRun.id))
        .filter(TaskNodeRun.run_seq > 1)
        .scalar()
        or 0
    )

    # 成本与 token(actual=实际, estimate=预估)
    cost_actual = (
        db.query(func.sum(CostRecord.cost_amount))
        .filter(CostRecord.cost_type == "actual")
        .scalar()
        or 0
    )
    cost_estimate = (
        db.query(func.sum(CostRecord.cost_amount))
        .filter(CostRecord.cost_type == "estimate")
        .scalar()
        or 0
    )
    prompt_tokens, completion_tokens = (
        db.query(
            func.sum(CostRecord.prompt_tokens),
            func.sum(CostRecord.completion_tokens),
        )
        .filter(CostRecord.cost_type == "actual")
        .one()
    )

    user_count = db.query(func.count(UserModel.id)).scalar() or 0

    # 近 7 天任务趋势
    since = datetime.now(timezone.utc) - timedelta(days=7)
    trend_rows = (
        db.query(func.date(Task.created_at), func.count(Task.id))
        .filter(Task.created_at >= since)
        .group_by(func.date(Task.created_at))
        .order_by(func.date(Task.created_at))
        .all()
    )
    trend = [{"date": str(d), "count": c} for d, c in trend_rows]

    logger.info("admin_metrics_snapshot", task_total=task_total, users=user_count)
    # 完成率与自修复率(节点执行统计)
    node_total_runs = (
        db.query(func.count(TaskNodeRun.id)).scalar() or 0
    )
    node_retries = (
        db.query(func.count(TaskNodeRun.id)).filter(TaskNodeRun.run_seq > 1).scalar() or 0
    )
    completed_count = task_status.get("completed", 0)
    completion_rate = round(completed_count / task_total * 100, 1) if task_total else 0.0
    self_heal_rate = (
        round(node_retries / node_total_runs * 100, 1) if node_total_runs else 0.0
    )
    from src.models import AuditLog

    audit_log_count = db.query(func.count(AuditLog.id)).scalar() or 0
    cost_record_count = db.query(func.count(CostRecord.id)).scalar() or 0

    return {
        "prometheus": prom,
        "db": {
            "task_total": task_total,
            "task_status": task_status,
            "completion_rate": completion_rate,
            "node_total_runs": node_total_runs,
            "node_retries": node_retries,
            "self_heal_rate": self_heal_rate,
            "node_stats": node_stats,
            "retry_count": retried,
            "cost_actual": float(cost_actual or 0),
            "cost_estimate": float(cost_estimate or 0),
            "cost_record_count": cost_record_count,
            "audit_log_count": audit_log_count,
            "tokens": {
                "prompt": int(prompt_tokens or 0),
                "completion": int(completion_tokens or 0),
            },
            "user_count": user_count,
            "trend_7d": trend,
        },
    }


@router.post("/tasks", response_model=TaskSubmitResponse, summary="提交分析任务")
def submit_task(
    body: TaskSubmitRequest,
    background: BackgroundTasks,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TaskSubmitResponse:
    """提交自然语言分析任务, 返回 task_id 供前端轮询。

    闲聊(问候/寒暄/自我介绍等非分析需求)在入口直接拦截: 返回 422 且 detail 以
    CHAT_REPLY:: 开头携带助手回复, 前端捕获后以助手消息展示, 不创建任务记录、
    不进入流水线(避免闲聊产出空白任务或挂起澄清)。
    """
    from src.utils.chat_gate import chat_reply, is_chitchat

    if is_chitchat(body.query):
        raise HTTPException(status_code=422, detail="CHAT_REPLY::" + chat_reply(body.query))

    task = _create_task(
        db,
        body.query,
        actor=current_user.name if current_user else None,
        client_ip=request.client.host if request.client else "",
        user_agent=request.headers.get("user-agent", ""),
        session_id=body.session_id,
        data_source_id=body.data_source_id,
    )

    try:
        # 任务分发: 显式启用 Celery(且 broker 可用)才走队列; 默认 FastAPI 后台任务直接执行,
        # 避免"broker 可达但无 worker 消费导致任务卡死"的隐患
        if settings.celery_enabled:
            run_pipeline_task.delay(task.id)
            logger.info("task_dispatched", task_id=task.id, channel="celery")
        else:
            background.add_task(run_pipeline_task.run, task.id)
            logger.info("task_dispatched", task_id=task.id, channel="background")
    except Exception as exc:  # noqa: BLE001 — broker 未就绪时回退
        logger.warning("celery_unavailable_fallback", task_id=task.id, error=str(exc))
        background.add_task(run_pipeline_task.run, task.id)

    return TaskSubmitResponse(task_id=task.id)


@router.get("/tasks/{task_id}/status", response_model=TaskStatusResponse, summary="轮询任务状态")
def get_task_status(
    task_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TaskStatusResponse:
    """前端每 2s 轮询: 返回状态/进度/结果路径。"""
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    # 权限: 普通用户只能查看自己提交的任务; 审批人/管理员可见全部
    if not _is_privileged_user(current_user) and task.created_by != current_user.name:
        raise HTTPException(status_code=403, detail="无权查看其他用户的任务")
    # 任务完成时附带报告正文(自然语言结论), 供前端直接渲染
    # result_path 指向下载产物(PDF 或 md), 正文统一读同名 .md
    report_content: str | None = None
    if task.status == "completed" and task.result_path:
        from pathlib import Path

        rel = task.result_path.replace("/static/reports/", "").replace(".pdf", ".md")
        rp = settings.reports_dir / rel
        try:
            report_content = rp.read_text(encoding="utf-8")[:12000]
            # 图表相对路径(如 ![](xxx.png))转绝对 URL, 前端网页才能加载图片
            # (PDF 用 weasyprint base_url 从文件读取, 不受影响)
            import re as _re

            base_url = task.result_path.rsplit("/", 1)[0]  # /static/reports/YYYY/MM/DD
            report_content = _re.sub(
                r"\]\(([^)/][^)]*\.png)\)",
                lambda m: f"]({base_url}/{m.group(1)})",
                report_content,
            )
        except OSError:
            report_content = None
    return TaskStatusResponse(
        task_id=task.id,
        status=task.status,
        progress=task.progress,
        progress_detail=task.progress_detail or "",
        progress_percent=task.progress_percent,
        result_path=task.result_path,
        summary=task.summary,
        report_content=report_content,
        error_log=task.error_log,
        has_pdf=bool(task.result_path and task.result_path.endswith(".pdf")),
        has_board=_task_has_board(task.result_path, task.id),
    )


@router.get("/tasks/{task_id}/download", summary="下载任务报告(PDF/MD)")
def download_task_report(
    task_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """下载任务报告: 显式 Content-Type + attachment + no-store 防缓存。

    参考开源项目标准做法: 下载走专用接口而非直连静态文件, 避免浏览器
    缓存/类型误判(用户曾下载到 HTML 缓存导致 PDF 打不开)。
    """
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    if not _is_privileged_user(current_user) and task.created_by != current_user.name:
        raise HTTPException(status_code=403, detail="无权下载其他用户的任务")
    if not task.result_path:
        raise HTTPException(status_code=404, detail="该任务无报告文件")

    from pathlib import Path as _P

    fpath = settings.reports_dir / task.result_path.replace("/static/reports/", "")
    if not fpath.exists() or fpath.stat().st_size == 0:
        raise HTTPException(status_code=404, detail="报告文件不存在")

    media_type = "application/pdf" if fpath.suffix == ".pdf" else "text/markdown; charset=utf-8"
    filename = f"report-{task_id[:8]}.{fpath.suffix.lstrip('.')}"
    return FileResponse(
        fpath,
        media_type=media_type,
        filename=filename,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
        },
    )


def _is_privileged_user(user: User) -> bool:
    """审批人/管理员可查看全部任务信息, 普通用户只能看自己提交的。"""
    return bool(user and ("admin" in (user.roles or []) or "approver" in (user.roles or [])))


@router.get("/tasks/{task_id}/board", summary="任务交互式看板数据(图表+明细+联动)")
def get_task_board(
    task_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    """返回任务生成的交互式看板 JSON(前端 ECharts 渲染 + 点击品类下钻联动)。

    看板数据由 reporter 生成报告时同步产出({task_id}.board.json), 含
    KPI 卡 / 柱状图 / 饼图 / 趋势折线 / 明细表(下钻目标) / 血缘摘要。
    """
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    if not _is_privileged_user(current_user) and task.created_by != current_user.name:
        raise HTTPException(status_code=403, detail="无权查看其他用户的任务")
    if not task.result_path:
        raise HTTPException(status_code=404, detail="该任务无分析结果")

    # result_path = /static/reports/YYYY/MM/DD/xxx.pdf -> 同目录 {task_id}.board.json
    rel_dir = task.result_path.replace("/static/reports/", "").rsplit("/", 1)[0]
    board_file = settings.reports_dir / rel_dir / f"{task_id}.board.json"
    if not board_file.exists():
        raise HTTPException(status_code=404, detail="该任务暂无看板数据(旧版本生成, 请重新提交任务生成)")
    return json.loads(board_file.read_text(encoding="utf-8"))


# 下钻维度白名单: 前端只传维度键, 后端映射到真实列, 杜绝任意列探测/SQL 注入
# 下钻指标与一级品类明细对齐: 近7天销售额 / 上周销售额 / 环比增长率(%)
# 下钻计划按看板主维度(原始列名)动态选择, 不再写死"一级品类 -> 二级品类"
_DRiLL_DIMENSIONS: dict[str, dict] = {
    "category_l1": {
        "label": "二级品类",
        "join": "JOIN products p ON p.product_id = oi.product_id",
        "group_col": "p.category_l2",
        "filter_col": "p.category_l1",
        "columns": ["二级品类", "近7天销售额", "上周销售额", "环比增长率(%)"],
    },
    "category_l2": {
        "label": "商品",
        "join": "JOIN products p ON p.product_id = oi.product_id",
        "group_col": "p.product_name",
        "filter_col": "p.category_l2",
        "columns": ["商品", "近7天销售额", "上周销售额", "环比增长率(%)"],
    },
    "customer_id": {
        "label": "一级品类",
        "join": "JOIN products p ON p.product_id = oi.product_id\nJOIN customers c ON c.customer_id = o.customer_id",
        "group_col": "p.category_l1",
        "filter_col": "c.customer_id",
        "columns": ["一级品类", "近7天销售额", "上周销售额", "环比增长率(%)"],
    },
    "product_name": {
        "label": "商品",
        "join": "JOIN products p ON p.product_id = oi.product_id",
        "group_col": "p.product_name",
        "filter_col": "p.product_name",
        "columns": ["商品", "近7天销售额", "上周销售额", "环比增长率(%)"],
    },
    "order_day": {
        "label": "品类",
        "join": "JOIN products p ON p.product_id = oi.product_id",
        "group_col": "p.category_l1",
        "filter_col": "o.order_date::date",
        "columns": ["品类", "近7天销售额", "上周销售额", "环比增长率(%)"],
    },
}


def _drill_plan_for(drill_key: str | None, legacy_dimension: str = "category_l2") -> dict:
    """按看板主维度(原始列名)选择下钻计划。

    - customer_id -> 该客户的品类构成
    - category_l1/category -> 二级品类明细(旧 board 无 drill_key 时按 legacy 默认同样映射)
    - category_l2 -> 商品明细
    """
    key = drill_key
    if key is None:
        # 旧版看板无 drill_key: 前端默认传 category_l2 表示"一级品类下钻二级品类"(旧语义)
        key = "category_l1" if legacy_dimension == "category_l2" else legacy_dimension
    if key in ("category", "category_l1"):
        return _DRiLL_DIMENSIONS["category_l1"]
    if key in _DRiLL_DIMENSIONS:
        return _DRiLL_DIMENSIONS[key]
    return _DRiLL_DIMENSIONS["category_l1"]  # 兜底: 品类下钻


def _load_board_json(task_id: str) -> dict | None:
    """读取任务看板 JSON(含 drill_key 主维度, 决定下钻计划)。"""
    try:
        import glob

        hits = glob.glob(str(settings.reports_dir / "*/*/*" / f"{task_id}.board.json"))
        if not hits:
            return None
        with open(hits[0], encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001 — 看板读取失败按无处理
        return None


def _is_order_metric(mc: str) -> bool:
    """订单数类指标列名判定(精确匹配, 避免 order_day/order_date/user_count 误判)。"""
    if mc in {"order_count", "orders", "order_num", "order_qty", "order_quantity",
              "order_number", "total_orders", "count"}:
        return True
    return mc.startswith("order_") and not mc.startswith(
        ("order_day", "order_date", "order_time", "order_status", "order_amount", "order_item")
    )


def _is_quantity_metric(mc: str) -> bool:
    """销量类指标列名判定(精确匹配, 避免 user_num/item_num/order_num 误判)。"""
    return mc in {"quantity", "quantity_sold", "total_quantity", "sold_quantity", "qty"} or mc.endswith("_qty")


def _build_drill_sql(dim: dict, safe_value: str, metric_col: str | None = None,
                     time_range: dict | None = None) -> tuple[str, list[str]]:
    """构造下钻 SQL 与列中文名。

    metric_col 为看板值列原始列名(如 order_count/quantity_sold/sales_7d):
    - 订单数类(order/count): 按 COUNT(DISTINCT order_id) 聚合, 不能再用销售额
      (用户查"各品类订单数量"时下钻必须还是订单数 —— 回归根因)
    - 销量类(quantity/qty/num): SUM(quantity)
    - 其余默认销售额(近7天/上周/环比, 兼容无 metric_col 的旧看板)

    time_range 为看板持久化的主查询时间范围事实(_extract_time_range 结果):
    - 具体日期(single_day/range): 下钻**同一时间口径**过滤 —— 单日查询下钻
      不能显示近7天/上周(回归根因), 列也随口径动态(如 "8月7日" -> 单列销售额)
    - 无/相对窗口: 保持原行为(销售额近7天/上周环比; 订单数全时段)
    """
    where_time = ""
    if time_range and time_range.get("kind") in ("single_day", "range"):
        st, en = time_range.get("start"), time_range.get("end")
        so = time_range.get("start_op") or ">="  # 保留主查询边界运算符(> / <= / BETWEEN)
        eo = time_range.get("end_op") or "<"
        import re as _re  # noqa: PLC0415
        _ok = _re.compile(r"^\d{4}-\d{2}-\d{2}$")  # 防御: 仅接受服务端正则提取的日期格式
        if st and en and _ok.match(st) and _ok.match(en):
            where_time = f"\nAND o.order_date {so} '{st}' AND o.order_date {eo} '{en}'"
        elif st and _ok.match(st):
            where_time = f"\nAND o.order_date {so} '{st}'"
        elif en and _ok.match(en):
            where_time = f"\nAND o.order_date {eo} '{en}'"

    mc = (metric_col or "").strip().lower()
    # 注: 无具体日期时订单数/销量分支不加时间窗口(旧行为, 全时段);
    # 具体日期(single_day/range)时三条分支都按主查询同一时间口径过滤。
    if _is_order_metric(mc):
        sql = (
            "SELECT " + dim["group_col"] + " AS category,\n"
            "  COUNT(DISTINCT o.order_id) AS order_count\n"
            "FROM orders o JOIN order_items oi ON oi.order_id = o.order_id\n"
            + dim["join"] + "\n"
            "WHERE o.order_status = '已完成'\n"
            f"AND {dim['filter_col']} = '{safe_value}'"
            + where_time + "\n"
            "GROUP BY 1 ORDER BY 2 DESC"
        )
        return sql, [dim["label"], "订单数"]
    if _is_quantity_metric(mc):
        sql = (
            "SELECT " + dim["group_col"] + " AS category,\n"
            "  SUM(oi.quantity) AS quantity_sold\n"
            "FROM orders o JOIN order_items oi ON oi.order_id = o.order_id\n"
            + dim["join"] + "\n"
            "WHERE o.order_status = '已完成'\n"
            f"AND {dim['filter_col']} = '{safe_value}'"
            + where_time + "\n"
            "GROUP BY 1 ORDER BY 2 DESC"
        )
        return sql, [dim["label"], "销量"]
    # 具体日期范围: 单列销售额(下钻与主查询同口径, 不再近7天/上周/环比)
    if where_time:
        sql = (
            "SELECT " + dim["group_col"] + " AS category,\n"
            "  COALESCE(SUM(oi.total_item_amount), 0) AS sales\n"
            "FROM orders o JOIN order_items oi ON oi.order_id = o.order_id\n"
            + dim["join"] + "\n"
            "WHERE o.order_status = '已完成'\n"
            f"AND {dim['filter_col']} = '{safe_value}'"
            + where_time + "\n"
            "GROUP BY 1 ORDER BY 2 DESC"
        )
        return sql, [dim["label"], "销售额"]
    sql = (
        "SELECT " + dim["group_col"] + " AS category,\n"
        "  SUM(CASE WHEN o.order_date >= NOW() - INTERVAL '7 days' "
        "           THEN oi.total_item_amount ELSE 0 END) AS sales_7d,\n"
        "  SUM(CASE WHEN o.order_date >= NOW() - INTERVAL '14 days' AND o.order_date < NOW() - INTERVAL '7 days' "
        "           THEN oi.total_item_amount ELSE 0 END) AS sales_last_week,\n"
        "  ROUND(\n"
        "    (SUM(CASE WHEN o.order_date >= NOW() - INTERVAL '7 days' "
        "              THEN oi.total_item_amount ELSE 0 END)\n"
        "     - SUM(CASE WHEN o.order_date >= NOW() - INTERVAL '14 days' AND o.order_date < NOW() - INTERVAL '7 days' "
        "              THEN oi.total_item_amount ELSE 0 END))::numeric\n"
        "    / NULLIF(SUM(CASE WHEN o.order_date >= NOW() - INTERVAL '14 days' AND o.order_date < NOW() - INTERVAL '7 days' "
        "              THEN oi.total_item_amount ELSE 0 END), 0) * 100, 2) AS change_rate_pct\n"
        "FROM orders o JOIN order_items oi ON oi.order_id = o.order_id\n"
        + dim["join"] + "\n"
        "WHERE o.order_status = '已完成'\n"
        f"AND {dim['filter_col']} = '{safe_value}'\n"
        "GROUP BY 1 ORDER BY 2 DESC"
    )
    return sql, dim["columns"]


def _parse_drill_rows(output: str) -> list[list]:
    """解析下钻查询的 rows=N 输出为行列表(兼容 datetime.date 等嵌套括号)。"""
    import re as _re

    from src.nodes.reporter import _parse_row_values

    rows: list[list] = []
    m = _re.search(r"^rows=\d+\s*$", output, flags=_re.M)
    body = output[m.end():] if m else output
    for line in body.splitlines():
        line = line.strip()
        if not (line.startswith("(") and line.endswith(")")):
            continue
        vals = _parse_row_values(line)
        if vals:
            rows.append(vals)
    return rows


@router.get("/tasks/{task_id}/drill", summary="看板下钻: 一级品类 -> 二级品类明细")
def drill_task_board(
    task_id: str,
    value: str = Query(..., description="一级品类值, 如 服饰鞋包"),
    dimension: str = Query("category_l2", description="下钻维度(白名单)"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    """看板下钻: 按主维度动态下钻(品类 -> 二级品类 / 客户 -> 该客户品类构成)。

    安全性: dimension 走白名单映射真实列; value 为点击图表的维度值, 做单引号转义
    防止 SQL 注入; 查询通过沙箱只读执行并记录血缘(query_runs)。
    """
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    if not _is_privileged_user(current_user) and task.created_by != current_user.name:
        raise HTTPException(status_code=403, detail="无权查看其他用户的任务")
    # 下钻计划按看板主维度(原始列名)动态选择: 客户维度下钻"该客户品类构成", 品类下钻二级品类
    board = _load_board_json(task_id)
    drill_key = (board or {}).get("drill_key")
    metric_col = (board or {}).get("metric_col")  # 值列原始列名: 下钻按同指标聚合(订单数->订单数, 非销售额)
    drill_time_range = (board or {}).get("time_range")  # 主查询时间范围事实: 下钻同口径(单日->单日, 非近7天)
    dim = _drill_plan_for(drill_key if drill_key else None, dimension)
    if not value or len(value) > 64:
        raise HTTPException(status_code=400, detail="无效的下钻值")
    safe = value.replace("'", "''")

    sql, drill_columns = _build_drill_sql(dim, safe, metric_col, drill_time_range)
    try:
        from src.sandbox.docker_sandbox import run_in_sandbox
        from src.tools.lineage import record_query_run

        # 数据级权限强制: 下钻 SQL 同样受表/列/行级策略约束(与 executor 一致)
        from src.tools.data_policy import apply_data_policy

        new_sql, denied = apply_data_policy(sql, current_user.name, current_user.roles)
        if denied:
            raise HTTPException(status_code=403, detail=f"数据权限拒绝: {denied}")
        sql = new_sql

        res = run_in_sandbox(sql, backend="auto")
        if res.get("status") != "success":
            # 防御: 旧版 board 的 drill_key 可能是日期列(日期在前的多维查询), 图表点击
            # 传的却是品类值 -> 按日期过滤报错(invalid input syntax for type date)。
            # 此时回退到"二级品类"下钻重试一次, 避免看板下钻直接失败。
            if str(dim.get("filter_col", "")).lower().startswith(("o.order_date", "o.order_day", "date", "to_char")):
                fb = _DRiLL_DIMENSIONS["category_l1"]
                fb_sql, fb_cols = _build_drill_sql(fb, safe, metric_col, drill_time_range)
                res = run_in_sandbox(fb_sql, backend="auto")
                if res.get("status") == "success":
                    dim = fb
                    drill_columns = fb_cols
            if res.get("status") != "success":
                raise HTTPException(status_code=500, detail="下钻查询失败: " + (res.get("error") or "")[:200])
        rows = _parse_drill_rows(res.get("output") or "")
        record_query_run(
            task_id=task_id,
            sql_text=sql,
            run_order=99,  # 下钻查询统一标 99, 与报告主体查询区分
            rows_returned=len(rows),
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("drill_query_failed", task_id=task_id, error=str(exc)[:200])
        raise HTTPException(status_code=500, detail="下钻查询异常") from exc

    # 指标列与看板主维度对齐: 列名/标签随下钻计划动态
    return {
        "dimension": drill_key or dimension,
        "value": value,
        "label": dim["label"],
        "columns": drill_columns,
        "rows": rows,
    }


def _task_has_board(result_path: str | None, task_id: str) -> bool:
    """判断任务是否生成了看板数据(board.json 存在; 简洁问答/只要PDF 等模式不生成)。"""
    if not result_path:
        return False
    try:
        rel_dir = result_path.replace("/static/reports/", "").rsplit("/", 1)[0]
        return (settings.reports_dir / rel_dir / f"{task_id}.board.json").exists()
    except Exception:  # noqa: BLE001 — 文件检查失败按无看板处理
        return False


@router.get("/tasks", response_model=TaskListResponse, summary="任务列表(分页+状态筛选)")
def list_tasks(
    status: str | None = None,
    keyword: str | None = Query(None, max_length=100, description="按需求内容模糊搜索"),
    date_from: str | None = Query(None, description="起始日期(YYYY-MM-DD), 与状态筛选相互独立可组合"),
    date_to: str | None = Query(None, description="结束日期(YYYY-MM-DD), 含当日"),
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TaskListResponse:
    """历史任务列表: 普通用户只能看到自己提交的任务; 审批人/管理员可见全部任务。

    筛选条件(状态/关键词/时间范围)相互独立, 可任意组合。
    """
    from datetime import datetime, time

    q = db.query(Task).order_by(Task.created_at.desc())
    if not _is_privileged_user(current_user):
        q = q.filter(Task.created_by == current_user.name)
    if status:
        q = q.filter(Task.status == status)
    if keyword:
        q = q.filter(Task.user_query.ilike(f"%{keyword.strip()}%"))
    if date_from:
        try:
            q = q.filter(Task.created_at >= datetime.fromisoformat(date_from))
        except ValueError:
            raise HTTPException(status_code=400, detail="date_from 格式错误, 应为 YYYY-MM-DD")
    if date_to:
        try:
            end = datetime.combine(datetime.fromisoformat(date_to).date(), time.max)
            q = q.filter(Task.created_at <= end)
        except ValueError:
            raise HTTPException(status_code=400, detail="date_to 格式错误, 应为 YYYY-MM-DD")
    total = q.count()
    rows = q.offset((page - 1) * page_size).limit(page_size).all()

    return TaskListResponse(
        tasks=[
            TaskListItem(
                task_id=t.id,
                query=t.user_query,
                status=t.status,
                submitted_by=t.created_by,
                progress=t.progress or "",
                progress_detail=t.progress_detail or "",
                result_path=t.result_path,
                has_pdf=bool(t.result_path and t.result_path.endswith(".pdf")),
                has_board=_task_has_board(t.result_path, t.id),
                created_at=t.created_at.isoformat() if t.created_at else None,
                updated_at=t.updated_at.isoformat() if t.updated_at else None,
            )
            for t in rows
        ],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/dashboard", summary="工作台概览(仅管理员)")
def dashboard(
    db: Session = Depends(get_db),
    _: User = Depends(require_role("admin")),
) -> dict:
    """工作台首页数据: 任务/完成率/成本/token/近7天趋势/最新任务。

    权限: 仅管理员可查看(全局任务统计/成本/token 属平台级运营数据,
    前端路由已仅对 admin 展示, 后端保持一致, 避免普通用户直接调 API 越权)。
    """
    from datetime import datetime, timedelta, timezone

    from src.models import CostRecord, Task, TaskNodeRun
    from src.models import User as UserModel

    status_rows = db.query(Task.status, func.count(Task.id)).group_by(Task.status).all()
    task_status = {s: c for s, c in status_rows}
    task_total = sum(task_status.values())
    completed = task_status.get("completed", 0)
    completion_rate = round(completed / task_total * 100, 1) if task_total else 0.0

    node_runs = db.query(func.count(TaskNodeRun.id)).scalar() or 0
    node_retries = (
        db.query(func.count(TaskNodeRun.id)).filter(TaskNodeRun.run_seq > 1).scalar() or 0
    )

    cost = (
        db.query(func.sum(CostRecord.cost_amount))
        .filter(CostRecord.cost_type == "actual")
        .scalar()
        or 0
    )
    tokens = db.query(
        func.sum(CostRecord.prompt_tokens), func.sum(CostRecord.completion_tokens)
    ).one()

    since = datetime.now(timezone.utc) - timedelta(days=7)
    trend_rows = (
        db.query(func.date(Task.created_at), func.count(Task.id))
        .filter(Task.created_at >= since)
        .group_by(func.date(Task.created_at))
        .order_by(func.date(Task.created_at))
        .all()
    )

    latest = db.query(Task).order_by(Task.created_at.desc()).limit(5).all()

    user_count = db.query(func.count(UserModel.id)).scalar() or 0
    return {
        "task_total": task_total,
        "task_status": task_status,
        "completion_rate": completion_rate,
        "node_runs": node_runs,
        "node_retries": node_retries,
        "cost_actual": float(cost or 0),
        "tokens": {
            "prompt": int(tokens[0] or 0),
            "completion": int(tokens[1] or 0),
        },
        "user_count": user_count,
        "trend_7d": [{"date": str(d), "count": c} for d, c in trend_rows],
        "latest_tasks": [
            {
                "task_id": t.id,
                "query": t.user_query,
                "status": t.status,
                "created_at": t.created_at.isoformat() if t.created_at else None,
            }
            for t in latest
        ],
    }


@router.get("/admin/audit-logs", response_model=AuditLogResponse, summary="操作日志: 任务操作记录(仅管理员)")
def list_audit_logs(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    keyword: Optional[str] = Query(
        None, description="搜索: 提交者/操作内容(任务)/审批者/备注 任一匹配"
    ),
    date_from: Optional[str] = Query(None, description="提交时间起(YYYY-MM-DD, 含当天)"),
    date_to: Optional[str] = Query(None, description="提交时间止(YYYY-MM-DD, 含当天)"),
    submitted_by: Optional[str] = Query(None, description="提交者筛选(模糊)"),
    approver: Optional[str] = Query(None, description="审批人筛选(模糊)"),
    approval_result: Optional[str] = Query(
        None, description="审批结果: approved/rejected/pending/none"
    ),
    result: Optional[str] = Query(
        None, description="执行结果: completed/failed/canceled/running/pending"
    ),
    db: Session = Depends(get_db),
    _: User = Depends(require_role("admin")),
) -> AuditLogResponse:
    """按任务聚合的操作日志(每任务一行), 支持关键字搜索与多维筛选。

    - keyword: 提交者 / 操作内容(query) / 审批者(actor) / 备注(detail.comment) 任一包含即命中
    - 各筛选维度独立, 不选默认全部(不限定该维度)
    - 审批结果 pending=有审批流但未审批; none=无需审批
    """
    from datetime import datetime, timedelta

    from sqlalchemy import Text, cast, or_

    from src.models import AuditLog

    APPROVAL_EVENTS = ("approved", "rejected")

    def _exists_approval(cond_extra=None):
        """EXISTS: 存在满足条件的审批事件(approved/rejected)。"""
        sub = db.query(AuditLog.id).filter(
            AuditLog.task_id == Task.id, AuditLog.event.in_(APPROVAL_EVENTS)
        )
        if cond_extra is not None:
            sub = sub.filter(cond_extra)
        return sub.exists()

    q = db.query(Task)
    filters = []

    if keyword:
        kw = f"%{keyword.strip()}%"
        filters.append(
            or_(
                Task.created_by.ilike(kw),
                Task.user_query.ilike(kw),
                Task.id.ilike(kw),
                _exists_approval(AuditLog.actor.ilike(kw)),
                _exists_approval(cast(AuditLog.detail, Text).ilike(kw)),  # 备注 detail.comment
            )
        )
    if date_from:
        try:
            d_from = datetime.strptime(date_from, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(status_code=422, detail="date_from 格式应为 YYYY-MM-DD")
        filters.append(Task.created_at >= d_from)
    if date_to:
        try:
            d_to = datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1)
        except ValueError:
            raise HTTPException(status_code=422, detail="date_to 格式应为 YYYY-MM-DD")
        filters.append(Task.created_at < d_to)
    if submitted_by:
        filters.append(Task.created_by.ilike(f"%{submitted_by.strip()}%"))
    if approver:
        filters.append(_exists_approval(AuditLog.actor.ilike(f"%{approver.strip()}%")))

    # 审批结果
    if approval_result == "approved":
        filters.append(_exists_approval(AuditLog.event == "approved"))
    elif approval_result == "rejected":
        filters.append(_exists_approval(AuditLog.event == "rejected"))
    elif approval_result == "pending":
        filters.append(
            db.query(AuditLog.id)
            .filter(AuditLog.task_id == Task.id, AuditLog.event == "awaiting_approval")
            .exists()
        )
        filters.append(~_exists_approval())
    elif approval_result == "none":
        filters.append(
            ~db.query(AuditLog.id)
            .filter(
                AuditLog.task_id == Task.id,
                AuditLog.event.in_(("awaiting_approval",) + APPROVAL_EVENTS),
            )
            .exists()
        )

    if result:
        filters.append(Task.status == result)

    if filters:
        q = q.filter(*filters)
    total = q.count() or 0

    tasks = (
        q.order_by(Task.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    task_ids = [t.id for t in tasks]
    if not task_ids:
        return AuditLogResponse(logs=[], total=0, page=page, page_size=page_size)

    # 一次取回这些任务的审计事件, 组装审批信息
    audit_rows = (
        db.query(AuditLog)
        .filter(AuditLog.task_id.in_(task_ids))
        .order_by(AuditLog.created_at.asc())
        .all()
    )
    by_task: dict[str, list] = {}
    for r in audit_rows:
        by_task.setdefault(r.task_id, []).append(r)

    logs = []
    for t in tasks:
        evs = by_task.get(t.id, [])
        approver = None
        approval_result = "none"  # approved/rejected/pending/none
        approval_comment = None
        has_approval_flow = False
        for r in evs:
            if r.event in ("approved", "rejected"):
                approver = r.actor
                approval_result = "approved" if r.event == "approved" else "rejected"
                approval_comment = (r.detail or {}).get("comment") if isinstance(r.detail, dict) else None
            elif r.event == "awaiting_approval":
                has_approval_flow = True
        if approval_result == "none" and has_approval_flow:
            approval_result = "pending"
        result = "running" if t.status == "running" else t.status
        logs.append(
            AuditLogItem(
                task_id=t.id,
                query=t.user_query or "",
                submitted_by=t.created_by or "-",
                submitted_at=t.created_at.isoformat() if t.created_at else None,
                approver=approver,
                approval_result=approval_result,
                approval_comment=approval_comment,
                result=result,
                error=(t.error_log or "")[:500] if t.status == "failed" else None,
                updated_at=t.updated_at.isoformat() if t.updated_at else None,
            )
        )
    return AuditLogResponse(logs=logs, total=total, page=page, page_size=page_size)


@router.get(
    "/admin/audit-logs/export",
    summary="导出操作日志(CSV, 仅管理员)",
    response_class=PlainTextResponse,
)
def export_audit_logs(
    keyword: Optional[str] = Query(None, description="搜索关键字(与列表页一致)"),
    date_from: Optional[str] = Query(None, description="提交时间起(YYYY-MM-DD)"),
    date_to: Optional[str] = Query(None, description="提交时间止(YYYY-MM-DD)"),
    submitted_by: Optional[str] = Query(None),
    approver: Optional[str] = Query(None),
    approval_result: Optional[str] = Query(None),
    result: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    _: User = Depends(require_role("admin")),
) -> PlainTextResponse:
    """审计合规(P0): 按列表页相同筛选条件导出全量审计为 CSV(utf-8-sig, Excel 可直接打开)。

    审计日志为 append-only, 不提供任何清空接口 —— 导出归档是唯一的数据外取途径。
    """
    import csv
    import io
    from datetime import datetime, timedelta

    from sqlalchemy import Text, cast, or_

    from src.models import AuditLog

    q = db.query(Task)
    if keyword:
        kw = f"%{keyword.strip()}%"
        q = q.filter(
            or_(
                Task.created_by.ilike(kw),
                Task.user_query.ilike(kw),
                Task.id.ilike(kw),
                AuditLog.actor.ilike(kw),
            )
        )
    if date_from:
        try:
            q = q.filter(Task.created_at >= datetime.strptime(date_from, "%Y-%m-%d"))
        except ValueError:
            raise HTTPException(status_code=422, detail="date_from 格式应为 YYYY-MM-DD") from None
    if date_to:
        try:
            q = q.filter(Task.created_at < datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1))
        except ValueError:
            raise HTTPException(status_code=422, detail="date_to 格式应为 YYYY-MM-DD") from None
    if submitted_by:
        q = q.filter(Task.created_by.ilike(f"%{submitted_by.strip()}%"))
    if result:
        q = q.filter(Task.status == result)
    q = q.order_by(Task.created_at.desc()).limit(50000)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["task_id", "提交者", "需求", "状态", "提交时间", "审批人", "审批结果", "审批意见", "错误信息"])
    for t in q.all():
        evs = db.query(AuditLog).filter(AuditLog.task_id == t.id).order_by(AuditLog.created_at.asc()).all()
        approver = None
        approval_result = None
        approval_comment = None
        for r in evs:
            if r.event in ("approved", "rejected"):
                approver = r.actor
                approval_result = r.event
                approval_comment = (r.detail or {}).get("comment") if isinstance(r.detail, dict) else None
        writer.writerow([
            t.id, t.created_by or "-", (t.user_query or "").replace("\n", " "),
            t.status, t.created_at.isoformat() if t.created_at else "",
            approver or "", approval_result or "", approval_comment or "",
            (t.error_log or "")[:500] if t.status == "failed" else "",
        ])
    data = "\ufeff" + buf.getvalue()  # BOM 供 Excel 识别 UTF-8
    filename = f"audit-logs-{datetime.now().strftime('%Y%m%d-%H%M%S')}.csv"
    return PlainTextResponse(
        data,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/admin/audit-logs/{task_id}/events", summary="任务审计时间线(详情页, 仅管理员)")
def list_task_events(
    task_id: str,
    db: Session = Depends(get_db),
    _: User = Depends(require_role("admin")),
) -> list[AuditEventItem]:
    """某任务的全部审计事件(按时间升序), 供操作日志详情页展示完整时间线。"""
    from src.models import AuditLog

    rows = (
        db.query(AuditLog)
        .filter(AuditLog.task_id == task_id)
        .order_by(AuditLog.created_at.asc())
        .all()
    )
    return [
        AuditEventItem(
            id=r.id,
            event=r.event,
            actor=r.actor,
            node_name=r.node_name,
            detail=_audit_detail(r.detail),
            created_at=r.created_at.isoformat() if r.created_at else None,
        )
        for r in rows
    ]


def _audit_detail(detail) -> str:
    """审计日志详情: JSON 列可能是 dict, 统一转文本并截断。"""
    if detail is None:
        return ""
    if isinstance(detail, dict):
        try:
            detail = json.dumps(detail, ensure_ascii=False)
        except Exception:  # noqa: BLE001
            detail = str(detail)
    return str(detail)[:200]


@router.get("/approvals/pending",
    response_model=PendingApprovalResponse,
    summary="待审批任务列表(审批人/管理员)",
)
def list_pending_approvals(
    db: Session = Depends(get_db),
    _: User = Depends(require_role("approver", "admin")),
) -> PendingApprovalResponse:
    """返回所有等待人工审批的任务(审批中心数据源)。"""
    from sqlalchemy import desc

    rows = (
        db.query(Task)
        .filter(Task.status == "awaiting_approval")
        .order_by(desc(Task.created_at))
        .all()
    )
    return PendingApprovalResponse(
        tasks=[
            PendingApprovalItem(
                task_id=t.id,
                query=t.user_query,
                progress_detail=t.progress_detail or "",
                created_at=t.created_at.isoformat() if t.created_at else None,
            )
            for t in rows
        ],
        total=len(rows),
    )


@router.post(
    "/tasks/{task_id}/approve",
    summary="人机协同审批回调",
)
def approve_task(
    task_id: str,
    body: ApproveRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("approver", "admin")),
) -> dict:
    """审批回调: 通过 Command(resume=...) 从 LangGraph 挂起点恢复执行(官方 interrupt 模式)。

    - approved=True  -> 图从 human_approval 继续, 进入 Reporter
    - approved=False -> 任务按拒绝终止
    - 权限: 仅 approver/admin 可审批; 职责分离(SoD)——不能审批自己提交的任务
    - 审计: 审批人以登录用户 current_user 为准, 不信任请求体中的 approver 字段
      (该字段保留仅为兼容旧前端, 传值被忽略)
    """
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    if task.status != "awaiting_approval":
        raise HTTPException(status_code=409, detail=f"task not awaiting approval, current={task.status}")
    # 职责分离(SoD): 普通用户不能审批自己提交的任务; 管理员(超级角色)豁免——
    # 单管理员环境下管理员提交的任务需自己能批, 否则永远无法通过
    is_admin = "admin" in (current_user.roles or [])
    if task.created_by == current_user.name and not is_admin:
        raise HTTPException(status_code=403, detail="不能审批自己提交的任务(管理员可豁免)")

    try:
        from src.graph import resume_task

        result = resume_task(
            task_id=task_id,
            approved=body.approved,
            approver=current_user.name,
            comment=body.comment,
            client_ip=request.client.host if request.client else "",
            user_agent=request.headers.get("user-agent", ""),
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("approve_failed", task_id=task_id, error=str(exc))
        raise HTTPException(status_code=500, detail=f"审批恢复执行失败: {exc}") from exc

    logger.info("approval_recorded", task_id=task_id, approved=body.approved, approver=current_user.name)
    return {"task_id": task_id, "approved": body.approved, "status": result.get("status")}


@router.post("/tasks/{task_id}/cancel", summary="取消任务(阶段3 OR-08)")
def cancel_task(
    task_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    """取消任务: 置 Redis 取消标志 + 状态置 canceled。

    - pending/awaiting_approval: 立即生效
    - running: 图当前调用无法中途打断(单次 LLM/沙箱有超时), 执行结束落 canceled 而非 completed
    - completed/failed/canceled: 不可取消(409)
    """
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    # 权限: 普通用户只能取消自己提交的任务; 审批人/管理员可取消任意任务
    if not _is_privileged_user(current_user) and task.created_by != current_user.name:
        raise HTTPException(status_code=403, detail="无权取消其他用户的任务")
    if task.status in ("completed", "failed", "canceled"):
        raise HTTPException(status_code=409, detail=f"task cannot be canceled, current={task.status}")

    try:
        from src.api.deps import get_redis

        redis = next(get_redis())
        redis.set(f"cancel:{task_id}", "1", ex=86400)
    except Exception as exc:  # noqa: BLE001
        logger.warning("cancel_flag_redis_failed", task_id=task_id, error=str(exc))

    task.status = "canceled"
    task.progress = "任务已取消"
    task.current_node = task.current_node or "api"
    db.add(
        AuditLog(
            task_id=task_id,
            event="task_canceled",
            actor=current_user.name if current_user else "system",
            node_name="api",
        )
    )
    db.commit()
    logger.info("task_canceled", task_id=task_id)
    return {"task_id": task_id, "status": "canceled"}


# ---------------------------------------------------------------------------
# 会话(Chat Sessions): 多会话隔离, 消息落库, 首条消息自动生成标题
# ---------------------------------------------------------------------------
class SessionItem(BaseModel):
    id: int
    title: str
    is_pinned: bool = False
    message_count: int = 0
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class SessionListResponse(BaseModel):
    sessions: list[SessionItem]


class ChatMessageItem(BaseModel):
    id: int
    role: str
    type: str
    content: str
    task_id: Optional[str] = None
    has_pdf: bool = False
    has_board: bool = False
    status: Optional[str] = None  # task 消息附任务当前状态(历史回放时动态补, 供前端恢复轮询)
    created_at: Optional[str] = None


class SessionMessagesResponse(BaseModel):
    session_id: int
    messages: list[ChatMessageItem]


class ChatMessageCreate(BaseModel):
    role: str = Field(..., pattern="^(user|assistant)$", description="user / assistant")
    type: str = Field("text", pattern="^(text|task|chat)$")
    content: str = Field("", max_length=12000)
    task_id: Optional[str] = None
    has_pdf: bool = False
    has_board: bool = False


class SessionUpdate(BaseModel):
    """会话更新: 重命名标题 / 置顶开关(可选字段, 提供哪个更新哪个)。"""

    title: Optional[str] = Field(None, max_length=100)
    is_pinned: Optional[bool] = None


def _get_owned_session(db, session_id: int, owner: str) -> ChatSession:
    """按归属取会话; 不存在或非本人返回 404(不暴露存在性)。"""
    from src.models import ChatSession

    s = db.get(ChatSession, session_id)
    if s is None or s.owner != owner:
        raise HTTPException(status_code=404, detail="会话不存在")
    return s


@router.get("/chat/sessions", response_model=SessionListResponse, summary="我的会话列表")
def list_chat_sessions(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SessionListResponse:
    """当前用户的会话列表(仅本人; 置顶优先, 再按更新时间倒序; 旧会话保留不删除)。"""
    from src.models import ChatMessage, ChatSession

    rows = (
        db.query(ChatSession)
        .filter(ChatSession.owner == current_user.name)
        .order_by(
            ChatSession.is_pinned.desc(),
            ChatSession.updated_at.desc(),
            ChatSession.id.desc(),
        )
        .all()
    )
    counts = (
        dict(
            db.query(ChatMessage.session_id, func.count(ChatMessage.id))
            .filter(ChatMessage.session_id.in_([r.id for r in rows]))
            .group_by(ChatMessage.session_id)
            .all()
        )
        if rows
        else {}
    )
    return SessionListResponse(
        sessions=[
            SessionItem(
                id=r.id,
                title=r.title,
                is_pinned=bool(r.is_pinned),
                message_count=counts.get(r.id, 0),
                created_at=r.created_at.isoformat() if r.created_at else None,
                updated_at=r.updated_at.isoformat() if r.updated_at else None,
            )
            for r in rows
        ]
    )


@router.post("/chat/sessions", response_model=SessionItem, summary="新建会话")
def create_chat_session(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SessionItem:
    """新建会话(默认标题'新对话', 首条消息后自动生成标题)。"""
    from src.models import ChatSession

    s = ChatSession(owner=current_user.name, title="新对话")
    db.add(s)
    db.commit()
    db.refresh(s)
    return SessionItem(
        id=s.id,
        title=s.title,
        is_pinned=bool(s.is_pinned),
        message_count=0,
        created_at=s.created_at.isoformat() if s.created_at else None,
        updated_at=s.updated_at.isoformat() if s.updated_at else None,
    )


@router.patch("/chat/sessions/{session_id}", response_model=SessionItem, summary="更新会话(重命名/置顶)")
def update_chat_session(
    session_id: int,
    body: SessionUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SessionItem:
    """更新会话: 重命名标题或切换置顶(归属校验; 提供哪个字段更新哪个)。"""
    s = _get_owned_session(db, session_id, current_user.name)
    if body.title is not None:
        t = " ".join(body.title.split())[:100]
        if not t:
            raise HTTPException(status_code=422, detail="标题不能为空")
        s.title = t
    if body.is_pinned is not None:
        s.is_pinned = body.is_pinned
    db.commit()
    db.refresh(s)
    return SessionItem(
        id=s.id,
        title=s.title,
        is_pinned=bool(s.is_pinned),
        created_at=s.created_at.isoformat() if s.created_at else None,
        updated_at=s.updated_at.isoformat() if s.updated_at else None,
    )


@router.delete("/chat/sessions/{session_id}", summary="删除会话(消息级联删除, 任务保留)")
def delete_chat_session(
    session_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    """删除会话及其消息(级联)。

    说明: 会话内已触发的分析任务独立存储于 tasks 表(无外键), 删除会话
    不会删除任务, 任务仍完整保留并展示在"任务历史"中(符合需求)。
    """
    from src.models import ChatSession

    s = _get_owned_session(db, session_id, current_user.name)
    db.delete(s)  # 消息通过 FK ON DELETE CASCADE 一并删除
    db.commit()
    logger.info("chat_session_deleted", session_id=session_id, owner=current_user.name)
    return {"ok": True, "deleted_session_id": session_id}




@router.get(
    "/chat/sessions/{session_id}/messages",
    response_model=SessionMessagesResponse,
    summary="会话消息",
)
def list_session_messages(
    session_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SessionMessagesResponse:
    """会话全部消息(归属校验; 恢复/切换会话时前端加载)。"""
    from src.models import ChatMessage

    _get_owned_session(db, session_id, current_user.name)
    rows = (
        db.query(ChatMessage)
        .filter(ChatMessage.session_id == session_id)
        .order_by(ChatMessage.id.asc())
        .all()
    )
    return SessionMessagesResponse(
        session_id=session_id,
        messages=[
            ChatMessageItem(
                id=m.id,
                role=m.role,
                type=m.type,
                content=m.content,
                task_id=m.task_id,
                # 输出标志动态计算: 优先按任务当前状态(PDF/看板是否真实存在), 而非落库快照。
                # 快照会在旧版本前端(未传 has_pdf)或任务中途状态下失真, 导致历史消息丢失按钮。
                has_pdf=_msg_has_pdf(m.task_id, db, fallback=m.has_pdf),
                has_board=_msg_has_board(m.task_id, db, fallback=m.has_board),
                status=_msg_status(m.task_id, db),
                created_at=m.created_at.isoformat() if m.created_at else None,
            )
            for m in rows
        ],
    )


def _msg_has_pdf(task_id: str | None, db: Session, fallback: bool) -> bool:
    """消息 PDF 标志: 按任务最新 result_path 动态判断, 任务不存在时回退落库值。"""
    if not task_id:
        return fallback
    t = db.get(Task, task_id)
    if not t or not t.result_path:
        return fallback
    return t.result_path.endswith(".pdf")


def _msg_status(task_id: str | None, db: Session) -> str | None:
    """消息对应任务的当前状态(历史回放时动态补; 任务不存在返回 None)。"""
    if not task_id:
        return None
    t = db.get(Task, task_id)
    return t.status if t else None


def _msg_has_board(task_id: str | None, db: Session, fallback: bool) -> bool:
    """消息看板标志: 按 board.json 是否存在动态判断, 任务不存在时回退落库值。"""
    if not task_id:
        return fallback
    t = db.get(Task, task_id)
    if not t or not t.result_path:
        return fallback
    return _task_has_board(t.result_path, t.id)


@router.post(
    "/chat/sessions/{session_id}/messages",
    response_model=ChatMessageItem,
    summary="写入会话消息",
)
def post_session_message(
    session_id: int,
    body: ChatMessageCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ChatMessageItem:
    """写入一条消息(用户问题或助手回复)。

    - 会话归属校验(隔离)
    - 首条用户消息到达时自动生成标题(取前 30 字)
    - 任务消息存 report 内容快照, 历史会话可原样回放
    """
    from src.models import ChatMessage, ChatSession

    s = _get_owned_session(db, session_id, current_user.name)
    msg = ChatMessage(
        session_id=session_id,
        role=body.role,
        type=body.type,
        content=body.content,
        task_id=body.task_id,
        report_snapshot=body.content if body.type == "task" else None,
        has_pdf=body.has_pdf,
        has_board=body.has_board,
    )
    db.add(msg)
    # 首条用户消息 -> 自动生成会话标题(updated_at 由 ORM onupdate 自动刷新)
    if body.role == "user" and s.title == "新对话" and body.content.strip():
        t = " ".join(body.content.split())[:30]
        if t:
            s.title = t
    db.commit()
    db.refresh(msg)
    return ChatMessageItem(
        id=msg.id,
        role=msg.role,
        type=msg.type,
        content=msg.content,
        task_id=msg.task_id,
        has_pdf=msg.has_pdf,
        has_board=msg.has_board,
        created_at=msg.created_at.isoformat() if msg.created_at else None,
    )


# ---------------------------------------------------------------------------
# 用户管理增强: 删除用户 / 调整权限(仅管理员, 含安全约束与审计)
# ---------------------------------------------------------------------------
class UserRolesUpdate(BaseModel):
    roles: list[str] = Field(..., description="目标角色列表(可选 user/approver/admin)")


@router.delete("/users/{user_id}", summary="删除用户(仅管理员)")
def delete_user(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin")),
) -> dict:
    """删除用户: 禁止删除自己与唯一管理员; 历史任务/审计保留(审计留痕)。

    安全约束(调研: open-webui 禁止删主管理员):
    - 不能删除自己
    - 目标为唯一 admin 时拒绝(至少保留一个管理员)
    """
    from src.models import User as UserModel

    target = db.get(UserModel, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    if target.id == current_user.id:
        raise HTTPException(status_code=400, detail="不能删除自己")
    if "admin" in (target.roles or []):
        admin_count = sum(
            1 for u in db.query(UserModel).all() if "admin" in (u.roles or [])
        )
        if admin_count <= 1:
            raise HTTPException(status_code=403, detail="至少保留一个管理员, 无法删除")

    db.delete(target)
    db.commit()
    logger.info("user_deleted", username=target.username, actor=current_user.name)
    # 审计: 独立表记录删除(不被级联)
    db.add(
        AuditLog(
            event="user_deleted",
            actor=current_user.name,
            detail={"target_user_id": str(user_id), "target_username": target.username},
        )
    )
    db.commit()
    return {"ok": True, "deleted_user_id": str(user_id)}


@router.put("/users/{user_id}/roles", summary="调整用户权限(仅管理员)")
def update_user_roles(
    user_id: int,
    body: UserRolesUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin")),
) -> UserOut:
    """调整用户角色: 白名单校验; 不能调整自己; 至少保留一个管理员; 写审计。"""
    from src.models import User as UserModel

    roles = list(dict.fromkeys(body.roles))
    for r in roles:
        if r not in _ALLOWED_ROLES:
            raise HTTPException(status_code=422, detail=f"非法角色: {r}(可选 user/approver/admin)")

    target = db.get(UserModel, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    if target.id == current_user.id:
        raise HTTPException(status_code=400, detail="不能调整自己的权限")

    old_roles = list(target.roles or [])
    # 降级管理员保护: 目标从 admin 降级时, 至少保留一个管理员
    if "admin" in old_roles and "admin" not in roles:
        admin_count = sum(
            1 for u in db.query(UserModel).all() if "admin" in (u.roles or [])
        )
        if admin_count <= 1:
            raise HTTPException(status_code=403, detail="至少保留一个管理员, 无法降级")

    target.roles = roles
    db.commit()
    db.refresh(target)
    logger.info("user_roles_updated", username=target.username, roles=roles, actor=current_user.name)
    # 审计: 变更前后角色
    db.add(
        AuditLog(
            event="user_role_updated",
            actor=current_user.name,
            detail={
                "target_user_id": str(user_id),
                "target_username": target.username,
                "before": old_roles,
                "after": roles,
            },
        )
    )
    db.commit()
    return UserOut(id=str(target.id), name=target.username, roles=[str(r) for r in target.roles])


# ---------------------------------------------------------------------------
# 数据级权限管理(仅管理员): 表/列/行级策略 CRUD
# ---------------------------------------------------------------------------
class DataPolicyItem(BaseModel):
    id: int
    target_type: str
    target_name: str
    table_name: str
    row_filter: Optional[str] = None
    col_access: dict = {}
    mask_expression: Optional[str] = None
    enabled: bool = True
    created_by: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class DataPolicyListResponse(BaseModel):
    policies: list[DataPolicyItem]
    total: int


class DataPolicyCreate(BaseModel):
    target_type: str = Field(..., pattern="^(role|user)$", description="role | user")
    target_name: str = Field(..., min_length=1, max_length=64, description="角色名或用户名")
    table_name: str = Field(..., min_length=1, max_length=128, description="业务表名")
    row_filter: Optional[str] = Field(
        None, max_length=1000, description="行级过滤 WHERE 表达式片段(可选)"
    )
    col_access: dict = Field(
        default_factory=dict,
        description='{"列名": "allow|mask|deny", ...}',
    )
    mask_expression: Optional[str] = Field(
        None, max_length=500, description="mask 列脱敏表达式(PostgreSQL, 缺省 '***')"
    )
    enabled: bool = True


class DataPolicyUpdate(BaseModel):
    row_filter: Optional[str] = Field(None, max_length=1000)
    col_access: dict | None = None
    mask_expression: Optional[str] = Field(None, max_length=500)
    enabled: bool | None = None


def _validate_policy_expr(expr: str | None, field: str) -> None:
    """校验策略表达式: 必须能解析为单条合法表达式, 禁止分号/多语句。"""
    if not expr:
        return
    expr = expr.strip()
    if ";" in expr or "\n" in expr:
        raise HTTPException(status_code=422, detail=f"{field} 不能包含分号或多语句")
    try:
        import sqlglot

        sqlglot.parse_one(expr, read="postgres")
    except Exception:  # noqa: BLE001
        raise HTTPException(
            status_code=422, detail=f"{field} 不是合法的 SQL 表达式: {expr[:80]}"
        ) from None


def _validate_col_access(col_access: dict) -> None:
    """校验列访问映射: 值必须为 allow/mask/deny。"""
    from src.models.data_policy import COL_MODES

    for col, mode in (col_access or {}).items():
        if not isinstance(col, str) or not col:
            raise HTTPException(status_code=422, detail="列名不合法")
        if mode not in COL_MODES:
            raise HTTPException(
                status_code=422, detail=f"列 {col} 的访问模式非法: {mode}(可选 allow/mask/deny)"
            )


@router.get("/admin/data-policies", response_model=DataPolicyListResponse, summary="数据权限规则列表(仅管理员)")
def list_data_policies(
    db: Session = Depends(get_db),
    _: User = Depends(require_role("admin")),
) -> DataPolicyListResponse:
    """数据级权限规则列表(按表分组展示)。"""
    from src.models import DataPolicyRule

    rows = db.query(DataPolicyRule).order_by(DataPolicyRule.table_name, DataPolicyRule.target_type).all()
    return DataPolicyListResponse(
        policies=[
            DataPolicyItem(
                id=r.id,
                target_type=r.target_type,
                target_name=r.target_name,
                table_name=r.table_name,
                row_filter=r.row_filter,
                col_access=r.col_access or {},
                mask_expression=r.mask_expression,
                enabled=bool(r.enabled),
                created_by=r.created_by,
                created_at=r.created_at.isoformat() if r.created_at else None,
                updated_at=r.updated_at.isoformat() if r.updated_at else None,
            )
            for r in rows
        ],
        total=len(rows),
    )


@router.post("/admin/data-policies", response_model=DataPolicyItem, summary="创建数据权限规则(仅管理员)")
def create_data_policy(
    body: DataPolicyCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin")),
) -> DataPolicyItem:
    """创建数据权限规则(同一 目标+表 唯一, 重复创建 409)。"""
    from sqlalchemy.exc import IntegrityError

    from src.models import DataPolicyRule

    _validate_policy_expr(body.row_filter, "row_filter")
    _validate_policy_expr(body.mask_expression, "mask_expression")
    _validate_col_access(body.col_access)

    rule = DataPolicyRule(
        target_type=body.target_type,
        target_name=body.target_name,
        table_name=body.table_name,
        row_filter=(body.row_filter or "").strip() or None,
        col_access=body.col_access,
        mask_expression=(body.mask_expression or "").strip() or None,
        enabled=body.enabled,
        created_by=current_user.name,
    )
    db.add(rule)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"规则已存在: {body.target_type}:{body.target_name} @ {body.table_name}(可更新)",
        ) from None
    db.refresh(rule)
    _audit_data_policy(db, "data_policy_created", current_user, rule)
    return DataPolicyItem(
        id=rule.id,
        target_type=rule.target_type,
        target_name=rule.target_name,
        table_name=rule.table_name,
        row_filter=rule.row_filter,
        col_access=rule.col_access or {},
        mask_expression=rule.mask_expression,
        enabled=bool(rule.enabled),
        created_by=rule.created_by,
        created_at=rule.created_at.isoformat() if rule.created_at else None,
        updated_at=rule.updated_at.isoformat() if rule.updated_at else None,
    )


@router.put("/admin/data-policies/{policy_id}", response_model=DataPolicyItem, summary="更新数据权限规则(仅管理员)")
def update_data_policy(
    policy_id: int,
    body: DataPolicyUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin")),
) -> DataPolicyItem:
    """更新规则: row_filter / col_access / mask_expression / enabled(提供哪个更新哪个)。"""
    from src.models import DataPolicyRule

    rule = db.get(DataPolicyRule, policy_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="规则不存在")
    old = {
        "row_filter": rule.row_filter,
        "col_access": rule.col_access or {},
        "mask_expression": rule.mask_expression,
        "enabled": rule.enabled,
    }
    if body.row_filter is not None:
        _validate_policy_expr(body.row_filter, "row_filter")
        rule.row_filter = body.row_filter.strip() or None
    if body.col_access is not None:
        _validate_col_access(body.col_access)
        rule.col_access = body.col_access
    if body.mask_expression is not None:
        _validate_policy_expr(body.mask_expression, "mask_expression")
        rule.mask_expression = body.mask_expression.strip() or None
    if body.enabled is not None:
        rule.enabled = body.enabled
    db.commit()
    db.refresh(rule)
    _audit_data_policy(
        db, "data_policy_updated", current_user, rule, detail={"before": old}
    )
    return DataPolicyItem(
        id=rule.id,
        target_type=rule.target_type,
        target_name=rule.target_name,
        table_name=rule.table_name,
        row_filter=rule.row_filter,
        col_access=rule.col_access or {},
        mask_expression=rule.mask_expression,
        enabled=bool(rule.enabled),
        created_by=rule.created_by,
        created_at=rule.created_at.isoformat() if rule.created_at else None,
        updated_at=rule.updated_at.isoformat() if rule.updated_at else None,
    )


@router.delete("/admin/data-policies/{policy_id}", summary="删除数据权限规则(仅管理员)")
def delete_data_policy(
    policy_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin")),
) -> dict:
    """删除规则后恢复默认允许(该表/目标不再受限)。"""
    from src.models import DataPolicyRule

    rule = db.get(DataPolicyRule, policy_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="规则不存在")
    detail = {
        "target": f"{rule.target_type}:{rule.target_name}",
        "table": rule.table_name,
        "row_filter": rule.row_filter,
        "col_access": rule.col_access or {},
    }
    db.delete(rule)
    db.commit()
    _audit_data_policy(db, "data_policy_deleted", current_user, None, detail=detail)
    return {"ok": True, "deleted_policy_id": policy_id}


def _audit_data_policy(
    db: Session, event: str, actor: User, rule, detail: dict | None = None
) -> None:
    """数据权限变更审计(谁改了谁能看什么)。"""
    info = detail or {}
    if rule is not None:
        info.update(
            {
                "target": f"{rule.target_type}:{rule.target_name}",
                "table": rule.table_name,
                "row_filter": rule.row_filter,
                "col_access": rule.col_access or {},
                "enabled": bool(rule.enabled),
            }
        )
    db.add(
        AuditLog(
            event=event,
            actor=actor.name,
            node_name="api",
            detail=info,
        )
    )
    db.commit()


# ---------------------------------------------------------------------------
# 数据源配置化(仅管理员): 注册真实业务库, 任务可按数据源路由
# ---------------------------------------------------------------------------
class DataSourceItem(BaseModel):
    id: int
    name: str
    tables_whitelist: list = []
    description: str = ""
    enabled: bool = True
    conn_fields: dict = {}  # 脱敏展示: {"host","port","dbname","user"} 不含密码
    created_by: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class DataSourceListResponse(BaseModel):
    sources: list[DataSourceItem]
    total: int


class DataSourceConnFields(BaseModel):
    """分字段连接(非程序员友好表单): 由后端拼接连接串。"""

    host: str = Field(..., description="主机地址(如 db.example.com 或 127.0.0.1)")
    port: Optional[int] = Field(None, description="端口(默认 5432)")
    dbname: str = Field(..., description="数据库名")
    user: str = Field("", description="用户名")
    password: str = Field("", description="密码(不落明文, 加密后存库)")


class DataSourceCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=64, description="数据源名称(唯一)")
    db_url: Optional[str] = Field(None, max_length=1000, description="连接串(与 conn_fields 二选一)")
    conn_fields: Optional[DataSourceConnFields] = None
    tables_whitelist: list[str] = Field(default_factory=list, description="表白名单(空=全部)")
    description: str = Field(default="", max_length=256)
    enabled: bool = True


class DataSourceUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=64)
    db_url: Optional[str] = Field(None, max_length=1000, description="留空=不修改连接串")
    conn_fields: Optional[DataSourceConnFields] = None
    tables_whitelist: Optional[list[str]] = None
    description: Optional[str] = Field(None, max_length=256)
    enabled: Optional[bool] = None


def _resolve_ds_url(db_url: str | None, conn_fields) -> str:
    """db_url 与 conn_fields 二选一; 优先 conn_fields(分字段表单)。"""
    from src.tools.data_source import build_db_url, validate_db_url

    if conn_fields is not None:
        try:
            url = build_db_url(
                conn_fields.host, conn_fields.port or "", conn_fields.dbname,
                conn_fields.user, conn_fields.password,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
    elif db_url:
        url = db_url.strip()
    else:
        raise HTTPException(status_code=422, detail="请填写连接信息(连接串或分字段表单)")
    ok, err = validate_db_url(url)
    if not ok:
        raise HTTPException(status_code=422, detail=err)
    return url


def _ds_conn_fields(db_url_enc: str) -> dict:
    """从加密连接串解析脱敏展示字段(不返回密码)。"""
    try:
        from src.utils.security import decrypt

        from src.tools.schema_provider import parse_db_url

        p = parse_db_url(decrypt(db_url_enc))
        return {"host": p["host"], "port": p["port"], "dbname": p["dbname"], "user": p["user"]}
    except Exception:  # noqa: BLE001 — 解析失败返回空
        return {}


def _ds_to_item(ds) -> DataSourceItem:
    return DataSourceItem(
        id=ds.id,
        name=ds.name,
        tables_whitelist=ds.tables_whitelist or [],
        description=ds.description or "",
        enabled=bool(ds.enabled),
        conn_fields=_ds_conn_fields(ds.db_url_enc),
        created_by=ds.created_by,
        created_at=ds.created_at.isoformat() if ds.created_at else None,
        updated_at=ds.updated_at.isoformat() if ds.updated_at else None,
    )


@router.get("/data-sources", response_model=DataSourceListResponse, summary="数据源列表(仅管理员)")
def list_data_sources(
    db: Session = Depends(get_db),
    _: User = Depends(require_role("admin")),
) -> DataSourceListResponse:
    """数据源列表(不返回连接串, 仅元信息)。"""
    from src.models import DataSource

    rows = db.query(DataSource).order_by(DataSource.id).all()
    return DataSourceListResponse(sources=[_ds_to_item(r) for r in rows], total=len(rows))


@router.post("/data-sources", response_model=DataSourceItem, summary="创建数据源(仅管理员)")
def create_data_source(
    body: DataSourceCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin")),
) -> DataSourceItem:
    """注册数据源: 连接信息加密落库 + 连接性校验。"""
    from src.models import DataSource
    from src.utils.security import encrypt

    url = _resolve_ds_url(body.db_url, body.conn_fields)
    ds = DataSource(
        name=body.name.strip(),
        db_url_enc=encrypt(url),
        tables_whitelist=body.tables_whitelist or [],
        description=body.description,
        enabled=body.enabled,
        created_by=current_user.name,
    )
    db.add(ds)
    db.commit()
    db.refresh(ds)
    logger.info("data_source_created", name=ds.name, actor=current_user.name)
    return _ds_to_item(ds)


@router.put("/data-sources/{source_id}", response_model=DataSourceItem, summary="更新数据源(仅管理员)")
def update_data_source(
    source_id: int,
    body: DataSourceUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin")),
) -> DataSourceItem:
    """更新数据源(提供哪个字段更新哪个; 连接信息不填则不修改)。"""
    from src.models import DataSource
    from src.utils.security import encrypt

    ds = db.get(DataSource, source_id)
    if ds is None:
        raise HTTPException(status_code=404, detail="数据源不存在")
    if body.name is not None:
        ds.name = body.name.strip()
    if body.db_url or body.conn_fields is not None:
        url = _resolve_ds_url(body.db_url, body.conn_fields)
        ds.db_url_enc = encrypt(url)
    if body.tables_whitelist is not None:
        ds.tables_whitelist = body.tables_whitelist
    if body.description is not None:
        ds.description = body.description
    if body.enabled is not None:
        ds.enabled = body.enabled
    db.commit()
    db.refresh(ds)
    logger.info("data_source_updated", name=ds.name, actor=current_user.name)
    return _ds_to_item(ds)


@router.delete("/data-sources/{source_id}", summary="删除数据源(仅管理员)")
def delete_data_source(
    source_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin")),
) -> dict:
    """删除数据源(历史任务保留 data_source_id 引用, 路由时回退主库)。"""
    from src.models import DataSource

    ds = db.get(DataSource, source_id)
    if ds is None:
        raise HTTPException(status_code=404, detail="数据源不存在")
    name = ds.name
    db.delete(ds)
    db.commit()
    logger.info("data_source_deleted", name=name, actor=current_user.name)
    return {"ok": True, "deleted_source_id": source_id}


@router.post("/data-sources/test", summary="测试数据源连接(仅管理员)")
def test_data_source(body: DataSourceCreate):
    """仅测试连接, 不落库(前端"测试连接"按钮; 支持分字段或连接串)。"""
    from src.tools.data_source import test_data_source_connection

    url = _resolve_ds_url(body.db_url, body.conn_fields)
    ok, err = test_data_source_connection(url)
    if not ok:
        raise HTTPException(status_code=422, detail=err)
    return {"ok": True, "message": "连接成功"}


class SchemaTablesRequest(BaseModel):
    """拉取表清单请求: 用分字段/连接串 或 已有数据源。"""

    db_url: Optional[str] = None
    conn_fields: Optional[DataSourceConnFields] = None
    data_source_id: Optional[int] = None


@router.post("/admin/schema-tables", summary="拉取数据源表与列清单(仅管理员)")
def list_schema_tables(body: SchemaTablesRequest, db: Session = Depends(get_db)):
    """返回数据库 public schema 的表+列清单, 供 数据源表白名单勾选 / 指标选列 / 权限选列。"""
    from src.tools.data_source import fetch_schema_tables, resolve_db_url

    url = body.db_url
    if body.conn_fields is not None or url:
        url = _resolve_ds_url(body.db_url, body.conn_fields)
    else:
        url = resolve_db_url(body.data_source_id)
    try:
        tables = fetch_schema_tables(url)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"无法读取表清单: {str(exc)[:200]}") from None
    return {"tables": tables, "total": len(tables)}


# ---------------------------------------------------------------------------
# 指标口径自助管理(仅管理员): 指标/语义层 CRUD —— 口径锁定变成业务方可维护的资产
# ---------------------------------------------------------------------------
class MetricItem(BaseModel):
    id: int
    name_en: str
    name_cn: str
    alias: list = []
    description: str = ""
    agg: str = "sum"
    expr: str = ""
    filter: str = ""
    unit: str = ""
    source_tables: list = []
    category: str = "general"
    status: str = "active"
    updated_at: Optional[str] = None


class MetricListResponse(BaseModel):
    metrics: list[MetricItem]
    total: int


class MetricCreate(BaseModel):
    name_en: str = Field(..., min_length=1, max_length=128, pattern=r"^[a-zA-Z_][a-zA-Z0-9_]*$", description="英文标识(唯一)")
    name_cn: str = Field(..., min_length=1, max_length=128)
    alias: list[str] = Field(default_factory=list, description="同义词/业务叫法")
    description: str = Field(default="", max_length=1000)
    agg: str = Field("sum", pattern="^(sum|count|count_distinct|avg|max|min|custom)$")
    expr: str = Field("", max_length=2000, description="口径表达式(如 total_item_amount)")
    filter: str = Field("", max_length=1000, description="默认过滤(如 order_status='已完成')")
    unit: str = Field("", max_length=32)
    source_tables: list[str] = Field(default_factory=list)
    category: str = Field("general", max_length=64)
    status: str = Field("active", pattern="^(active|deprecated)$")


class MetricUpdate(BaseModel):
    name_cn: Optional[str] = None
    alias: Optional[list[str]] = None
    description: Optional[str] = None
    agg: Optional[str] = Field(None, pattern="^(sum|count|count_distinct|avg|max|min|custom)$")
    expr: Optional[str] = None
    filter: Optional[str] = None
    unit: Optional[str] = None
    source_tables: Optional[list[str]] = None
    category: Optional[str] = None
    status: Optional[str] = Field(None, pattern="^(active|deprecated)$")


def _metric_to_item(m) -> MetricItem:
    return MetricItem(
        id=m.id,
        name_en=m.name_en,
        name_cn=m.name_cn,
        alias=m.alias or [],
        description=m.description or "",
        agg=m.agg,
        expr=m.expr or "",
        filter=m.filter or "",
        unit=m.unit or "",
        source_tables=m.source_tables or [],
        category=m.category or "general",
        status=m.status,
        updated_at=m.updated_at.isoformat() if m.updated_at else None,
    )


@router.get("/admin/metric-definitions", response_model=MetricListResponse, summary="指标定义列表(仅管理员)")
def list_metric_definitions(
    db: Session = Depends(get_db),
    _: User = Depends(require_role("admin")),
) -> MetricListResponse:
    """全部指标定义(含 deprecated)。"""
    from src.models import MetricDefinition

    rows = db.query(MetricDefinition).order_by(MetricDefinition.id).all()
    return MetricListResponse(metrics=[_metric_to_item(r) for r in rows], total=len(rows))


@router.post("/admin/metric-definitions", response_model=MetricItem, summary="创建指标定义(仅管理员)")
def create_metric_definition(
    body: MetricCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin")),
) -> MetricItem:
    """新增指标: 口径(agg+expr+filter)入库即对 LLM 生效(口径锁定的唯一事实来源)。"""
    from src.models import MetricDefinition
    from src.tools.metric_registry import reload_metric_registry

    exists = db.query(MetricDefinition).filter(MetricDefinition.name_en == body.name_en).first()
    if exists:
        raise HTTPException(status_code=409, detail=f"指标 {body.name_en} 已存在")
    m = MetricDefinition(
        name_en=body.name_en,
        name_cn=body.name_cn,
        alias=body.alias or [],
        description=body.description,
        agg=body.agg,
        expr=body.expr,
        filter=body.filter,
        unit=body.unit,
        source_tables=body.source_tables or [],
        category=body.category or "general",
        status=body.status,
    )
    db.add(m)
    db.commit()
    db.refresh(m)
    reload_metric_registry()
    logger.info("metric_defined", name_en=m.name_en, actor=current_user.name)
    return _metric_to_item(m)


@router.put("/admin/metric-definitions/{metric_id}", response_model=MetricItem, summary="更新指标定义(仅管理员)")
def update_metric_definition(
    metric_id: int,
    body: MetricUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin")),
) -> MetricItem:
    """更新指标口径(提供哪个更新哪个); 变更后注册器热重载。"""
    from src.models import MetricDefinition
    from src.tools.metric_registry import reload_metric_registry

    m = db.get(MetricDefinition, metric_id)
    if m is None:
        raise HTTPException(status_code=404, detail="指标不存在")
    for f in ("name_cn", "alias", "description", "agg", "expr", "filter", "unit",
              "source_tables", "category", "status"):
        v = getattr(body, f)
        if v is not None:
            setattr(m, f, v)
    db.commit()
    db.refresh(m)
    reload_metric_registry()
    logger.info("metric_updated", name_en=m.name_en, actor=current_user.name)
    return _metric_to_item(m)


@router.delete("/admin/metric-definitions/{metric_id}", summary="下线指标定义(仅管理员)")
def delete_metric_definition(
    metric_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin")),
) -> dict:
    """下线指标: 置 status=deprecated(LLM 不再使用), 保留历史定义与血缘可追溯。"""
    from src.models import MetricDefinition
    from src.tools.metric_registry import reload_metric_registry

    m = db.get(MetricDefinition, metric_id)
    if m is None:
        raise HTTPException(status_code=404, detail="指标不存在")
    m.status = "deprecated"
    db.commit()
    reload_metric_registry()
    logger.info("metric_deprecated", name_en=m.name_en, actor=current_user.name)
    return {"ok": True, "deprecated_metric_id": metric_id}


# ---------------------------------------------------------------------------
# 报告数字可核验(P1): 任务血缘列表 + 单条 SQL 重跑看明细 —— 每个数字来源可验证
# ---------------------------------------------------------------------------
class LineageItem(BaseModel):
    id: int
    run_order: int
    sql_text: str
    tables: list = []
    status: str = "success"
    rows_returned: int = 0
    duration_ms: int = 0
    created_at: Optional[str] = None


class LineageResponse(BaseModel):
    task_id: str
    runs: list[LineageItem]
    total: int


@router.get("/tasks/{task_id}/lineage", response_model=LineageResponse, summary="任务血缘/溯源(报告数字来源)")
def list_task_lineage(
    task_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> LineageResponse:
    """报告溯源: 该任务所有 SQL 执行记录(SQL/涉及表/行数/耗时), 供"数据核验"面板展示。"""
    from src.models import QueryRun

    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    if not _is_privileged_user(current_user) and task.created_by != current_user.name:
        raise HTTPException(status_code=403, detail="无权查看其他用户的任务")
    rows = (
        db.query(QueryRun)
        .filter(QueryRun.task_id == task_id)
        .order_by(QueryRun.run_order.asc(), QueryRun.id.asc())
        .all()
    )
    return LineageResponse(
        task_id=task_id,
        runs=[
            LineageItem(
                id=r.id,
                run_order=r.run_order,
                sql_text=r.sql_text,
                tables=r.tables or [],
                status=r.status,
                rows_returned=r.rows_returned,
                duration_ms=r.duration_ms,
                created_at=r.created_at.isoformat() if r.created_at else None,
            )
            for r in rows
        ],
        total=len(rows),
    )


@router.post("/tasks/{task_id}/query-runs/{run_id}/rerun", summary="重跑溯源 SQL(报告数字核验)")
def rerun_query_run(
    task_id: str,
    run_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    """重新执行某条溯源 SQL, 返回真实明细(受数据权限约束), 用于核验报告数字。

    - 权限: 与任务查看一致(本人或审批人/管理员); 且重跑仍走 apply_data_policy,
      权限变更后无法绕过
    - 返回前 50 行明细 + 行数, 便于用户点击数字对账
    """
    from src.models import QueryRun
    from src.sandbox.docker_sandbox import run_in_sandbox

    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    if not _is_privileged_user(current_user) and task.created_by != current_user.name:
        raise HTTPException(status_code=403, detail="无权查看其他用户的任务")
    run = (
        db.query(QueryRun)
        .filter(QueryRun.id == run_id, QueryRun.task_id == task_id)
        .first()
    )
    if run is None:
        raise HTTPException(status_code=404, detail="溯源记录不存在")

    # 数据级权限强制(与 executor 一致)
    from src.tools.data_policy import apply_data_policy

    new_sql, denied = apply_data_policy(run.sql_text, current_user.name, current_user.roles)
    if denied:
        raise HTTPException(status_code=403, detail=f"数据权限拒绝: {denied}")
    from src.tools.data_source import resolve_db_url

    res = run_in_sandbox(new_sql, backend="auto", db_url=resolve_db_url(getattr(task, "data_source_id", None)))
    if res.get("status") != "success":
        return {"ok": False, "error": (res.get("error") or "")[:300]}
    output = res.get("output") or ""
    lines = output.splitlines()
    # rows=N + header + 样本行(最多 10); 明细返回前 50 行(从输出解析)
    return {
        "ok": True,
        "row_count": res.get("row_count", 0),
        "sql_text": new_sql,
        "sample": lines[-10:] if len(lines) > 10 else lines,
    }


# ---------------------------------------------------------------------------
# 定时任务(仅管理员) + 站内通知(本人): P1 定时分析 + 结果推送
# ---------------------------------------------------------------------------
class ScheduledTaskItem(BaseModel):
    id: int
    name: str
    query: str
    cron: str
    schedule_type: str = "daily"
    schedule_time: str = "09:00"
    schedule_weekday: str = "1"
    cron_desc: str = ""  # cron 的业务语言描述(如 每天 09:00)
    data_source_id: Optional[int] = None
    data_source_ids: list = []
    owner: Optional[str] = None
    notify_users: list = []
    approval_status: str = "pending"
    approved_by: Optional[str] = None
    approved_at: Optional[str] = None
    enabled: bool = True
    last_run_at: Optional[str] = None
    next_run_at: Optional[str] = None
    created_at: Optional[str] = None


class ScheduledTaskListResponse(BaseModel):
    tasks: list[ScheduledTaskItem]
    total: int


class ScheduledTaskCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    query: str = Field(..., min_length=2, max_length=5000)
    cron: Optional[str] = Field(None, max_length=64, description="自定义 cron(高级模式, 与 schedule_type 二选一)")
    schedule_type: str = Field("daily", pattern="^(daily|weekly|monthly|custom)$", description="daily/weekly/monthly/custom")
    schedule_time: str = Field("09:00", max_length=8, description="执行时间 HH:MM")
    schedule_weekday: str = Field("1", max_length=16, description="weekly: cron 星期值 0=周日..6=周六")
    schedule_day: int = Field(1, ge=1, le=28, description="monthly: 每月第几天")
    data_source_id: Optional[int] = None
    data_source_ids: list[int] = Field(default_factory=list, description="目标数据源列表(空=主库; 仅管理员生效)")
    notify_users: list[str] = Field(default_factory=list, description="推送人员范围(仅管理员生效)")
    enabled: bool = True


class ScheduledTaskUpdate(BaseModel):
    name: Optional[str] = None
    query: Optional[str] = None
    cron: Optional[str] = None
    schedule_type: Optional[str] = Field(None, pattern="^(daily|weekly|monthly|custom)$")
    schedule_time: Optional[str] = Field(None, max_length=8)
    schedule_weekday: Optional[str] = Field(None, max_length=16)
    schedule_day: Optional[int] = Field(None, ge=1, le=28)
    data_source_id: Optional[int] = None
    data_source_ids: Optional[list[int]] = None
    notify_users: Optional[list[str]] = None
    enabled: Optional[bool] = None


def _sched_to_item(s) -> ScheduledTaskItem:
    from src.tools.scheduler import cron_description

    return ScheduledTaskItem(
        id=s.id,
        name=s.name,
        query=s.query,
        cron=s.cron,
        schedule_type=getattr(s, "schedule_type", None) or "daily",
        schedule_time=getattr(s, "schedule_time", None) or "09:00",
        schedule_weekday=getattr(s, "schedule_weekday", None) or "1",
        cron_desc=cron_description(s.cron),
        data_source_id=s.data_source_id,
        data_source_ids=_sched_ds_ids(s),
        owner=s.owner,
        notify_users=s.notify_users or [],
        approval_status=getattr(s, "approval_status", None) or "pending",
        approved_by=s.approved_by,
        approved_at=s.approved_at.isoformat() if s.approved_at else None,
        enabled=bool(s.enabled),
        last_run_at=s.last_run_at.isoformat() if s.last_run_at else None,
        next_run_at=s.next_run_at.isoformat() if s.next_run_at else None,
        created_at=s.created_at.isoformat() if s.created_at else None,
    )


def _sched_ds_ids(s) -> list[int]:
    """任务目标数据源列表: 新字段优先, 兼容旧 data_source_id。"""
    ids = list(getattr(s, "data_source_ids", None) or [])
    if not ids and getattr(s, "data_source_id", None):
        ids = [s.data_source_id]
    return ids


def _resolve_ds_ids(body, is_admin: bool) -> list[int]:
    """解析请求中的目标数据源列表(仅管理员; 空=[主库])。"""
    if not is_admin:
        return []
    if body.data_source_ids:
        return [int(x) for x in body.data_source_ids]
    if body.data_source_id:
        return [int(body.data_source_id)]
    return []


def _resolve_schedule_cron(body) -> tuple[str, str, str, str]:
    """把自然语言频率翻译为 cron; 返回 (cron, schedule_type, schedule_time, schedule_weekday)。

    优先 schedule_type 翻译; 显式给了 cron 且无 schedule_type 时直接用 cron(兼容旧前端)。
    """
    from src.tools.scheduler import build_cron

    provided = body.model_fields_set  # 显式提供的字段(区分默认值与未传)
    sched_type = body.schedule_type if "schedule_type" in provided else None
    custom = body.cron if "cron" in provided else ""
    # 兼容: 只显式传了 cron 而未传 schedule_type -> 视为自定义 cron
    if not sched_type and custom:
        sched_type = "custom"
    sched_type = sched_type or "daily"
    time_str = body.schedule_time if "schedule_time" in provided else "09:00"
    weekday = body.schedule_weekday if "schedule_weekday" in provided else "1"
    day = body.schedule_day if "schedule_day" in provided else 1
    try:
        cron = build_cron(sched_type, time_str, weekday, day, custom)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    _validate_cron(cron)
    return cron, sched_type, time_str, weekday


def _validate_cron(expr: str) -> None:
    try:
        from apscheduler.triggers.cron import CronTrigger

        CronTrigger.from_crontab(expr.strip())
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"非法 cron 表达式: {expr}") from None


@router.get("/scheduled-tasks", response_model=ScheduledTaskListResponse, summary="定时任务列表(本人; 管理员看全部)")
def list_scheduled_tasks(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ScheduledTaskListResponse:
    """定时任务列表: 普通用户/审批人只看自己的; 管理员可看全部(可选 owner 过滤)。"""
    from src.models import ScheduledTask

    q = db.query(ScheduledTask)
    is_admin = "admin" in (current_user.roles or [])
    if not is_admin:
        q = q.filter(ScheduledTask.owner == current_user.name)
    rows = q.order_by(ScheduledTask.id.desc()).all()
    return ScheduledTaskListResponse(tasks=[_sched_to_item(r) for r in rows], total=len(rows))


@router.post("/scheduled-tasks", response_model=ScheduledTaskItem, summary="创建定时任务(登录用户)")
def create_scheduled_task(
    body: ScheduledTaskCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ScheduledTaskItem:
    """创建定时任务: owner=当前用户; 结果默认只通知创建人。

    推送人员范围(notify_users)仅管理员可设置; 普通用户传该字段会被忽略。
    """
    from src.models import ScheduledTask
    from src.tools.scheduler import refresh_schedule

    cron, sched_type, time_str, weekday = _resolve_schedule_cron(body)
    is_admin = "admin" in (current_user.roles or [])
    notify_users = body.notify_users if is_admin else []
    ds_ids = _resolve_ds_ids(body, is_admin)
    s = ScheduledTask(
        name=body.name.strip(),
        query=body.query.strip(),
        cron=cron,
        schedule_type=sched_type,
        schedule_time=time_str,
        schedule_weekday=weekday,
        data_source_id=ds_ids[0] if ds_ids else None,
        data_source_ids=ds_ids,
        owner=current_user.name,
        notify_users=notify_users,
        enabled=body.enabled,
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    refresh_schedule()  # 立即纳入调度
    logger.info("scheduled_task_created", name=s.name, actor=current_user.name)
    return _sched_to_item(s)


@router.put("/scheduled-tasks/{task_id}", response_model=ScheduledTaskItem, summary="更新定时任务(本人/管理员)")
def update_scheduled_task(
    task_id: int,
    body: ScheduledTaskUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ScheduledTaskItem:
    from src.models import ScheduledTask
    from src.tools.scheduler import refresh_schedule

    s = db.get(ScheduledTask, task_id)
    if s is None:
        raise HTTPException(status_code=404, detail="定时任务不存在")
    is_admin = "admin" in (current_user.roles or [])
    # 权限: 本人可改自己的任务; 管理员可改任意
    if not is_admin and s.owner != current_user.name:
        raise HTTPException(status_code=403, detail="无权操作他人的定时任务")
    if body.name is not None:
        s.name = body.name.strip()
    if body.query is not None:
        s.query = body.query.strip()
    # 频率变更: 有显式提供的频率字段则整体翻译; 否则保持现状(兼容旧前端)
    freq_changed = bool(
        body.model_fields_set
        & {"cron", "schedule_type", "schedule_time", "schedule_weekday", "schedule_day"}
    )
    if freq_changed:
        cron, sched_type, time_str, weekday = _resolve_schedule_cron(body)
        s.cron = cron
        s.schedule_type = sched_type
        s.schedule_time = time_str
        s.schedule_weekday = weekday
    if body.data_source_ids is not None and is_admin:
        ds_ids = [int(x) for x in body.data_source_ids]
        s.data_source_ids = ds_ids
        s.data_source_id = ds_ids[0] if ds_ids else None
    if body.data_source_id is not None and is_admin:
        s.data_source_ids = [int(body.data_source_id)]
        s.data_source_id = int(body.data_source_id)
    if body.notify_users is not None and is_admin:
        s.notify_users = body.notify_users
    if body.enabled is not None:
        s.enabled = body.enabled
    db.commit()
    db.refresh(s)
    refresh_schedule()
    logger.info("scheduled_task_updated", name=s.name)
    return _sched_to_item(s)


@router.delete("/scheduled-tasks/{task_id}", summary="删除定时任务(本人/管理员)")
def delete_scheduled_task(
    task_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    from src.models import ScheduledTask
    from src.tools.scheduler import refresh_schedule

    s = db.get(ScheduledTask, task_id)
    if s is None:
        raise HTTPException(status_code=404, detail="定时任务不存在")
    is_admin = "admin" in (current_user.roles or [])
    if not is_admin and s.owner != current_user.name:
        raise HTTPException(status_code=403, detail="无权操作他人的定时任务")
    db.delete(s)
    db.commit()
    refresh_schedule()
    return {"ok": True, "deleted_scheduled_task_id": task_id}


# ---------- 站内通知(本人) ----------
class NotificationItem(BaseModel):
    id: int
    title: str
    content: str = ""
    task_id: Optional[str] = None
    kind: str = "scheduled"
    read: bool = False
    created_at: Optional[str] = None


class NotificationListResponse(BaseModel):
    notifications: list[NotificationItem]
    unread: int
    total: int


@router.get("/notifications", response_model=NotificationListResponse, summary="我的通知列表(未读优先)")
def list_notifications(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> NotificationListResponse:
    from src.models import Notification

    q = db.query(Notification).filter(Notification.user == current_user.name)
    unread = q.filter(Notification.read.is_(False)).count()
    total = q.count()
    rows = (
        q.order_by(Notification.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return NotificationListResponse(
        notifications=[
            NotificationItem(
                id=r.id,
                title=r.title,
                content=r.content,
                task_id=r.task_id,
                kind=r.kind,
                read=bool(r.read),
                created_at=r.created_at.isoformat() if r.created_at else None,
            )
            for r in rows
        ],
        unread=unread,
        total=total,
    )


@router.post("/notifications/read", summary="标记通知已读")
def mark_notifications_read(
    body: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    """标记已读: body={"id": n} 标记单条; {"all": true} 全部标记。"""
    from src.models import Notification

    q = db.query(Notification).filter(Notification.user == current_user.name)
    if body.get("all"):
        q = q.filter(Notification.read.is_(False))
        n = q.update({"read": True}, synchronize_session=False)
    else:
        nid = body.get("id")
        item = q.filter(Notification.id == nid).first()
        if item is None:
            raise HTTPException(status_code=404, detail="通知不存在")
        item.read = True
        n = 1
    db.commit()
    return {"ok": True, "marked": n}


# ---------------------------------------------------------------------------
# 数据字典(仅管理员): 为表/列维护中文名, 前端选择下拉即时生效
# ---------------------------------------------------------------------------
class SchemaDictItem(BaseModel):
    id: int
    table_name: str
    column_name: str = ""
    cn_name: str
    created_by: Optional[str] = None
    updated_at: Optional[str] = None


class SchemaDictListResponse(BaseModel):
    items: list[SchemaDictItem]
    total: int


class SchemaDictCreate(BaseModel):
    table_name: str = Field(..., min_length=1, max_length=128, description="表名")
    column_name: str = Field("", max_length=128, description="列名(空=表级中文名)")
    cn_name: str = Field(..., min_length=1, max_length=256, description="中文名/业务说明")


class SchemaDictUpdate(BaseModel):
    cn_name: Optional[str] = Field(None, min_length=1, max_length=256)


@router.get("/admin/schema-dict", response_model=SchemaDictListResponse, summary="数据字典列表(仅管理员)")
def list_schema_dict(
    keyword: Optional[str] = Query(None, max_length=100),
    db: Session = Depends(get_db),
    _: User = Depends(require_role("admin")),
) -> SchemaDictListResponse:
    """数据字典: 表/列中文名清单(支持关键字搜索 表名/列名/中文名)。"""
    from src.models import SchemaDict

    q = db.query(SchemaDict)
    if keyword:
        kw = f"%{keyword.strip()}%"
        q = q.filter(
            or_(
                SchemaDict.table_name.ilike(kw),
                SchemaDict.column_name.ilike(kw),
                SchemaDict.cn_name.ilike(kw),
            )
        )
    rows = q.order_by(SchemaDict.table_name, SchemaDict.column_name).all()
    return SchemaDictListResponse(
        items=[
            SchemaDictItem(
                id=r.id,
                table_name=r.table_name,
                column_name=r.column_name or "",
                cn_name=r.cn_name,
                created_by=r.created_by,
                updated_at=r.updated_at.isoformat() if r.updated_at else None,
            )
            for r in rows
        ],
        total=len(rows),
    )


@router.post("/admin/schema-dict", response_model=SchemaDictItem, summary="新增数据字典项(仅管理员)")
def create_schema_dict(
    body: SchemaDictCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin")),
) -> SchemaDictItem:
    from sqlalchemy.exc import IntegrityError

    from src.models import SchemaDict

    exists = (
        db.query(SchemaDict)
        .filter(
            SchemaDict.table_name == body.table_name.strip(),
            SchemaDict.column_name == (body.column_name or "").strip(),
        )
        .first()
    )
    if exists:
        raise HTTPException(status_code=409, detail="该 表/列 的中文名已存在(可编辑)")
    item = SchemaDict(
        table_name=body.table_name.strip(),
        column_name=(body.column_name or "").strip(),
        cn_name=body.cn_name.strip(),
        created_by=current_user.name,
    )
    db.add(item)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="该 表/列 的中文名已存在(可编辑)") from None
    db.refresh(item)
    logger.info("schema_dict_created", table=item.table_name, column=item.column_name, actor=current_user.name)
    return SchemaDictItem(
        id=item.id, table_name=item.table_name, column_name=item.column_name or "",
        cn_name=item.cn_name, created_by=item.created_by,
        updated_at=item.updated_at.isoformat() if item.updated_at else None,
    )


@router.put("/admin/schema-dict/{item_id}", response_model=SchemaDictItem, summary="更新数据字典项(仅管理员)")
def update_schema_dict(
    item_id: int,
    body: SchemaDictUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(require_role("admin")),
) -> SchemaDictItem:
    from src.models import SchemaDict

    item = db.get(SchemaDict, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="字典项不存在")
    if body.cn_name is not None:
        item.cn_name = body.cn_name.strip()
    db.commit()
    db.refresh(item)
    return SchemaDictItem(
        id=item.id, table_name=item.table_name, column_name=item.column_name or "",
        cn_name=item.cn_name, created_by=item.created_by,
        updated_at=item.updated_at.isoformat() if item.updated_at else None,
    )


@router.delete("/admin/schema-dict/{item_id}", summary="删除数据字典项(仅管理员)")
def delete_schema_dict(
    item_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_role("admin")),
) -> dict:
    from src.models import SchemaDict

    item = db.get(SchemaDict, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="字典项不存在")
    db.delete(item)
    db.commit()
    return {"ok": True, "deleted_id": item_id}


class PermanentApprovalRequest(BaseModel):
    approved: bool = Field(..., description="true=永久批准该定时任务; false=永久拒绝(停用)")


@router.post("/admin/scheduled-tasks/{task_id}/permanent-approval", summary="永久审批定时任务(仅管理员)")
def permanent_approval_scheduled_task(
    task_id: int,
    body: PermanentApprovalRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin")),
) -> dict:
    """定时任务永久审批: 一次性授权, 此后该任务触发的分析不再逐单挂起审批。

    - approved=True : 标记 approved(审计留痕), 后续触发全部自动放行
    - approved=False: 标记 rejected 并停用该任务(通知创建人)
    审批动作写入审计日志(audit_logs), 满足合规追溯。
    """
    from datetime import datetime, timezone

    from src.models import AuditLog, ScheduledTask, Notification

    s = db.get(ScheduledTask, task_id)
    if s is None:
        raise HTTPException(status_code=404, detail="定时任务不存在")
    if body.approved:
        s.approval_status = "approved"
        s.approved_by = current_user.name
        s.approved_at = datetime.now(timezone.utc)
        if not s.enabled:
            s.enabled = True
    else:
        s.approval_status = "rejected"
        s.approved_by = current_user.name
        s.approved_at = datetime.now(timezone.utc)
        s.enabled = False  # 拒绝 -> 停用, 不再触发
    db.add(
        AuditLog(
            task_id=None,
            event="scheduled_permanent_" + ("approved" if body.approved else "rejected"),
            actor=current_user.name,
            node_name="api",
            detail={"scheduled_task_id": task_id, "name": s.name, "owner": s.owner},
        )
    )
    if not body.approved:
        # 通知创建人: 定时任务被永久拒绝
        db.add(
            Notification(
                user=s.owner or current_user.name,
                title="定时任务被拒绝",
                content=f"定时任务「{s.name}」未通过永久审批, 已停用。",
                task_id=None,
                kind="scheduled",
            )
        )
    db.commit()
    logger.info(
        "scheduled_permanent_approval",
        task_id=task_id,
        approved=body.approved,
        actor=current_user.name,
    )
    return {"ok": True, "task_id": task_id, "approval_status": s.approval_status, "enabled": s.enabled}
