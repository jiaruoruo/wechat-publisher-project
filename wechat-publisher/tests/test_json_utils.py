"""tests for tools/json_utils.py"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.json_utils import extract_json_from_response, safe_extract_json


class TestExtractJson(unittest.TestCase):
    def test_pure_json(self):
        data = extract_json_from_response('{"topic": "AI"}')
        self.assertEqual(data["topic"], "AI")

    def test_json_code_block(self):
        raw = "以下是选题：\n```json\n{\"topic\": \"AI趋势\", \"score\": 8}\n```\n请查收。"
        data = extract_json_from_response(raw)
        self.assertEqual(data["topic"], "AI趋势")
        self.assertEqual(data["score"], 8)

    def test_plain_code_block(self):
        raw = '```\n{"topic": "科技"}\n```'
        self.assertEqual(extract_json_from_response(raw)["topic"], "科技")

    def test_extra_text_after_json(self):
        raw = '结果如下：{"topic": "AI"}（以上为结果）'
        self.assertEqual(extract_json_from_response(raw)["topic"], "AI")

    def test_safe_extract_with_default(self):
        default = {"topic": "默认"}
        result = safe_extract_json("这不是 JSON", default=default)
        self.assertEqual(result, default)

    def test_safe_extract_empty_default(self):
        result = safe_extract_json("not json")
        self.assertEqual(result, {})

    def test_safe_extract_valid_passthrough(self):
        self.assertEqual(safe_extract_json('{"a": 1}')["a"], 1)


if __name__ == "__main__":
    unittest.main()
