"""选题评分纯函数单测（无网络、无 IO）"""

import unittest

from tools.topic_scorer import DEFAULT_WEIGHTS, score_topic


_HIGH = [{"tier": "authoritative", "confidence": "high"} for _ in range(10)]
_METRICS = [
    {"title": "旧文A", "read_count": 10000, "share_count": 200, "like_count": 50},
    {"title": "旧文B", "read_count": 20000, "share_count": 300, "like_count": 80},
]


class TestScore(unittest.TestCase):
    def test_empty_all_returns_zero(self):
        score, detail = score_topic({"title": "x"}, [], [], [])
        self.assertEqual(score, 0.0)
        self.assertIn("matched_patterns", detail)
        self.assertIn("weights", detail)
        self.assertIn("heat", detail)

    def test_heat_only(self):
        _, detail = score_topic({"title": "x"}, _HIGH, [], [])
        self.assertGreater(detail["heat"], 0)
        self.assertEqual(detail["pattern"], 0.0)

    def test_pattern_match(self):
        patterns = [{"pattern_type": "numeric", "pattern": "数字型:30倍"}]
        _, detail = score_topic({"title": "30 倍能效提升如何实现"}, [], patterns, [])
        self.assertIn("数字型:30倍", detail["matched_patterns"])
        self.assertGreater(detail["pattern"], 0)

    def test_history_scale(self):
        _, detail = score_topic({"title": "旧文A"}, [], [], _METRICS)
        self.assertGreater(detail["history"], 0)

    def test_custom_weights(self):
        s_heat, _ = score_topic(
            {"title": "x"}, _HIGH, [], [],
            weights={"heat": 1.0, "pattern": 0.0, "history": 0.0},
        )
        s_pat, _ = score_topic(
            {"title": "x"}, _HIGH, [], [],
            weights={"heat": 0.0, "pattern": 1.0, "history": 0.0},
        )
        # pattern 维度无数据，权重移到 heat 时分数更高
        self.assertGreater(s_heat, s_pat)

    def test_weights_normalized(self):
        _, detail = score_topic(
            {"title": "x"}, [], [], [],
            weights={"heat": 2, "pattern": 2, "history": 2},
        )
        self.assertAlmostEqual(sum(detail["weights"].values()), 1.0, delta=0.01)

    def test_score_in_range(self):
        score, _ = score_topic(
            {"title": "2026 年十大模型盘点"}, _HIGH,
            [{"pattern_type": "numeric", "pattern": "数字型:2026"}],
            _METRICS,
        )
        self.assertTrue(0.0 <= score <= 100.0)

    def test_default_weights_keys(self):
        self.assertEqual(set(DEFAULT_WEIGHTS.keys()), {"heat", "pattern", "history"})


if __name__ == "__main__":
    unittest.main()
