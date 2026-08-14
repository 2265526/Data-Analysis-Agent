"""FastAPI 应用入口:挂载静态报告目录 + /api/v1 路由 + 健康检查。

启动:
    uvicorn main:app --host 0.0.0.0 --port 8001
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from src.api.routes import router as api_router
from src.utils.logger import get_logger
from src.utils.metrics import metrics as metrics_registry
from src.utils.settings import get_settings

settings = get_settings()
logger = get_logger(__name__)

# 前端构建产物目录(生产模式): web/dist 存在时由 FastAPI 同源托管(SPA)
FRONTEND_DIST = Path(__file__).resolve().parent / "web" / "dist"

app = FastAPI(
    title="Data Pipeline Agent API",
    description="企业级数据分析多智能体平台:自然语言 -> 自动化分析报告",
    version="0.1.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

# 跨域:允许 Vite dev server(5173)与任意前端访问
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 报告产物静态服务:/static/reports/YYYY/MM/DD/xxx.pdf|.html
settings.reports_dir.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=settings.static_dir), name="static")

# 业务路由(前缀 /api/v1)
app.include_router(api_router, prefix="/api/v1")


@app.get("/health", tags=["系统"])
def health() -> dict:
    """健康检查,供 docker-compose / 负载均衡探活。"""
    return {"status": "ok", "service": "data-pipeline-agent"}


@app.get("/metrics", response_class=PlainTextResponse, include_in_schema=False, tags=["系统"])
def metrics_endpoint() -> str:
    """监控指标导出(Prometheus 文本格式, 对应优化方案新增的 6 类指标)。

    - task_executed_total / self_heal_* / task_retry_count  自修复成功率、重试分布
    - circuit_breaker_trips_total                           熔断器触发次数
    - llm_tokens_total                                      各节点 Token 消耗占比
    - sandbox_exec_duration_seconds                         SQL/Python 实际执行耗时
    - tool_param_rejections_total                           工具调用异常参数拒绝次数
    """
    return metrics_registry.snapshot()


# ---------------------------------------------------------------------------
# 前端托管(生产模式): web/dist 存在时, 同源提供 SPA 页面与静态资源
#   开发模式使用 Vite dev server(5173, /api 代理到本服务), 二者互不影响
# ---------------------------------------------------------------------------
if FRONTEND_DIST.exists():
    app.mount(
        "/assets",
        StaticFiles(directory=FRONTEND_DIST / "assets"),
        name="frontend-assets",
    )

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str) -> FileResponse:
        """SPA fallback: 非 API/静态路径统一返回 index.html(支持前端路由刷新)。"""
        if full_path.startswith(
            ("api/", "static/", "docs", "redoc", "metrics", "health", "openapi", "assets")
        ):
            raise HTTPException(status_code=404)
        return FileResponse(FRONTEND_DIST / "index.html")


@app.on_event("startup")
async def on_startup() -> None:
    # 建表(幂等) + 种子默认管理员 admin/admin
    from src.api.bootstrap import ensure_schema_and_default_admin

    ensure_schema_and_default_admin()
    # 定时任务调度器(APScheduler, 内嵌单机): 加载启用的定时任务
    from src.tools.scheduler import start_scheduler

    start_scheduler()
    logger.info("service_started", port=settings.app_port)


@app.on_event("shutdown")
async def on_shutdown() -> None:
    from src.tools.scheduler import shutdown_scheduler

    shutdown_scheduler()
