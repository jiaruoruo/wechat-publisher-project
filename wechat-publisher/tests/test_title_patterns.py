"""标题模式库纯函数单测（无网络、无 IO）"""

import unittest

from tools.title_patterns import (
    PATTERN_TYPES,
    classify_patterns,
    extract_patterns,
    match_patterns,
)


class TestClassify(unittest.TestCase):
    def test_numeric_year(self):
        self.assertIn("numeric", classify_patterns("2026 年 AI 十大趋势解读"))

    def test_numeric_percent(self):
        self.assertIn("numeric", classify_patterns("能效提升 30 倍"))

    def test_question_mark(self):
        self.assertIn("question", classify_patterns("为什么 AI 会失控？"))

    def test_question_word(self):
        self.assertIn("question", classify_patterns("如何训练一个机器人"))

    def test_contrast(self):
        self.assertIn("contrast", classify_patterns("NVIDIA 对比 Intel 的算力逆袭"))

    def test_list(self):
        self.assertIn("list", classify_patterns("大模型十大应用盘点"))

    def test_urgency(self):
        self.assertIn("urgency", classify_patterns("刚刚！重磅发布新架构"))

    def test_no_pattern(self):
        self.assertEqual(classify_patterns("一篇普通的技术文章"), [])

    def test_multiple_types(self):
        types = classify_patterns("2026 年十大 AI 趋势：为什么爆发？")
        self.assertIn("numeric", types)
        self.assertIn("list", types)
        self.assertIn("question", types)


class TestExtract(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(extract_patterns([]), [])

    def test_aggregates_types(self):
        titles = [
            "2026 年趋势", "2026 榜单",
            "为什么 AI 火了？", "为什么芯片缺货？",
            "大模型十大应用盘点",
        ]
        pats = extract_patterns(titles)
        types = {p["pattern_type"] for p in pats}
        self.assertIn("numeric", types)
        self.assertIn("question", types)
        self.assertIn("list", types)

    def test_required_keys(self):
        pats = extract_patterns(["2026 年十大模型"])
        self.assertTrue(pats)
        for k in ("pattern_type", "pattern", "hit_count", "sample"):
            self.assertIn(k, pats[0])

    def test_hit_count_positive(self):
        pats = extract_patterns(["2026 趋势", "2026 榜单"])
        self.assertEqual(pats[0]["hit_count"], 2)


class TestMatch(unittest.TestCase):
    def test_match_by_type(self):
        patterns = [
            {"pattern_type": "numeric", "pattern": "数字型:2026"},
            {"pattern_type": "list", "pattern": "盘点型:枚举清单"},
        ]
        matched = match_patterns("2026 年 AI 趋势", patterns)
        self.assertIn("数字型:2026", matched)

    def test_no_match(self):
        patterns = [{"pattern_type": "question", "pattern": "疑问型:引发好奇"}]
        self.assertEqual(match_patterns("一篇普通的文章", patterns), [])

    def test_empty_inputs(self):
        self.assertEqual(match_patterns("", []), [])
        self.assertEqual(match_patterns("标题", []), [])


if __name__ == "__main__":
    unittest.main()
