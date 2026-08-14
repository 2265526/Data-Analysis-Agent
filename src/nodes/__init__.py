"""智能体节点: Supervisor / Planner / Coder / Executor / Reporter / HumanApproval。

每个节点本质是一个纯函数: 输入 PipelineState, 返回要更新的状态片段。

注意: make_llm 必须在导入子模块之前定义, 子模块通过
`from src.nodes import make_llm` 引用, 避免循环导入。

监控埋点(对应优化方案指标):
- circuit_breaker_trips_total: LLM 调用熔断触发次数(指标3)
- llm_tokens_total: 各节点 Token 消耗(指标4)
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from langchain_core.callbacks import BaseCallbackHandler
from langchain_openai import ChatOpenAI

from src.utils.circuit import CircuitBreaker
from src.utils.metrics import metrics
from src.utils.settings import get_settings

settings = get_settings()

# 全局 LLM 熔断器: 保护所有节点对 LLM 的调用, 打开瞬间上报指标
_llm_breaker = CircuitBreaker(
    "llm_call",
    failure_threshold=max(settings.circuit_failure_threshold, 1),
    open_timeout=30.0,
    on_trip=lambda name: metrics.inc(
        "circuit_breaker_trips_total", labels={"breaker": name}
    ),
)


class _TokenUsageCapture(BaseCallbackHandler):
    """捕获单次 LLM 调用的 token 用量(标准 callbacks API, 兼容结构化输出)。"""

    def __init__(self) -> None:
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.captured = False

    def on_llm_end(self, response: Any, **kwargs: Any) -> None:  # noqa: ARG002
        try:
            for gen_list in response.generations:
                for gen in gen_list:
                    message = getattr(gen, "message", None)
                    meta = (getattr(message, "response_metadata", None) or {})
                    usage = meta.get("token_usage") or {}
                    self.prompt_tokens += usage.get("prompt_tokens", 0) or 0
                    self.completion_tokens += usage.get("completion_tokens", 0) or 0
                    self.captured = True
        except Exception:  # noqa: BLE001 — 统计失败不影响主流程
            pass


def _invoke_with_metrics(
    func, node: str, *args: Any, task_id: str | None = None, model: str | None = None, **kwargs: Any
) -> Any:
    """熔断保护 + token 统计 + 成本落库的统一调用入口。

    task_id: 来自 PipelineState, 用于 task_node_runs/cost_records 落库
    model: 模型名(默认取 node 名, LLM wrapper 传入真实模型名)
    """
    handler = _TokenUsageCapture()
    config = dict(kwargs.pop("config", None) or {})
    callbacks = list(config.get("callbacks") or [])
    callbacks.append(handler)
    config["callbacks"] = callbacks

    # OR-11 全局限流: LLM 调用前检查令牌桶, 超时等待则拒绝(熔断保护前置)
    from src.utils.rate_limit import limiter

    if not limiter.wait_until_available(key="llm"):
        raise RuntimeError(f"触发全局限流(rate={settings.rate_limit_per_min}/min), 等待超时被拒绝")

    try:
        started = time.monotonic()
        result = _llm_breaker.call(func, *args, config=config, **kwargs)
    except Exception:
        # 主备切换反馈(开发流程 2.2): 连续失败切换/回滚
        from src.utils.model_router import get_model_router

        get_model_router().record_failure(node)
        raise
    else:
        from src.utils.model_router import get_model_router

        get_model_router().record_success(node)
        elapsed_ms = int((time.monotonic() - started) * 1000)

    if handler.captured:
        if handler.prompt_tokens:
            metrics.inc(
                "llm_tokens_total",
                handler.prompt_tokens,
                labels={"node": node, "type": "prompt"},
            )
        if handler.completion_tokens:
            metrics.inc(
                "llm_tokens_total",
                handler.completion_tokens,
                labels={"node": node, "type": "completion"},
            )
        # 成本落库(旁路, 失败不影响主流程)
        if task_id:
            from src.utils.run_records import record_llm_run

            record_llm_run(
                task_id=task_id,
                node=node,
                model=model or node,
                prompt_tokens=handler.prompt_tokens,
                completion_tokens=handler.completion_tokens,
                duration_ms=elapsed_ms,
            )
    return result


class _LLMWrapper:
    """包装 ChatOpenAI: 统一熔断 + Token 统计 + 成本落库, 兼容 invoke / with_structured_output。"""

    def __init__(self, chat: ChatOpenAI, node: str = "") -> None:
        self._chat = chat
        self._node = node or getattr(chat, "model_name", "") or "unknown"

    @property
    def model_name(self) -> str:
        return getattr(self._chat, "model_name", self._node)

    def invoke(self, messages: Any, **kwargs: Any) -> Any:
        task_id = kwargs.pop("task_id", None)
        return _invoke_with_metrics(
            self._chat.invoke, self._node, messages, task_id=task_id, model=self.model_name, **kwargs
        )

    def with_structured_output(self, *args: Any, **kwargs: Any):
        """结构化输出: 包装底层 Runnable, 保证熔断与统计同样生效。"""
        structured = self._chat.with_structured_output(*args, **kwargs)
        return _StructuredWrapper(structured, self._node, self.model_name)


class _StructuredWrapper:
    """包住 with_structured_output 返回的 Runnable。"""

    def __init__(self, structured: Any, node: str, model: str) -> None:
        self._structured = structured
        self._node = node
        self._model = model

    def invoke(self, messages: Any, **kwargs: Any) -> Any:
        task_id = kwargs.pop("task_id", None)
        return _invoke_with_metrics(
            self._structured.invoke, self._node, messages, task_id=task_id, model=self._model, **kwargs
        )


def _resolve_llm_config(model: str) -> dict:
    """按模型名路由到对应服务商: qwen 系列 -> 百炼(OpenAI 兼容端点), 其余 -> DeepSeek。"""
    if "qwen" in model:
        return {
            "api_key": settings.dashscope_api_key,
            "base_url": settings.dashscope_base_url,
        }
    return {
        "api_key": settings.deepseek_api_key,
        "base_url": settings.deepseek_base_url,
    }


def _proxy_port_alive(proxy_url: str) -> bool:
    """探测代理主机:端口是否存活(0.4s 超时)。

    VPN/代理软件关掉后, 环境变量里的代理地址会变成死端口 —— 此时继续走代理必然
    Connection error。存活探测让"代理不可用 -> 自动直连"成为可能。
    """
    import socket
    from urllib.parse import urlparse

    try:
        u = urlparse(proxy_url)
        host = u.hostname or "127.0.0.1"
        port = u.port or 1080
        with socket.create_connection((host, port), timeout=0.4):
            return True
    except Exception:  # noqa: BLE001 - 探测失败视为代理不可用
        return False


def _build_llm_http_client():
    """构造 LLM HTTP 客户端(适配用户环境的 socks 代理, 代理不可用时自动直连)。

    背景: 开发机系统代理常见写法 http_proxy=socks://127.0.0.1:PORT, 而 httpx 只识别
    socks5/socks5h scheme —— 直接透传环境变量会抛 "Unknown scheme for proxy URL",
    导致所有 LLM 调用失败。这里将 socks:// 归一化为 socks5h://(域名解析也走代理),
    并显式构造 http_client 传给 ChatOpenAI。

    关键点: **始终返回一个显式 client(非 None)**。若返回 None, ChatOpenAI 会退回到
    自己的默认 httpx 客户端(trust_env=True), 重新从环境变量读取 socks:// 代理并再次
    报 Unknown scheme —— 这是"直连分支反而报代理错误"的最终来源。
    因此直连时返回 httpx.Client(trust_env=False): 彻底忽略环境变量代理。

    - LLM_PROXY 显式指定代理(最高优先级, 不探测——用户明确要求走代理)
    - 系统代理环境变量(HTTPS_PROXY/ALL_PROXY/HTTP_PROXY 等)存在时先探测端口存活,
      代理已关(如 VPN 退出)则忽略并直连 —— 本项目模型端点为国内直连可达
    - 无代理环境变量 -> 显式直连(trust_env=False)
    - 代理构造失败(如 socks 扩展缺失)-> 降级直连, 不让代理配置挂死任务
    """
    import os

    import httpx

    proxy = os.environ.get("LLM_PROXY")
    use_proxy = bool(proxy)
    if not use_proxy:
        inherited = (
            os.environ.get("HTTPS_PROXY")
            or os.environ.get("https_proxy")
            or os.environ.get("ALL_PROXY")
            or os.environ.get("all_proxy")
            or os.environ.get("HTTP_PROXY")
            or os.environ.get("http_proxy")
        )
        if inherited and _proxy_port_alive(inherited):
            proxy = inherited
            use_proxy = True
    if use_proxy and proxy.startswith("socks://"):
        proxy = "socks5h://" + proxy[len("socks://"):]
    try:
        # trust_env=False: 无论走代理还是直连, 都不让 httpx 再从环境变量读代理
        return httpx.Client(
            proxy=proxy if use_proxy else None,
            trust_env=False,
            timeout=settings.llm_timeout,
        )
    except Exception:  # noqa: BLE001 - 代理问题不影响主链路, 降级直连
        return httpx.Client(trust_env=False, timeout=settings.llm_timeout)


def make_llm(model: str, temperature: float = 0.1, node: str = "") -> ChatOpenAI:
    """构造 LLM 客户端(模型分级路由, 开发流程 2.2 表1)。

    实际模型: 优先取 model_routes 表配置(节点->模型->priority), 无配置时用传入的 model。
    - qwen 系列(qwen-flash 辅助模型) -> 百炼 OpenAI 兼容端点 + DASHSCOPE_API_KEY
    - deepseek 系列(核心推理) -> DeepSeek 官方端点 + DEEPSEEK_API_KEY

    node: 节点名, 用于 Token 消耗指标与主备切换状态(如 supervisor/planner/coder/reporter/aux_*)
    """
    from src.utils.model_router import get_model_router

    resolved = get_model_router().resolve(node, fallback=model)
    config = _resolve_llm_config(resolved)
    return _LLMWrapper(
        ChatOpenAI(
            model=resolved,
            api_key=config["api_key"],
            base_url=config["base_url"],
            temperature=temperature,
            timeout=settings.llm_timeout,
            max_retries=settings.llm_max_retries,
            http_client=_build_llm_http_client(),
        ),
        node=node or resolved,
    )


from src.nodes.supervisor import supervisor_node  # noqa: E402
from src.nodes.planner import planner_node  # noqa: E402
from src.nodes.clarifier import clarifier_node  # noqa: E402
from src.nodes.coder import coder_node  # noqa: E402
from src.nodes.executor import executor_node  # noqa: E402
from src.nodes.reporter import reporter_node  # noqa: E402
from src.nodes.human_approval import human_approval_node  # noqa: E402

__all__ = [
    "supervisor_node",
    "planner_node",
    "coder_node",
    "executor_node",
    "reporter_node",
    "human_approval_node",
    "make_llm",
]
