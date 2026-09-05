"""排版美化 Agent - 将 Markdown 转换为公众号兼容的富文本 HTML"""

import re
import logging
from datetime import datetime

import markdown

from agents.base import BaseAgent
from graph.state import ArticleState

logger = logging.getLogger(__name__)


# 预设排版模板样式
TEMPLATES = {
    "simple": {
        "body": "font-size:16px; line-height:1.8; color:#333333; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;",
        "h2": "font-size:18px; font-weight:bold; color:#1a1a1a; margin:30px 0 15px 0; padding-bottom:8px; border-bottom:1px solid #eee;",
        "h3": "font-size:17px; font-weight:bold; color:#1a1a1a; margin:25px 0 12px 0;",
        "p": "margin:0 0 20px 0; text-align:justify;",
        "blockquote": "border-left:3px solid #07c160; background:#f7f7f7; padding:15px 20px; margin:20px 0; color:#666;",
        "img": "width:100%; border-radius:4px; margin:15px 0;",
    },
    "business": {
        "body": "font-size:15px; line-height:1.9; color:#2c3e50; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;",
        "h2": "font-size:18px; font-weight:bold; color:#2c3e50; margin:30px 0 15px 0; padding-left:12px; border-left:4px solid #3498db;",
        "h3": "font-size:16px; font-weight:bold; color:#2c3e50; margin:25px 0 12px 0;",
        "p": "margin:0 0 18px 0; text-align:justify;",
        "blockquote": "border-left:3px solid #3498db; background:#f0f7ff; padding:15px 20px; margin:20px 0; color:#555;",
        "img": "width:100%; border-radius:6px; margin:15px 0; box-shadow:0 2px 8px rgba(0,0,0,0.1);",
    },
    "lively": {
        "body": "font-size:16px; line-height:1.8; color:#333; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;",
        "h2": "font-size:19px; font-weight:bold; color:#ff6b35; margin:30px 0 15px 0; text-align:center;",
        "h3": "font-size:17px; font-weight:bold; color:#ff6b35; margin:25px 0 12px 0;",
        "p": "margin:0 0 20px 0; text-align:justify;",
        "blockquote": "border-left:3px solid #ff6b35; background:#fff5f0; padding:15px 20px; margin:20px 0; color:#666; border-radius:0 8px 8px 0;",
        "img": "width:100%; border-radius:8px; margin:15px 0;",
    },
}


class FormatterAgent(BaseAgent):
    """排版美化 Agent：将 Markdown 转换为公众号兼容 HTML"""

    agent_name = "formatter"
    prompt_file = "formatter.yaml"

    def __init__(self, llm_router, config: dict):
        super().__init__(llm_router, config)
        self.template_name = config.get("content", {}).get("template", "simple")

    def run(self, state: ArticleState) -> dict:
        """执行排版转换"""
        content = state.get("content", "")
        inline_images = state.get("inline_images", [])

        # 方案一：先尝试用 LLM 生成精美排版
        try:
            formatted_html = self._format_with_llm(content, inline_images)
        except Exception as e:
            logger.warning(f"LLM 排版失败，使用本地转换: {e}")
            formatted_html = self._format_locally(content, inline_images)

        # 图片占位符 {{IMAGE_PATH_N}} 保持原样：由发布阶段（publish_actions）
        # 上传微信 CDN 后替换为外链 URL（失败回退 base64），实现图片素材外链分离，
        # 避免 HTML 内嵌数 MB base64。formatter.yaml 也要求 LLM 保留该占位符。

        return {
            "formatted_html": formatted_html,
            # metadata 走 schema 级归并，只需返回新增字段
            "metadata": {
                "formatted_at": datetime.now().isoformat(),
                "template": self.template_name,
            },
        }

    def _format_with_llm(self, content: str, inline_images: list[str]) -> str:
        """使用 LLM 进行排版转换"""
        template = self.template_name
        prompt = f"""请将以下 Markdown 文章转换为微信公众号兼容的富文本 HTML。

使用「{template}」排版模板。

## Markdown 原文
{content}

请直接输出 HTML 内容，不要包含代码块标记。"""

        response = self.invoke(prompt)

        # 清理响应
        html = response.strip()
        if html.startswith("```html"):
            html = html[len("```html"):].strip()
        if html.startswith("```"):
            html = html[3:].strip()
        if html.endswith("```"):
            html = html[:-3].strip()

        return html

    def _format_locally(self, content: str, inline_images: list[str]) -> str:
        """本地 Markdown 转 HTML（降级方案）"""
        # 先移除 [IMAGE: ...] 标记，避免 markdown 库解析错误
        content = re.sub(r"\[IMAGE:\s*.*?\]", "", content)

        # 转换 Markdown 为 HTML
        html = markdown.markdown(
            content,
            extensions=["extra", "codehilite", "toc"],
        )

        # 应用内联样式
        styles = TEMPLATES.get(self.template_name, TEMPLATES["simple"])
        html = self._apply_inline_styles(html, styles)

        return html

    def _apply_inline_styles(self, html: str, styles: dict) -> str:
        """为 HTML 元素添加内联样式（使用 BeautifulSoup 安全解析）"""
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")

        # 为段落添加样式
        for p in soup.find_all("p"):
            existing = p.get("style", "")
            p["style"] = f"{styles['p']} {existing}".strip()

        # 为 h2 添加样式
        for h in soup.find_all("h2"):
            existing = h.get("style", "")
            h["style"] = f"{styles['h2']} {existing}".strip()

        # 为 h3 添加样式
        for h in soup.find_all("h3"):
            existing = h.get("style", "")
            h["style"] = f"{styles['h3']} {existing}".strip()

        # 为引用块添加样式
        for bq in soup.find_all("blockquote"):
            existing = bq.get("style", "")
            bq["style"] = f"{styles['blockquote']} {existing}".strip()

        # 为图片添加样式
        for img in soup.find_all("img"):
            existing = img.get("style", "")
            img["style"] = f"{styles['img']} {existing}".strip()

        # 提取 body 内容（BeautifulSoup 会自动包裹 html/body）
        body_content = "".join(str(c) for c in soup.body.children) if soup.body else str(soup)

        # 包裹外层 section
        return f'<section style="{styles["body"]}">{body_content.strip()}</section>'