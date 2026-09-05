"""竞品标题采集单测（mock 网络，临时 DB 验证落库）"""

import os
import shutil
import tempfile
import unittest
from unittest import mock

from tools import competitor_feed
from tools.content_db import ContentDB


class TestCollect(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="compfeed_")
        self.db_path = os.path.join(self.tmp, "a.db")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _fake_results(self, n):
        return [
            {"title": f"竞品爆款标题{i}", "snippet": "", "url": f"https://e{i}"}
            for i in range(n)
        ]

    def test_collect_basic(self):
        with mock.patch("tools.web_search.web_search", return_value=self._fake_results(5)):
            rows = competitor_feed.collect_titles(["AI"], max_keywords=1, results_per_keyword=5)
        self.assertEqual(len(rows), 5)
        self.assertTrue(all(r["keyword"] == "AI" for r in rows))

    def test_collect_empty(self):
        with mock.patch("tools.web_search.web_search", return_value=[]):
            rows = competitor_feed.collect_titles(["AI"])
        self.assertEqual(rows, [])

    def test_collect_no_keywords(self):
        self.assertEqual(competitor_feed.collect_titles([]), [])

    def test_collect_skips_blank_title(self):
        results = [{"title": "", "url": "x"}, {"title": "有效标题", "url": "y"}]
        with mock.patch("tools.web_search.web_search", return_value=results):
            rows = competitor_feed.collect_titles(["AI"], max_keywords=1, results_per_keyword=5)
        self.assertEqual(len(rows), 1)

    def test_build_bank_saves(self):
        with mock.patch("tools.web_search.web_search", return_value=self._fake_results(4)):
            with mock.patch(
                "tools.title_patterns.extract_patterns",
                return_value=[{"pattern_type": "numeric", "pattern": "数字型:2026",
                               "hit_count": 1, "sample": "2026 趋势"}],
            ):
                titles, patterns = competitor_feed.build_competitor_bank(
                    ContentDB(self.db_path), ["AI"], max_keywords=1, results_per_keyword=4
                )
        self.assertEqual(len(titles), 4)
        self.assertEqual(len(patterns), 1)
        with ContentDB(self.db_path) as db:
            self.assertEqual(len(db.get_competitor_titles()), 4)
            self.assertEqual(len(db.get_title_patterns()), 1)

    def test_build_bank_empty_no_crash(self):
        with mock.patch("tools.web_search.web_search", return_value=[]):
            titles, patterns = competitor_feed.build_competitor_bank(
                ContentDB(self.db_path), ["AI"]
            )
        self.assertEqual(titles, [])
        self.assertEqual(patterns, [])


if __name__ == "__main__":
    unittest.main()
