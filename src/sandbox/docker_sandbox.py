"""Docker 容器隔离执行器(生产环境沙箱)。

安全约束:
- --network none       禁止外部网络访问
- mem_limit / cpu_quota 资源限制, 防止死循环耗尽宿主
- 30s 执行超时, 超时强制 kill 容器
- 非 root 用户, 只读文件系统(仅 /sandbox/tmp 可写)
- SQL 代码先做只读校验, 危险语句直接拒绝
"""
from __future__ import annotations

import tempfile
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from src.tools.sql_validator import looks_like_sql, validate_readonly
from src.utils.logger import get_logger
from src.utils.settings import get_settings

logger = get_logger(__name__)
settings = get_settings()


class DockerSandbox:
    """基于 docker-py 的一次性沙箱容器执行器。"""

    name = "docker"

    def __init__(
        self,
        image: str | None = None,
        timeout: int | None = None,
        mem_limit: str | None = None,
        cpu_quota: int | None = None,
    ) -> None:
        self.image = image or settings.sandbox_image
        self.timeout = timeout or settings.sandbox_timeout
        self.mem_limit = mem_limit or settings.sandbox_mem_limit
        self.cpu_quota = cpu_quota or settings.sandbox_cpu_quota
        self._client: Optional[Any] = None

    @property
    def client(self):
        """惰性连接 Docker 守护进程。"""
        if self._client is None:
            import docker

            self._client = docker.from_env()
        return self._client

    def execute(self, code: str) -> Dict[str, Any]:
        """在一次性容器中执行代码, 执行完即销毁容器。

        Returns: {"status": "success"|"error", "output": str, "error": str}
        """
        code = code.strip()
        if not code:
            return {"status": "error", "output": "", "error": "代码为空"}

        # SQL 代码: 强制只读校验
        if looks_like_sql(code):
            ok, reason = validate_readonly(code)
            if not ok:
                return {"status": "error", "output": "", "error": f"SQL 只读校验失败: {reason}"}

        container = None
        try:
            container = self.client.containers.run(
                image=self.image,
                command=["python", "-c", code],          # 代码经 -c 传入, 避免挂载冲突
                network_disabled=True,                   # 禁止外网
                mem_limit=self.mem_limit,                # 内存上限
                cpu_quota=self.cpu_quota,                # CPU 配额
                nano_cpus=None,
                user="sandbox",                          # 非 root
                read_only=True,                          # 根文件系统只读
                tmpfs={"/sandbox/tmp": "rw,size=100m"},  # 仅 tmp 可写(matplotlib/临时文件)
                detach=True,
                auto_remove=False,
            )
            result = container.wait(timeout=self.timeout)
            logs = container.logs(stdout=True, stderr=True).decode("utf-8", errors="replace")
            exit_code = result.get("StatusCode", -1)

            if exit_code == 0:
                return {"status": "success", "output": logs[-4000:], "error": ""}
            return {"status": "error", "output": "", "error": logs[-4000:] or f"exit code={exit_code}"}

        except Exception as exc:  # noqa: BLE001
            logger.warning("sandbox_exec_failed", error=str(exc), image=self.image)
            return {"status": "error", "output": "", "error": f"沙箱执行异常: {exc}"}
        finally:
            if container is not None:
                try:
                    container.remove(force=True)  # 执行完即销毁
                except Exception:  # noqa: BLE001
                    pass


def run_in_sandbox(code: str, backend: str = "auto", db_url: str | None = None) -> Dict[str, Any]:
    """统一入口: SQL 走本地只读执行(沙箱禁网无法连库), Python 走 Docker 沙箱(失败降级本地)。

    Args:
        backend: auto | docker | local
        db_url: 数据源连接串(SQL 执行目标库; 默认主库 settings.database_url)
    """
    code = (code or "").strip()
    is_sql = looks_like_sql(code)

    if backend == "local" or is_sql:
        # SQL: Docker 容器 network_disabled 无法连接数据库, 由本地只读执行器处理
        from src.sandbox.local_sandbox import LocalSandbox

        return LocalSandbox().execute(code, db_url=db_url)
    if backend == "docker":
        return DockerSandbox().execute(code)

    # auto: Python 代码优先 Docker(真隔离), 不可用时降级本地模拟
    try:
        result = DockerSandbox().execute(code)
        return result
    except Exception as exc:  # noqa: BLE001
        logger.warning("docker_sandbox_unavailable", error=str(exc))
        from src.sandbox.local_sandbox import LocalSandbox

        return LocalSandbox().execute(code)
