"""tests for graph/state.py create_initial_state"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from graph.state import create_initial_state


class TestCreateInitialState(unittest.TestCase):
    def test_defaults(self):
        state = create_initial_state()
        self.assertEqual(state["retry_count"], 0)
        self.assertEqual(state["inline_images"], [])
        self.assertEqual(state["review_result"], {})
        self.assertEqual(state["publish_result"], {})
        self.assertEqual(state["review_history"], [])
        self.assertEqual(state["metadata"]["max_retries"], 2)
        self.assertIn("started_at", state["metadata"])

    def test_custom_max_retries(self):
        state = create_initial_state(max_retries=5)
        self.assertEqual(state["metadata"]["max_retries"], 5)

    def test_extra_fields(self):
        state = create_initial_state(extra={"topic": "AI趋势"})
        self.assertEqual(state["topic"], "AI趋势")

    def test_extra_metadata_merges_instead_of_replacing(self):
        state = create_initial_state(
            max_retries=3,
            extra={"metadata": {"triggered_by": "feishu"}},
        )
        self.assertEqual(state["metadata"]["max_retries"], 3)
        self.assertEqual(state["metadata"]["triggered_by"], "feishu")

    def test_extra_overrides_defaults(self):
        state = create_initial_state(extra={"retry_count": 1})
        self.assertEqual(state["retry_count"], 1)

    def test_reducers_merge_like_langgraph_schema(self):
        from graph.state import merge_metadata, append_review_history
        # 与 LangGraph schema 注解使用的同一归并器
        self.assertEqual(merge_metadata({"a": 1}, {"b": 2}), {"a": 1, "b": 2})
        self.assertEqual(merge_metadata(None, {"b": 2}), {"b": 2})
        self.assertEqual(merge_metadata({"a": 1}, {"a": 2}), {"a": 2})
        self.assertEqual(append_review_history([{"x": 1}], [{"y": 2}]), [{"x": 1}, {"y": 2}])
        self.assertEqual(append_review_history(None, [{"y": 2}]), [{"y": 2}])


if __name__ == "__main__":
    unittest.main()
