"""模型路由(开发流程 2.2 模型分级选型 / 142+144 段): model_routes 表驱动 + 主备切换。

- 表驱动: 节点模型从 model_routes 读取(enabled=true, 按 priority 升序), 无配置回退 settings
- 主备切换: 连续 2 次调用失败(或单次超时)切换 priority 下一模型; 新模型连续失败 2 次
  回滚主模型并转降级; 全局 LLM 熔断器打开期间不做切换尝试(直接走降级/缓存)
- 分级路由(表1): supervisor/错误分类/摘要压缩 -> qwen-flash(百炼), 核心推理 -> deepseek-chat
"""
from __future__ import annotations

import threading
from typing import Dict, List, Optional

from src.api.deps import SessionLocal
from src.models.model_routes import ModelRoute
from src.utils.logger import get_logger
from src.utils.settings import get_settings

logger = get_logger(__name__)
settings = get_settings()


class ModelRouter:
    """按节点维护主/备模型与切换状态(进程内, 线程安全)。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # node -> {"routes": [model...], "index": 当前模型下标, "failures": 连续失败数}
        self._state: Dict[str, dict] = {}

    # ---- 模型列表解析 ----
    def _routes_for(self, node: str, fallback: str) -> List[str]:
        """该节点可用的模型列表(priority 升序); 无配置时 [fallback]。"""
        try:
            db = SessionLocal()
            try:
                rows = (
                    db.query(ModelRoute)
                    .filter(ModelRoute.node == node, ModelRoute.enabled.is_(True))
                    .order_by(ModelRoute.priority.asc())
                    .all()
                )
                models = [r.model_name for r in rows]
                return models or [fallback]
            finally:
                db.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("model_routes_query_failed", node=node, error=str(exc))
            return [fallback]

    def resolve(self, node: str, fallback: str) -> str:
        """返回该节点当前应使用的模型名。"""
        with self._lock:
            st = self._state.get(node)
            if st is None:
                routes = self._routes_for(node, fallback)
                st = {"routes": routes, "index": 0, "failures": 0, "degraded": False}
                self._state[node] = st
            return st["routes"][st["index"]]

    # ---- 调用结果反馈(主备切换/回滚) ----
    def record_success(self, node: str) -> None:
        with self._lock:
            st = self._state.get(node)
            if st is not None:
                st["failures"] = 0

    def record_failure(self, node: str) -> None:
        """记录一次调用失败; 满足条件时切换/回滚。

        熔断器打开期间不做切换(由 _llm_breaker.state 判断)。
        """
        from src.nodes import _llm_breaker  # 延迟导入避免循环依赖

        with self._lock:
            st = self._state.get(node)
            if st is None:
                return
            if _llm_breaker.state == "open":
                return  # 熔断打开: 不做切换尝试, 直接走降级
            st["failures"] += 1
            if st["failures"] < settings.model_switch_failures:
                return
            routes = st["routes"]
            if len(routes) <= 1:
                st["degraded"] = True  # 无备用模型, 标记降级
                st["failures"] = 0
                return
            if st["index"] == 0:
                # 主模型连续失败 -> 切换 priority 下一模型
                st["index"] += 1
                st["failures"] = 0
                logger.warning(
                    "model_switched",
                    node=node,
                    to=routes[st["index"]],
                    reason="主模型连续失败",
                )
            else:
                # 备选模型连续失败 -> 回滚主模型并转降级
                st["index"] = 0
                st["failures"] = 0
                st["degraded"] = True
                logger.warning(
                    "model_rolled_back",
                    node=node,
                    to=routes[0],
                    reason="备选模型连续失败",
                )

    def is_degraded(self, node: str) -> bool:
        with self._lock:
            st = self._state.get(node)
            return bool(st and st.get("degraded"))


# 全局单例
_router: Optional[ModelRouter] = None
_router_lock = threading.Lock()


def get_model_router() -> ModelRouter:
    global _router
    with _router_lock:
        if _router is None:
            _router = ModelRouter()
        return _router
