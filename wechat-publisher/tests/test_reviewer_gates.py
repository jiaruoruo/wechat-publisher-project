"""审核 Agent 门控单测

覆盖「一票否决落到代码层」后的确定性逻辑：
- details 字段缺失 / 非数值 → 跳过门控（不误杀）
- compliance 文本命中违规判定 → 阻断；否定语境（未发现违规）→ 不阻断
- title_score / readability_score 维度分门槛
- 分类敏感词扫描与 banned_keywords 向后兼容
- run() 收口裁决：即便 LLM 说通过，代码门控仍可否决

用桩路由器构造 ReviewerAgent（不触发真实 LLM），只测确定性分支。
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.reviewer import ReviewerAgent

DEFAULT_NEGATIVE_KEYWORDS = ["违规", "违反", "不通过", "敏感", "侵权", "虚假宣传"]

# 足够长且含插图标记的正文，规避「正文过短/无插图」这两条 advisory 干扰
LONG_CONTENT = "这是一段用于测试的正文内容。" * 30 + "\n\n[IMAGE: 示意图]\n"


class _StubRouter:
    """最小桩：BaseAgent 仅调用 get_llm，单测不触发 LLM"""

    def __init__(self, config: dict):
        self.config = config

    def get_llm(self, name: str):
        return None


def _make_agent(review_cfg: dict) -> ReviewerAgent:
    config = {"review": review_cfg}
    return ReviewerAgent(_StubRouter(config), config)


class TestComplianceGate(unittest.TestCase):
    """details.compliance 文本门控"""

    def setUp(self):
        self.agent = _make_agent(
            {"compliance_negative_keywords": DEFAULT_NEGATIVE_KEYWORDS}
        )

    def test_missing_details_skips_gate(self):
        self.assertFalse(self.agent._enforce_details_gates({})["blocked"])

    def test_empty_compliance_skips_gate(self):
        self.assertFalse(
            self.agent._enforce_details_gates({"compliance": ""})["blocked"]
        )

    def test_violation_text_blocks(self):
        result = self.agent._enforce_details_gates(
            {"compliance": "存在违规内容，涉嫌虚假宣传"}
        )
        self.assertTrue(result["blocked"])
        self.assertIn("违规", result["report"]["compliance_violations"])
        self.assertIn("虚假宣传", result["report"]["compliance_violations"])

    def test_clean_compliance_passes(self):
        result = self.agent._enforce_details_gates({"compliance": "合规，无问题"})
        self.assertFalse(result["blocked"])

    def test_negated_text_not_blocked(self):
        """否定语境防护：「未发现违规」「不存在侵权」等不得误判"""
        for text in [
            "未发现违规",
            "不存在侵权",
            "内容不敏感",
            "未发现任何敏感性表述",
            "文章不涉及违规内容",
            "经详细核查后未发现任何违规情况",
        ]:
            with self.subTest(text=text):
                result = self.agent._enforce_details_gates({"compliance": text})
                self.assertFalse(result["blocked"], f"「{text}」不应被判为违规")

    def test_affirmative_violation_still_blocked(self):
        """肯定语境必须被拦截（含「不仅…还…」这类易被误判为否定的表述）"""
        for text in ["存在侵权风险", "审核不通过", "不仅违规还侵权"]:
            with self.subTest(text=text):
                result = self.agent._enforce_details_gates({"compliance": text})
                self.assertTrue(result["blocked"], f"「{text}」应被判为违规")

    def test_disabled_when_no_keywords_configured(self):
        agent = _make_agent({"compliance_negative_keywords": []})
        self.assertFalse(
            agent._enforce_details_gates({"compliance": "严重违规"})["blocked"]
        )


class TestDimensionScoreGate(unittest.TestCase):
    """title_score / readability_score 维度分门槛"""

    def setUp(self):
        self.agent = _make_agent(
            {"min_title_score": 4, "min_readability_score": 4}
        )

    def test_below_threshold_blocks(self):
        result = self.agent._enforce_details_gates(
            {"title_score": 3, "readability_score": 9}
        )
        self.assertTrue(result["blocked"])
        self.assertIn("标题", result["issues"][0])

    def test_readability_below_threshold_blocks(self):
        result = self.agent._enforce_details_gates(
            {"title_score": 9, "readability_score": 2}
        )
        self.assertTrue(result["blocked"])
        self.assertTrue(any("可读性" in i for i in result["issues"]))

    def test_above_threshold_passes(self):
        result = self.agent._enforce_details_gates(
            {"title_score": 8, "readability_score": 8}
        )
        self.assertFalse(result["blocked"])
        self.assertEqual(result["report"]["title_score"], 8)

    def test_missing_field_skips(self):
        result = self.agent._enforce_details_gates({"compliance": "合规"})
        self.assertFalse(result["blocked"])

    def test_threshold_zero_disables_check(self):
        agent = _make_agent({"min_title_score": 0, "min_readability_score": 0})
        result = agent._enforce_details_gates(
            {"title_score": 1, "readability_score": 1}
        )
        self.assertFalse(result["blocked"])

    def test_non_numeric_values_skipped(self):
        for bad in ["高分", None, True, [], {}]:
            with self.subTest(bad=bad):
                result = self.agent._enforce_details_gates(
                    {"title_score": bad, "readability_score": 8}
                )
                self.assertFalse(result["blocked"])

    def test_numeric_string_is_converted(self):
        """LLM 返回字符串数字时应正常参与比较"""
        result = self.agent._enforce_details_gates(
            {"title_score": "3", "readability_score": 8}
        )
        self.assertTrue(result["blocked"])
        self.assertEqual(result["report"]["title_score"], 3)


class TestSensitiveWords(unittest.TestCase):
    """分类敏感词扫描"""

    def test_category_hit_blocks_with_label(self):
        agent = _make_agent(
            {"sensitive_words": {"absolute_terms": ["国家级", "最佳"]}}
        )
        result = agent._deterministic_checks("标题", LONG_CONTENT + "国家级最佳", "")
        self.assertTrue(result["blocked"])
        self.assertIn("绝对化用语", result["issues"][0])
        self.assertEqual(
            result["report"]["sensitive_words"]["absolute_terms"],
            ["国家级", "最佳"],
        )

    def test_clean_content_passes(self):
        agent = _make_agent({"sensitive_words": {"absolute_terms": ["国家级"]}})
        result = agent._deterministic_checks("标题", LONG_CONTENT, "")
        self.assertFalse(result["blocked"])

    def test_title_is_also_scanned(self):
        agent = _make_agent({"sensitive_words": {"absolute_terms": ["顶级"]}})
        result = agent._deterministic_checks("顶级好文", LONG_CONTENT, "")
        self.assertTrue(result["blocked"])

    def test_empty_category_ignored(self):
        agent = _make_agent({"sensitive_words": {"political": []}})
        result = agent._deterministic_checks("标题", LONG_CONTENT, "")
        self.assertFalse(result["blocked"])

    def test_banned_keywords_backward_compatible(self):
        agent = _make_agent({"banned_keywords": ["内部机密"]})
        result = agent._deterministic_checks("标题", LONG_CONTENT + "内部机密", "")
        self.assertTrue(result["blocked"])
        self.assertIn("内部机密", result["issues"][0])

    def test_malformed_sensitive_words_ignored(self):
        """配置写成非 dict 时不应崩溃"""
        agent = _make_agent({"sensitive_words": ["国家级"]})
        result = agent._deterministic_checks("标题", LONG_CONTENT, "")
        self.assertFalse(result["blocked"])


class TestRunAdjudication(unittest.TestCase):
    """run() 收口裁决：LLM 结论与代码门控的合并结果"""

    def _run(
        self, review_cfg: dict, llm_payload: dict, content: str = LONG_CONTENT
    ) -> dict:
        agent = _make_agent(review_cfg)
        agent.invoke = lambda prompt, json_mode=False: json.dumps(
            llm_payload, ensure_ascii=False
        )
        return agent.run(
            {
                "article_title": "测试标题",
                "content": content,
                "retry_count": 0,
            }
        )

    def test_compliance_veto_overrides_llm_pass(self):
        """LLM 说通过且分数够，但 compliance 写了违规 → 仍判不通过"""
        result = self._run(
            {"min_score": 7, "compliance_negative_keywords": DEFAULT_NEGATIVE_KEYWORDS},
            {
                "passed": True,
                "score": 9,
                "feedback": "写得不错",
                "details": {"compliance": "存在违规内容"},
            },
        )
        self.assertFalse(result["review_result"]["passed"])
        self.assertIn("一票否决", result["review_result"]["feedback"])

    def test_dimension_score_veto_overrides_llm_pass(self):
        result = self._run(
            {"min_score": 7, "min_title_score": 4},
            {"passed": True, "score": 9, "feedback": "", "details": {"title_score": 2}},
        )
        self.assertFalse(result["review_result"]["passed"])
        self.assertIn("标题", result["review_result"]["feedback"])

    def test_sensitive_word_veto(self):
        """正文命中分类敏感词时，即便 LLM 通过也应被否决"""
        result = self._run(
            {"min_score": 7, "sensitive_words": {"absolute_terms": ["国家级"]}},
            {"passed": True, "score": 9, "feedback": ""},
            content=LONG_CONTENT + "本产品为国家级",
        )
        self.assertFalse(result["review_result"]["passed"])
        self.assertIn("绝对化用语", result["review_result"]["feedback"])

    def test_all_gates_pass(self):
        result = self._run(
            {
                "min_score": 7,
                "min_title_score": 4,
                "min_readability_score": 4,
                "compliance_negative_keywords": DEFAULT_NEGATIVE_KEYWORDS,
            },
            {
                "passed": True,
                "score": 9,
                "feedback": "",
                "details": {"compliance": "未发现违规", "title_score": 8},
            },
        )
        self.assertTrue(result["review_result"]["passed"])
        self.assertTrue(result["review_result"]["parse_ok"])

    def test_missing_details_does_not_block(self):
        """e2e 测试里的假 LLM 不返回 details，不得因缺字段误杀"""
        result = self._run(
            {"min_score": 7, "min_title_score": 4, "min_readability_score": 4},
            {"passed": True, "score": 8, "feedback": "内容优质"},
        )
        self.assertTrue(result["review_result"]["passed"])

    def test_score_below_min_score_blocks(self):
        result = self._run({"min_score": 7}, {"passed": True, "score": 5})
        self.assertFalse(result["review_result"]["passed"])

    def test_history_records_details(self):
        """review_history 需保留各维度明细以便审计"""
        result = self._run(
            {"min_score": 7},
            {
                "passed": True,
                "score": 8,
                "feedback": "",
                "details": {"title_score": 8, "compliance": "合规"},
            },
        )
        entry = result["review_history"][0]
        self.assertEqual(entry["score"], 8)
        self.assertEqual(entry["details"]["title_score"], 8)

    def test_naturalness_veto(self):
        """正文 AI 味过重（naturalness 分低）时，即便 LLM 通过也应被否决"""
        result = self._run(
            {"min_score": 7, "min_naturalness_score": 4},
            {
                "passed": True,
                "score": 9,
                "feedback": "",
                "details": {"naturalness_score": 2},
            },
            content=LONG_CONTENT,
        )
        self.assertFalse(result["review_result"]["passed"])
        self.assertIn("自然度", result["review_result"]["feedback"])


class TestNaturalnessGate(unittest.TestCase):
    """details.naturalness_score 维度分门槛（缺字段/非数值跳过，不误杀）"""

    def setUp(self):
        self.agent = _make_agent({"min_naturalness_score": 4})

    def test_missing_field_skips(self):
        self.assertFalse(
            self.agent._enforce_details_gates({"compliance": "合规"})["blocked"]
        )

    def test_below_threshold_blocks(self):
        result = self.agent._enforce_details_gates({"naturalness_score": 2})
        self.assertTrue(result["blocked"])
        self.assertIn("自然度", result["issues"][0])

    def test_above_threshold_passes(self):
        result = self.agent._enforce_details_gates({"naturalness_score": 8})
        self.assertFalse(result["blocked"])
        self.assertEqual(result["report"]["naturalness_score"], 8)

    def test_threshold_zero_disables(self):
        agent = _make_agent({"min_naturalness_score": 0})
        result = agent._enforce_details_gates({"naturalness_score": 1})
        self.assertFalse(result["blocked"])

    def test_non_numeric_skipped(self):
        result = self.agent._enforce_details_gates({"naturalness_score": "偏低"})
        self.assertFalse(result["blocked"])


if __name__ == "__main__":
    unittest.main()
