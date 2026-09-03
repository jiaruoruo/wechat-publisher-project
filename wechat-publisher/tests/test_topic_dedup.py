"""选题去重相似度函数单测 - 纯函数，无外部依赖"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.topic_dedup import DEFAULT_THRESHOLD, find_most_similar, normalize, similarity


class TestNormalize(unittest.TestCase):
    def test_none_and_empty(self):
        self.assertEqual(normalize(None), "")
        self.assertEqual(normalize(""), "")
        self.assertEqual(normalize("   "), "")

    def test_strips_punctuation_and_whitespace(self):
        self.assertEqual(normalize("AI 大模型：最新趋势！"), "ai大模型最新趋势")

    def test_fullwidth_to_halfwidth(self):
        # 全角 ＡＩ 应归一为 ai，避免同一选题因全半角写法差异绕过去重
        self.assertEqual(normalize("ＡＩ"), normalize("AI"))

    def test_case_insensitive(self):
        self.assertEqual(normalize("GPT"), normalize("gpt"))


class TestSimilarity(unittest.TestCase):
    def test_identical_is_one(self):
        self.assertAlmostEqual(similarity("AI 大模型趋势", "AI 大模型趋势"), 1.0)

    def test_empty_is_zero(self):
        # 空历史标题不能误判为重复
        self.assertEqual(similarity("AI 大模型趋势", ""), 0.0)
        self.assertEqual(similarity("", "AI 大模型趋势"), 0.0)
        self.assertEqual(similarity(None, None), 0.0)

    def test_completely_different_is_low(self):
        self.assertLess(similarity("大模型训练成本分析", "夏日家常凉菜做法"), 0.3)

    def test_word_order_swap_still_high(self):
        # 纯序列比会低估「换序」，靠 Jaccard 兜住
        score = similarity("AI 大模型趋势", "大模型 AI 趋势")
        self.assertGreater(score, 0.8)

    def test_fullwidth_equivalent_is_one(self):
        self.assertAlmostEqual(similarity("ＡＩ大模型", "AI大模型"), 1.0)


class TestFindMostSimilar(unittest.TestCase):
    HISTORY = [
        {"title": "2026 年 AI 大模型十大趋势解读", "topic": "AI 大模型趋势"},
        {"title": "RAG 检索增强生成实战指南", "topic": "RAG 实践"},
    ]

    def test_exact_duplicate_detected(self):
        match = find_most_similar("2026 年 AI 大模型十大趋势解读", self.HISTORY)
        self.assertTrue(match["is_duplicate"])
        self.assertAlmostEqual(match["score"], 1.0)
        self.assertEqual(match["field"], "title")

    def test_topic_field_also_compared(self):
        # 标题不同但 topic 相同 —— 只比对标题会漏掉这种情况
        match = find_most_similar("RAG 检索增强生成实战指南", self.HISTORY)
        self.assertTrue(match["is_duplicate"])
        self.assertEqual(match["field"], "title")

        match2 = find_most_similar("RAG 实践", self.HISTORY)
        self.assertTrue(match2["is_duplicate"])
        self.assertEqual(match2["field"], "topic")

    def test_unrelated_not_duplicate(self):
        match = find_most_similar("夏日家常凉菜的十种做法", self.HISTORY)
        self.assertFalse(match["is_duplicate"])
        self.assertLess(match["score"], DEFAULT_THRESHOLD)

    def test_empty_history_returns_zero(self):
        match = find_most_similar("任意选题", [])
        self.assertEqual(match["score"], 0.0)
        self.assertEqual(match["matched"], "")
        self.assertFalse(match["is_duplicate"])

    def test_plain_string_history_supported(self):
        match = find_most_similar("RAG 检索增强生成实战指南", ["RAG 检索增强生成实战指南"])
        self.assertTrue(match["is_duplicate"])

    def test_threshold_respected(self):
        candidate = "AI 大模型趋势盘点"
        # 阈值抬高后，同一候选不再判定为重复
        self.assertTrue(find_most_similar(candidate, self.HISTORY, 0.5)["is_duplicate"])
        self.assertFalse(find_most_similar(candidate, self.HISTORY, 0.99)["is_duplicate"])


if __name__ == "__main__":
    unittest.main()
