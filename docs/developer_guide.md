# 开发者指南(Developer Guide)

## 1. 快速上手

```bash
# 环境准备(Python 3.12 + Poetry)
poetry install

# 配置密钥
cp .env .env.local   # 填写 DEEPSEEK_API_KEY / DASHSCOPE_API_KEY

# 启动服务
poetry run uvicorn main:app --reload --port 8001        # FastAPI + 前端产物托管(同源), Swagger: /docs
# 前端开发模式(Vite :5173, 热更新; /api 代理到 8001):
cd web && npm install && npm run dev

# 数据库初始化
poetry run python scripts/init_db.py

# 沙箱镜像(宿主机需 Docker)
docker build -f sandbox.Dockerfile -t data-sandbox:v1 .

# Celery worker(异步执行流水线)
poetry run celery -A src.api.routes:celery_app worker -l info
```

## 2. 架构总览

```
┌─────────────┐   ┌──────────────┐   ┌──────────────┐   ┌──────────────┐
│  Vue 3 前端  │   │   FastAPI    │   │   Celery     │   │  LangGraph   │
│  (web/ 同源) │──▶│   (8001)     │──▶│   worker     │──▶│  流水线      │
└─────────────┘   └──────┬───────┘   └──────────────┘   └──────┬───────┘
                         │                                      │
              PostgreSQL │  Redis         Chroma ──────── 沙箱(Docker)
              (任务/审计) │  (队列/缓存)    (向量检索)          (执行)
```

- **src/state.py**: `PipelineState` TypedDict,所有节点共享(含 actor / data_source_id)
- **src/graph.py**: 状态图组装、条件路由、Checkpointer、`execute_task` 入口
- **src/nodes/**: 每个智能体节点一个文件,纯函数 `(state) -> partial_state`
- **src/sandbox/**: 执行引擎(`DockerSandbox` 生产 / `LocalSandbox` 兜底,支持按数据源 db_url 路由)
- **src/tools/**: 图表、SQL 校验、向量检索、`data_policy.py`(数据级权限改写)、`data_source.py`(数据源路由)、`scheduler.py`(APScheduler 定时任务)

### 2.1 新增功能模块速览(2026-08 产品增强)

| 模块 | 位置 | 说明 |
|---|---|---|
| 数据源配置化 | `src/models/data_source.py` + `src/tools/data_source.py` + 路由 `/data-sources` | 连接串 AES-256-GCM 加密落库;`resolve_db_url(data_source_id)` 全链路路由(schema 注入 / SQL 执行);MCP 预检仅主库 |
| 指标自助管理 | 路由 `/admin/metric-definitions` + `web/src/views/admin/MetricDefinitions.vue` | CRUD 后调 `metric_registry.reload_metric_registry()` 热重载,LLM 立即生效 |
| 报告溯源 | 路由 `/tasks/{id}/lineage`、`/query-runs/{run_id}/rerun` + `Board.vue` 溯源抽屉 | 重跑仍走 `apply_data_policy`,权限变更不可绕过 |
| 定时任务+通知 | `src/models/scheduled_task.py` / `notification.py` + `src/tools/scheduler.py` + 路由 `/admin/scheduled-tasks`、`/notifications` | APScheduler 随应用启动;`refresh_schedule()` 增删改后热同步 |
| 审计导出 | 路由 `/admin/audit-logs/export` + `AuditLogs.vue` 导出按钮 | CSV(utf-8-sig)按筛选导出;审计 append-only 无清空接口 |

## 3. 新增智能体节点指南

1. 在 `src/nodes/` 新建 `xxx.py`,实现 `def xxx_node(state: PipelineState) -> dict`
2. 在 `src/nodes/__init__.py` 导出该函数
3. 在 `src/graph.py`:
   - `builder.add_node("xxx", xxx_node)`
   - 添加边:直线流程用 `add_edge`,分支用 `add_conditional_edges` + 路由函数
   - 如需中断:节点内使用 `interrupt()` 并配置 `interrupt_before`
4. 在 `src/state.py` 中补充状态字段(TypedDict)
5. 编写 `tests/unit/test_xxx.py`,Mock LLM 输出

## 4. 状态图扩展规范

- **节点约束**: 节点只返回"要更新的状态片段",不得直接读写 DB(状态外副作用放 `execute_task`)
- **路由约定**: 分支节点返回 `{"route": "目标节点名"}`,路由函数读取 `state["route"]` 并映射
- **结构化输出**: 所有 LLM 节点使用 `with_structured_output(PydanticModel)`,失败降级不可让流水线崩溃
- **状态膨胀控制**: 大结果集只传摘要(`exec_result[:2000]`),历史步骤可压缩

## 5. 故障排查手册

| 现象 | 排查 |
| --- | --- |
| 任务一直 pending | Celery worker 未启动;`docker compose exec app celery -A src.api.routes:celery_app worker -l info` |
| 沙箱执行失败 "Docker daemon" | 确认宿主机 docker 可用,App 容器挂载了 `/var/run/docker.sock` |
| Chroma 不可用 | 确认宿主机本地 Chroma 已在 8000 端口运行(`chroma run` / `docker run -p 8000:8000 chromadb/chroma`);代码会自动降级为无检索模式 |
| 审批后任务未恢复 | 审批结果写入 Redis 键 `approval:{task_id}`;确认 worker 与 API 使用同一 Redis |
| LLM 报 401 | 检查 `.env` 的 `DEEPSEEK_API_KEY`,或切换 `DASHSCOPE_API_KEY` |

## 6. 部署

依赖宿主机本地中间件(PostgreSQL 5432 / Redis 6379 / Chroma 8000):

```bash
docker compose up -d --build
# 前端页面 + FastAPI: http://localhost:8001(/docs 即 Swagger; 前端由后端同源托管 web/dist)
```
