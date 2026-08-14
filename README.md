# Data Pipeline Agent

企业级数据分析多智能体平台:输入自然语言需求(如"统计最近 7 天各品类销售额,对比上周变化"),由 **Supervisor → Planner → Coder → Executor → Reporter** 多智能体编排自动产出**含图表、数据明细、行动建议的 PDF/HTML 分析报告**,全程支持安全只读执行、人机协同审批、断点恢复与全链路审计。

---

## 一、这个项目能做什么?(功能详解)

面向业务人员,无需 SQL 技能即可完成数据分析。核心能力:

| 功能 | 说明 |
| --- | --- |
| **自然语言 → 分析报告** | 输入业务问题,自动拆解需求、编写 SQL、安全执行、生成报告 |
| **四类输出形态** | Markdown 报告 / PDF 报告(含图表) / 交互式看板(可下钻) / 简洁问答(只要答案) |
| **多会话对话** | 左侧会话栏:新建 / 置顶 / 重命名 / 删除 / 切换;消息与报告持久化,随时回放 |
| **人机协同审批** | 敏感表查询、大结果集、显式要求时挂起,审批人批准后从断点继续 |
| **指标口径锁定** | 平台维护核心指标语义目录,跨任务统计口径一致,杜绝口径漂移 |
| **数据血缘溯源** | 报告附录展示每步查询的来源表/行数/耗时,结果可追溯 |
| **操作日志与审计** | 提交/审批/执行全事件留痕,支持关键字搜索与多维筛选(时间/提交者/审批人/结果) |
| **用户与权限管理** | 普通用户 / 审批人 / 管理员三角色;创建、重置密码、删除、调整权限 |
| **数据级权限** | 表级/列级/行级数据访问控制(角色+用户,用户优先):deny 拒绝、mask 脱敏、行级 WHERE 过滤,SQL 执行前 AST 改写强制,管理端可视化配置(详见 `docs/data_permission.md`) |
| **数据源配置化** | 管理员注册真实业务库(PostgreSQL 只读,连接串 AES-256-GCM 加密落库);任务可指定数据源,新库/新表自动进入表结构注入,无需改代码 |
| **指标自助管理** | 管理员在「指标口径」页维护核心指标目录(新增/改口径/下线,注册器热重载,对 LLM 立即生效)——口径锁定成为业务方可维护的资产 |
| **报告数字可核验** | 看板页「数据溯源」:每条 SQL 的来源表/行数/耗时 + 一键重跑看真实明细(受数据权限约束),让 AI 报告的数字可对账 |
| **定时任务 + 结果推送** | 管理员配置 cron 定时分析(APScheduler 内嵌,随应用启动),完成后站内通知推送(顶栏铃铛,未读角标) |
| **审计合规** | 操作日志按筛选导出 CSV 归档;**append-only 不可清空**,满足等保/GDPR 追溯 |
| **管理员运营看板** | 任务统计、Token 与成本、节点明细、进程内指标(重启清零) |
| **MCP 集成** | 接入 PostgreSQL MCP Server(只读):按需表结构检索 + 复杂 SQL EXPLAIN 预检,提升首轮正确率 |

### 一次分析的完整流程

```
用户提问 → Supervisor 识别需求(闲聊直接回复)
        → Planner 拆解多步计划(独立步骤并行)
        → Coder 注入真实表结构+锁定指标口径+历史代码,生成只读 SQL
        → Executor 只读校验 → (复杂 SQL)MCP EXPLAIN 预检 → 本地只读事务执行
              ├─ 失败 → 自动修复(≤3 次)
              ├─ 敏感表/大结果集 → 人工审批 → 批准后从断点继续
        → Reporter 生成执行摘要/图表/明细/建议 → MD + PDF + 看板
```

### 安全设计

- **SQL 三层防护**:关键字只读校验 → 会话级只读事务(物理不可写) → 沙箱隔离
- **沙箱隔离**:Docker 容器无网络、CPU/内存受限、30s 超时、非 root、一次性销毁
- **敏感数据**:身份证/手机号等敏感列查询强制人工审批;日志脱敏
- **审计合规**:全事件落 `audit_logs`(操作者/IP/前后状态快照),满足等保/GDPR 追溯

---

## 二、技术栈

| 模块 | 技术 |
| --- | --- |
| 多智能体框架 | LangGraph(状态图 / Checkpointer 断点恢复 / Human-in-the-loop / 并行步骤) |
| LLM | DeepSeek(核心推理)+ 通义千问 DashScope(低成本路由,按节点配置) |
| Web 服务 | FastAPI(`/api/v1`) + Vue 3 + Element Plus + Pinia + ECharts |
| 数据库 | PostgreSQL + SQLAlchemy 2.0(create_all + 幂等列迁移,无 Alembic 依赖) |
| 任务队列 | Celery + Redis(可选,默认 FastAPI 后台任务) |
| 缓存 / 向量 | Redis(结果缓存)/ Chroma(历史成功代码检索) |
| 沙箱执行 | Docker 容器隔离(docker-py,无网络 / 资源限制 / 30s 超时) |
| MCP | postgres-mcp(mcp Python SDK,restricted 只读模式) |
| 日志 / 安全 | structlog(JSON 脱敏)+ AES-256-GCM 加密 / PBKDF2 口令哈希 |

## 三、目录结构

```
.
├── docker-compose.yml          # 拉起 App + 沙箱镜像构建(中间件用宿主机本地服务)
├── Dockerfile                  # 应用镜像构建
├── sandbox.Dockerfile          # 沙箱基础镜像(python:3.12-slim, 白名单库 pandas/numpy/matplotlib)
├── .env                        # 环境变量(密钥/连接串/沙箱镜像名/MCP 开关)
├── pyproject.toml              # Poetry 依赖管理
├── main.py                     # FastAPI 应用入口(API 路由 + 前端产物托管 + SPA fallback)
├── web/                        # 前端工程(Vue 3 + Vite, 构建产物由后端托管)
├── src/
│   ├── state.py                # PipelineState TypedDict
│   ├── graph.py                # LangGraph 状态图编排 / 断点恢复 / 审批恢复
│   ├── models/                 # SQLAlchemy ORM(users/tasks/audit/chat/metric/...)
│   ├── api/                    # FastAPI 路由(routes) / 认证(deps) / 启动引导(bootstrap)
│   ├── nodes/                  # 智能体节点(supervisor/planner/coder/executor/reporter/human_approval/clarifier)
│   ├── sandbox/                # 安全执行引擎(local 只读 SQL / docker 容器)
│   ├── tools/                  # chart_gen / sql_validator / schema_retriever / schema_provider /
│   │                           # metric_registry(指标语义层) / lineage(血缘) / mcp_client(PG MCP)
│   └── utils/                  # settings / logger / security / cache / aux_llm
├── data/                       # 产品文档(需求分析/技术实现方案/数据库设计/使用教程)
├── docs/                       # 架构图 / OpenAPI / 开发指南
├── scripts/                    # init_db / seed_metrics / ab_mcp(量化对比) / gen_docs(文档生成)
├── static/reports/             # 报告产物(YYYY/MM/DD 分层: md / pdf / board.json / png)
└── tests/                      # unit / integration(102 用例)
```

## 四、快速启动

### 方式一:Docker Compose 部署(中间件用本地服务)

前提:宿主机已运行本地中间件 —— **PostgreSQL(5433)、Redis(6379)、Chroma(8000)**。

```bash
cp .env .env.local            # 按需修改密钥
docker compose up -d --build
```

前端页面由 FastAPI **同源托管**(`web/dist` 构建产物,含 SPA 路由 fallback):

- 前端页面 + FastAPI: http://localhost:8001(`/docs` 即 Swagger)

### 方式二:本地开发(宿主机)

```bash
poetry install                # 安装依赖(已在 .venv)
docker build -f sandbox.Dockerfile -t data-sandbox:v1 . # 沙箱镜像

# 后端(注意: 必须用 --reload-dir src 限定热重载范围, 否则 .venv 变化会触发无限重启)
.venv/bin/uvicorn main:app --host 0.0.0.0 --port 8001 --reload --reload-dir src

# 前端开发模式(可选)
cd web && npm install && npm run dev   # Vite :5173, /api 与 /static 代理到 8001
```

> 生产模式:浏览器访问 `http://localhost:8001`;前端开发模式:访问 `http://localhost:5173`。
> 修改前端后需 `npm run build` 重新构建,后端才会托管新产物。

### 环境依赖

| 组件 | 说明 |
| --- | --- |
| PostgreSQL(5433) | 业务数据 + 平台元数据(建表由启动引导自动完成) |
| Redis(6379) | 结果缓存 / 取消标志 |
| Chroma(8000) | 历史成功代码向量检索(不可用时自动降级) |
| Docker + `data-sandbox:v1` | Python 代码沙箱(不可用时 SQL 本地执行兜底) |
| LLM 密钥 | `.env` 配置 DeepSeek / DashScope |
| postgres-mcp | `.venv` 内已安装,由 `mcp_client.py` 自动拉起(可用 `PG_MCP_ENABLED=0` 关闭;仅主库启用) |
| APScheduler | `.venv` 内(定时任务调度器,随应用启动内嵌运行) |

## 五、核心接口(OpenAPI 3.0)

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/v1/auth/login` | 登录获取 JWT |
| POST | `/api/v1/tasks` | 提交分析任务(自然语言需求) |
| GET | `/api/v1/tasks/{id}/status` | 轮询任务状态(含进度/报告正文) |
| GET | `/api/v1/tasks` | 任务历史(分页/关键词/状态筛选) |
| POST | `/api/v1/tasks/{id}/approve` | 人机协同审批(通过/拒绝+备注) |
| POST | `/api/v1/tasks/{id}/cancel` | 取消任务 |
| GET | `/api/v1/tasks/{id}/download` | 下载报告(PDF/MD) |
| GET | `/api/v1/tasks/{id}/board`、`/drill` | 交互式看板数据 / 下钻明细 |
| GET | `/api/v1/chat/sessions` | 会话列表(置顶优先/消息数) |
| POST/PATCH/DELETE | `/api/v1/chat/sessions`、`/{id}` | 新建 / 更新(重命名/置顶) / 删除会话 |
| GET/POST | `/api/v1/chat/sessions/{id}/messages` | 会话消息回放 / 写入(自动生成标题) |
| GET | `/api/v1/users` | 用户列表 |
| POST | `/api/v1/users` | 创建用户 |
| PUT/DELETE | `/api/v1/users/{id}/password`、`/roles`、`/` | 重置密码 / 调整权限 / 删除用户 |
| GET | `/api/v1/admin/audit-logs` | 操作日志(搜索 + 多维筛选) |
| GET | `/api/v1/admin/audit-logs/export` | 导出操作日志 CSV(审计归档, append-only) |
| GET | `/api/v1/admin/audit-logs/{id}/events` | 任务审计时间线 |
| GET/POST/PUT/DELETE | `/api/v1/data-sources`、`/{id}` | 数据源配置化(仅管理员;POST 含连接测试) |
| GET/POST/PUT/DELETE | `/api/v1/admin/metric-definitions`、`/{id}` | 指标口径自助管理(仅管理员,热重载) |
| GET/POST/PUT/DELETE | `/api/v1/admin/scheduled-tasks`、`/{id}` | 定时分析任务(仅管理员, cron) |
| GET/POST | `/api/v1/notifications`、`/notifications/read` | 站内通知列表(本人)/标记已读 |
| GET | `/api/v1/tasks/{id}/lineage` | 报告溯源:任务 SQL 血缘列表 |
| POST | `/api/v1/tasks/{id}/query-runs/{run_id}/rerun` | 重跑溯源 SQL 核验报告数字(数据权限约束) |
| GET | `/api/v1/admin/metrics` | 运营看板数据(任务/成本/进程内指标) |
| GET | `/health`、`/metrics` | 健康检查 / Prometheus 指标 |

## 六、文档索引

| 文档 | 位置 |
| --- | --- |
| 需求分析(功能/规则/验收) | `data/需求分析.docx` |
| 技术实现方案(架构/接口/核心机制) | `data/技术实现方案.docx` |
| 数据库设计(全量 20 张表) | `data/数据库设计.docx` |
| 使用教程(日常操作指南) | `data/使用教程.md` |
| 开发指南 | `docs/developer_guide.md` |

## 七、开发规范要点

- **结构化输出**:LLM 节点使用提示词约束 JSON + Pydantic 校验,不依赖模型 `response_format`;解析失败自动降级。
- **安全执行**:Executor 强制 SQL 只读校验;SQL 由本地只读事务执行,Python 代码走 Docker 沙箱。
- **自修复与缓存**:执行失败自动重写(≤3 次,错误分类);同需求+同结构复用历史成功代码(口径变更自动失效)。
- **MCP 降级**:复杂 SQL(≥2 JOIN/CTE/子查询/长 SQL)EXPLAIN 预检;MCP 不可用时自动跳过,不阻塞任务。
- **降级策略**:LLM/向量库/MCP 任一不可用均优雅降级,不阻塞主流程。
- **审计合规**:每次请求的输入输出、智能体调用路径、审批人/时间均落 `audit_logs`。
