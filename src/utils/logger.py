"""结构化日志(structlog, JSON 输出 + 敏感字段脱敏)。

- 每条日志携带 run_id / node_name / state_snapshot 等上下文
- 自动屏蔽 password / token / api_key / 身份证号等字段
"""
from __future__ import annotations

import logging
import re
import sys
from typing import Any, Dict

import structlog

# 敏感字段名(键匹配即脱敏)
SENSITIVE_KEYS = {
    "password", "passwd", "pwd", "secret", "token", "api_key",
    "apikey", "access_key", "accesskey", "authorization",
    "cookie", "session", "private_key",
}

# 身份证号 / 手机号掩码
_ID_CARD_RE = re.compile(r"\b\d{17}[\dXx]\b")
_PHONE_RE = re.compile(r"\b1[3-9]\d{9}\b")


def _mask_value(key: str, value: Any) -> Any:
    """对敏感键名做掩码, 对文本中的身份证/手机号做掩码。"""
    if isinstance(value, str) and key.lower() in SENSITIVE_KEYS:
        return "***MASKED***"
    if isinstance(value, str):
        value = _PHONE_RE.sub(lambda m: m.group(0)[:3] + "****" + m.group(0)[7:], value)
        value = _ID_CARD_RE.sub(lambda m: m.group(0)[:6] + "**********" + m.group(0)[-1], value)
    return value


def _redact_processor(
    logger: logging.Logger, method_name: str, event_dict: Dict[str, Any]
) -> Dict[str, Any]:
    """structlog 处理器: 递归脱敏 event_dict 中的敏感字段。"""
    for key in list(event_dict.keys()):
        event_dict[key] = _mask_value(key, event_dict[key])
    return event_dict


def _setup_structlog() -> None:
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _redact_processor,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelName("INFO")
        ),
        logger_factory=structlog.PrintLoggerFactory(sys.stdout),
        cache_logger_on_first_use=True,
    )


_setup_structlog()


def get_logger(name: str = "data_pipeline_agent"):
    """获取带模块名的结构化 logger。"""
    return structlog.get_logger(name)


def bind_run_context(run_id: str, node_name: str | None = None) -> None:
    """绑定全链路追踪上下文(run_id / node_name), 自动注入后续所有日志。"""
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(run_id=run_id, node_name=node_name)
