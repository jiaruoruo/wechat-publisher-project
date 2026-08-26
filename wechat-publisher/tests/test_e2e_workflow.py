"""端到端工作流测试 - 需要 langgraph 等运行时依赖（未安装时自动跳过）

用桩 LLM / 桩网络 / 桩浏览器跑完整的 LangGraph 工作流，验证：
- retry_counter 节点：审核失败 → 计数 → 回退重写循环，max_retries 强制进入排版
- 审核通过 / 部分重试等分支的路由
- json_mode 是否真的向 LLM 传了 response_format（bind）
- stream（progress_callback）与 invoke 两种路径下最终状态的完整性

运行：python -m unittest tests.e2e_workflow -v
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import langgraph  # noqa: F401
    DEPS_AVAILABLE = True
except ImportError:
    DEPS_AVAILABLE = False

if not DEPS_AVAILABLE:
    # 跳过行为可感知：无论 unittest discover 还是 pytest 运行，
    # 均在结果输出中给出明确可见的警告（详见 tests/README.md）
    print(
        "WARNING: langgraph 等运行时依赖未安装，test_e2e_workflow.py 的全部端到端用例将被跳过（skip）",
        file=sys.stderr,
    )

if DEPS_AVAILABLE:
    import models.llm_router as lr_mod
    import models.image_model as im_mod
    import tools.web_search as ws_mod
    import agents.publisher as pub_mod
    from models.llm_router import load_config
    from graph.workflow import ArticleWorkflow


# ── 桩组件 ─────────────────────────────────────────────

class FakeResponse:
    def __init__(self, text: str):
        self.content = text


class FakeLLM:
    """记录调用次数与 json_mode（bind response_format）使用情况

    responder 通过 holder["responder"] 间接引用，保证每次 invoke
    都使用当前 scenario 的响应函数（跨用例复用 llm 缓存时不会串）。
    """

    def __init__(self, agent_name: str, holder: dict):
        self.agent_name = agent_name
        self.holder = holder
        self.invoke_count = 0
        self.json_mode_requested = False

    def bind(self, **kwargs):
        if kwargs.get("response_format") == {"type": "json_object"}:
            self.json_mode_requested = True
        return self

    def invoke(self, messages, **kwargs):
        self.invoke_count += 1
        prompt = ""
        for m in messages:
            if getattr(m, "type", "") == "human":
                prompt = m.content
        text = self.holder["responder"](self.agent_name, prompt)
        return FakeResponse(text)


class Scenario:
    """按序消费审核结果（最后一次结果重复使用）"""

    def __init__(self, review_results: list[bool]):
        self.results = list(review_results)
        self.review_calls = 0

    def responder(self, agent_name: str, prompt: str) -> str:
        if agent_name == "topic_planner":
            return json.dumps({
                "topic": "AI 大模型新趋势",
                "outline": "1. 引言\n2. 正文\n3. 总结",
                "target_audience": "科技爱好者",
                "article_title": "2026 年 AI 大模型十大趋势",
                "summary": "深度解析最新趋势",
                "cover_prompt": "AI concept art",
            }, ensure_ascii=False)
        if agent_name == "content_writer":
            return "## 引言\n\n正文内容……\n\n[IMAGE: 数据图表]\n\n## 总结\n"
        if agent_name == "image_generator":
            return "A professional illustration about AI technology"
        if agent_name == "reviewer":
            self.review_calls += 1
            idx = min(self.review_calls - 1, len(self.results) - 1)
            passed = self.results[idx]
            return json.dumps({
                "passed": passed,
                "score": 8 if passed else 4,
                "feedback": "内容优质" if passed else "请补充数据来源",
            }, ensure_ascii=False)
        if agent_name == "formatter":
            return "<section><h2>引言</h2><p>正文</p></section>"
        return ""


def _install_stubs() -> tuple[dict, dict]:
    """安装桩：LLM、图片生成、网络搜索、浏览器/发布"""
    llms: dict[str, FakeLLM] = {}
    holder: dict = {"responder": None}

    def fake_get_llm(self, agent_name: str):
        if agent_name not in llms:
            llms[agent_name] = FakeLLM(agent_name, holder)
        return llms[agent_name]

    lr_mod.LLMRouter.get_llm = fake_get_llm

    def fake_generate(self, prompt: str, size: str = "", output_name: str | None = None):
        return os.path.join("storage", "images", f"{output_name or 'img'}.png")

    im_mod.ImageGenerator.generate = fake_generate
    ws_mod.search_trending = lambda keywords, domain="": "热点摘要：AI 大模型最新进展。"

    class StubSession:
        def __init__(self, config):
            self.page = None

        def start(self):
            return None

        def check_login(self):
            return True

        def take_screenshot(self, name=""):
            return ""

        def login_interactive(self, timeout=120):
            pass

        def close(self):
            pass

    class StubActions:
        def __init__(self, page, config):
            pass

        def execute_publish(self, **kwargs):
            return {"success": True, "mode": "draft", "preview_path": "", "error": ""}

    pub_mod.WechatSession = StubSession
    pub_mod.PublishActions = StubActions

    return llms, holder


def _make_config(max_retries: int = 2) -> dict:
    config = load_config()
    config.setdefault("storage", {})["db_path"] = os.path.join(tempfile.mkdtemp(), "e2e.db")
    config.setdefault("notification", {})["enabled"] = False
    config.setdefault("review", {})["max_retries"] = max_retries
    return config


# ── 测试用例 ───────────────────────────────────────────

@unittest.skipUnless(DEPS_AVAILABLE, "需要 langgraph 等运行时依赖")
class TestWorkflowRetry(unittest.TestCase):
    """验证 retry_counter 节点与重试路由"""

    @classmethod
    def setUpClass(cls):
        cls.llms, cls.holder = _install_stubs()

    def _run(self, review_results, use_callback=True):
        scenario = Scenario(review_results)
        self.holder["responder"] = scenario.responder
        workflow = ArticleWorkflow(_make_config())
        order = []
        result = workflow.run(
            progress_callback=(lambda n, s: order.append(n)) if use_callback else None,
        )
        return result, order, scenario

    def test_always_fail_hits_max_retries(self):
        result, order, _ = self._run([False])
        # max_retries=2：第三次审核仍失败 → 强制进入排版，retry_count=2
        self.assertEqual(result["retry_count"], 2)
        self.assertFalse(result["review_result"]["passed"])
        # review_history 为顶层通道（schema 级 append 归并），三次审核三条记录
        self.assertEqual(len(result["review_history"]), 3)
        self.assertEqual(order.count("retry_counter"), 2)
        self.assertEqual(order.count("content_writer"), 3)
        # retry_counter 之后必须回到 content_writer（回退重写）
        for i, node in enumerate(order):
            if node == "retry_counter":
                self.assertEqual(order[i + 1], "content_writer")
        # 最终进入排版与发布
        self.assertEqual(order[-2:], ["formatter", "publisher"])

    def test_always_pass_no_retry(self):
        result, order, _ = self._run([True])
        self.assertEqual(result["retry_count"], 0)
        self.assertTrue(result["review_result"]["passed"])
        self.assertNotIn("retry_counter", order)
        self.assertEqual(order.count("content_writer"), 1)

    def test_pass_after_one_retry(self):
        result, order, _ = self._run([False, True])
        self.assertEqual(result["retry_count"], 1)
        self.assertTrue(result["review_result"]["passed"])
        self.assertEqual(order.count("retry_counter"), 1)
        self.assertEqual(order.count("content_writer"), 2)
        self.assertEqual(len(result["review_history"]), 2)

    def test_invoke_path_final_state_complete(self):
        # 无回调走 invoke()：最终状态必须完整（含归并后的 metadata/review_history）
        result, _, _ = self._run([False], use_callback=False)
        self.assertEqual(result["article_title"], "2026 年 AI 大模型十大趋势")
        self.assertEqual(result["retry_count"], 2)
        self.assertEqual(result["publish_result"]["success"], True)
        # metadata 归并：初始 max_retries + 各节点部分更新均保留
        self.assertEqual(result["metadata"]["max_retries"], 2)
        self.assertIn("topic_created_at", result["metadata"])
        self.assertIn("reviewed_at", result["metadata"])
        self.assertIn("formatted_at", result["metadata"])
        self.assertIn("published_at", result["metadata"])
        self.assertEqual(len(result["review_history"]), 3)

    def test_stream_path_final_state_complete(self):
        # 有回调走 stream()：累积后的最终状态必须完整（此前回归点），
        # 且 metadata/review_history 走与 schema 一致的归并，不被部分更新覆盖
        result, order, _ = self._run([True], use_callback=True)
        self.assertEqual(result["article_title"], "2026 年 AI 大模型十大趋势")
        self.assertEqual(result["summary"], "深度解析最新趋势")
        self.assertEqual(result["publish_result"]["success"], True)
        self.assertEqual(result["metadata"]["max_retries"], 2)
        self.assertIn("reviewed_at", result["metadata"])
        self.assertEqual(len(result["review_history"]), 1)
        # 回调收到完整状态：topic_planner 阶段即可见标题
        self.assertTrue(order)

    def test_high_max_retries_explicit_recursion_limit(self):
        # max_retries=6 → 节点数 6+4*6=30；workflow.run 显式按节点数计算
        # recursion_limit（2 倍余量 = 60）传入，高重试仍应完整跑完
        scenario = Scenario([False])
        self.holder["responder"] = scenario.responder
        workflow = ArticleWorkflow(_make_config(max_retries=6))
        result = workflow.run()
        self.assertEqual(result["retry_count"], 6)
        self.assertEqual(len(result["review_history"]), 7)  # 第 1 次 + 6 轮回退
        self.assertEqual(result["publish_result"]["success"], True)

    def test_high_max_retries_stream_path(self):
        # 回调（stream）路径同样显式传入 recursion_limit
        scenario = Scenario([False])
        self.holder["responder"] = scenario.responder
        workflow = ArticleWorkflow(_make_config(max_retries=5))
        result = workflow.run(progress_callback=lambda n, s: None)
        self.assertEqual(result["retry_count"], 5)
        self.assertEqual(len(result["review_history"]), 6)
        self.assertEqual(result["publish_result"]["success"], True)

    def test_review_history_persisted_to_db_metadata(self):
        # review_history 为顶层通道，但落库时应重新嵌入 articles.metadata（旧版行为）
        import json
        import sqlite3

        scenario = Scenario([False])
        self.holder["responder"] = scenario.responder
        config = _make_config(max_retries=2)
        workflow = ArticleWorkflow(config)
        result = workflow.run()
        self.assertEqual(len(result["review_history"]), 3)

        conn = sqlite3.connect(config["storage"]["db_path"])
        try:
            row = conn.execute("SELECT metadata FROM articles ORDER BY id DESC LIMIT 1").fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        meta = json.loads(row[0])
        self.assertEqual(len(meta["review_history"]), 3)
        self.assertEqual(meta["review_history"][0]["retry"], 0)
        self.assertEqual(meta["review_history"][-1]["passed"], False)

    def test_recursion_limit_guards_runaway_loop(self):
        # 显式 recursion_limit 的另一价值：失控循环快速失败。
        # 模拟条件路由 bug（永远 rejected 且 max_retries 巨大）→ 节点数超出
        # 计算出的限制时应抛 GraphRecursionError，而不是无限执行。
        scenario = Scenario([False])
        self.holder["responder"] = scenario.responder
        from graph.workflow import _recursion_limit
        from graph.state import create_initial_state
        workflow = ArticleWorkflow(_make_config(max_retries=3))
        limit = _recursion_limit(3)  # max(25, 2*(6+12)) = 36
        state = create_initial_state(max_retries=3)
        # 把限制压到远小于正常完成所需节点数（6+4*3=18），必然提前触顶
        with self.assertRaises(Exception) as ctx:
            workflow.app.invoke(state, config={"recursion_limit": 10})
        self.assertIn("Recursion limit", str(ctx.exception))


@unittest.skipUnless(DEPS_AVAILABLE, "需要 langgraph 等运行时依赖")
class TestJsonMode(unittest.TestCase):
    """验证 json_mode 确实向 LLM 传入 response_format"""

    @classmethod
    def setUpClass(cls):
        cls.llms, cls.holder = _install_stubs()

    def test_json_mode_sent_only_to_json_contract_agents(self):
        scenario = Scenario([True])
        self.holder["responder"] = scenario.responder
        ArticleWorkflow(_make_config()).run()
        # JSON 契约的 agent 必须请求 json_mode
        self.assertTrue(self.llms["topic_planner"].json_mode_requested)
        self.assertTrue(self.llms["reviewer"].json_mode_requested)
        # 非 JSON 契约的 agent 不应请求
        self.assertFalse(self.llms["content_writer"].json_mode_requested)
        self.assertFalse(self.llms["formatter"].json_mode_requested)


if __name__ == "__main__":
    unittest.main()
