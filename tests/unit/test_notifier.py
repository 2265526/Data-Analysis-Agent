"""通知/告警预留接口单元测试: 默认 console 可用, 预留通道不静默失败。"""
from __future__ import annotations

import pytest

from src.utils.notifier import (
    CHANNEL_CONSOLE,
    CHANNEL_DINGTALK,
    CHANNEL_PAGERDUTY,
    CHANNEL_WECOM,
    BaseNotifier,
    ConsoleNotifier,
    _NotImplementedNotifier,
    get_notifier,
)


def test_default_notifier_is_console() -> None:
    # 默认配置 notify_channel="console"(见 src/utils/settings.py)
    notifier = get_notifier()
    assert isinstance(notifier, ConsoleNotifier)


def test_console_notifier_does_not_raise() -> None:
    notifier = ConsoleNotifier()
    assert notifier.channel == CHANNEL_CONSOLE
    # 默认实现仅写日志, 不抛错
    notifier.send_approval(task_id="t1", query="计算留存率")
    notifier.send_alert(level="critical", title="失败率过高", message="detail")


def test_stub_channels_log_but_do_not_raise() -> None:
    for channel, reason in [
        (CHANNEL_WECOM, "wecom"),
        (CHANNEL_DINGTALK, "dingtalk"),
        (CHANNEL_PAGERDUTY, "pagerduty"),
    ]:
        stub = _NotImplementedNotifier(channel, reason)
        stub.send_approval(task_id="t1")     # 不应抛错
        stub.send_alert(level="warning", title="x")  # 不应抛错
        assert stub.channel == channel
