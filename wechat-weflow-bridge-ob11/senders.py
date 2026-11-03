"""
消息发送器模块：UIA 发送器与工厂函数。
"""

import logging
from uia_sender import UiaSender

log = logging.getLogger("ob11-bridge")


def create_sender():
    """创建 UIA 消息发送器"""
    log.info("使用 UIA 发送消息（纯键盘/剪贴板模拟）")
    return UiaSender()


def create_moments_sender():
    """创建朋友圈发送器（UIA 自动化）"""
    from moments_sender import MomentsSender
    log.info("使用 UIA 朋友圈发送器")
    return MomentsSender()


def create_moments_social_sender():
    """创建定时浏览、点赞和评论使用的主窗口 UIA 驱动。"""
    from moments_social_sender import MomentsSocialSender
    log.info("使用 UIA 朋友圈互动发送器")
    return MomentsSocialSender()
