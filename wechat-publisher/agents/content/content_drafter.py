"""创作阶段 4/7 - 内容生成

基于大纲与标题产出完整 Markdown 正文，并在需配图处插入 [IMAGE: 描述] 标记。
标记正则与 agents/image_generator.py 的 _extract_image_markers 保持一致，
保证产物能被既有配图节点原样解析。

自由文本契约（不走 json_mode，避免 JSON 转义破坏 Markdown）。
"""

import logging

from agents.base import BaseAgent
from graph.state import ArticleState

logger = logging.getLogger(__name__)

# 与 tools/content_writer 一致的默认值：配置缺段时不至于写出 0 字文章
_DEFAULT_MIN_WORDS = 1500
_DEFAULT_MAX_WORDS = 3000
_DEFAULT_IMAGE_COUNT = 3
_DEFAULT_STYLE = "专业但通俗易懂"


class ContentDrafterAgent(BaseAgent):
    """内容生成：基于大纲与标题产出详细正文"""

    agent_name = "content_drafter"
    prompt_file = "content_drafter.yaml"

    def __init__(self, llm_router, config: dict):
        super().__init__(llm_router, config)
        content_config = config.get("content", {})
        length = content_config.get("article_length") or {}
        self.min_words = int(length.get("min") or _DEFAULT_MIN_WORDS)
        self.max_words = int(length.get("max") or _DEFAULT_MAX_WORDS)
        # 用 or 而非 dict.get 默认值：配置写了空串时也要回落，
        # 否则会把一个空的「内容风格要求」段落塞进 prompt。
        self.style = content_config.get("style") or _DEFAULT_STYLE

    def run(self, state: ArticleState) -> dict:
        """LangGraph 节点入口（本套件主要由 pipeline 以 generate() 调用）"""
        content = self.generate(
            topic=state.get("topic", ""),
            outline=state.get("outline", ""),
            title=state.get("article_title", ""),
            analysis=state.get("topic_analysis") or {},
        )
        return {"content": content}

    def generate(
        self,
        *,
        topic: str,
        outline: str,
        title: str,
        analysis: dict | None = None,
        min_words: int | None = None,
        max_words: int | None = None,
        image_count: int | None = None,
        style: str | None = None,
    ) -> str:
        """生成正文，返回 Markdown 字符串"""
        analysis = analysis or {}
        min_words = self.min_words if min_words is None else min_words
        max_words = self.max_words if max_words is None else max_words
        image_count = _DEFAULT_IMAGE_COUNT if image_count is None else image_count
        style = style or self.style

        prompt = f"""请根据以下大纲与标题，写出一篇完整的微信公众号文章。

## 标题
{title}

## 主题
{topic}

## 目标受众
{analysis.get("target_audience") or "（未给出）"}

## 切入角度
{analysis.get("angle") or "（未给出）"}

## 写作目标
{analysis.get("content_goal") or "（未给出）"}

## 内容风格要求
{style}

## 文章大纲（必须严格依照，不得增删章节或重排顺序）
{outline or "（未给出大纲，请自行组织一篇结构清晰的文章）"}

## 写作要求
- 字数：{min_words}-{max_words} 字
- 在需要配图的位置插入 [IMAGE: 图片描述] 标记，共 {image_count} 处，每个标记单独占一行
- 开头三句必须抓住读者注意力，禁止套话开篇

请直接输出 Markdown 格式的文章正文。"""

        response = self.invoke(prompt)
        return self._extract_content(response)

    def _extract_content(self, response: str) -> str:
        """去除模型可能附加的代码块围栏"""
        content = (response or "").strip()

        if content.startswith("```markdown"):
            content = content[len("```markdown"):].strip()
        if content.startswith("```"):
            content = content[3:].strip()
        if content.endswith("```"):
            content = content[:-3].strip()

        return content.strip()
