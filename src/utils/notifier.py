"""通知与告警预留接口(审批通知 + 告警推送)。

背景: 企业微信/钉钉机器人卡片审批、PagerDuty 告警推送依赖企业应用注册、
公网回调域名、HTTPS 等外部条件, 个人开发环境不具备, 因此**仅定义统一接口
与默认实现, 不做实际开发**(详见 data/开发流程.docx 备注)。

现状与预留:
- console      (默认) ConsoleNotifier: 结构化日志输出, 单机可直接运行
- wecom        (预留) 需企业微信应用 + 公网回调域名 + HTTPS
- dingtalk     (预留) 需钉钉机器人 webhook + 公网回调域名
- pagerduty    (预留) 需 PagerDuty routing key 与公网出口

接入方式: settings.notify_channel 切换通道; 外部条件具备后在对应
_NotImplementedNotifier 处补全真实实现即可, 调用方无需改动。

用法:
    from src.utils.notifier import get_notifier

    notifier = get_notifier()
    notifier.send_approval(task_id="...", query="计算近7天留存率")
    notifier.send_alert(level="critical", title="任务失败率超阈值", message="...")
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from src.utils.logger import get_logger
from src.utils.settings import get_settings

logger = get_logger(__name__)
settings = get_settings()

CHANNEL_CONSOLE = "console"
CHANNEL_WECOM = "wecom"
CHANNEL_DINGTALK = "dingtalk"
CHANNEL_PAGERDUTY = "pagerduty"


class BaseNotifier(ABC):
    """通知/告警统一接口。"""

    channel = "base"

    @abstractmethod
    def send_approval(self, task_id: str, query: str = "", approver_url: str = "") -> None:
        """发送人工审批通知(卡片/消息), 提示审批人去审批接口操作。

        Args:
            task_id: 待审批任务 ID
            query: 用户原始需求(摘要)
            approver_url: 审批入口地址(如 /api/v1/tasks/{id}/approve)
        """

    @abstractmethod
    def send_alert(self, level: str, title: str, message: str = "") -> None:
        """发送告警。level: warning | critical。"""


class ConsoleNotifier(BaseNotifier):
    """默认实现: 结构化日志输出(个人开发环境可直接运行, 无副作用)。"""

    channel = CHANNEL_CONSOLE

    def send_approval(self, task_id: str, query: str = "", approver_url: str = "") -> None:
        logger.info(
            "notify_approval_requested",
            channel=self.channel,
            task_id=task_id,
            query=query[:200],
            approver_url=approver_url,
        )

    def send_alert(self, level: str, title: str, message: str = "") -> None:
        log = logger.warning if level == "warning" else logger.error
        log("notify_alert", channel=self.channel, level=level, title=title, message=message[:500])


class _NotImplementedNotifier(BaseNotifier):
    """预留占位实现: 通道被配置但真实集成未开发, 显式记录日志避免静默失败。

    待外部条件(企业应用/公网域名/HTTPS 等)具备后, 将本类替换为真实实现:
    例如 WeComNotifier 调用 webhook 接口推送卡片、PagerDutyNotifier 调用
    Events API v2, 并在 __init__ 读取 settings.wecom_webhook_url 等配置。
    """

    def __init__(self, channel: str, reason: str) -> None:
        self.channel = channel
        self._reason = reason

    def _stub_log(self, method: str, **extra: str) -> None:
        logger.warning(
            "notifier_stub_not_implemented",
            channel=self.channel,
            method=method,
            reason=self._reason,
            **extra,
        )

    def send_approval(self, task_id: str, query: str = "", approver_url: str = "") -> None:
        self._stub_log("send_approval", task_id=task_id, approver_url=approver_url)

    def send_alert(self, level: str, title: str, message: str = "") -> None:
        self._stub_log("send_alert", level=level, title=title)


def get_notifier() -> BaseNotifier:
    """按 settings.notify_channel 返回通知器; 未知/未配置通道回退 console。"""
    channel = (settings.notify_channel or CHANNEL_CONSOLE).lower()
    if channel == CHANNEL_CONSOLE:
        return ConsoleNotifier()
    if channel == CHANNEL_WECOM:
        return _NotImplementedNotifier(
            CHANNEL_WECOM,
            "需企业微信应用 + 公网回调域名 + HTTPS, 见开发流程 6.2 备注",
        )
    if channel == CHANNEL_DINGTALK:
        return _NotImplementedNotifier(
            CHANNEL_DINGTALK,
            "需钉钉机器人 webhook + 公网回调域名, 见开发流程 6.2 备注",
        )
    if channel == CHANNEL_PAGERDUTY:
        return _NotImplementedNotifier(
            CHANNEL_PAGERDUTY,
            "需 PagerDuty routing key 与公网出口, 见开发流程 5.5 备注",
        )
    return ConsoleNotifier()
