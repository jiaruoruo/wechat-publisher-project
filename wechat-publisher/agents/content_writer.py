"""内容创作 Agent - 撰写图文正文

流水线定位：选题 agent（topic_planner）产出选题后，本节点**内部**串行执行
7 个创作阶段，把原本「一次 LLM 直接成文」升级为精细化创作：

    1 主题分析 → 2 大纲生成 → 3 标题创作 → 4 内容生成
      → 5 摘要提炼 → 6 标签提取 → 7 图像提示词生成

对外契约不变：在主工作流中仍是**单个** content_writer 节点
（graph/workflow.py 的节点与连线零改动，递归上限不受影响）。

可靠性约定：
- 任一阶段失败 → 记 warning + metadata 打降级标记 → 回落为 topic_planner
  已产出的同名字段（即退化成改造前行为）；
- 正文阶段整体失败 → 用 _build_new_prompt 兜底单次成文。
  因此最差情况等价于改造前，绝不比现在更难产。

覆盖约定：
- 7 阶段产出的 outline / article_title / summary / cover_prompt / target_audience
  覆盖 topic_planner 的同名字段（后者多为弱值，如 summary 仅为 outline[:50]）；
- 但命令行 --title 显式指定的标题不被覆盖（metadata.title_pinned 保护）。
"""

import logging
from datetime import datetime

from agents.base import BaseAgent
from graph.state import ArticleState
from agents.content.pipeline import (
    FallbackLLMRouter,
    run_content_pipeline,
    summary_to_text,
)
from agents.content.summary_extractor import SummaryExtractorAgent
from agents.content.tag_extractor import TagExtractorAgent

logger = logging.getLogger(__name__)

# 期望插图数量：与改造前「每篇 2-4 处」的建议一致，取中值
_DEFAULT_IMAGE_COUNT = 3


class ContentWriterAgent(BaseAgent):
    """内容创作 Agent：在选题确定后串行执行 7 个创作阶段产出文章"""

    agent_name = "content_writer"
    prompt_file = "content_writer.yaml"

    def __init__(self, llm_router, config: dict):
        super().__init__(llm_router, config)
        self.content_config = config.get("content", {})
        self.min_words = self.content_config.get("article_length", {}).get("min", 1500)
        self.max_words = self.content_config.get("article_length", {}).get("max", 3000)
        # 品牌风格（content.style）：与 topic_planner 共用同一配置来源，
        # 使调性同时作用于「选题」与「正文」。
        # 用 `or` 而非 dict.get 默认值：配置里写了空串时也要回落，
        # 否则会把一个空的「内容风格要求」段落塞进 prompt。
        self.style = self.content_config.get("style") or "专业但通俗易懂"
        self.image_count = int(self.content_config.get("image_count") or _DEFAULT_IMAGE_COUNT)

    def run(self, state: ArticleState) -> dict:
        """执行内容创作

        首次成文走 7 阶段精细化流水线；审核不通过的重写沿用既有重写契约，
        不重跑全链路（避免每次重试都放大 7 倍调用）。
        """
        topic = state.get("topic", "")
        outline = state.get("outline", "")
        target_audience = state.get("target_audience", "")
        article_title = state.get("article_title", "")
        retry_count = state.get("retry_count", 0)
        review_result = state.get("review_result", {})

        # 检查是否为重写（审核不通过的情况）
        if retry_count > 0 and review_result:
            return self._run_rewrite(
                state=state,
                topic=topic,
                outline=outline,
                target_audience=target_audience,
                article_title=article_title,
                review_result=review_result,
                retry_count=retry_count,
            )

        return self._run_pipeline(
            state=state,
            topic=topic,
            outline=outline,
            target_audience=target_audience,
            article_title=article_title,
            retry_count=retry_count,
        )

    # ── 首次成文：7 阶段精细化创作 ──────────────────────

    def _run_pipeline(
        self,
        state: ArticleState,
        topic: str,
        outline: str,
        target_audience: str,
        article_title: str,
        retry_count: int,
    ) -> dict:
        """串行执行 7 个创作阶段，并用新值覆盖 topic_planner 的弱值"""
        metadata = state.get("metadata") or {}
        # 用户显式指定标题（cmd_run --title）时不覆盖，只把候选留档参考。
        # 取 metadata 中保存的用户原始输入，而非 state.article_title
        # （后者可能已被 topic_planner 改写，例如被拼成「选题｜标题」）。
        title_pinned = str(metadata.get("title_pinned") or "")

        result = run_content_pipeline(
            llm_router=self.llm_router,
            config=self.config,
            topic=topic,
            audience_hint=target_audience,
            style=self.style,
            min_words=self.min_words,
            max_words=self.max_words,
            image_count=self.image_count,
            title_pinned=title_pinned,
            fallback={
                "outline": outline,
                "article_title": article_title,
                "summary": state.get("summary", ""),
                "cover_prompt": state.get("cover_prompt", ""),
                "target_audience": target_audience,
            },
            content_fallback=self._draft_fallback,
            on_stage=lambda name, idx: logger.info(
                f"[content_writer] 创作阶段 {idx + 1}/7：{name}"
            ),
        )

        degraded = result.get("degraded") or []
        if degraded:
            logger.warning(f"创作流水线降级阶段: {', '.join(degraded)}")

        # metadata 走 schema 级归并（Annotated merge_metadata），只需返回新增字段
        new_metadata = {
            "content_created_at": datetime.now().isoformat(),
            "retry_count": retry_count,
            "topic_analysis": result["topic_analysis"],
            "summary_detail": result["summary"],
            "content_stage_timings_ms": result["stage_timings_ms"],
        }
        # 空值不落库，避免 metadata 被空列表/空串污染
        for key, value in (
            ("outline_sections", result.get("outline_sections")),
            ("title_candidates", result.get("title_candidates")),
            ("tags", result.get("tags")),
            ("inline_prompts", result.get("inline_prompts")),
            ("content_pipeline_degraded", degraded),
        ):
            if value:
                new_metadata[key] = value

        return {
            "content": result["content"],
            "article_title": result["title"] or article_title or topic,
            "outline": result["outline"] or outline,
            "target_audience": result["target_audience"] or target_audience,
            "summary": result["summary_text"] or state.get("summary", ""),
            "cover_prompt": result["cover_prompt"] or state.get("cover_prompt", ""),
            "metadata": new_metadata,
        }

    def _draft_fallback(
        self,
        *,
        title: str,
        outline: str,
        target_audience: str,
        topic: str,
    ) -> str:
        """正文阶段失败时的兜底：复用改造前的单次成文路径（_build_new_prompt）"""
        prompt = self._build_new_prompt(
            title=title,
            topic=topic,
            outline=outline,
            target_audience=target_audience,
        )
        return self._extract_content(self.invoke(prompt))

    # ── 重写路径：沿用既有契约，仅刷新摘要与标签 ──────────

    def _run_rewrite(
        self,
        state: ArticleState,
        topic: str,
        outline: str,
        target_audience: str,
        article_title: str,
        review_result: dict,
        retry_count: int,
    ) -> dict:
        """审核不通过后的重写

        沿用改造前的重写契约（1 次 LLM），不重跑主题分析/大纲/标题/配图；
        仅基于重写后的正文刷新摘要与标签（非关键，失败保留旧值）。
        """
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
        content = self._extract_content(self.invoke(prompt))

        new_metadata = {
            "content_created_at": datetime.now().isoformat(),
            "retry_count": retry_count,
        }

        if content.strip():
            self._refresh_summary_and_tags(content, article_title, new_metadata)

        return {
            "content": content,
            "article_title": article_title or topic,
            "summary": new_metadata.pop("summary_text", state.get("summary", "")),
            "metadata": new_metadata,
        }

    def _refresh_summary_and_tags(
        self, content: str, article_title: str, new_metadata: dict
    ) -> None:
        """基于重写后的正文刷新摘要与标签（失败静默保留旧值）"""
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
            logger.warning(f"重写后刷新摘要/标签失败，保留原有值: {e}")

    # ── prompt 构建（既有契约，测试直接断言，勿改结构）────

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

## 内容风格要求
{self.style}

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

## 内容风格要求
{self.style}

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
