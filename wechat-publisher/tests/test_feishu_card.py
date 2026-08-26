"""tests for hermes/feishu_client.py build_result_card review history"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from hermes.feishu_client import FeishuClient
    CARD_AVAILABLE = True
except ImportError:
    CARD_AVAILABLE = False


def _div_contents(card: dict) -> list[str]:
    """提取卡片所有 div 元素里的 lark_md 文本"""
    return [
        el["text"]["content"]
        for el in card["elements"]
        if el["tag"] == "div"
    ]


@unittest.skipUnless(CARD_AVAILABLE, "需要 requests 等运行时依赖")
class TestResultCardReviewHistory(unittest.TestCase):
    def test_review_history_rendered(self):
        card = FeishuClient.build_result_card(
            article_title="测试文章",
            success=True,
            details={"审核评分": "8"},
            review_history=[
                {"retry": 0, "passed": False, "score": 5, "feedback": "请补充数据来源"},
                {"retry": 1, "passed": True, "score": 8, "feedback": "内容优质"},
            ],
        )
        block = next(c for c in _div_contents(card) if "审核历史" in c)
        self.assertIn("第1次 5/10 ❌", block)
        self.assertIn("请补充数据来源", block)
        self.assertIn("第2次 8/10 ✅", block)
        self.assertIn("内容优质", block)

    def test_no_review_history_no_section(self):
        card = FeishuClient.build_result_card("t", True, {"a": "1"})
        self.assertFalse(any("审核历史" in c for c in _div_contents(card)))

    def test_empty_review_history_no_section(self):
        card = FeishuClient.build_result_card("t", True, {}, review_history=[])
        self.assertFalse(any("审核历史" in c for c in _div_contents(card)))

    def test_feedback_truncated_to_40_chars(self):
        card = FeishuClient.build_result_card(
            "t", False, {},
            review_history=[{"retry": 0, "passed": False, "score": 3, "feedback": "很" * 100}],
        )
        block = next(c for c in _div_contents(card) if "审核历史" in c)
        summary = block.split("· ", 1)[1]
        self.assertEqual(len(summary), 40)

    def test_missing_feedback_key_safe(self):
        card = FeishuClient.build_result_card(
            "t", True, {},
            review_history=[{"retry": 0, "passed": True, "score": 9}],
        )
        block = next(c for c in _div_contents(card) if "审核历史" in c)
        self.assertIn("第1次 9/10 ✅", block)
        self.assertNotIn("· ", block)


if __name__ == "__main__":
    unittest.main()
