# ============================================================================
# Data Pipeline Agent 应用镜像(前后端分离)
#   - 阶段 1: node 构建前端(web/ -> web/dist)
#   - 阶段 2: python 运行 FastAPI, 同源托管前端产物 + LangGraph 智能体运行时
# 构建: docker build -t data-pipeline-agent:latest .
# ============================================================================

# ---------- 阶段 1: 前端构建 ----------
FROM node:22-alpine AS web-builder
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --registry=https://registry.npmmirror.com
COPY web/ ./
RUN npm run build

# ---------- 阶段 2: 应用运行时 ----------
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    POETRY_VERSION=2.4.1 \
    POETRY_VIRTUALENVS_CREATE=false \
    POETRY_NO_INTERACTION=1

# 系统依赖:
#   gcc/libpq-dev     —— psycopg2 编译
#   pango/cairo/gdk   —— weasyprint 渲染 PDF 所需运行库
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libgdk-pixbuf-2.0-0 \
    libffi-dev \
    shared-mime-info \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Poetry 依赖管理
RUN pip install --no-cache-dir "poetry==${POETRY_VERSION}"

# 先装依赖, 充分利用层缓存
COPY pyproject.toml ./
RUN poetry install --only main --no-interaction --no-ansi

# 拷贝项目源码 + 前端构建产物
COPY . .
COPY --from=web-builder /web/dist ./web/dist

# 报告产物目录(与宿主机 volume 挂载)
RUN mkdir -p /app/static/reports

EXPOSE 8000

# 默认启动 FastAPI(同源托管前端页面: http://host:8000/)
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
