"""Hermes Agent - 飞书机器人与 WeChat Multi-Agent System 的管理接口

Hermes Agent 作为系统的「接口人」，负责：
1. 通过飞书接收人类指令（/start, /status, /history 等）
2. 触发和管理工作流执行
3. 实时同步执行状态到飞书群
4. 推送通知（Cookie 过期、发布结果、审核失败等）
"""

import os
import sys
import time
import logging
import threading
from datetime import datetime
from typing import Any

# 确保项目根目录在 Python 路径中
from config.paths import BASE_DIR
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from hermes.feishu_client import FeishuClient
from hermes.webhook_server import FeishuWebhookServer
from hermes.commands import CommandParser, ParsedCommand
from models.llm_router import load_config
from graph.workflow import ArticleWorkflow
from graph.state import create_initial_state
from tools.content_db import ContentDB
from tools.notifier import Notifier
from config.agent_labels import AGENT_LABELS
from config.paths import PAUSE_FLAG_PATH
from config.validation import validate_config

logger = logging.getLogger(__name__)


class HermesAgent:
    """
    Hermes Agent - WeChat Multi-Agent System 的飞书管理接口

    架构：
      飞书用户 ──(消息)──> 飞书服务器 ──(webhook)──> Hermes Agent
                                                       │
                                                       ├── 解析命令
                                                       ├── 触发工作流
                                                       ├── 查询状态
                                                       │
      飞书用户 <──(卡片消息)── 飞书服务器 <──(API)──────┘
    """

    def __init__(self, config: dict | None = None):
        self.config = config or load_config()
        self.feishu = FeishuClient(self.config)
        self.parser = CommandParser()
        self.db = ContentDB(self.config.get("storage", {}).get("db_path"))
        self.notifier = Notifier(self.config)

        # 工作流状态跟踪
        self._workflow_status: dict[str, Any] = {
            "running": False,
            "current_article": "",
            "current_stage": "",
            "progress": {},
            "last_result": None,
            "last_run_at": None,
        }
        self._status_lock = threading.Lock()

        # Webhook 服务器（安全加固：默认绑定回环 + 可选 IP 白名单 + 强制加密）
        feishu_config = self.config.get("feishu", {})

        require_encrypt = self._resolve_require_encrypt(feishu_config)
        if require_encrypt and not feishu_config.get("encrypt_key"):
            raise RuntimeError(
                "feishu.require_encrypt 已开启（或 HERMES_REQUIRE_ENCRYPT=1）但未配置 "
                "feishu.encrypt_key，Hermes 拒绝启动"
            )

        self.webhook_server = FeishuWebhookServer(
            host=feishu_config.get("webhook_host", "127.0.0.1"),
            port=feishu_config.get("webhook_port", 9000),
            on_event=self._handle_event,
            verification_token=feishu_config.get("verification_token", ""),
            encrypt_key=feishu_config.get("encrypt_key", ""),
            allow_from=self._resolve_allow_from(feishu_config),
            require_encrypt=require_encrypt,
        )

    @staticmethod
    def _resolve_require_encrypt(feishu_config: dict) -> bool:
        """强制加密开关：feishu.require_encrypt 或环境变量 HERMES_REQUIRE_ENCRYPT=1"""
        if feishu_config.get("require_encrypt"):
            return True
        return os.environ.get("HERMES_REQUIRE_ENCRYPT", "").strip().lower() in (
            "1", "true", "yes", "on",
        )

    @staticmethod
    def _resolve_allow_from(feishu_config: dict) -> list[str]:
        """IP 白名单：环境变量 FEISHU_WEBHOOK_ALLOW_FROM 优先，其次 feishu.webhook_allow_from"""
        env = os.environ.get("FEISHU_WEBHOOK_ALLOW_FROM", "").strip()
        if env:
            return [s.strip() for s in env.split(",") if s.strip()]
        value = feishu_config.get("webhook_allow_from", [])
        if isinstance(value, str):
            return [s.strip() for s in value.split(",") if s.strip()]
        if isinstance(value, list):
            return [s.strip() for s in value if isinstance(s, str) and s.strip()]
        return []

    # ─── 启动与停止 ──────────────────────────────────────────

    def start(self):
        """启动 Hermes Agent（Webhook 服务器 + 通知桥接）"""
        logger.info("Hermes Agent 正在启动...")
        self.webhook_server.start()

        # 桥接 Notifier → 飞书
        self._setup_notification_bridge()

        # 输出配置校验结果（不影响启动）
        for severity, message in validate_config(self.config):
            (logger.warning if severity == "error" else logger.info)(f"配置校验[{severity}]: {message}")

        # 发送启动通知
        card = FeishuClient.build_status_card(
            title="🤖 Hermes Agent 已上线",
            status="就绪",
            details=[
                ("Webhook 端口", str(self.config.get("feishu", {}).get("webhook_port", 9000))),
                ("内容领域", self.config.get("content", {}).get("domain", "未配置")),
                ("发布模式", self.config.get("wechat", {}).get("publish_mode", "draft")),
            ],
            color="green",
        )
        self.feishu.send_card_to_default(card)
        logger.info("Hermes Agent 已启动")

    def stop(self):
        """停止 Hermes Agent"""
        self.webhook_server.stop()
        self._teardown_notification_bridge()
        self.db.close()  # 关闭持久数据库连接
        logger.info("Hermes Agent 已停止")

    def run_forever(self):
        """阻塞运行（保持主线程存活）"""
        self.start()
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("收到中断信号，正在停止...")
            self.stop()

    # ─── 事件处理 ─────────────────────────────────────────────

    def _handle_event(self, event: dict):
        """处理来自飞书的事件"""
        event_type = event.get("type", "")

        if event_type == "message":
            self._handle_message(event)
        elif event_type == "card_action":
            self._handle_card_action(event)

    def _handle_message(self, event: dict):
        """处理飞书消息"""
        text = event.get("text", "").strip()
        message_id = event.get("message_id", "")
        chat_id = event.get("chat_id", "")
        user_id = event.get("user_id", "")

        # 去掉 @机器人 的前缀
        if text.startswith("@_user"):
            parts = text.split(" ", 1)
            text = parts[1] if len(parts) > 1 else ""

        # 解析命令
        command = self.parser.parse(
            text,
            message_id=message_id,
            chat_id=chat_id,
            user_id=user_id,
        )

        if command is None:
            # 非命令消息，忽略或回复帮助
            if text and not text.startswith("/"):
                logger.debug(f"忽略非命令消息: {text[:30]}")
            return

        # 路由命令
        self._dispatch_command(command)

    def _dispatch_command(self, cmd: ParsedCommand):
        """路由并执行命令"""
        reply_to = cmd.message_id

        handlers = {
            "start": self._cmd_start,
            "status": self._cmd_status,
            "history": self._cmd_history,
            "config": self._cmd_config,
            "login": self._cmd_login,
            "help": self._cmd_help,
            "pause": self._cmd_pause,
            "resume": self._cmd_resume,
        }

        handler = handlers.get(cmd.action)
        if handler:
            try:
                handler(cmd)
            except Exception as e:
                logger.error(f"命令执行失败 [{cmd.action}]: {e}", exc_info=True)
                error_card = FeishuClient.build_status_card(
                    title="命令执行失败",
                    status=f"错误: {e}",
                    details=[("命令", cmd.raw_text)],
                    color="red",
                )
                self.feishu.reply_card(reply_to, error_card)
        else:
            self.feishu.reply_text(reply_to, f"未知命令: {cmd.action}\n输入 /help 查看可用命令")

    # ─── 命令实现 ─────────────────────────────────────────────

    def _cmd_start(self, cmd: ParsedCommand):
        """/start [主题] - 触发工作流"""
        with self._status_lock:
            if self._workflow_status["running"]:
                card = FeishuClient.build_status_card(
                    title="⚠️ 任务正在执行中",
                    status="运行中",
                    details=[
                        ("当前文章", self._workflow_status.get("current_article", "")),
                        ("当前阶段", self._workflow_status.get("current_stage", "")),
                    ],
                    color="orange",
                )
                self.feishu.reply_card(cmd.message_id, card)
                return

            # 持锁置 running=True 后再启动线程，避免两个并发 /start 同时启动工作流
            self._workflow_status["running"] = True
            self._workflow_status["current_stage"] = "启动中"
            self._workflow_status["progress"] = self._empty_progress()

        topic = cmd.params.get("topic", "")
        label = f"「{topic}」" if topic else "自动选题"

        # 回复确认
        ack_card = FeishuClient.build_status_card(
            title="🚀 任务已启动",
            status="正在执行",
            details=[
                ("选题方式", label),
                ("触发者", cmd.user_name or cmd.user_id or "飞书用户"),
            ],
            color="blue",
        )
        self.feishu.reply_card(cmd.message_id, ack_card)

        # 在后台线程执行工作流
        thread = threading.Thread(
            target=self._run_workflow_async,
            args=(topic,),
            daemon=True,
        )
        try:
            thread.start()
        except Exception as e:
            logger.error(f"启动工作流线程失败: {e}", exc_info=True)
            with self._status_lock:
                self._workflow_status["running"] = False
            self.feishu.reply_card(
                cmd.message_id,
                FeishuClient.build_status_card(
                    title="❌ 任务启动失败",
                    status=f"错误: {e}",
                    details=[],
                    color="red",
                ),
            )

    def _cmd_status(self, cmd: ParsedCommand):
        """/status - 查看当前状态"""
        with self._status_lock:
            status = self._workflow_status.copy()

        if status["running"]:
            progress = status.get("progress", {})
            card = FeishuClient.build_workflow_card(
                article_title=status.get("current_article", "未知"),
                stage=status.get("current_stage", "未知"),
                progress=progress,
            )
        elif status["last_result"]:
            result = status["last_result"]
            publish_result = result.get("publish_result", {})
            card = FeishuClient.build_result_card(
                article_title=result.get("article_title", "未知"),
                success=publish_result.get("success", False),
                details={
                    "发布模式": publish_result.get("mode", ""),
                    "审核评分": str(result.get("review_result", {}).get("score", "")),
                    "重写次数": str(result.get("retry_count", 0)),
                    "上次执行": status.get("last_run_at", ""),
                },
                review_history=result.get("review_history"),
            )
        else:
            card = FeishuClient.build_status_card(
                title="📊 系统状态",
                status="空闲",
                details=[
                    ("最近执行", status.get("last_run_at", "暂无")),
                    ("内容领域", self.config.get("content", {}).get("domain", "")),
                ],
                color="blue",
            )

        self.feishu.reply_card(cmd.message_id, card)

    def _cmd_history(self, cmd: ParsedCommand):
        """/history - 查看最近文章"""
        # 只读查询走短连接（with 上下文自动关闭），避免在 Webhook worker 线程
        # 上为每条请求遗留一条 thread-local 持久 sqlite 连接。
        with ContentDB(self.config.get("storage", {}).get("db_path")) as db:
            titles = db.get_recent_titles(days=30)
        if not titles:
            self.feishu.reply_text(cmd.message_id, "最近 30 天暂无文章记录。")
            return

        lines = [f"📝 最近 30 天文章（共 {len(titles)} 篇）：\n"]
        for i, title in enumerate(titles[:10], 1):
            lines.append(f"  {i}. {title}")
        if len(titles) > 10:
            lines.append(f"\n  ... 还有 {len(titles) - 10} 篇")

        self.feishu.reply_text(cmd.message_id, "\n".join(lines))

    def _cmd_config(self, cmd: ParsedCommand):
        """/config - 查看配置摘要"""
        content = self.config.get("content", {})
        wechat = self.config.get("wechat", {})
        review = self.config.get("review", {})
        schedule = self.config.get("schedule", {})

        text = f"""📋 当前配置摘要：

📌 内容设置
  领域：{content.get('domain', '未设置')}
  关键词：{', '.join(content.get('keywords', []))}
  字数：{content.get('article_length', {}).get('min', 1500)}-{content.get('article_length', {}).get('max', 3000)}
  模板：{content.get('template', 'simple')}

📌 发布设置
  模式：{wechat.get('publish_mode', 'draft')}

📌 审核设置
  最大重试：{review.get('max_retries', 2)} 次
  最低分数：{review.get('min_score', 7)}/10

📌 调度设置
  Cron：{schedule.get('cron', '未设置')}
  时区：{schedule.get('timezone', 'Asia/Shanghai')}"""

        self.feishu.reply_text(cmd.message_id, text)

    def _cmd_login(self, cmd: ParsedCommand):
        """/login - 提醒重新登录"""
        self.feishu.reply_text(
            cmd.message_id,
            "请在服务器上运行以下命令重新登录公众号：\n\n  python main.py login\n\n登录成功后 Cookie 将自动更新。",
        )

    def _cmd_help(self, cmd: ParsedCommand):
        """/help - 显示帮助"""
        self.feishu.reply_text(cmd.message_id, CommandParser.get_help_text())

    def _cmd_pause(self, cmd: ParsedCommand):
        """/pause - 暂停调度"""
        pause_flag = PAUSE_FLAG_PATH
        os.makedirs(os.path.dirname(pause_flag), exist_ok=True)
        with open(pause_flag, "w") as f:
            f.write(f"Paused at {datetime.now().isoformat()}\nBy: {cmd.user_name or cmd.user_id}\nSend /resume to resume.\n")
        self.feishu.reply_text(cmd.message_id, "⏸️ 定时调度已暂停。发送 /resume 恢复。")
        logger.info(f"定时调度已暂停（通过飞书命令），标志文件: {pause_flag}")

    def _cmd_resume(self, cmd: ParsedCommand):
        """/resume - 恢复调度"""
        pause_flag = PAUSE_FLAG_PATH
        if os.path.exists(pause_flag):
            os.remove(pause_flag)
            self.feishu.reply_text(cmd.message_id, "▶️ 定时调度已恢复。")
            logger.info(f"定时调度已恢复（通过飞书命令），已删除标志文件: {pause_flag}")
        else:
            self.feishu.reply_text(cmd.message_id, "▶️ 调度器当前未暂停，无需恢复。")

    # ─── 工作流执行 ───────────────────────────────────────────

    def _run_workflow_async(self, topic: str = ""):
        """在后台线程执行工作流，并同步状态到飞书"""
        with self._status_lock:
            self._workflow_status["running"] = True
            self._workflow_status["progress"] = self._empty_progress()

        try:
            workflow = ArticleWorkflow(self.config)

            # 构建初始状态（统一走 create_initial_state，保证默认字段齐全）
            initial_state = create_initial_state(
                max_retries=self.config.get("review", {}).get("max_retries", 2),
                extra={
                    "metadata": {"triggered_by": "feishu"},
                    **({"topic": topic} if topic else {}),
                },
            )

            # 初始进度卡片
            self._send_progress_update("启动", initial_state)

            # 进度回调：每完成一个 Agent 节点时更新飞书进度
            # （workflow.run 传入的是累积后的完整状态，含 article_title 等字段）
            def on_node_complete(node_name: str, node_state: dict):
                with self._status_lock:
                    if node_name in self._workflow_status["progress"]:
                        self._workflow_status["progress"][node_name] = "completed"
                    self._workflow_status["current_stage"] = AGENT_LABELS.get(node_name, node_name)
                self._send_progress_update(
                    AGENT_LABELS.get(node_name, node_name),
                    node_state,
                )

            # 执行工作流（带进度回调）
            result = workflow.run(
                initial_state if initial_state else None,
                progress_callback=on_node_complete,
            )

            # 更新状态
            with self._status_lock:
                self._workflow_status["running"] = False
                self._workflow_status["last_result"] = dict(result) if result else {}
                self._workflow_status["last_run_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                # 选题降级中止时后续节点不会执行，进度会永久停在 pending，
                # 状态卡片一直显示「等待中」；这里统一置为 skipped。
                if (result.get("metadata") or {}).get("topic_degraded"):
                    self._workflow_status["progress"] = {
                        node: ("skipped" if status == "pending" else status)
                        for node, status in self._workflow_status["progress"].items()
                    }

            # 发送结果卡片
            publish_result = result.get("publish_result", {}) if result else {}
            result_card = FeishuClient.build_result_card(
                article_title=result.get("article_title", "未知") if result else "未知",
                success=publish_result.get("success", False),
                details={
                    "发布模式": publish_result.get("mode", ""),
                    "审核评分": str(result.get("review_result", {}).get("score", "")) if result else "",
                    "重写次数": str(result.get("retry_count", 0)) if result else "0",
                    "错误": publish_result.get("error", ""),
                },
                review_history=result.get("review_history") if result else None,
            )
            self.feishu.send_card_to_default(result_card)

        except Exception as e:
            logger.error(f"工作流执行失败: {e}", exc_info=True)
            with self._status_lock:
                self._workflow_status["running"] = False

            error_card = FeishuClient.build_status_card(
                title="❌ 工作流执行失败",
                status=f"错误: {e}",
                details=[("时间", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))],
                color="red",
            )
            self.feishu.send_card_to_default(error_card)

    def _send_progress_update(self, stage: str, state: dict):
        """发送进度更新到飞书"""
        with self._status_lock:
            self._workflow_status["current_stage"] = stage
            article_title = state.get("article_title", state.get("topic", "新文章"))
            self._workflow_status["current_article"] = article_title

            card = FeishuClient.build_workflow_card(
                article_title=article_title,
                stage=stage,
                progress=self._workflow_status["progress"],
            )
        self.feishu.send_card_to_default(card)

    # ─── 通知桥接 ─────────────────────────────────────────────

    def _empty_progress(self) -> dict[str, str]:
        """初始化进度跟踪字典（含 retry_counter 节点）"""
        return {
            "topic_planner": "pending",
            "content_writer": "pending",
            "image_generator": "pending",
            "reviewer": "pending",
            "retry_counter": "pending",
            "formatter": "pending",
            "publisher": "pending",
        }

    def _setup_notification_bridge(self):
        """将 Notifier 的输出桥接到飞书（显式回调注册，非 monkey-patch）"""
        def bridge_send(title: str, message: str, level: str = "info"):
            color_map = {"info": "blue", "warning": "orange", "error": "red"}
            try:
                card = FeishuClient.build_status_card(
                    title=f"🔔 {title}",
                    status=message[:200],
                    details=[],
                    color=color_map.get(level, "blue"),
                )
                self.feishu.send_card_to_default(card)
            except Exception as e:
                logger.warning(f"飞书通知桥接发送失败: {e}")

        self.notifier.register_bridge(bridge_send)
        self._notification_bridge = bridge_send
        logger.info("通知桥接已设置：Notifier → 飞书")

    def _teardown_notification_bridge(self):
        """拆除通知桥接（注销显式回调）"""
        if hasattr(self, "_notification_bridge"):
            self.notifier.unregister_bridge(self._notification_bridge)
            del self._notification_bridge
            logger.info("通知桥接已拆除")