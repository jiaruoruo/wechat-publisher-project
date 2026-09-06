"""去AI味改写 Agent - 对正文做去套话/去排比堆砌的改写，提升人工写作质感

定位：content_writer 与 image_generator 之间的独立文本改写节点。
职责：仅做语言质感优化，不改动事实/数据/结构；保留 [IMAGE: ...] 标记与标题层级。

可靠性约定（与 content_writer 一致）：
- LLM 失败 / 输出异常 / 保全校验不过 → 记 warning + 降级标记，保留原文，绝不阻断出稿；
- 最差情况等价于「没有去AI味节点」，流程仍能产出文章。
"""

import logging
import re

from agents.base import BaseAgent
from graph.state import ArticleState
from agents.content.pipeline import FallbackLLMRouter, summary_to_text

logger = logging.getLogger(__name__)

# [IMAGE: ...] 标记是正文的文内配图注入锚点（image_generator 直接正则解析 content），
# 数量减少或格式破坏会导致配图丢失。改写后必须保全（位置与数量）。
_IMAGE_MARKER_RE = re.compile(r"\[IMAGE:\s*.*?\]")
# 改写后正文过短视为改写失败（避免 LLM 把长文压缩成残篇）
_MIN_LENGTH_RATIO = 0.6


class DeaiRewriterAgent(BaseAgent):
    """去AI味改写 Agent：在不改动事实的前提下，把正文改得更像人类手写"""

    agent_name = "deai_rewriter"
    prompt_file = "deai.yaml"

    def __init__(self, llm_router, config: dict):
        super().__init__(llm_router, config)
        self.enabled = bool(config.get("content", {}).get("deai_enabled", True))

    def run(self, state: ArticleState) -> dict:
        """去AI味改写，返回需合并进全局状态的字段

        返回：
          - 关闭/空正文：{}（透传，行为等同现状）
          - 改写成功：{"content", "summary", "metadata": {"deai_applied": True, ...}}
          - 降级（改写失败/校验不过）：{"metadata": {"deai_degraded": [原因]}}
            （不动 content，保证不阻断出稿）
        """
        if not self.enabled:
            logger.info("[deai] 已关闭（content.deai_enabled=false），透传原文")
            return {}

        content = str(state.get("content", "") or "")
        if not content.strip():
            return {}

        article_title = str(state.get("article_title", "") or "")

        # 1) 改写
        try:
            rewritten = self._rewrite(content, article_title)
        except Exception as e:
            logger.warning(f"[deai] 改写调用失败，保留原文: {e}")
            return {"metadata": {"deai_degraded": ["llm_error"]}}

        # 2) 结构保全校验：标记数量不减少、正文长度不缩水
        ok, reason = self._validate(content, rewritten)
        if not ok:
            logger.warning(
                f"[deai] 改写未通过保全校验（{reason}），保留原文；"
                f"原标记数={len(_IMAGE_MARKER_RE.findall(content))} "
                f"新标记数={len(_IMAGE_MARKER_RE.findall(rewritten))}"
            )
            return {"metadata": {"deai_degraded": [reason]}}

        # 3) 刷新摘要与标签（基于新正文；失败静默保留旧值）
        new_metadata: dict = {"deai_applied": True}
        self._refresh_summary_and_tags(rewritten, article_title, new_metadata)

        return {
            "content": rewritten,
            "summary": new_metadata.pop("summary_text", state.get("summary", "")),
            "metadata": new_metadata,
        }

    def _rewrite(self, content: str, article_title: str) -> str:
        prompt = f"""请对下面这篇成稿做「去AI味」润色，使其读起来更像资深人类作者手写。

## 文章标题
{article_title}

## 正文
{content}

## 改写要求
1. 去除 AI 写作常见套话：排比堆砌、空洞升华结尾、"首先/其次/最后"机械过渡、
   过度对仗、高频"可以说/值得一提的是/综上所述"、无信息量的总结段。
2. 保留全部事实、数据、引述与专业判断，不得编造或改写数字。
3. 保留文章结构与标题层级（## / ### 等 Markdown 标题）。
4. 必须原样保留文中所有 [IMAGE: 描述] 插图标记，位置与数量均不得改变。
5. 语言更口语化、有节奏感、有观点，像一个有想法的专栏作者。
6. 直接输出润色后的完整 Markdown 正文，不要任何解释或前后缀。"""

        return self._extract_content(self.invoke(prompt))

    def _extract_content(self, response: str) -> str:
        """从 LLM 响应提取正文（剥离可能的 ```markdown 围栏）"""
        content = (response or "").strip()
        if content.startswith("```markdown"):
            content = content[len("```markdown"):].strip()
        if content.startswith("```"):
            content = content[3:].strip()
        if content.endswith("```"):
            content = content[:-3].strip()
        return content

    def _validate(self, original: str, rewritten: str) -> tuple[bool, str]:
        """结构保全校验：改写结果不得破坏配图锚点与篇幅

        - [IMAGE: ...] 标记数量不得减少（减少 = 配图丢失）
        - 正文长度不得低于原文的 60%（防止被压缩成残篇）
        """
        if not rewritten.strip():
            return False, "empty_output"
        orig_markers = _IMAGE_MARKER_RE.findall(original)
        new_markers = _IMAGE_MARKER_RE.findall(rewritten)
        if len(new_markers) < len(orig_markers):
            return False, f"image_markers_lost:{len(orig_markers)}->{len(new_markers)}"
        if len(rewritten) < _MIN_LENGTH_RATIO * len(original):
            return (
                False,
                f"too_short:{len(rewritten)}<{_MIN_LENGTH_RATIO:.0%}*{len(original)}",
            )
        return True, ""

    def _refresh_summary_and_tags(
        self, content: str, article_title: str, new_metadata: dict
    ) -> None:
        """基于改写后的正文刷新摘要与标签（复用 content_writer 同款 Extractor）

        失败静默保留旧值 —— 去AI味的核心是正文质感，摘要/标签刷新为非关键步骤。
        """
        # 本地 import 避免模块级循环依赖
        from agents.content.summary_extractor import SummaryExtractorAgent
        from agents.content.tag_extractor import TagExtractorAgent

        try:
            router = FallbackLLMRouter(self.llm_router)
            summary = SummaryExtractorAgent(router, self.config).generate(
                title=article_title, content=content
            )
            if summary:
                new_metadata["summary_detail"] = summary
                summary_text = summary_to_text(summary)
                if summary_text:
                    new_metadata["summary_text"] = summary_text

            tags = (
                TagExtractorAgent(router, self.config).generate(
                    title=article_title, content=content, analysis={}
                )
                or {}
            ).get("tags") or []
            if tags:
                new_metadata["tags"] = tags
        except Exception as e:
            logger.warning(f"[deai] 刷新摘要/标签失败，保留原有值: {e}")
