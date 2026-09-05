"""创作阶段 1/7 - 主题分析

把「主题」拆解成后续大纲、标题、正文都能直接消费的判断依据：
目标受众、内容方向、写作目标、切入角度、核心价值、语气、必覆盖要点。

JSON 契约，解析失败由 _coerce 兜底，绝不抛异常。
"""

import logging

from agents.base import BaseAgent
from graph.state import ArticleState
from tools.json_utils import safe_extract_json

logger = logging.getLogger(__name__)

_DEFAULT_AUDIENCE = "对该主题感兴趣的读者"
_MAX_KEY_POINTS = 6


class TopicAnalyzerAgent(BaseAgent):
    """主题分析：深度理解主题，明确内容方向与受众"""

    agent_name = "topic_analyzer"
    prompt_file = "topic_analyzer.yaml"

    def run(self, state: ArticleState) -> dict:
        """LangGraph 节点入口（本套件主要由 pipeline 以 generate() 调用）"""
        analysis = self.generate(
            topic=state.get("topic", ""),
            audience_hint=state.get("target_audience", ""),
        )
        return {"metadata": {"topic_analysis": analysis}}

    def generate(self, *, topic: str, audience_hint: str = "", style: str = "") -> dict:
        """分析主题，返回结构化结论"""
        prompt = f"""请深度分析以下主题，为后续的大纲、标题与正文写作提供判断依据。

## 主题
{topic}

## 受众提示（可能为空，仅供参考，不要照抄）
{audience_hint or "（无）"}

## 内容风格要求
{style or "（无特殊要求）"}

请严格按 JSON 格式输出分析结果。"""

        raw = self.invoke(prompt, json_mode=True)
        data = safe_extract_json(raw, default={})
        return self._coerce(data, topic, audience_hint)

    def _coerce(self, data: dict, topic: str, audience_hint: str) -> dict:
        """规范化：缺字段给空串，key_points 限条数，受众为空时回落"""
        key_points = data.get("key_points")
        if not isinstance(key_points, list):
            key_points = []
        key_points = [str(p).strip() for p in key_points if str(p).strip()][:_MAX_KEY_POINTS]

        def _str(key: str) -> str:
            return str(data.get(key) or "").strip()

        return {
            "target_audience": _str("target_audience") or audience_hint or _DEFAULT_AUDIENCE,
            "content_direction": _str("content_direction"),
            "content_goal": _str("content_goal"),
            "angle": _str("angle"),
            "value_prop": _str("value_prop"),
            "tone": _str("tone"),
            "key_points": key_points,
            "topic": topic,
        }
