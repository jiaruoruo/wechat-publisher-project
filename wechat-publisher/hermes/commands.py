"""飞书命令解析 - 解析用户发送的文本命令"""

import re
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ParsedCommand:
    """解析后的命令"""
    action: str                           # 动作类型
    params: dict[str, Any] = field(default_factory=dict)  # 参数
    raw_text: str = ""                    # 原始文本
    message_id: str = ""                  # 飞书消息 ID
    chat_id: str = ""                     # 来源群聊 ID
    user_id: str = ""                     # 发送者 ID
    user_name: str = ""                   # 发送者名称


class CommandParser:
    """
    飞书消息命令解析器

    支持的命令格式：
      /start              - 立即执行一次工作流（自动选题）
      /start <主题>       - 指定主题执行工作流
      /status             - 查看当前任务状态
      /history            - 查看最近文章列表
      /config             - 查看当前配置摘要
      /login              - 提醒重新登录公众号
      /help               - 显示帮助信息
      /pause              - 暂停定时调度
      /resume             - 恢复定时调度
    """

    # 命令正则映射
    PATTERNS = [
        (r"^/start\s*$", "start", {}),
        (r"^/start\s+(.+)$", "start", {"topic": 1}),
        (r"^/status\s*$", "status", {}),
        (r"^/history\s*$", "history", {}),
        (r"^/config\s*$", "config", {}),
        (r"^/login\s*$", "login", {}),
        (r"^/help\s*$", "help", {}),
        (r"^/pause\s*$", "pause", {}),
        (r"^/resume\s*$", "resume", {}),
    ]

    def parse(
        self,
        text: str,
        message_id: str = "",
        chat_id: str = "",
        user_id: str = "",
        user_name: str = "",
    ) -> ParsedCommand | None:
        """
        解析文本消息为命令

        Returns:
            ParsedCommand 或 None（无法识别时）
        """
        text = text.strip()

        for pattern, action, param_groups in self.PATTERNS:
            match = re.match(pattern, text, re.IGNORECASE)
            if match:
                params = {}
                for param_name, group_idx in param_groups.items():
                    params[param_name] = match.group(group_idx).strip()

                return ParsedCommand(
                    action=action,
                    params=params,
                    raw_text=text,
                    message_id=message_id,
                    chat_id=chat_id,
                    user_id=user_id,
                    user_name=user_name,
                )

        return None

    @staticmethod
    def get_help_text() -> str:
        """返回帮助文本"""
        return """📋 WeChat Multi-Agent System 命令列表：

/start          - 立即执行一次工作流（自动选题）
/start <主题>   - 指定主题执行工作流
/status         - 查看当前任务执行状态
/history        - 查看最近文章列表
/config         - 查看当前配置摘要
/login          - 提醒重新登录公众号
/pause          - 暂停定时调度
/resume         - 恢复定时调度
/help           - 显示此帮助信息

示例：
  /start AI大模型最新进展
  /status
  /history"""
