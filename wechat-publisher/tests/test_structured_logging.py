"""tests for config/structured_logging.py"""

import io
import json
import logging
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.structured_logging import log_event


class TestStructuredLogging(unittest.TestCase):
    def setUp(self):
        self.stream = io.StringIO()
        self.handler = logging.StreamHandler(self.stream)
        self.logger = logging.getLogger("config.structured_logging")
        self.logger.addHandler(self.handler)
        self.logger.setLevel(logging.DEBUG)
        self.logger.propagate = False

    def tearDown(self):
        self.logger.removeHandler(self.handler)
        self.logger.propagate = True

    def test_log_event_emits_json_line(self):
        log_event("workflow.complete", article_title="AI趋势", score=8)
        payload = json.loads(self.stream.getvalue().strip())
        self.assertEqual(payload["event"], "workflow.complete")
        self.assertEqual(payload["data"]["article_title"], "AI趋势")
        self.assertEqual(payload["data"]["score"], 8)
        self.assertIn("timestamp", payload)

    def test_unsupported_level_falls_back_to_info(self):
        log_event("x", level="bogus", k=1)
        payload = json.loads(self.stream.getvalue().strip())
        self.assertEqual(payload["event"], "x")


if __name__ == "__main__":
    unittest.main()
