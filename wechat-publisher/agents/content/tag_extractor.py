"""创作阶段 6/7 - 标签提取

从标题与正文中识别关键词标签，做去重、去空、限长、限量归一化。
系统原先没有标签能力，本阶段为净新增。

JSON 契约，解析失败由 _coerce 兜底，绝不抛异常。
"""

import logging

from agents.base import BaseAgent
from graph.state import ArticleState
from tools.json_utils import safe_extract_json

logger = logging.getLogger(__name__)

_MIN_TAGS = 3
_MAX_TAGS = 8
_MIN_TAG_LEN = 2
_MAX_TAG_LEN = 8
_CONTENT_HEAD = 2500


class TagExtractorAgent(BaseAgent):
    """标签提取：自动识别并生成内容关键词标签"""

    agent_name = "tag_extractor"
    prompt_file = "tag_extractor.yaml"

    def run(self, state: ArticleState) -> dict:
        """LangGraph 节点入口（本套件主要由 pipeline 以 generate() 调用）"""
        result = self.generate(
            title=state.get("article_title", ""),
            content=state.get("content", ""),
            analysis=state.get("topic_analysis") or {},
        )
        return {"metadata": {"tags": result["tags"]}}

    def generate(self, *, title: str, content: str, analysis: dict | None = None) -> dict:
        """提取标签，返回 {tags: [...]}"""
        analysis = analysis or {}
        prompt = f"""请从下面这篇成稿中提取关键词标签。

## 文章标题
{title}

## 目标受众
{analysis.get("target_audience") or "（未给出）"}

## 正文
{(content or "")[:_CONTENT_HEAD]}

请严格按 JSON 格式输出标签。"""

        raw = self.invoke(prompt, json_mode=True)
        data = safe_extract_json(raw, default={})
        return self._coerce(data)

    def _coerce(self, data: dict) -> dict:
        """规范化：去空、去重、限长、限量"""
        raw_tags = data.get("tags")
        if not isinstance(raw_tags, list):
            raw_tags = []

        tags: list[str] = []
        seen: set[str] = set()
        for item in raw_tags:
            tag = str(item or "").strip().strip("《》「」『』\"'（）()[]【】")
            if not tag or tag in seen:
                continue
            if not (_MIN_TAG_LEN <= len(tag) <= _MAX_TAG_LEN):
                continue
            seen.add(tag)
            tags.append(tag)
            if len(tags) >= _MAX_TAGS:
                break

        if tags and len(tags) < _MIN_TAGS:
            logger.info(f"标签数量偏少（{len(tags)} 个），已保留")

        return {"tags": tags}
