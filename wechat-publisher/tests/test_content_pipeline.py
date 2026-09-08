"""内容创作七阶段流水线单测

覆盖：七阶段调用顺序与次数、结构化产出解析、title_pinned 保护、
分级降级、正文兜底、插图描述回填、模型键回落到 content_writer。

全部使用桩件，不触发任何真实 API 调用。
"""

import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.content import pipeline as pipeline_mod
from agents.content.pipeline import (
    FallbackLLMRouter,
    apply_inline_prompts,
    run_content_pipeline,
    summary_to_text,
)

# ── 各阶段的桩件返回 ────────────────────────────────────

ANALYSIS_JSON = """{"target_audience": "AI 从业者", "content_direction": "讲清推理成本",
 "content_goal": "理解成本构成", "angle": "从账单切入", "value_prop": "省时间",
 "tone": "冷静克制", "key_points": ["要点1", "要点2"]}"""

OUTLINE_JSON = """{"outline": "## 开头\\n- 钩子\\n\\n## 主体\\n- 论点",
 "sections": [{"heading": "开头", "points": ["钩子"]}, {"heading": "主体", "points": ["论点"]}]}"""

TITLE_JSON = """{"candidates": [{"title": "标题A", "style": "疑问式", "hook": "h", "appeal": "a"},
 {"title": "标题B", "style": "数字式", "hook": "h2", "appeal": "a2"}],
 "recommended": "标题A", "reason": "更抓人"}"""

CONTENT_MD = (
    "# 标题A\n\n开头段落。\n\n[IMAGE: 原始插图描述一]\n\n"
    "## 主体\n\n正文内容。\n\n[IMAGE: 原始插图描述二]\n"
)

SUMMARY_JSON = """{"one_liner": "一句话摘要", "key_points": ["要点1", "要点2"], "gist": "精华段"}"""

TAGS_JSON = """{"tags": ["大模型", "推理成本", "算力", "推理成本", "x"]}"""

IMAGE_JSON = """{"cover_prompt": "封面描述", "inline_prompts": ["精炼插图一", "精炼插图二"]}"""

RESPONSES = {
    "topic_analyzer": ANALYSIS_JSON,
    "outline_generator": OUTLINE_JSON,
    "title_generator": TITLE_JSON,
    "content_drafter": CONTENT_MD,
    "summary_extractor": SUMMARY_JSON,
    "tag_extractor": TAGS_JSON,
    "image_prompt_generator": IMAGE_JSON,
}


# ── 桩件 ────────────────────────────────────────────────


class StubLLM:
    """按 Agent 名返回脚本化响应；error 非空时模拟调用异常"""

    def __init__(self, response: str = "", error: Exception | None = None):
        self.calls = 0
        self.prompts: list[str] = []
        self.response = response
        self.error = error

    def bind(self, **kwargs):
        return self

    def invoke(self, messages, **kwargs):
        self.calls += 1
        self.prompts.append(messages[-1].content if messages else "")
        if self.error:
            raise self.error
        return type("R", (), {"content": self.response})()


class StubRouter:
    """为每个 Agent 名分配独立桩件，便于分阶段断言调用次数

    barrier / barrier_names 可选：在构造阶段就为指定阶段注入 BarrierLLM，
    用于验证这些阶段确为并发执行（agent 在 __init__ 时即缓存 self.llm，
    事后覆盖 router 缓存无效，故必须在构造期注入）。
    """

    def __init__(
        self,
        responses: dict | None = None,
        errors: dict | None = None,
        barrier: "threading.Barrier | None" = None,
        barrier_names: "tuple[str, ...] | None" = None,
    ):
        self.config = {}
        self._responses = responses or {}
        self._errors = errors or {}
        self._barrier = barrier
        self._barrier_names = set(barrier_names or [])
        self._llms: dict[str, StubLLM] = {}

    def get_llm(self, agent_name):
        if agent_name not in self._llms:
            response = self._responses.get(agent_name, "")
            error = self._errors.get(agent_name)
            llm = StubLLM(response, error)
            if self._barrier is not None and agent_name in self._barrier_names:
                llm = BarrierLLM(self._barrier, response)
            self._llms[agent_name] = llm
        return self._llms[agent_name]

    def llm_for(self, agent_name: str) -> StubLLM:
        return self.get_llm(agent_name)


class BarrierLLM(StubLLM):
    """并发探针：调用时先等栅栏

    只有两个阶段的调用真的同时发生，栅栏才会放行；若退化成串行，
    先到者等不到同伴会超时抛错，进而被记为降级 —— 测试据此反证并发。
    """

    def __init__(self, barrier: threading.Barrier, response: str = ""):
        super().__init__(response)
        self.barrier = barrier

    def invoke(self, messages, **kwargs):
        self.barrier.wait(timeout=5)
        return super().invoke(messages, **kwargs)


class RaisingRouter:
    """模拟真实 LLMRouter：缺模型键时 raise ValueError"""

    def __init__(self):
        self.config = {"models": {"content_writer": {"provider": "p", "model": "m"}}}
        self.asked: list[str] = []

    def get_llm(self, agent_name):
        self.asked.append(agent_name)
        if agent_name != "content_writer":
            raise ValueError(f"未找到 Agent '{agent_name}' 的模型配置")
        return StubLLM("ok")


def run(**overrides):
    """以默认脚本化响应跑一次流水线，kwargs 覆盖默认入参"""
    params = dict(
        llm_router=StubRouter(RESPONSES),
        config={"content": {"style": "测试风格"}},
        topic="测试主题",
    )
    params.update(overrides)
    return run_content_pipeline(**params)


# ── 用例 ────────────────────────────────────────────────


class TestStageOrder(unittest.TestCase):
    def test_seven_stages_run_in_order_when_serial(self):
        # 串行模式用于需要稳定阶段顺序的场景（调试/排查）
        seen: list[str] = []
        run(on_stage=lambda name, idx: seen.append(name), parallel=False)

        self.assertEqual(seen, list(pipeline_mod.STAGE_NAMES))

    def test_parallel_mode_runs_all_seven_stages(self):
        seen: list[str] = []
        run(on_stage=lambda name, idx: seen.append(name))

        self.assertEqual(len(seen), 7)
        self.assertEqual(sorted(seen), sorted(pipeline_mod.STAGE_NAMES))
        # 前四个串行阶段顺序确定；图像阶段在并行组之后，必定最后
        self.assertEqual(seen[:4], list(pipeline_mod.STAGE_NAMES[:4]))
        self.assertEqual(seen[-1], "image_prompt_generator")

    def test_each_agent_called_exactly_once(self):
        router = StubRouter(RESPONSES)
        run(llm_router=router)

        for name in pipeline_mod.STAGE_NAMES:
            self.assertEqual(router.llm_for(name).calls, 1, f"{name} 应只调用一次")


class TestParallelStages(unittest.TestCase):
    def test_parallel_group_is_summary_and_tag(self):
        self.assertEqual(
            tuple(pipeline_mod.PARALLEL_STAGES), ("summary_extractor", "tag_extractor")
        )

    def test_summary_and_tag_run_concurrently(self):
        """栅栏探针反证并发：若退化成串行，先到者等不到同伴会超时并记为降级"""
        barrier = threading.Barrier(len(pipeline_mod.PARALLEL_STAGES))
        router = StubRouter(
            RESPONSES,
            barrier=barrier,
            barrier_names=pipeline_mod.PARALLEL_STAGES,
        )

        result = run(llm_router=router)

        self.assertEqual(result["degraded"], [])
        self.assertEqual(result["summary"]["one_liner"], "一句话摘要")
        self.assertEqual(result["tags"], ["大模型", "推理成本", "算力"])


class TestStructuredOutputs(unittest.TestCase):
    def test_parses_all_outputs(self):
        result = run()

        self.assertEqual(result["topic_analysis"]["target_audience"], "AI 从业者")
        self.assertIn("## 开头", result["outline"])
        self.assertEqual(len(result["outline_sections"]), 2)
        self.assertEqual(result["title"], "标题A")
        self.assertEqual(len(result["title_candidates"]), 2)
        # 正文经 _extract_content 处理过（去代码块围栏 + strip），故期望值同样 strip
        expected = (
            CONTENT_MD.strip()
            .replace("原始插图描述一", "精炼插图一")
            .replace("原始插图描述二", "精炼插图二")
        )
        self.assertEqual(result["content"], expected)
        self.assertEqual(result["summary"]["one_liner"], "一句话摘要")
        self.assertEqual(result["summary_text"], "一句话摘要")
        self.assertEqual(result["cover_prompt"], "封面描述")
        self.assertEqual(result["inline_prompts"], ["精炼插图一", "精炼插图二"])
        self.assertEqual(result["degraded"], [])

    def test_tags_are_normalized(self):
        # 去重（"推理成本" 出现两次）、限长（"x" 被过滤）
        self.assertEqual(run()["tags"], ["大模型", "推理成本", "算力"])


class TestTitlePinned(unittest.TestCase):
    def test_pinned_title_not_overwritten(self):
        result = run(title_pinned="用户指定标题")

        self.assertEqual(result["title"], "用户指定标题")
        # 候选照常产出，供留档参考
        self.assertEqual(len(result["title_candidates"]), 2)

    def test_unpinned_uses_recommended(self):
        router = StubRouter(RESPONSES)
        result = run(llm_router=router, fallback={"article_title": "前序标题"})

        self.assertEqual(result["title"], "标题A")


class TestDegradation(unittest.TestCase):
    def test_analysis_failure_falls_back_to_previous_values(self):
        router = StubRouter(RESPONSES, errors={"topic_analyzer": RuntimeError("boom")})
        result = run(llm_router=router, fallback={"target_audience": "前序受众"})

        self.assertIn("topic_analyzer", result["degraded"])
        # 主题分析降级后，受众回落前序值而非抛异常
        self.assertEqual(result["target_audience"], "前序受众")

    def test_outline_failure_keeps_previous_outline(self):
        router = StubRouter(RESPONSES, errors={"outline_generator": RuntimeError("boom")})
        result = run(llm_router=router, fallback={"outline": "前序大纲"})

        self.assertIn("outline_generator", result["degraded"])
        self.assertEqual(result["outline"], "前序大纲")

    def test_all_json_invalid_degrades_without_exception(self):
        # 所有 JSON 阶段都返回不可解析文本：整体不得抛异常
        bad = {k: "这不是 JSON" for k in RESPONSES}
        bad["content_drafter"] = CONTENT_MD
        router = StubRouter(bad)
        result = run(llm_router=router, fallback={"article_title": "前序标题"})

        self.assertEqual(result["title"], "前序标题")
        self.assertEqual(result["tags"], [])
        self.assertEqual(result["content"], CONTENT_MD.strip())

    def test_content_drafter_failure_uses_content_fallback(self):
        router = StubRouter(RESPONSES, errors={"content_drafter": RuntimeError("boom")})
        captured = {}

        def fallback_draft(*, title, outline, target_audience, topic):
            captured.update(title=title, outline=outline, topic=topic)
            return "# 兜底成文"

        result = run(llm_router=router, content_fallback=fallback_draft)

        self.assertEqual(result["content"], "# 兜底成文")
        self.assertEqual(captured["title"], "标题A")
        self.assertEqual(captured["topic"], "测试主题")
        # 后续阶段仍基于兜底正文继续执行
        self.assertNotIn("summary_extractor", result["degraded"])

    def test_content_drafter_failure_without_fallback_marks_degraded(self):
        router = StubRouter(RESPONSES, errors={"content_drafter": RuntimeError("boom")})
        result = run(llm_router=router)

        self.assertIn("content_drafter", result["degraded"])
        self.assertEqual(result["content"], "")


class TestInlinePromptBackfill(unittest.TestCase):
    def test_backfills_when_counts_match(self):
        content = "段落\n\n[IMAGE: 原始一]\n\n继续\n\n[IMAGE: 原始二]\n"
        new, applied = apply_inline_prompts(content, ["精炼一", "精炼二"])

        self.assertTrue(applied)
        self.assertIn("[IMAGE: 精炼一]", new)
        self.assertIn("[IMAGE: 精炼二]", new)
        self.assertNotIn("原始一", new)

    def test_keeps_original_when_counts_mismatch(self):
        content = "段落\n\n[IMAGE: 原始一]\n"
        new, applied = apply_inline_prompts(content, ["精炼一", "多出来的一条"])

        self.assertFalse(applied)
        self.assertEqual(new, content)

    def test_no_markers_means_no_backfill(self):
        content = "没有任何插图标记的正文"
        new, applied = apply_inline_prompts(content, ["精炼一"])

        self.assertFalse(applied)
        self.assertEqual(new, content)


class TestSummaryToText(unittest.TestCase):
    def test_prefers_one_liner(self):
        self.assertEqual(
            summary_to_text({"one_liner": "一句话", "key_points": ["a"], "gist": "g"}),
            "一句话",
        )

    def test_falls_back_to_key_points(self):
        self.assertEqual(summary_to_text({"key_points": ["a", "b"]}), "a；b")

    def test_falls_back_to_gist(self):
        self.assertEqual(summary_to_text({"gist": "精华"}), "精华")

    def test_empty_summary(self):
        self.assertEqual(summary_to_text({}), "")


class TestModelKeyFallback(unittest.TestCase):
    def test_missing_model_key_falls_back_to_content_writer(self):
        inner = RaisingRouter()
        router = FallbackLLMRouter(inner)

        llm = router.get_llm("topic_analyzer")

        self.assertIsNotNone(llm)
        self.assertIn("content_writer", inner.asked)

    def test_existing_key_is_used_directly(self):
        inner = RaisingRouter()
        router = FallbackLLMRouter(inner)

        router.get_llm("content_writer")

        self.assertEqual(inner.asked, ["content_writer"])


if __name__ == "__main__":
    unittest.main()
