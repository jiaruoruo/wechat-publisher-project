"""tests for graph/conditions.py"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from graph.conditions import review_condition, increment_retry


class TestReviewCondition(unittest.TestCase):
    def test_approved(self):
        state = {
            "review_result": {"passed": True, "score": 8},
            "retry_count": 0,
            "metadata": {"max_retries": 2},
        }
        self.assertEqual(review_condition(state), "approved")

    def test_rejected_under_max_retries(self):
        state = {
            "review_result": {"passed": False, "score": 4},
            "retry_count": 1,
            "metadata": {"max_retries": 2},
        }
        self.assertEqual(review_condition(state), "rejected")

    def test_max_retries_reached(self):
        state = {
            "review_result": {"passed": False, "score": 4},
            "retry_count": 2,
            "metadata": {"max_retries": 2},
        }
        self.assertEqual(review_condition(state), "max_retries")

    def test_missing_metadata_uses_default_max_retries(self):
        state = {"review_result": {"passed": False}, "retry_count": 2}
        self.assertEqual(review_condition(state), "max_retries")

    def test_missing_review_result_defaults_passed(self):
        state = {"retry_count": 0, "metadata": {"max_retries": 2}}
        self.assertEqual(review_condition(state), "approved")

    def test_retry_count_reaches_max_retries_goes_formatter(self):
        # 回退两次（retry_count=2）后仍不通过 → 强制进入排版
        state = {
            "review_result": {"passed": False, "score": 3},
            "retry_count": 2,
            "metadata": {"max_retries": 2},
        }
        self.assertEqual(review_condition(state), "max_retries")


class TestIncrementRetry(unittest.TestCase):
    """workflow 层重试计数节点"""

    def test_increments_from_zero(self):
        self.assertEqual(increment_retry({"retry_count": 0}), {"retry_count": 1})

    def test_increments_missing_field(self):
        self.assertEqual(increment_retry({}), {"retry_count": 1})

    def test_increments_existing_count(self):
        self.assertEqual(increment_retry({"retry_count": 2}), {"retry_count": 3})


if __name__ == "__main__":
    unittest.main()
