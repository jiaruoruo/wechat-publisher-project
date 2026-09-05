"""创作阶段 2/7 - 大纲生成

把「主题 + 主题分析」变成一份有主线、层级清晰、可直接照着写的结构框架。
同时产出给人看的 Markdown 文本（outline）与给机器用的结构（sections）。

JSON 契约，解析失败由 _coerce 兜底，绝不抛异常。
"""

import logging

from agents.base import BaseAgent
from graph.state import ArticleState
from tools.json_utils import safe_extract_json

logger = logging.getLogger(__name__)

_MAX_SECTIONS = 8
_MAX_POINTS_PER_SECTION = 6


class OutlineGeneratorAgent(BaseAgent):
    """大纲生成：构建逻辑清晰的内容结构框架"""

    agent_name = "outline_generator"
    prompt_file = "outline_generator.yaml"

    def run(self, state: ArticleState) -> dict:
        """LangGraph 节点入口（本套件主要由 pipeline 以 generate() 调用）"""
        result = self.generate(
            topic=state.get("topic", ""),
            analysis=state.get("topic_analysis") or {},
        )
        return {
            "outline": result["outline"],
            "metadata": {"outline_sections": result["sections"]},
        }

    def generate(self, *, topic: str, analysis: dict | None = None) -> dict:
        """生成大纲，返回 {outline, sections}"""
        analysis = analysis or {}
        key_points = analysis.get("key_points") or []
        key_points_text = "\n".join(f"- {p}" for p in key_points) or "（无）"

        prompt = f"""请根据以下主题与分析结论，产出一份逻辑清晰的内容结构框架。

## 主题
{topic}

## 目标受众
{analysis.get("target_audience") or "（未给出）"}

## 内容方向
{analysis.get("content_direction") or "（未给出）"}

## 切入角度
{analysis.get("angle") or "（未给出）"}

## 必覆盖要点（必须全部落进大纲）
{key_points_text}

请严格按 JSON 格式输出大纲。"""

        raw = self.invoke(prompt, json_mode=True)
        data = safe_extract_json(raw, default={})
        return self._coerce(data, topic)

    def _coerce(self, data: dict, topic: str) -> dict:
        """规范化：outline 为字符串；sections 缺失时由 outline 反解，反之亦然"""
        outline = str(data.get("outline") or "").strip()

        sections: list[dict] = []
        raw_sections = data.get("sections")
        if isinstance(raw_sections, list):
            for item in raw_sections[:_MAX_SECTIONS]:
                if not isinstance(item, dict):
                    continue
                heading = str(item.get("heading") or "").strip()
                if not heading:
                    continue
                points = item.get("points")
                if not isinstance(points, list):
                    points = []
                points = [str(p).strip() for p in points if str(p).strip()][:_MAX_POINTS_PER_SECTION]
                sections.append({"heading": heading, "points": points})

        # 两者互补：缺哪个就用哪个补齐，保证下游至少拿到一份可用的大纲
        if not outline and sections:
            outline = self._render_outline(sections)
        if outline and not sections:
            sections = self._parse_outline(outline)

        if not outline:
            # 不在这里造占位大纲：返回空串，由 pipeline 回落到 topic_planner
            # 已产出大纲（改造前行为），避免用无意义的占位符覆盖掉可用的旧值。
            logger.warning("大纲生成结果为空，交由调用方按降级处理")

        return {"outline": outline, "sections": sections}

    @staticmethod
    def _render_outline(sections: list[dict]) -> str:
        """由 sections 渲染 Markdown 大纲文本"""
        lines: list[str] = []
        for sec in sections:
            lines.append(f"## {sec['heading']}")
            lines.extend(f"- {p}" for p in sec.get("points", []))
            lines.append("")
        return "\n".join(lines).strip()

    @staticmethod
    def _parse_outline(outline: str) -> list[dict]:
        """由 Markdown 大纲文本反解 sections（粗解析，仅用于结构化留档）"""
        sections: list[dict] = []
        current: dict | None = None
        for line in outline.splitlines():
            line = line.strip()
            if line.startswith("#"):
                current = {"heading": line.lstrip("#").strip(), "points": []}
                sections.append(current)
            elif line.startswith("-") and current is not None:
                current["points"].append(line.lstrip("-").strip())
        return sections
