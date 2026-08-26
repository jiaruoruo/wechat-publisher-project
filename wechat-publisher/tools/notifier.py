"""通知工具 - Cookie 过期、发布结果等场景的通知机制"""

import os
import logging
import smtplib
import json
from typing import Callable
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from config.paths import NOTIFICATION_LOG_PATH

logger = logging.getLogger(__name__)


class Notifier:
    """通知系统 - 支持多种通知方式

    桥接机制：通过 register_bridge() 注册外部回调（如飞书卡片推送），
    替代 monkey-patch _send_notification 私有方法的方式，重构安全。
    """

    def __init__(self, config: dict):
        self.config = config.get("notification", {})
        self.enabled = self.config.get("enabled", False)
        self._bridges: list[Callable] = []
        if not self.enabled:
            logger.warning(
                "通知未启用（notification.enabled=false）："
                "审核失败/发布失败等关键事件仅记录到日志文件，不会推送邮件/企业微信"
            )

    def register_bridge(self, func: Callable):
        """注册通知桥接回调，签名: func(title: str, message: str, level: str)"""
        if func not in self._bridges:
            self._bridges.append(func)

    def unregister_bridge(self, func: Callable):
        """注销通知桥接回调"""
        if func in self._bridges:
            self._bridges.remove(func)

    def notify_cookie_expired(self, error_msg: str = ""):
        """Cookie 过期通知 - 提醒用户重新登录"""
        message = (
            "微信公众号登录已过期！\n"
            "请运行以下命令重新登录：\n"
            "  python main.py login\n"
        )
        if error_msg:
            message += f"\n错误信息: {error_msg}"

        self._send_notification(
            title="公众号登录过期",
            message=message,
            level="warning",
        )

    def notify_publish_result(self, title: str, success: bool, error: str = "", preview_path: str = ""):
        """发布结果通知"""
        status = "成功" if success else "失败"
        message = f"文章「{title}」发布{status}。\n"
        if error:
            message += f"错误信息: {error}\n"
        if preview_path:
            message += f"预览截图: {preview_path}\n"

        self._send_notification(
            title=f"公众号发布{status}",
            message=message,
            level="info" if success else "error",
        )

    def notify_review_failed(self, article_title: str, retry_count: int, feedback: str):
        """审核失败通知 - 达到最大重试次数"""
        message = (
            f"文章「{article_title}」经过 {retry_count} 次修改后仍未通过审核。\n"
            f"审核反馈: {feedback}\n"
            "文章将强制进入排版发布流程，请人工检查。"
        )
        self._send_notification(
            title="文章审核未通过",
            message=message,
            level="warning",
        )

    def _send_notification(self, title: str, message: str, level: str = "info"):
        """发送通知（多通道）"""
        # 始终记录到日志文件
        self._log_notification(title, message, level)

        # 控制台输出
        log_func = {
            "warning": logger.warning,
            "error": logger.error,
        }.get(level, logger.info)
        log_func(f"[通知] {title}: {message}")

        # 桥接回调（如飞书卡片推送），无论 enabled 与否都执行
        for bridge in list(self._bridges):
            try:
                bridge(title, message, level)
            except Exception as e:
                logger.warning(f"通知桥接回调失败: {e}")

        if not self.enabled:
            return

        # 邮件通知（如果配置了）
        email_config = self.config.get("email", {})
        if email_config.get("enabled"):
            self._send_email(title, message, email_config)

        # 企业微信通知（如果配置了）
        wechat_config = self.config.get("wechat_work", {})
        if wechat_config.get("enabled"):
            self._send_wechat_work(message, wechat_config)

    def _log_notification(self, title: str, message: str, level: str):
        """记录通知到文件"""
        os.makedirs(os.path.dirname(NOTIFICATION_LOG_PATH), exist_ok=True)
        entry = {
            "title": title,
            "message": message,
            "level": level,
        }
        with open(NOTIFICATION_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _send_email(self, title: str, message: str, config: dict):
        """发送邮件通知"""
        try:
            msg = MIMEMultipart()
            msg["From"] = config["sender"]
            msg["To"] = config["receiver"]
            msg["Subject"] = f"[公众号自动发布] {title}"
            msg.attach(MIMEText(message, "plain", "utf-8"))

            with smtplib.SMTP_SSL(config["smtp_host"], config.get("smtp_port", 465)) as server:
                server.login(config["sender"], config["password"])
                server.sendmail(config["sender"], config["receiver"], msg.as_string())

            logger.info(f"邮件通知已发送: {title}")
        except Exception as e:
            logger.error(f"邮件通知发送失败: {e}")

    def _send_wechat_work(self, message: str, config: dict):
        """发送企业微信通知"""
        try:
            import requests

            webhook_url = config.get("webhook_url", "")
            if not webhook_url:
                return

            payload = {
                "msgtype": "text",
                "text": {"content": f"[公众号自动发布]\n{message}"},
            }
            response = requests.post(webhook_url, json=payload, timeout=10)
            response.raise_for_status()
            logger.info("企业微信通知已发送")
        except Exception as e:
            logger.error(f"企业微信通知发送失败: {e}")
