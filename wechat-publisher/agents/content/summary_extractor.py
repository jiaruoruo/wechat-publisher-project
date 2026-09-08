"""创作阶段 5/7 - 摘要提炼

从成稿中提炼一句话摘要、核心要点清单与精华段落。
用于替换原链路中 topic_planner 留下的 outline[:50] 弱兜底摘要。

JSON 契约，解析失败由 _coerce 兜底，绝不抛异常。
"""

import logging

from agents.base import BaseAgent
from graph.state import ArticleState
from tools.json_utils import safe_extract_json

logger = logging.getLogger(__name__)

_MAX_KEY_POINTS = 5
_MAX_GIST_LEN = 200
# 正文过长时只截取前后段送入，避免超出上下文与浪费 token
_CONTENT_HEAD = 3000
_CONTENT_TAIL = 1000


class SummaryExtractorAgent(BaseAgent):
    """摘要提炼：从长文中提取核心要点与精华摘要"""

    agent_name = "summary_extractor"
    prompt_file = "summary_extractor.yaml"

    def run(self, state: ArticleState) -> dict:
        """LangGraph 节点入口（本套件主要由 pipeline 以 generate() 调用）"""
        result = self.generate(
            title=state.get("article_title", ""),
            content=state.get("content", ""),
        )
        return {"summary": result["one_liner"]}

    def generate(self, *, title: str, content: str) -> dict:
        """提炼摘要，返回 {one_liner, key_points, gist}"""
        prompt = f"""请从下面这篇成稿中提炼摘要。

## 文章标题
{title}

## 正文
{self._truncate(content)}

请严格按 JSON 格式输出摘要。"""

        raw = self.invoke(prompt, json_mode=True)
        data = safe_extract_json(raw, default={})
        return self._coerce(data)

    @staticmethod
    def _truncate(content: str) -> str:
        """超长正文只取开头与结尾，保证开头（论点）与结尾（结论）都在"""
        if len(content) <= _CONTENT_HEAD + _CONTENT_TAIL:
            return content
        return f"{content[:_CONTENT_HEAD]}\n\n……（正文略）……\n\n{content[-_CONTENT_TAIL:]}"

    def _coerce(self, data: dict) -> dict:
        """规范化：要点限条数，精华段限长度"""
        key_points = data.get("key_points")
        if not isinstance(key_points, list):
            key_points = []
        key_points = [str(p).strip() for p in key_points if str(p).strip()][:_MAX_KEY_POINTS]

        return {
            "one_liner": str(data.get("one_liner") or "").strip(),
            "key_points": key_points,
            "gist": str(data.get("gist") or "").strip()[:_MAX_GIST_LEN],
        }
