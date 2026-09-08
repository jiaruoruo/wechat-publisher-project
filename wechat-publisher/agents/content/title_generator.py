"""创作阶段 3/7 - 标题创作

产出多个风格各异的候选标题（疑问/数字/对比/悬念/痛点），并给出推荐的一条。
用户显式指定标题时，本阶段仍照常执行，由调用方决定只取候选、不覆盖标题。

JSON 契约，解析失败由 _coerce 兜底，绝不抛异常。
"""

import logging

from agents.base import BaseAgent
from graph.state import ArticleState
from tools.json_utils import safe_extract_json

logger = logging.getLogger(__name__)

_MAX_CANDIDATES = 5
_MAX_TITLE_LEN = 40


class TitleGeneratorAgent(BaseAgent):
    """标题创作：生成吸引眼球且符合主题的候选标题"""

    agent_name = "title_generator"
    prompt_file = "title_generator.yaml"

    def run(self, state: ArticleState) -> dict:
        """LangGraph 节点入口（本套件主要由 pipeline 以 generate() 调用）"""
        result = self.generate(
            topic=state.get("topic", ""),
            outline=state.get("outline", ""),
            analysis=state.get("topic_analysis") or {},
        )
        return {"metadata": {"title_candidates": result["candidates"]}}

    def generate(self, *, topic: str, outline: str = "", analysis: dict | None = None) -> dict:
        """生成候选标题，返回 {candidates, recommended, reason}"""
        analysis = analysis or {}
        prompt = f"""请为下面这篇公众号文章产出候选标题。

## 主题
{topic}

## 目标受众
{analysis.get("target_audience") or "（未给出）"}

## 核心价值
{analysis.get("value_prop") or "（未给出）"}

## 文章大纲
{outline or "（未给出）"}

请严格按 JSON 格式输出候选标题。"""

        raw = self.invoke(prompt, json_mode=True)
        data = safe_extract_json(raw, default={})
        return self._coerce(data)

    def _coerce(self, data: dict) -> dict:
        """规范化：过滤空标题与超长标题，recommended 必须落在候选内"""
        candidates: list[dict] = []
        seen: set[str] = set()
        raw_candidates = data.get("candidates")
        if isinstance(raw_candidates, list):
            for item in raw_candidates:
                if not isinstance(item, dict):
                    continue
                title = str(item.get("title") or "").strip()
                if not title or title in seen or len(title) > _MAX_TITLE_LEN:
                    continue
                seen.add(title)
                candidates.append({
                    "title": title,
                    "style": str(item.get("style") or "").strip(),
                    "hook": str(item.get("hook") or "").strip(),
                    "appeal": str(item.get("appeal") or "").strip(),
                })
                if len(candidates) >= _MAX_CANDIDATES:
                    break

        recommended = str(data.get("recommended") or "").strip()
        if recommended and recommended not in seen:
            # 推荐项不在候选里（模型自相矛盾）时补进候选，保证二者一致
            candidates.insert(0, {"title": recommended, "style": "", "hook": "", "appeal": ""})
            seen.add(recommended)
        if not recommended and candidates:
            recommended = candidates[0]["title"]

        return {
            "candidates": candidates,
            "recommended": recommended,
            "reason": str(data.get("reason") or "").strip(),
        }
