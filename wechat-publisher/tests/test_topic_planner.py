"""选题策划 Agent 单测 - 桩掉 LLM、网络搜索，数据库指向临时目录

覆盖：指定选题跳过搜索、解析失败降级、LLM 异常重试、去重硬校验与重选、
指定选题不去重、cover_prompt 派生、逐字段兜底、配置钳制、retry_count 不再写入。
"""

import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.topic_planner import TopicPlannerAgent, resolve_planning_config
from tools.content_db import ContentDB

# 历史文章里的既有标题，用于构造「重复选题」场景
EXISTING_TITLE = "2026 年 AI 大模型十大趋势解读"


class StubLLM:
    """按序返回预设响应；响应为 Exception 实例时抛出，用于模拟调用失败"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.call_count = 0
        self.prompts = []

    def bind(self, **kwargs):
        return self

    def invoke(self, messages, **kwargs):
        self.call_count += 1
        prompt = ""
        for m in messages:
            if getattr(m, "type", "") == "human":
                prompt = m.content
        self.prompts.append(prompt)
        if not self.responses:
            return types.SimpleNamespace(content="")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return types.SimpleNamespace(content=item)


class StubRouter:
    def __init__(self, llm):
        self._llm = llm
        self.config = {}

    def get_llm(self, agent_name):
        return self._llm


def topic_payload(**overrides):
    payload = {
        "topic": "AI 大模型新趋势",
        "outline": "1. 引言\n2. 正文\n3. 总结",
        "target_audience": "科技爱好者",
        "article_title": "2026 年 AI 大模型新趋势",
        "summary": "深度解析最新趋势",
        "cover_prompt": "AI concept art",
    }
    payload.update(overrides)
    return payload


class TopicPlannerTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="topic_test_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db_path = os.path.join(self.tmp, "articles.db")

        # 全局禁用热点搜索，除非用例显式启用（杜绝真实联网）
        patcher = mock.patch(
            "tools.web_search.search_trending_detailed",
            return_value={"text": "热点摘要：AI 大模型最新进展。", "sources": ["https://a"], "ok": True},
        )
        self.trending_mock = patcher.start()
        self.addCleanup(patcher.stop)

    # ── 构造工具 ────────────────────────────────────────────

    def make_agent(self, responses, planning=None, keywords=None):
        content = {
            "domain": "科技",
            "keywords": keywords if keywords is not None else ["AI", "人工智能"],
            "style": "专业但通俗易懂",
        }
        if planning is not None:
            content["topic_planning"] = planning

        config = {
            "content": content,
            "storage": {"db_path": self.db_path},
        }
        llm = StubLLM(responses)
        agent = TopicPlannerAgent(StubRouter(llm), config)
        return agent, llm

    def seed_history(self, title=EXISTING_TITLE, topic="AI 大模型趋势"):
        db = ContentDB(self.db_path)
        try:
            db.save_article(title=title, topic=topic, summary="", content="正文")
        finally:
            db.close()

    @staticmethod
    def respond(payload=None, raw=None):
        if raw is not None:
            return raw
        return json.dumps(payload if payload is not None else topic_payload(), ensure_ascii=False)


class TestSpecifiedTopic(TopicPlannerTestCase):
    def test_skips_trending_search(self):
        # 指定选题：跳过搜索引擎；mock 多源采集使 trending_ok 取证为空集，断言稳定
        with mock.patch("tools.sources_feed.collect_and_grade", return_value=[]):
            agent, _ = self.make_agent([self.respond()])
            result = agent.run({"topic": "量子计算入门"})

        self.trending_mock.assert_not_called()
        self.assertTrue(result["metadata"]["trending_skipped"])
        self.assertFalse(result["metadata"]["trending_ok"])

    def test_auto_topic_calls_trending_search(self):
        agent, _ = self.make_agent([self.respond()])
        result = agent.run({})

        self.trending_mock.assert_called_once()
        self.assertTrue(result["metadata"]["trending_ok"])
        self.assertFalse(result["metadata"]["trending_skipped"])

    def test_specified_topic_wins_over_llm_output(self):
        agent, _ = self.make_agent([self.respond()])
        result = agent.run({"topic": "量子计算入门"})

        self.assertEqual(result["topic"], "量子计算入门")
        self.assertIn("量子计算入门", result["article_title"])

    def test_specified_topic_skips_dedup_even_when_duplicate(self):
        # 指定选题是用户显式意图，去重会直接违背指令
        self.seed_history()
        agent, llm = self.make_agent([self.respond(topic_payload(article_title=EXISTING_TITLE))])
        result = agent.run({"topic": EXISTING_TITLE})

        self.assertFalse(result["metadata"]["dedup_checked"])
        self.assertNotIn("dedup_warning", result["metadata"])
        self.assertIn("指定选题", result["metadata"]["dedup_skipped_reason"])
        # 只调用一次，没有触发重选
        self.assertEqual(llm.call_count, 1)

    def test_skip_search_disabled_by_config(self):
        agent, _ = self.make_agent([self.respond()], planning={"skip_search_when_specified": False})
        agent.run({"topic": "量子计算入门"})

        self.trending_mock.assert_called_once()


class TestDegradation(TopicPlannerTestCase):
    def test_unparseable_response_degrades(self):
        agent, _ = self.make_agent(["这不是 JSON，只是一段闲聊"])
        result = agent.run({})

        self.assertTrue(result["metadata"]["topic_degraded"])
        self.assertIn("缺少必需字段", result["metadata"]["topic_degraded_reason"])
        # 降级不应把占位垃圾塞进 outline
        self.assertEqual(result["outline"], "")

    def test_missing_outline_degrades(self):
        agent, _ = self.make_agent([self.respond(raw=json.dumps({"topic": "某选题"}, ensure_ascii=False))])
        result = agent.run({})

        self.assertTrue(result["metadata"]["topic_degraded"])

    def test_llm_exception_retries_then_degrades(self):
        agent, llm = self.make_agent([RuntimeError("timeout"), RuntimeError("timeout again")])
        result = agent.run({})

        self.assertEqual(llm.call_count, 2)
        self.assertTrue(result["metadata"]["topic_degraded"])
        self.assertIn("连续失败", result["metadata"]["topic_degraded_reason"])

    def test_llm_recovers_on_retry(self):
        agent, llm = self.make_agent([RuntimeError("timeout"), self.respond()])
        result = agent.run({})

        self.assertEqual(llm.call_count, 2)
        self.assertFalse(result["metadata"].get("topic_degraded", False))
        self.assertEqual(result["topic"], "AI 大模型新趋势")

    def test_degraded_does_not_write_retry_count(self):
        agent, _ = self.make_agent(["bad response"])
        result = agent.run({})

        self.assertNotIn("retry_count", result)


class TestDedupReselection(TopicPlannerTestCase):
    # 与历史完全无关的选题：topic 与 article_title 都必须改，
    # 因为去重对两个字段都会比对（只改标题会被 topic 命中）
    FRESH = {"topic": "家常凉菜做法大全", "article_title": "夏日家常凉菜的十种做法"}

    def test_duplicate_triggers_one_reselection(self):
        self.seed_history()
        agent, llm = self.make_agent([
            self.respond(topic_payload(article_title=EXISTING_TITLE)),
            self.respond(topic_payload(article_title=EXISTING_TITLE)),
        ])
        result = agent.run({})

        self.assertEqual(llm.call_count, 2)
        self.assertEqual(result["metadata"]["topic_attempts"], 2)
        self.assertTrue(result["metadata"]["dedup_checked"])

    def test_still_duplicate_only_warns(self):
        self.seed_history()
        agent, _ = self.make_agent([
            self.respond(topic_payload(article_title=EXISTING_TITLE)),
            self.respond(topic_payload(article_title=EXISTING_TITLE)),
        ])
        result = agent.run({})

        warning = result["metadata"].get("dedup_warning")
        self.assertIsNotNone(warning)
        self.assertAlmostEqual(warning["score"], 1.0, places=2)
        self.assertEqual(warning["matched"], EXISTING_TITLE)
        # 只告警，不降级、不阻塞
        self.assertFalse(result["metadata"].get("topic_degraded", False))

    def test_reselection_succeeds_clears_warning(self):
        self.seed_history()
        agent, _ = self.make_agent([
            self.respond(topic_payload(article_title=EXISTING_TITLE)),
            self.respond(topic_payload(**self.FRESH)),
        ])
        result = agent.run({})

        self.assertEqual(result["metadata"]["topic_attempts"], 2)
        self.assertNotIn("dedup_warning", result["metadata"])
        self.assertEqual(result["article_title"], self.FRESH["article_title"])

    def test_avoid_clause_injected_on_reselection(self):
        self.seed_history()
        agent, llm = self.make_agent([
            self.respond(topic_payload(article_title=EXISTING_TITLE)),
            self.respond(topic_payload(**self.FRESH)),
        ])
        agent.run({})

        self.assertIn("重要：避免重复", llm.prompts[1])
        self.assertNotIn("重要：避免重复", llm.prompts[0])

    def test_non_duplicate_topic_calls_llm_once(self):
        self.seed_history()
        agent, llm = self.make_agent([self.respond(topic_payload(**self.FRESH))])
        result = agent.run({})

        self.assertEqual(llm.call_count, 1)
        self.assertNotIn("dedup_warning", result["metadata"])

    def test_no_history_never_duplicate(self):
        agent, llm = self.make_agent([self.respond()])
        result = agent.run({})

        self.assertEqual(llm.call_count, 1)
        self.assertNotIn("dedup_warning", result["metadata"])


class TestFieldCoercion(TopicPlannerTestCase):
    def test_missing_optional_fields_filled_not_degraded(self):
        minimal = {"topic": "某新选题", "outline": "1. 引言\n2. 正文"}
        agent, _ = self.make_agent([self.respond(raw=json.dumps(minimal, ensure_ascii=False))])
        result = agent.run({})

        self.assertFalse(result["metadata"].get("topic_degraded", False))
        self.assertEqual(result["target_audience"], "对科技感兴趣的读者")
        self.assertTrue(result["summary"])
        self.assertTrue(result["cover_prompt"])

    def test_cover_prompt_derived_when_missing(self):
        payload = topic_payload(cover_prompt="")
        agent, _ = self.make_agent([self.respond(payload)])
        result = agent.run({})

        self.assertTrue(result["metadata"]["cover_prompt_derived"])
        self.assertTrue(result["cover_prompt"])

    def test_cover_prompt_not_derived_when_provided(self):
        agent, _ = self.make_agent([self.respond(topic_payload(cover_prompt="AI concept art"))])
        result = agent.run({})

        self.assertNotIn("cover_prompt_derived", result["metadata"])
        self.assertEqual(result["cover_prompt"], "AI concept art")

    def test_candidates_recorded_in_metadata(self):
        payload = topic_payload(candidates=[
            {"topic": "候选一", "angle": "角度一", "hook": "钩子一"},
            {"topic": "候选二", "angle": "角度二", "hook": "钩子二"},
        ])
        agent, _ = self.make_agent([self.respond(payload)])
        result = agent.run({})

        self.assertEqual(len(result["metadata"]["topic_candidates"]), 2)
        self.assertEqual(result["metadata"]["topic_candidates"][0]["topic"], "候选一")

    def test_no_retry_count_written(self):
        # retry_count 是审核回退计数，选题节点不应越权写它
        agent, _ = self.make_agent([self.respond()])
        result = agent.run({})

        self.assertNotIn("retry_count", result)

    def test_result_keys_are_declared_state_fields(self):
        agent, _ = self.make_agent([self.respond()])
        result = agent.run({})

        declared = {
            "topic", "outline", "target_audience", "article_title",
            "summary", "cover_prompt", "metadata",
        }
        self.assertTrue(set(result).issubset(declared))


class TestPlanningConfig(unittest.TestCase):
    def test_missing_section_returns_defaults(self):
        resolved = resolve_planning_config({})
        self.assertEqual(resolved["history_days"], 30)
        self.assertAlmostEqual(resolved["dedup_threshold"], 0.82)

    def test_wrong_type_falls_back_to_defaults(self):
        resolved = resolve_planning_config({"topic_planning": "not a dict"})
        self.assertEqual(resolved["history_days"], 30)

    def test_out_of_range_clamped_to_default(self):
        resolved = resolve_planning_config({"topic_planning": {
            "history_days": 0,
            "max_keywords": 999,
            "dedup_threshold": 5,
            "max_topic_attempts": -1,
        }})
        self.assertEqual(resolved["history_days"], 30)
        self.assertEqual(resolved["max_keywords"], 3)
        self.assertAlmostEqual(resolved["dedup_threshold"], 0.82)
        self.assertEqual(resolved["max_topic_attempts"], 2)

    def test_bool_not_treated_as_int(self):
        # bool 是 int 子类，True 不应被当成 1
        resolved = resolve_planning_config({"topic_planning": {"history_days": True}})
        self.assertEqual(resolved["history_days"], 30)

    def test_valid_values_preserved(self):
        resolved = resolve_planning_config({"topic_planning": {
            "history_days": 7,
            "dedup_threshold": 0.9,
            "trending_cache_ttl_seconds": 0,
        }})
        self.assertEqual(resolved["history_days"], 7)
        self.assertAlmostEqual(resolved["dedup_threshold"], 0.9)
        self.assertEqual(resolved["trending_cache_ttl_seconds"], 0)

    def test_new_search_params_defaults(self):
        resolved = resolve_planning_config({})
        self.assertEqual(resolved["query_suffixes"], ["最新消息", "行业动态", "新闻"])
        self.assertFalse(resolved["include_domain_in_query"])
        self.assertEqual(resolved["max_query_variants"], 2)
        self.assertTrue(resolved["filter_aggregator_pages"])

    def test_new_search_params_illegal_fallback(self):
        resolved = resolve_planning_config({"topic_planning": {
            "query_suffixes": "not a list",
            "include_domain_in_query": "yes",
            "max_query_variants": 0,
            "filter_aggregator_pages": "no",
        }})
        self.assertEqual(resolved["query_suffixes"], ["最新消息", "行业动态", "新闻"])
        self.assertFalse(resolved["include_domain_in_query"])
        self.assertEqual(resolved["max_query_variants"], 2)
        self.assertTrue(resolved["filter_aggregator_pages"])

    def test_new_search_params_valid_preserved(self):
        resolved = resolve_planning_config({"topic_planning": {
            "query_suffixes": ["最新", "新闻"],
            "include_domain_in_query": True,
            "max_query_variants": 3,
            "filter_aggregator_pages": False,
        }})
        self.assertEqual(resolved["query_suffixes"], ["最新", "新闻"])
        self.assertTrue(resolved["include_domain_in_query"])
        self.assertEqual(resolved["max_query_variants"], 3)
        self.assertFalse(resolved["filter_aggregator_pages"])


class TestSuggestTopics(TopicPlannerTestCase):
    """suggest_topics：只读候选产出，不写库不发布；LLM 异常须返回 []"""

    def _stub_items(self):
        return [{
            "title": "NVIDIA 发布新一代 GPU",
            "url": "https://nvidia.example/1",
            "snippet": "算力再翻倍",
            "source": "NVIDIA Blog",
            "region": "intl",
            "tier": "authoritative",
            "topics": ["ai"],
            "confidence": "high",
            "verified": True,
        }]

    def test_returns_topic_list_with_required_fields(self):
        with mock.patch("tools.sources_feed.collect_and_grade", return_value=self._stub_items()):
            payload = {"topics": [{
                "title": "GPU 新纪元",
                "angle": "从算力视角切入",
                "why": "读者关心性价比",
                "direction": "ai",
                "region": "intl",
                "evidence": "NVIDIA 发布新一代 GPU",
                "confidence": "high",
            }]}
            agent, _ = self.make_agent([json.dumps(payload, ensure_ascii=False)])
            result = agent.suggest_topics(count=12)

        self.assertEqual(len(result), 1)
        t = result[0]
        for key in ("title", "angle", "why", "direction", "region", "evidence", "confidence"):
            self.assertIn(key, t)
        self.assertEqual(t["title"], "GPU 新纪元")
        self.assertEqual(t["direction"], "ai")

    def test_duplicate_titles_deduplicated(self):
        with mock.patch("tools.sources_feed.collect_and_grade", return_value=self._stub_items()):
            payload = {"topics": [
                {"title": "同一个选题", "direction": "ai", "region": "intl"},
                {"title": "同一个选题", "direction": "chip", "region": "domestic"},
            ]}
            agent, _ = self.make_agent([json.dumps(payload, ensure_ascii=False)])
            result = agent.suggest_topics(count=12)
        self.assertEqual(len(result), 1)

    def test_llm_exception_returns_empty(self):
        with mock.patch("tools.sources_feed.collect_and_grade", return_value=self._stub_items()):
            agent, _ = self.make_agent([RuntimeError("boom")])
            result = agent.suggest_topics(count=10)
        self.assertEqual(result, [])

    def test_unparseable_response_returns_empty(self):
        with mock.patch("tools.sources_feed.collect_and_grade", return_value=self._stub_items()):
            agent, _ = self.make_agent(["不是 JSON 只是一段闲聊"])
            result = agent.suggest_topics(count=10)
        self.assertEqual(result, [])

    def test_empty_evidence_still_works(self):
        # 多源采集失败（空列表）不应炸，应回退到基于领域知识策划
        with mock.patch("tools.sources_feed.collect_and_grade", return_value=[]):
            payload = {"topics": [{"title": "常青科普选题", "direction": "ai", "region": "any"}]}
            agent, _ = self.make_agent([json.dumps(payload, ensure_ascii=False)])
            result = agent.suggest_topics(count=10)
        self.assertEqual(len(result), 1)


if __name__ == "__main__":
    unittest.main()
