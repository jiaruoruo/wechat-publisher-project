"""飞书 API 客户端 - 发送消息、卡片消息、获取用户信息"""

import os
import time
import json
import logging
from typing import Any

import requests
from requests.adapters import HTTPAdapter

try:
    from urllib3.util.retry import Retry
except ImportError:  # 老版本 urllib3
    from requests.packages.urllib3.util.retry import Retry  # type: ignore

logger = logging.getLogger(__name__)

from config.agent_labels import AGENT_LABELS, STAGE_ICONS

# 飞书 Open API 基础 URL
FEISHU_API_BASE = "https://open.feishu.cn/open-apis"

# 重试配置：应对飞书瞬时限流(999)/网络抖动；不重试 4xx 业务错误（除 429）。
_MAX_RETRIES = 3
_BACKOFF_FACTOR = 0.5  # 退避：0.5s, 1s, 2s ...


class FeishuClient:
    """飞书 API 客户端，封装 tenant_access_token 管理和常用 API 调用"""

    def __init__(self, config: dict):
        feishu_config = config.get("feishu", {})
        self.app_id = feishu_config.get("app_id", "")
        self.app_secret = feishu_config.get("app_secret", "")
        self.verification_token = feishu_config.get("verification_token", "")
        self.encrypt_key = feishu_config.get("encrypt_key", "")
        self.default_chat_id = feishu_config.get("default_chat_id", "")  # 默认通知群

        self._access_token: str = ""
        self._token_expires_at: float = 0

        # 复用 Session（连接池 + 自动重试），避免每次请求都新建 TCP 连接。
        self._session = self._build_session()

    @staticmethod
    def _build_session() -> requests.Session:
        """构造带重试策略的 Session（仅对瞬态错误重试）"""
        session = requests.Session()
        retry = Retry(
            total=_MAX_RETRIES,
            backoff_factor=_BACKOFF_FACTOR,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(["GET", "POST", "PUT"]),
            raise_on_status=False,
            respect_retry_after_header=True,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=5, pool_maxsize=10)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        return session

    def _post(self, url: str, json_body: dict, timeout: int = 10) -> requests.Response:
        """发送 POST 请求，带连接复用与重试；超时/网络错误由 Retry 兜底"""
        return self._session.post(
            url, headers=self._headers(), json=json_body, timeout=timeout
        )

    # ─── Token 管理 ───────────────────────────────────────────

    def _get_tenant_access_token(self) -> str:
        """获取或刷新 tenant_access_token"""
        if self._access_token and time.time() < self._token_expires_at:
            return self._access_token

        url = f"{FEISHU_API_BASE}/auth/v3/tenant_access_token/internal"
        resp = self._post(url, {
            "app_id": self.app_id,
            "app_secret": self.app_secret,
        })
        resp.raise_for_status()
        data = resp.json()

        if data.get("code") != 0:
            raise RuntimeError(f"获取飞书 token 失败: {data.get('msg')}")

        self._access_token = data["tenant_access_token"]
        # 提前 5 分钟刷新
        self._token_expires_at = time.time() + data.get("expire", 7200) - 300
        logger.info("飞书 tenant_access_token 已刷新")
        return self._access_token

    def _headers(self) -> dict:
        """构造带 Authorization 的请求头"""
        return {
            "Authorization": f"Bearer {self._get_tenant_access_token()}",
            "Content-Type": "application/json; charset=utf-8",
        }

    # ─── 发送消息 ─────────────────────────────────────────────

    def send_text(self, chat_id: str, text: str) -> dict:
        """发送纯文本消息到指定群聊"""
        url = f"{FEISHU_API_BASE}/im/v1/messages?receive_id_type=chat_id"
        resp = self._post(url, {
            "receive_id": chat_id,
            "msg_type": "text",
            "content": json.dumps({"text": text}),
        })
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            logger.error(f"发送文本消息失败: {data.get('msg')}")
        return data

    def send_card(self, chat_id: str, card: dict) -> dict:
        """发送卡片消息到指定群聊"""
        url = f"{FEISHU_API_BASE}/im/v1/messages?receive_id_type=chat_id"
        resp = self._post(url, {
            "receive_id": chat_id,
            "msg_type": "interactive",
            "content": json.dumps(card),
        })
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            logger.error(f"发送卡片消息失败: {data.get('msg')}")
        return data

    def send_to_default(self, text: str):
        """发送文本到默认群"""
        if self.default_chat_id:
            self.send_text(self.default_chat_id, text)
        else:
            logger.warning("未配置 default_chat_id，跳过飞书通知")

    def send_card_to_default(self, card: dict):
        """发送卡片到默认群"""
        if self.default_chat_id:
            self.send_card(self.default_chat_id, card)
        else:
            logger.warning("未配置 default_chat_id，跳过飞书通知")

    # ─── 卡片消息构造 ─────────────────────────────────────────

    @staticmethod
    def build_status_card(
        title: str,
        status: str,
        details: list[tuple[str, str]],
        color: str = "blue",
    ) -> dict:
        """
        构建状态卡片消息

        Args:
            title: 卡片标题
            status: 状态文本
            details: [(字段名, 字段值), ...] 详情列表
            color: 标题颜色 (blue/green/red/orange)
        """
        # 状态对应的 emoji
        status_emoji = {
            "blue": "🔵",
            "green": "✅",
            "red": "❌",
            "orange": "⏳",
        }.get(color, "📋")

        elements = [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"**状态：** {status_emoji} {status}",
                },
            },
            {"tag": "hr"},
        ]

        # 详情字段
        for name, value in details:
            elements.append({
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"**{name}：** {value}",
                },
            })

        elements.append({
            "tag": "note",
            "elements": [
                {
                    "tag": "plain_text",
                    "content": f"WeChat Multi-Agent System | {time.strftime('%Y-%m-%d %H:%M:%S')}",
                }
            ],
        })

        return {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": color,
            },
            "elements": elements,
        }

    @staticmethod
    def build_workflow_card(
        article_title: str,
        stage: str,
        progress: dict,
    ) -> dict:
        """
        构建工作流进度卡片

        Args:
            article_title: 文章标题
            stage: 当前阶段
            progress: {agent_name: status, ...} 各 Agent 状态
        """
        stage_icons = STAGE_ICONS

        progress_lines = []
        for agent_name, status in progress.items():
            icon = stage_icons.get(status, "⬜")
            label = AGENT_LABELS.get(agent_name, agent_name)
            progress_lines.append(f"{icon} {label}")

        elements = [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"**当前阶段：** 🔄 {stage}\n\n" + "\n".join(progress_lines),
                },
            },
            {
                "tag": "note",
                "elements": [
                    {
                        "tag": "plain_text",
                        "content": f"WeChat Multi-Agent System | {time.strftime('%Y-%m-%d %H:%M:%S')}",
                    }
                ],
            },
        ]

        return {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": f"📝 {article_title}"},
                "template": "blue",
            },
            "elements": elements,
        }

    @staticmethod
    def build_result_card(
        article_title: str,
        success: bool,
        details: dict,
        review_history: list | None = None,
    ) -> dict:
        """
        构建发布结果卡片

        Args:
            article_title: 文章标题
            success: 是否发布成功
            details: 详情字段 {字段名: 值}
            review_history: 可选审核历史，元素形如
                {"retry": int, "passed": bool, "score": int, "feedback": str}，
                以「第N次 评分/10 ✅/❌ · 反馈摘要」渲染为多行
        """
        color = "green" if success else "red"
        status = "发布成功" if success else "发布失败"

        detail_list = [(k, str(v)) for k, v in details.items() if v]
        if review_history:
            lines = []
            for i, entry in enumerate(review_history, 1):
                score = entry.get("score", "?")
                passed = entry.get("passed", False)
                feedback = entry.get("feedback", "")
                line = f"第{i}次 {score}/10 {'✅' if passed else '❌'}"
                if feedback:
                    line += f" · {feedback[:40]}"
                lines.append(line)
            detail_list.append(("审核历史", "\n".join(lines)))

        return FeishuClient.build_status_card(
            title=f"📝 {article_title}",
            status=status,
            details=detail_list,
            color=color,
        )

    # ─── 回复消息 ─────────────────────────────────────────────

    def reply_text(self, message_id: str, text: str) -> dict:
        """回复指定消息"""
        url = f"{FEISHU_API_BASE}/im/v1/messages/{message_id}/reply"
        resp = self._post(url, {
            "msg_type": "text",
            "content": json.dumps({"text": text}),
        })
        resp.raise_for_status()
        return resp.json()

    def reply_card(self, message_id: str, card: dict) -> dict:
        """以卡片消息回复指定消息"""
        url = f"{FEISHU_API_BASE}/im/v1/messages/{message_id}/reply"
        resp = self._post(url, {
            "msg_type": "interactive",
            "content": json.dumps(card),
        })
        resp.raise_for_status()
        return resp.json()