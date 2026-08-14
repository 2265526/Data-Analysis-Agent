# 沙箱基础镜像，和你宿主机Python版本对齐3.12
FROM python:3.12-slim
# 关闭Python缓存、减少镜像体积
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
# 指定matplotlib、系统临时目录到可写路径
ENV TMPDIR=/sandbox/tmp
ENV MPLCONFIGDIR=/sandbox/tmp/matplotlib

# 安装系统依赖（仅数据分析必要组件）
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# 安装沙箱允许的白名单Python库
RUN pip install --no-cache-dir \
    pandas==2.2.2 \
    numpy==1.26.4 \
    matplotlib==3.9.2 \
    sqlalchemy==2.0.36 \
    psycopg2-binary==2.9.10

# 创建普通非root用户，禁止容器内root运行
RUN useradd -m -u 1000 sandbox
USER sandbox

# 工作目录
WORKDIR /sandbox

# 限制写入：仅tmp目录可临时存代码/图表，其余全部只读
VOLUME ["/sandbox/tmp"]

# 容器启动默认shell
CMD ["/bin/bash"]
