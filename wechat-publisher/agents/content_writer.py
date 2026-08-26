"""内容创作 Agent - 撰写图文正文"""

import json
import logging
from datetime import datetime

from agents.base import BaseAgent
from graph.state import ArticleState

logger = logging.getLogger(__name__)


class ContentWriterAgent(BaseAgent):
    """内容创作 Agent：根据选题撰写完整文章"""

    agent_name = "content_writer"
    prompt_file = "content_writer.yaml"

    def __init__(self, llm_router, config: dict):
        super().__init__(llm_router, config)
        self.content_config = config.get("content", {})
        self.min_words = self.content_config.get("article_length", {}).get("min", 1500)
        self.max_words = self.content_config.get("article_length", {}).get("max", 3000)

    def run(self, state: ArticleState) -> dict:
        """执行内容创作"""
        topic = state.get("topic", "")
        outline = state.get("outline", "")
        target_audience = state.get("target_audience", "")
        article_title = state.get("article_title", "")
        retry_count = state.get("retry_count", 0)
        review_result = state.get("review_result", {})

        # 检查是否为重写（审核不通过的情况）
        if retry_count > 0 and review_result:
            feedback = review_result.get("feedback", "")
            prompt = self._build_rewrite_prompt(
                title=article_title,
                topic=topic,
                outline=outline,
                target_audience=target_audience,
                original_content=state.get("content", ""),
                feedback=feedback,
                retry_count=retry_count,
            )
        else:
            prompt = self._build_new_prompt(
                title=article_title,
                topic=topic,
                outline=outline,
                target_audience=target_audience,
            )

        # 调用 LLM
        response = self.invoke(prompt)

        # 提取文章内容
        content = self._extract_content(response)

        return {
            "content": content,
            "article_title": article_title or topic,
            "summary": state.get("summary", ""),
            # metadata 走 schema 级归并（Annotated merge_metadata），只需返回新增字段
            "metadata": {
                "content_created_at": datetime.now().isoformat(),
                "retry_count": retry_count,
            },
        }

    def _build_new_prompt(
        self,
        title: str,
        topic: str,
        outline: str,
        target_audience: str,
    ) -> str:
        """构建新文章创作 prompt"""
        return f"""请根据以下选题信息撰写一篇微信公众号文章。

## 选题信息
- 标题：{title}
- 主题：{topic}
- 大纲：
{outline}
- 目标受众：{target_audience}

## 写作要求
- 字数：{self.min_words}-{self.max_words} 字
- 在需要配图的位置插入 [IMAGE: 图片描述] 标记
- 每篇文章建议 2-4 处插图标记

请直接输出 Markdown 格式的文章内容。"""

    def _build_rewrite_prompt(
        self,
        title: str,
        topic: str,
        outline: str,
        target_audience: str,
        original_content: str,
        feedback: str,
        retry_count: int,
    ) -> str:
        """构建重写 prompt"""
        return f"""请根据审核反馈修改以下文章。这是第 {retry_count} 次修改。

## 文章信息
- 标题：{title}
- 主题：{topic}
- 目标受众：{target_audience}

## 审核反馈
{feedback}

## 原文内容
{original_content}

## 修改要求
1. 根据审核反馈进行针对性修改
2. 保持文章的整体结构和风格
3. 字数：{self.min_words}-{self.max_words} 字
4. 保留 [IMAGE: 图片描述] 插图标记

请输出修改后的完整 Markdown 文章内容。"""

    def _extract_content(self, response: str) -> str:
        """从 LLM 响应中提取文章内容"""
        content = response.strip()

        # 去除可能的代码块标记
        if content.startswith("```markdown"):
            content = content[len("```markdown"):].strip()
        if content.startswith("```"):
            content = content[3:].strip()
        if content.endswith("```"):
            content = content[:-3].strip()

        return content
