"""创作阶段 7/7 - 图像提示词生成

为封面与各插图生成具体、有画面感的中文视觉描述。
输出中文即可：下游 image_generator._enhance_prompt 本就负责中文转英文，
避免重复翻译造成的语义漂移。

JSON 契约，解析失败由 _coerce 兜底，绝不抛异常。
"""

import logging
import re

from agents.base import BaseAgent
from graph.state import ArticleState
from tools.json_utils import safe_extract_json

logger = logging.getLogger(__name__)

# 与 agents/image_generator.py 的 _extract_image_markers 保持一致，
# 用于统计正文中实际存在的插图标记数量。
IMAGE_MARKER_RE = re.compile(r"\[IMAGE:\s*(.*?)\]")

_MAX_PROMPT_LEN = 300
_CONTENT_HEAD = 2500


class ImagePromptGeneratorAgent(BaseAgent):
    """图像提示词生成：为配图创作提供详细的视觉描述"""

    agent_name = "image_prompt_generator"
    prompt_file = "image_prompt_generator.yaml"

    def run(self, state: ArticleState) -> dict:
        """LangGraph 节点入口（本套件主要由 pipeline 以 generate() 调用）"""
        result = self.generate(
            title=state.get("article_title", ""),
            content=state.get("content", ""),
            outline=state.get("outline", ""),
        )
        return {
            "cover_prompt": result["cover_prompt"],
            "metadata": {"inline_prompts": result["inline_prompts"]},
        }

    def generate(
        self,
        *,
        title: str,
        content: str,
        outline: str = "",
        image_count: int | None = None,
    ) -> dict:
        """生成配图描述，返回 {cover_prompt, inline_prompts}"""
        marker_count = len(IMAGE_MARKER_RE.findall(content or ""))
        # 以正文中实际存在的标记数为准：这决定下游能生成几张插图
        target_count = marker_count if marker_count else (image_count or 0)

        prompt = f"""请为下面这篇成稿撰写配图的视觉描述。

## 文章标题
{title}

## 文章大纲
{outline or "（未给出）"}

## 正文
{(content or "")[:_CONTENT_HEAD]}

## 要求
- 正文中共有 {marker_count} 处 [IMAGE: ...] 标记，请产出 {target_count} 条插图描述，
  按标记在正文中出现的先后顺序一一对应
- 另产出 1 条封面图描述

请严格按 JSON 格式输出。"""

        raw = self.invoke(prompt, json_mode=True)
        data = safe_extract_json(raw, default={})
        return self._coerce(data, target_count)

    def _coerce(self, data: dict, target_count: int) -> dict:
        """规范化：去空、限长、插图数量截到目标条数"""
        cover_prompt = str(data.get("cover_prompt") or "").strip()[:_MAX_PROMPT_LEN]

        raw_prompts = data.get("inline_prompts")
        if not isinstance(raw_prompts, list):
            raw_prompts = []
        inline_prompts = [
            str(p).strip()[:_MAX_PROMPT_LEN]
            for p in raw_prompts
            if str(p or "").strip()
        ]
        if target_count and len(inline_prompts) > target_count:
            inline_prompts = inline_prompts[:target_count]

        return {"cover_prompt": cover_prompt, "inline_prompts": inline_prompts}
