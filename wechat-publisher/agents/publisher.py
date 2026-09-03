"""发布执行 Agent - 浏览器自动化登录公众号后台并发布"""

import logging
from datetime import datetime

from agents.base import BaseAgent
from graph.state import ArticleState
from browser.wechat_session import WechatSession
from browser.publish_actions import PublishActions
from tools.notifier import Notifier

logger = logging.getLogger(__name__)


class PublisherAgent(BaseAgent):
    """发布执行 Agent：通过浏览器自动化发布文章到公众号"""

    agent_name = "publisher"
    prompt_file = "publisher.yaml"

    def __init__(self, llm_router, config: dict):
        super().__init__(llm_router, config)
        self.session = WechatSession(config)
        self.publish_mode = config.get("wechat", {}).get("publish_mode", "draft")
        self.notifier = Notifier(config)

    def run(self, state: ArticleState) -> dict:
        """执行发布"""
        article_title = state.get("article_title", "")
        formatted_html = state.get("formatted_html", "")
        cover_image_path = state.get("cover_image_path", "")
        summary = state.get("summary", "")

        # 验证必要信息
        if not article_title or not formatted_html:
            return {
                "publish_result": {
                    "success": False,
                    "mode": self.publish_mode,
                    "error": "缺少标题或正文内容",
                },
            }

        # 执行发布前检查
        check_result = self._pre_publish_check(
            article_title, formatted_html, cover_image_path, summary
        )
        if not check_result.get("ready", True):
            logger.warning(f"发布前检查未通过: {check_result.get('notes', '')}")

        # 启动浏览器并发布
        try:
            self.session.start()

            # 检查登录状态
            if not self.session.check_login():
                logger.warning("登录已过期，尝试交互式登录...")
                self.notifier.notify_cookie_expired("Cookie 已过期，尝试重新登录")
                try:
                    self.session.login_interactive(timeout=120)
                except Exception as e:
                    self.notifier.notify_cookie_expired(str(e))
                    return {
                        "publish_result": {
                            "success": False,
                            "mode": self.publish_mode,
                            "error": f"登录失败，Cookie 已过期: {e}",
                        },
                    }

            # 执行发布操作
            actions = PublishActions(self.session.page, self.config, session=self.session)
            result = actions.execute_publish(
                title=article_title,
                html_content=formatted_html,
                cover_path=cover_image_path,
                summary=summary,
                inline_images=state.get("inline_images", []),
            )

            # 截图保存最终状态
            self.session.take_screenshot("final_state")

            # 发送发布结果通知
            self.notifier.notify_publish_result(
                title=article_title,
                success=result["success"],
                error=result.get("error", ""),
                preview_path=result.get("preview_path", ""),
            )

            return {
                "publish_result": {
                    "success": result["success"],
                    "mode": result["mode"],
                    "error": result.get("error", ""),
                    "published_at": datetime.now().isoformat(),
                    # 发表后的文章链接（复核页提取，取不到为空串）
                    "url": result.get("url", ""),
                    # 最终注入编辑器的 HTML（占位符已解析为 CDN 外链、封面已注入），
                    # 供落库实现「所见即所存」。用 getattr 兜底：测试替身/旧版
                    # PublishActions 可能没有该属性，不应因此打断发布结果。
                    "final_html": getattr(actions, "last_published_html", ""),
                },
                # metadata 走 schema 级归并，只需返回新增字段
                "metadata": {
                    "published_at": datetime.now().isoformat(),
                    "publish_mode": result["mode"],
                    "publish_url": result.get("url", ""),
                },
            }

        except Exception as e:
            logger.error(f"发布失败: {e}")
            return {
                "publish_result": {
                    "success": False,
                    "mode": self.publish_mode,
                    "error": str(e),
                },
            }
        finally:
            self.session.close()

    def _pre_publish_check(
        self,
        title: str,
        html_content: str,
        cover_path: str,
        summary: str,
    ) -> dict:
        """发布前检查"""
        checklist = {
            "title": bool(title),
            "content": bool(html_content) and len(html_content) > 100,
            "cover_image": bool(cover_path),
            "summary": bool(summary),
        }

        ready = all(checklist.values())
        notes = []

        if not checklist["title"]:
            notes.append("缺少标题")
        if not checklist["content"]:
            notes.append("正文内容不足")
        if not checklist["cover_image"]:
            notes.append("缺少封面图")
        if not checklist["summary"]:
            notes.append("缺少摘要（可选）")

        return {
            "ready": ready,
            "checklist": checklist,
            "notes": "; ".join(notes) if notes else "一切就绪",
        }