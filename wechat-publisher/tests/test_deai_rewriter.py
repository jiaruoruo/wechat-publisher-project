"""去AI味节点单测（无真实 LLM）

用桩路由器构造 DeaiRewriterAgent（不触发真实 LLM），直接对确定性逻辑断言：
- 开关关闭透传
- [IMAGE:] 标记丢失 → 降级保留原文
- LLM 异常 → 降级
- 保全校验：标记数量 / 篇幅
- 改写成功后刷新摘要与标签
"""

import unittest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.deai_rewriter import DeaiRewriterAgent, _IMAGE_MARKER_RE


class _StubRouter:
    """最小桩：与 reviewer 测试同款，BaseAgent 仅调用 get_llm（返回 None 即可）"""

    def __init__(self, config):
        self.config = config

    def get_llm(self, name):
        return None


def _make_agent(enabled=True, config=None):
    config = config or {"content": {"deai_enabled": enabled}}
    return DeaiRewriterAgent(_StubRouter(config), config)


class TestToggleAndDegradation(unittest.TestCase):
    def test_disabled_passthrough(self):
        agent = _make_agent(enabled=False)
        result = agent.run({"content": "## 标题\n\n正文 [IMAGE: a]\n"})
        self.assertEqual(result, {})

    def test_empty_content_passthrough(self):
        agent = _make_agent()
        self.assertEqual(agent.run({"content": ""}), {})
        self.assertEqual(agent.run({}), {})

    def test_marker_lost_degrades_keeps_original(self):
        agent = _make_agent()
        agent.invoke = lambda prompt, json_mode=False: "正文没了标记"
        result = agent.run({"content": "## 标题\n\n正文 [IMAGE: a]\n"})
        self.assertNotIn("content", result)
        self.assertIn("deai_degraded", result["metadata"])

    def test_llm_error_degrades(self):
        agent = _make_agent()

        def _boom(prompt, json_mode=False):
            raise RuntimeError("llm down")

        agent.invoke = _boom
        result = agent.run({"content": "## 标题\n\n正文 [IMAGE: a]\n"})
        self.assertNotIn("content", result)
        self.assertEqual(result["metadata"]["deai_degraded"], ["llm_error"])

    def test_rewrite_applies_and_refreshes(self):
        agent = _make_agent()
        original = "## 标题\n\n这是一段 AI 味很重的正文。[IMAGE: 数据图表]\n"
        rewritten = "## 标题\n\n这段正文更自然了。[IMAGE: 数据图表]"
        agent.invoke = lambda prompt, json_mode=False: rewritten
        # 用桩刷新直接写入 summary/tags，验证 run() 会采纳并回写 state
        agent._refresh_summary_and_tags = lambda c, t, m: m.update(
            {"summary_text": "改写后的摘要", "tags": ["标签1", "标签2"]}
        )
        result = agent.run(
            {"content": original, "article_title": "标题", "summary": "旧摘要"}
        )
        self.assertEqual(result["content"], rewritten)
        self.assertEqual(result["summary"], "改写后的摘要")
        self.assertEqual(result["metadata"]["tags"], ["标签1", "标签2"])
        self.assertTrue(result["metadata"]["deai_applied"])


class TestValidation(unittest.TestCase):
    def setUp(self):
        self.agent = _make_agent()

    def test_markers_preserved_ok(self):
        ok, reason = self.agent._validate(
            "x [IMAGE: a] y [IMAGE: b]", "p [IMAGE: a] q [IMAGE: b]"
        )
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    def test_markers_lost(self):
        ok, reason = self.agent._validate(
            "a [IMAGE: a] [IMAGE: b]", "b [IMAGE: a]"
        )
        self.assertFalse(ok)
        self.assertTrue(reason.startswith("image_markers_lost"))

    def test_too_short(self):
        ok, reason = self.agent._validate("x" * 1000, "短")
        self.assertFalse(ok)
        self.assertTrue(reason.startswith("too_short"))

    def test_empty_output(self):
        ok, reason = self.agent._validate("x [IMAGE: a]", "")
        self.assertFalse(ok)
        self.assertEqual(reason, "empty_output")

    def test_extract_strips_fence(self):
        self.assertEqual(
            self.agent._extract_content("```markdown\nhello\n```"), "hello"
        )


if __name__ == "__main__":
    unittest.main()
