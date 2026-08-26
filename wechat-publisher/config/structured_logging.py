"""结构化日志工具 — 键值对形式记录关键事件，便于日志检索与分析"""

import json
import logging
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)


def log_event(
    event: str,
    level: str = "info",
    **kwargs: Any,
) -> None:
    """
    记录结构化事件日志

    示例:
        log_event("workflow_complete", article_title="AI趋势", score=8, retries=1)

    Args:
        event: 事件名称（kebab-case）
        level: 日志级别 (debug/info/warning/error)
        **kwargs: 事件相关的键值对数据
    """
    payload = {
        "event": event,
        "timestamp": datetime.now().isoformat(),
        "data": kwargs,
    }
    message = json.dumps(payload, ensure_ascii=False, default=str)

    log_func = {
        "debug": logger.debug,
        "info": logger.info,
        "warning": logger.warning,
        "error": logger.error,
    }.get(level, logger.info)

    log_func(message)


def workflow_event(stage: str, article_title: str = "", **kwargs: Any) -> None:
    """记录工作流阶段事件（简化版）"""
    log_event("workflow." + stage, article_title=article_title, **kwargs)


def api_event(provider: str, call: str, duration_ms: float = 0, status: str = "ok", **kwargs: Any) -> None:
    """记录 API 调用事件"""
    log_event("api." + call, provider=provider, duration_ms=duration_ms, status=status, **kwargs)


def publish_event(success: bool, mode: str = "draft", title: str = "", **kwargs: Any) -> None:
    """记录发布事件"""
    log_event("publish", success=success, mode=mode, title=title, **kwargs)
