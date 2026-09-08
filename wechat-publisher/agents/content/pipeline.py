"""内容创作七阶段流水线 - content_writer 与 compose 共用的统一编排入口

设计要点：
1. 每阶段一次独立 LLM 调用（共 7 次），阶段解耦，可单独调试与重生成。
2. 任一阶段失败**不抛异常**：记入 degraded 并回落 fallback 值，
   保证最差情况等价于改造前的单次成文行为。
3. 正文阶段（阶段 4）失败时调用 content_fallback（由 content_writer 提供
   _build_new_prompt 兜底成文），使后续摘要/标签/配图仍基于真实正文产出。
4. 新结构化产出不进 LangGraph 状态顶层键，由调用方写入 metadata
   （schema 未声明键的更新会被 langgraph 丢弃，见 LANGGRAPH_1X_ADAPTATION.md）。
"""

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from agents.content.topic_analyzer import TopicAnalyzerAgent
from agents.content.outline_generator import OutlineGeneratorAgent
from agents.content.title_generator import TitleGeneratorAgent
from agents.content.content_drafter import ContentDrafterAgent
from agents.content.summary_extractor import SummaryExtractorAgent
from agents.content.tag_extractor import TagExtractorAgent
from agents.content.image_prompt_generator import (
    IMAGE_MARKER_RE,
    ImagePromptGeneratorAgent,
)

logger = logging.getLogger(__name__)

# 模型键缺失时的回落目标（与正文写作同源，保证新子 Agent 一定能起来）
FALLBACK_MODEL_KEY = "content_writer"

# 七阶段顺序（进度回调与降级记录都以此为准）
STAGE_NAMES = (
    "topic_analyzer",
    "outline_generator",
    "title_generator",
    "content_drafter",
    "summary_extractor",
    "tag_extractor",
    "image_prompt_generator",
)

# 可并行的阶段组：同以「正文」为唯一输入，彼此无数据依赖，可并发省一轮往返。
# 图像提示词阶段虽同样只读正文，但它产出的插图描述要回填进正文，
# 与摘要/标签并行会让「回填前/回填后」的正文语义产生歧义，故不并入本组。
PARALLEL_STAGES = ("summary_extractor", "tag_extractor")


class FallbackLLMRouter:
    """包装 LLMRouter：新子 Agent 的模型键缺失时回落到 FALLBACK_MODEL_KEY 的模型

    models/llm_router.py 的 get_llm 在缺键时直接 raise ValueError，而
    config/settings.yaml 已被 .gitignore 忽略（改 settings.yaml.example
    不会同步到本地），本地未补键会让整条创作链路起不来。这里做一次转发兜底，
    既不改 llm_router，也保留在 settings.yaml 中单独调温的能力。
    """

    def __init__(self, router, fallback_name: str = FALLBACK_MODEL_KEY):
        self._router = router
        self._fallback_name = fallback_name
        self._warned: set[str] = set()

    @property
    def config(self) -> dict:
        """BaseAgent.invoke 记录 api_event 时会读取 router.config"""
        return self._router.config

    def get_llm(self, agent_name: str):
        try:
            return self._router.get_llm(agent_name)
        except ValueError:
            if agent_name not in self._warned:
                self._warned.add(agent_name)
                logger.warning(
                    f"models 配置缺少 '{agent_name}'，回落使用 '{self._fallback_name}' 的模型；"
                    f"如需为该阶段单独调温，请在 config/settings.yaml 的 models 下补上该键"
                )
            return self._router.get_llm(self._fallback_name)


def build_content_agents(llm_router, config: dict) -> dict:
    """构造 7 个创作子 Agent（带模型键兜底）"""
    router = FallbackLLMRouter(llm_router)
    return {
        "topic_analyzer": TopicAnalyzerAgent(router, config),
        "outline_generator": OutlineGeneratorAgent(router, config),
        "title_generator": TitleGeneratorAgent(router, config),
        "content_drafter": ContentDrafterAgent(router, config),
        "summary_extractor": SummaryExtractorAgent(router, config),
        "tag_extractor": TagExtractorAgent(router, config),
        "image_prompt_generator": ImagePromptGeneratorAgent(router, config),
    }


def apply_inline_prompts(content: str, inline_prompts: list[str]) -> tuple[str, bool]:
    """用精炼后的插图描述回填正文的 [IMAGE: ...] 标记

    仅在数量完全对齐时替换（否则无法保证一一对应）；不对齐时原样返回，
    由调用方记 warning。下游 image_generator 无需改动即可受益于更优的描述。

    Returns:
        (正文, 是否发生回填)
    """
    prompts = [str(p).strip() for p in (inline_prompts or []) if str(p or "").strip()]
    if not prompts:
        return content, False

    marker_count = len(IMAGE_MARKER_RE.findall(content or ""))
    if marker_count == 0 or marker_count != len(prompts):
        logger.warning(
            f"插图描述数量（{len(prompts)}）与正文 [IMAGE:] 标记数量（{marker_count}）"
            f"不一致，保留正文原有标记"
        )
        return content, False

    iterator = iter(prompts)
    new_content = IMAGE_MARKER_RE.sub(lambda m: f"[IMAGE: {next(iterator)}]", content)
    return new_content, True


def summary_to_text(summary: dict) -> str:
    """把结构化摘要压成 ArticleState.summary 需要的字符串

    ArticleState.summary 是 str 通道，而摘要阶段产出的是结构化字典，
    这里取一句话摘要；缺失时依次退化为核心要点拼接、精华段。
    """
    if not summary:
        return ""
    one_liner = str(summary.get("one_liner") or "").strip()
    if one_liner:
        return one_liner
    key_points = [str(p).strip() for p in (summary.get("key_points") or []) if str(p).strip()]
    if key_points:
        return "；".join(key_points)
    return str(summary.get("gist") or "").strip()


def run_content_pipeline(
    *,
    llm_router,
    config: dict,
    topic: str,
    audience_hint: str = "",
    style: str = "",
    min_words: int = 1500,
    max_words: int = 3000,
    image_count: int = 3,
    title_pinned: str = "",
    fallback: dict | None = None,
    content_fallback: Callable | None = None,
    on_stage: Callable | None = None,
    parallel: bool = True,
) -> dict:
    """串行执行 7 个创作阶段

    Args:
        llm_router: LLMRouter 实例（内部包一层模型键兜底）
        config: 全量配置
        topic: 主题（人工从选题候选里指定的选题，或 topic_planner 自动选出的选题）
        audience_hint: 受众提示（缺省回落 fallback["target_audience"]）
        style: 写作风格（缺省回落子 Agent 自身配置）
        min_words / max_words: 正文字数范围
        image_count: 期望插图数量
        title_pinned: 非空表示用户显式指定标题，阶段 3 只出候选、不覆盖
        fallback: 前序值兜底 {"outline","article_title","summary","cover_prompt","target_audience"}
        content_fallback: 正文阶段失败时的兜底成文回调
            (title, outline, target_audience) -> str
        on_stage: 可选进度回调 on_stage(stage_name, index)
        parallel: True = 并行执行 PARALLEL_STAGES（摘要 ‖ 标签），省一轮 LLM 往返；
            False = 全部串行（调试/需要稳定阶段顺序时用）

    Returns:
        dict: 见模块文档中列出的键；degraded 记录发生降级的阶段名。
        约定：本函数不抛异常，任何阶段失败都降级处理。
    """
    fb = fallback or {}
    degraded: list[str] = []
    timings: dict[str, int] = {}
    agents = build_content_agents(llm_router, config)
    index = 0
    # 并行阶段共享这些可变状态，统一加锁
    lock = threading.Lock()

    def _stage(name: str, fn: Callable):
        """执行单个阶段：进度回调 + 计时 + 异常降级（失败返回 None）

        线程安全：index / degraded / timings 的写入全部加锁，
        因此可被并行组（摘要 ‖ 标签）在多线程下复用。
        """
        nonlocal index
        with lock:
            stage_index = index
            index += 1

        if on_stage:
            try:
                on_stage(name, stage_index)
            except Exception as e:
                logger.warning(f"进度回调失败 [{name}]: {e}")

        start = time.time()
        try:
            return fn()
        except Exception as e:
            logger.warning(f"[content/{name}] 阶段失败，按降级处理: {e}")
            with lock:
                degraded.append(name)
            return None
        finally:
            elapsed = round((time.time() - start) * 1000)
            with lock:
                timings[name] = elapsed

    def _run_parallel(stage_fns: dict[str, Callable]) -> dict[str, object]:
        """并行执行一组互不依赖的阶段，返回 {阶段名: 结果或 None}

        阶段内部异常已由 _stage 降级为 None 并记录 degraded，不会向外抛出；
        因此 fut.result() 不会因业务异常而炸掉整个流水线。
        """
        names = tuple(stage_fns)
        logger.info(f"[content] 并行执行阶段: {', '.join(names)}")
        with ThreadPoolExecutor(max_workers=len(names)) as pool:
            futures = {name: pool.submit(_stage, name, stage_fns[name]) for name in names}
            return {name: fut.result() for name, fut in futures.items()}

    # ── 阶段 1：主题分析 ────────────────────────────────
    analysis = _stage(
        "topic_analyzer",
        lambda: agents["topic_analyzer"].generate(
            topic=topic, audience_hint=audience_hint, style=style
        ),
    )
    if not analysis:
        analysis = {
            "target_audience": fb.get("target_audience") or audience_hint,
            "content_direction": "",
            "content_goal": "",
            "angle": "",
            "value_prop": "",
            "tone": "",
            "key_points": [],
            "topic": topic,
        }
    target_audience = analysis.get("target_audience") or fb.get("target_audience") or ""

    # ── 阶段 2：大纲生成 ────────────────────────────────
    outline_res = _stage(
        "outline_generator",
        lambda: agents["outline_generator"].generate(topic=topic, analysis=analysis),
    )
    if outline_res and outline_res.get("outline"):
        outline = outline_res["outline"]
        sections = outline_res.get("sections") or []
    else:
        outline = fb.get("outline") or ""
        sections = []

    # ── 阶段 3：标题创作 ────────────────────────────────
    title_res = _stage(
        "title_generator",
        lambda: agents["title_generator"].generate(
            topic=topic, outline=outline, analysis=analysis
        ),
    )
    title_res = title_res or {}
    candidates = title_res.get("candidates") or []
    recommended = title_res.get("recommended") or ""
    if title_pinned:
        # 用户显式指定标题：候选照常产出留档，但绝不覆盖用户意图
        title = title_pinned
    else:
        title = recommended or fb.get("article_title") or topic

    # ── 阶段 4：内容生成 ────────────────────────────────
    content = _stage(
        "content_drafter",
        lambda: agents["content_drafter"].generate(
            topic=topic,
            outline=outline,
            title=title,
            analysis=analysis,
            min_words=min_words,
            max_words=max_words,
            image_count=image_count,
            style=style,
        ),
    ) or ""

    if not content.strip():
        if content_fallback is not None:
            try:
                content = content_fallback(
                    title=title,
                    outline=outline,
                    target_audience=target_audience,
                    topic=topic,
                ) or ""
                if content.strip():
                    logger.warning("正文生成阶段失败，已使用调用方兜底单次成文")
            except Exception as e:
                logger.error(f"兜底成文也失败: {e}")
                content = ""
        if not content.strip():
            if "content_drafter" not in degraded:
                degraded.append("content_drafter")
            logger.error("正文生成为空，后续摘要/标签/配图将基于空正文降级产出")

    # ── 阶段 5+6：摘要提炼 ‖ 标签提取 ───────────────────
    # 两者都以「正文」为唯一输入、彼此无依赖，并行可省一轮 LLM 往返
    # （首次成文由 7 轮降为 6 轮）。parallel=False 时退回串行。
    parallel_fns = {
        "summary_extractor": lambda: agents["summary_extractor"].generate(
            title=title, content=content
        ),
        "tag_extractor": lambda: agents["tag_extractor"].generate(
            title=title, content=content, analysis=analysis
        ),
    }
    if parallel:
        parallel_results = _run_parallel(parallel_fns)
    else:
        parallel_results = {name: _stage(name, fn) for name, fn in parallel_fns.items()}

    summary_res = parallel_results["summary_extractor"]
    summary = summary_res or {
        "one_liner": fb.get("summary") or "",
        "key_points": [],
        "gist": "",
    }

    tag_res = parallel_results["tag_extractor"]
    tags = (tag_res or {}).get("tags") or []

    # ── 阶段 7：图像提示词生成 ──────────────────────────
    image_res = _stage(
        "image_prompt_generator",
        lambda: agents["image_prompt_generator"].generate(
            title=title, content=content, outline=outline, image_count=image_count
        ),
    )
    if image_res:
        cover_prompt = image_res.get("cover_prompt") or ""
        inline_prompts = image_res.get("inline_prompts") or []
    else:
        cover_prompt = fb.get("cover_prompt") or ""
        inline_prompts = []

    # 插图描述回填正文标记（数量对齐才替换）
    if content.strip() and inline_prompts:
        content, applied = apply_inline_prompts(content, inline_prompts)
        if applied:
            logger.info(f"已用精炼后的插图描述回填正文 {len(inline_prompts)} 处 [IMAGE:] 标记")

    if degraded:
        logger.warning(f"创作流水线发生降级的阶段: {', '.join(degraded)}")

    return {
        "topic_analysis": analysis,
        "target_audience": target_audience,
        "outline": outline,
        "outline_sections": sections,
        "title": title,
        "title_candidates": candidates,
        "content": content,
        "summary": summary,
        "summary_text": summary_to_text(summary),
        "tags": tags,
        "cover_prompt": cover_prompt,
        "inline_prompts": inline_prompts,
        "degraded": degraded,
        "stage_timings_ms": timings,
    }
