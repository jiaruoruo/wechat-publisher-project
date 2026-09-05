"""tests for config/validation.py"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.validation import validate_config


def _base_config() -> dict:
    """最小合法配置（深测 focus 在 review 段；其他段的 error/warning 不影响本测试）"""
    return {
        "models": {
            "topic_planner": {},
            "content_writer": {},
            "image_generator": {},
            "reviewer": {},
            "formatter": {},
            "publisher": {},
        },
        "api_keys": {"openai": "sk-x"},
        "content": {"domain": "科技"},
        "notification": {"enabled": True},
        "feishu": {},
    }


class TestMaxRetriesValidation(unittest.TestCase):
    def _max_retries_issues(self, value) -> list[str]:
        cfg = _base_config()
        cfg["review"] = {"max_retries": value}
        return [msg for sev, msg in validate_config(cfg) if "max_retries" in msg]

    def test_normal_max_retries_no_issue(self):
        self.assertEqual(self._max_retries_issues(2), [])

    def test_boundary_four_allowed(self):
        # max_retries=4 为成本告警阈值边界，不告警
        self.assertEqual(self._max_retries_issues(4), [])

    def test_high_max_retries_warns(self):
        # 超过 4 时提示重试带来的 LLM 成本（递归上限已由 workflow 显式处理，不提示递归）
        msgs = self._max_retries_issues(6)
        self.assertTrue(msgs and "超过 4" in msgs[0], msgs)
        self.assertNotIn("递归", msgs[0])

    def test_negative_rejected(self):
        msgs = self._max_retries_issues(-1)
        self.assertTrue(msgs and "非负整数" in msgs[0], msgs)

    def test_non_int_rejected(self):
        msgs = self._max_retries_issues("abc")
        self.assertTrue(msgs and "非负整数" in msgs[0], msgs)


if __name__ == "__main__":
    unittest.main()
