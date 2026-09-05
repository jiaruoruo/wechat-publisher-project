"""热点搜索单测 - 全程桩掉网络请求，禁止真实联网

覆盖：关键词去同义、跨关键词标题去重、TTL 缓存命中/过期、旧 search_trending 契约。
"""

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tools.web_search as ws


def _result(title: str, snippet: str = "摘要") -> dict:
    return {"title": title, "snippet": snippet, "url": f"https://example.com/{title}"}


class TestDedupeKeywords(unittest.TestCase):
    def test_synonyms_collapsed(self):
        # AI 与 人工智能 同义，合并为一个查询；大模型保留
        self.assertEqual(ws.dedupe_keywords(["AI", "人工智能", "大模型"], 3), ["AI", "大模型"])

    def test_exact_duplicates_removed(self):
        self.assertEqual(ws.dedupe_keywords(["AI", "AI", "ai"], 3), ["AI"])

    def test_max_keywords_respected(self):
        self.assertEqual(len(ws.dedupe_keywords(["AI", "区块链", "量子计算", "芯片"], 2)), 2)

    def test_order_preserved(self):
        self.assertEqual(ws.dedupe_keywords(["区块链", "AI", "量子计算"], 3), ["区块链", "AI", "量子计算"])

    def test_invalid_inputs_ignored(self):
        self.assertEqual(ws.dedupe_keywords(["", "  ", "AI"], 3), ["AI"])
        self.assertEqual(ws.dedupe_keywords([None, 123, "AI"], 3), ["AI"])

    def test_empty_input(self):
        self.assertEqual(ws.dedupe_keywords([], 3), [])
        self.assertEqual(ws.dedupe_keywords(None, 3), [])


class SearchTrendingTestCase(unittest.TestCase):
    """统一处理缓存文件隔离：把 CACHE_PATH 指到临时目录"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ws_test_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cache_path = os.path.join(self.tmp, "trending_cache.json")
        patcher = mock.patch.object(ws, "CACHE_PATH", self.cache_path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _patch_web_search(self, mapping):
        """按关键词返回不同结果；mapping: keyword -> list[result]"""

        def fake(query, max_results=5):
            for kw, results in mapping.items():
                if kw in query:
                    return results
            return []

        return mock.patch.object(ws, "web_search", side_effect=fake)


class TestSearchTrendingDetailed(SearchTrendingTestCase):
    def test_ok_and_sources(self):
        mapping = {"AI": [_result("新闻 A"), _result("新闻 B")]}
        with self._patch_web_search(mapping):
            detail = ws.search_trending_detailed(["AI"], "科技", cache_ttl_seconds=0)

        self.assertTrue(detail["ok"])
        self.assertIn("新闻 A", detail["text"])
        self.assertEqual(len(detail["sources"]), 2)

    def test_cross_keyword_title_dedupe(self):
        # 两个关键词搜出同一条热点，只应保留一次
        mapping = {
            "AI": [_result("同一条新闻")],
            "区块链": [_result("同一条新闻"), _result("区块链独有新闻")],
        }
        with self._patch_web_search(mapping):
            detail = ws.search_trending_detailed(["AI", "区块链"], "", cache_ttl_seconds=0)

        self.assertEqual(detail["text"].count("同一条新闻"), 1)
        self.assertIn("区块链独有新闻", detail["text"])

    def test_synonym_keyword_only_searched_once(self):
        mapping = {"AI": [_result("AI 新闻")]}
        with self._patch_web_search(mapping) as fake:
            ws.search_trending_detailed(["AI", "人工智能"], "", cache_ttl_seconds=0)

        # 同义合并后只发一个查询，而不是两个
        self.assertEqual(fake.call_count, 1)

    def test_all_channels_fail_returns_not_ok(self):
        with mock.patch.object(ws, "web_search", return_value=[]):
            detail = ws.search_trending_detailed(["AI"], "", cache_ttl_seconds=0)

        self.assertFalse(detail["ok"])
        self.assertEqual(detail["sources"], [])
        self.assertIn("未找到相关热点", detail["text"])

    def test_no_keywords_returns_not_ok(self):
        with mock.patch.object(ws, "web_search", return_value=[]) as fake:
            detail = ws.search_trending_detailed([], "", cache_ttl_seconds=0)

        self.assertFalse(detail["ok"])
        fake.assert_not_called()


class TestTrendingCache(SearchTrendingTestCase):
    def test_cache_hit_avoids_network(self):
        mapping = {"AI": [_result("缓存新闻")]}
        with self._patch_web_search(mapping) as fake:
            first = ws.search_trending_detailed(["AI"], "", cache_ttl_seconds=3600)
            second = ws.search_trending_detailed(["AI"], "", cache_ttl_seconds=3600)

        self.assertTrue(first["ok"])
        self.assertEqual(first, second)
        # 第二次命中缓存，不再发起网络请求
        self.assertEqual(fake.call_count, 1)

    def test_cache_key_is_hash_not_plaintext(self):
        with self._patch_web_search({"AI": [_result("新闻")]}):
            ws.search_trending_detailed(["AI"], "", cache_ttl_seconds=3600)

        with open(self.cache_path, "r", encoding="utf-8") as f:
            store = json.load(f)

        keys = " ".join(store.keys()).lower()
        # 缓存键应为哈希摘要，不得出现关键词明文
        self.assertNotIn("ai", keys)
        self.assertNotIn("最新", keys)
        # 每个键都是 16 位十六进制摘要
        for key in store:
            self.assertEqual(len(key), 16)
            int(key, 16)

    def test_cache_expired_triggers_refetch(self):
        mapping = {"AI": [_result("新闻")]}
        with self._patch_web_search(mapping) as fake:
            ws.search_trending_detailed(["AI"], "", cache_ttl_seconds=3600)
            # 手动把缓存条目的时间戳推到过期
            with open(self.cache_path, "r", encoding="utf-8") as f:
                store = json.load(f)
            for entry in store.values():
                entry["ts"] = time.time() - 7200
            with open(self.cache_path, "w", encoding="utf-8") as f:
                json.dump(store, f)
            ws.search_trending_detailed(["AI"], "", cache_ttl_seconds=3600)

        self.assertEqual(fake.call_count, 2)

    def test_ttl_zero_disables_cache(self):
        with self._patch_web_search({"AI": [_result("新闻")]}) as fake:
            ws.search_trending_detailed(["AI"], "", cache_ttl_seconds=0)
            ws.search_trending_detailed(["AI"], "", cache_ttl_seconds=0)

        self.assertEqual(fake.call_count, 2)
        self.assertFalse(os.path.exists(self.cache_path))

    def test_failure_not_cached(self):
        # 一次失败不应被缓存住整个 TTL，否则重试窗口内都无法恢复
        with mock.patch.object(ws, "web_search", return_value=[]):
            ws.search_trending_detailed(["AI"], "", cache_ttl_seconds=3600)

        self.assertFalse(os.path.exists(self.cache_path))

    def test_corrupt_cache_file_degrades_silently(self):
        with open(self.cache_path, "w", encoding="utf-8") as f:
            f.write("{ not valid json")

        with self._patch_web_search({"AI": [_result("新闻")]}):
            detail = ws.search_trending_detailed(["AI"], "", cache_ttl_seconds=3600)

        self.assertTrue(detail["ok"])


class TestAggregatorFilter(unittest.TestCase):
    def test_aggregator_patterns_detected(self):
        for title in ["新浪科技首页", "AI 最新资讯", "芯片行业周报", "机器人日报导航"]:
            self.assertTrue(ws.is_aggregator(title), msg=f"应判为聚合页: {title}")

    def test_specific_date_not_aggregator(self):
        # 含具体日期的「日报」仍有新闻价值，不应被过滤
        self.assertFalse(ws.is_aggregator("机器人行业日报 (08.31)"))
        self.assertFalse(ws.is_aggregator("2026 年中芯片行业深度观察"))

    def test_normal_news_not_aggregator(self):
        self.assertFalse(ws.is_aggregator("黄仁勋：AI 已迈过商业化拐点"))
        self.assertFalse(ws.is_aggregator("英伟达发布新一代 GPU"))

    def test_empty_title_not_aggregator(self):
        self.assertFalse(ws.is_aggregator(""))


class TestBuildQuery(unittest.TestCase):
    def test_default_no_domain(self):
        self.assertEqual(ws.build_query("AI", "最新消息"), "AI 最新消息")

    def test_domain_added_when_requested(self):
        self.assertEqual(ws.build_query("AI", "最新消息", "科技", True), "科技 AI 最新消息")

    def test_domain_skipped_when_disabled(self):
        self.assertEqual(ws.build_query("AI", "最新消息", "科技", False), "AI 最新消息")


class TestSuffixChain(SearchTrendingTestCase):
    def test_first_suffix_ok_uses_it(self):
        def fake(query, max_results=5):
            # 首后缀即有效，不应触发重试
            return [_result("AI 真新闻")]

        with mock.patch.object(ws, "web_search", side_effect=fake) as f:
            detail = ws.search_trending_detailed(
                ["AI"], "", cache_ttl_seconds=0,
                suffixes=["最新消息", "行业动态", "新闻"],
            )
        self.assertTrue(detail["ok"])
        self.assertIn("AI 真新闻", detail["text"])
        self.assertEqual(f.call_count, 1)

    def test_retries_only_bad_keyword(self):
        def fake(query, max_results=5):
            if "AI" in query and "最新消息" in query:
                return [_result("AI 真新闻")]
            if "芯片" in query and "最新消息" in query:
                return [_result("芯片 最新资讯")]  # 聚合页
            if "芯片" in query and "行业动态" in query:
                return [_result("芯片 真新闻")]
            return []

        with mock.patch.object(ws, "web_search", side_effect=fake) as f:
            detail = ws.search_trending_detailed(
                ["AI", "芯片"], "", cache_ttl_seconds=0,
                suffixes=["最新消息", "行业动态", "新闻"],
            )
        self.assertIn("AI 真新闻", detail["text"])
        self.assertIn("芯片 真新闻", detail["text"])
        self.assertNotIn("最新资讯", detail["text"])
        # AI 命中首后缀(1 次)，芯片 触发 1 次重试(共 2 次) → 合计 3
        self.assertEqual(f.call_count, 3)

    def test_all_aggregators_falls_back_nonempty(self):
        def fake(query, max_results=5):
            return [_result("门户首页资讯")]  # 永为聚合页

        with mock.patch.object(ws, "web_search", side_effect=fake):
            detail = ws.search_trending_detailed(
                ["AI"], "", cache_ttl_seconds=0,
                suffixes=["最新消息", "行业动态", "新闻"],
            )
        # 过滤后为空 → 回退未过滤结果，绝不返回空文本
        self.assertTrue(detail["ok"])
        self.assertIn("门户首页资讯", detail["text"])

    def test_filter_disabled_keeps_aggregators(self):
        def fake(query, max_results=5):
            return [_result("门户首页资讯")]

        with mock.patch.object(ws, "web_search", side_effect=fake):
            detail = ws.search_trending_detailed(
                ["AI"], "", cache_ttl_seconds=0, filter_aggregator=False,
            )
        self.assertIn("门户首页资讯", detail["text"])


class TestLegacyContract(SearchTrendingTestCase):
    def test_search_trending_still_returns_str(self):
        with self._patch_web_search({"AI": [_result("新闻")]}):
            text = ws.search_trending(["AI"], "科技")

        self.assertIsInstance(text, str)
        self.assertIn("新闻", text)

    def test_search_trending_signature_unchanged(self):
        import inspect

        sig = inspect.signature(ws.search_trending)
        self.assertEqual(list(sig.parameters), ["keywords", "domain"])
        self.assertEqual(sig.parameters["domain"].default, "")


if __name__ == "__main__":
    unittest.main()
