"""内容创作 Agent 单测 - 验证风格注入与字数注入

背景：`content.style` 原先只有 topic_planner 消费，正文写作拿不到品牌调性。
本测试锁定「风格必须同时进入新建与重写两条 prompt 路径」这一契约。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.content_writer import ContentWriterAgent

CUSTOM_STYLE = "面向大众读者的科技观察者口吻：术语先用一句人话解释清楚再展开。"
DEFAULT_STYLE = "专业但通俗易懂"


class StubLLM:
    def __init__(self):
        self.calls = 0

    def bind(self, **kwargs):
        return self

    def invoke(self, messages, **kwargs):
        self.calls += 1
        return type("R", (), {"content": "# 标题\n\n正文"})()


class StubRouter:
    def __init__(self, llm):
        self._llm = llm
        self.config = {}

    def get_llm(self, agent_name):
        return self._llm


def make_agent(config=None):
    """构造 Agent；未传 config 时使用最小配置（不含 content 段）"""
    llm = StubLLM()
    return ContentWriterAgent(StubRouter(llm), config or {}), llm


class TestStyleInjection(unittest.TestCase):
    def test_custom_style_in_new_prompt(self):
        agent, _ = make_agent({"content": {
            "style": CUSTOM_STYLE,
            "article_length": {"min": 1200, "max": 2400},
        }})
        prompt = agent._build_new_prompt(
            title="标题", topic="主题", outline="大纲", target_audience="受众"
        )
        self.assertIn("内容风格要求", prompt)
        self.assertIn(CUSTOM_STYLE, prompt)

    def test_custom_style_in_rewrite_prompt(self):
        # 重写路径同样要守住品牌调性，否则审核返工会丢掉调性
        agent, _ = make_agent({"content": {
            "style": CUSTOM_STYLE,
            "article_length": {"min": 1200, "max": 2400},
        }})
        prompt = agent._build_rewrite_prompt(
            title="标题", topic="主题", outline="大纲", target_audience="受众",
            original_content="原文", feedback="请补充数据来源", retry_count=1,
        )
        self.assertIn("内容风格要求", prompt)
        self.assertIn(CUSTOM_STYLE, prompt)

    def test_default_style_when_missing(self):
        agent, _ = make_agent({"content": {"article_length": {"min": 10, "max": 20}}})
        self.assertEqual(agent.style, DEFAULT_STYLE)

    def test_default_style_without_content_section(self):
        # content 段完全缺失不得抛异常（工作流启动不能因配置缺失而失败）
        agent, _ = make_agent({})
        self.assertEqual(agent.style, DEFAULT_STYLE)
        self.assertEqual(agent.min_words, 1500)
        self.assertEqual(agent.max_words, 3000)

    def test_style_falls_back_on_empty_string(self):
        agent, _ = make_agent({"content": {"style": ""}})
        # 空串应回落默认值，而不是把空行塞进 prompt
        self.assertEqual(agent.style, DEFAULT_STYLE)


class TestWordCountInjection(unittest.TestCase):
    def test_custom_word_count(self):
        agent, _ = make_agent({"content": {"article_length": {"min": 800, "max": 1600}}})
        prompt = agent._build_new_prompt(
            title="标题", topic="主题", outline="大纲", target_audience="受众"
        )
        self.assertIn("800-1600 字", prompt)

    def test_default_word_count(self):
        agent, _ = make_agent({})
        prompt = agent._build_new_prompt(
            title="标题", topic="主题", outline="大纲", target_audience="受众"
        )
        self.assertIn("1500-3000 字", prompt)

    def test_rewrite_prompt_keeps_word_count(self):
        agent, _ = make_agent({"content": {"article_length": {"min": 800, "max": 1600}}})
        prompt = agent._build_rewrite_prompt(
            title="标题", topic="主题", outline="大纲", target_audience="受众",
            original_content="原文", feedback="请补充数据来源", retry_count=1,
        )
        self.assertIn("800-1600 字", prompt)


class TestPromptIntegrity(unittest.TestCase):
    def test_new_prompt_keeps_existing_sections(self):
        # 注入风格不得挤掉原有段落
        agent, _ = make_agent({"content": {"style": CUSTOM_STYLE}})
        prompt = agent._build_new_prompt(
            title="标题A", topic="主题B", outline="大纲C", target_audience="受众D"
        )
        for fragment in ("标题A", "主题B", "大纲C", "受众D", "[IMAGE:", "Markdown"):
            self.assertIn(fragment, prompt)

    def test_rewrite_prompt_keeps_feedback(self):
        agent, _ = make_agent({"content": {"style": CUSTOM_STYLE}})
        prompt = agent._build_rewrite_prompt(
            title="标题A", topic="主题B", outline="大纲C", target_audience="受众D",
            original_content="原文E", feedback="反馈F", retry_count=2,
        )
        for fragment in ("原文E", "反馈F", "第 2 次修改"):
            self.assertIn(fragment, prompt)

    def test_run_uses_new_prompt_on_first_attempt(self):
        # 改造后首次成文走七阶段流水线：7 个子 Agent 各 1 次 LLM 调用
        agent, llm = make_agent({"content": {"style": CUSTOM_STYLE}})
        result = agent.run({
            "topic": "主题", "outline": "大纲",
            "article_title": "标题", "target_audience": "受众",
            "retry_count": 0, "review_result": {},
        })
        self.assertEqual(llm.calls, 7)
        # 桩件对 JSON 阶段返回的都是非 JSON 文本，解析失败后回落前序值：
        # 标题回落 topic_planner 已产出的标题，用户意图不被冲掉。
        self.assertEqual(result["article_title"], "标题")
        self.assertEqual(result["content"], "# 标题\n\n正文")

    def test_run_rewrite_path_does_not_rerun_full_pipeline(self):
        # 审核不通过时沿用重写契约：不重跑七阶段，仅重写正文 + 刷新摘要/标签
        agent, llm = make_agent({"content": {"style": CUSTOM_STYLE}})
        result = agent.run({
            "topic": "主题", "outline": "大纲",
            "article_title": "标题", "target_audience": "受众",
            "content": "原文", "retry_count": 1,
            "review_result": {"passed": False, "feedback": "请补充数据来源"},
        })
        # 1 次重写 + 2 次刷新（摘要、标签），远小于重跑七阶段的 7 次
        self.assertEqual(llm.calls, 3)
        self.assertEqual(result["article_title"], "标题")

    def test_title_pinned_is_not_overwritten(self):
        # 用户显式指定标题（cmd_run --title）时，标题创作阶段不得覆盖
        agent, _ = make_agent({"content": {"style": CUSTOM_STYLE}})
        result = agent.run({
            "topic": "主题", "outline": "大纲",
            "article_title": "用户指定标题", "target_audience": "受众",
            "retry_count": 0, "review_result": {},
            "metadata": {"title_pinned": "用户指定标题"},
        })
        self.assertEqual(result["article_title"], "用户指定标题")


if __name__ == "__main__":
    unittest.main()
