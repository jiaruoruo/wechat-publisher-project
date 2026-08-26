"""LangGraph 主工作流定义 - 编排所有 Agent 的协作流程"""

import logging
from collections.abc import Callable

from langgraph.graph import StateGraph, START, END

from graph.state import ArticleState, create_initial_state, CHANNEL_REDUCERS
from graph.conditions import review_condition, increment_retry
from models.llm_router import LLMRouter, load_config
from agents.topic_planner import TopicPlannerAgent
from agents.content_writer import ContentWriterAgent
from agents.image_generator import ImageGeneratorAgent
from agents.reviewer import ReviewerAgent
from agents.formatter import FormatterAgent
from agents.publisher import PublisherAgent
from tools.content_db import ContentDB
from tools.notifier import Notifier
from tools.publish_lock import acquire_publish_lock
from config.structured_logging import workflow_event

logger = logging.getLogger(__name__)

# LLMRouter 进程级缓存：每次 /start 都 new ArticleWorkflow 会重建 LLMRouter
# （含各 Agent 的 LLM 客户端）。同一 config 下复用，减少初始化开销。
# key 用 config 对象 id（同一进程内 config 不变则命中）。
_LLM_ROUTER_CACHE: dict[int, "LLMRouter"] = {}

# ── 递归限制计算 ────────────────────────────────────────
# 快乐路径 6 节点：topic_planner → content_writer → image_generator → reviewer → formatter → publisher
_HAPPY_PATH_NODES = 6
# 每轮回退 4 节点：retry_counter → content_writer → image_generator → reviewer
_RETRY_ROUND_NODES = 4


def _recursion_limit(max_retries: int) -> int:
    """根据最大重试次数计算显式 recursion_limit

    说明：langgraph 1.2 的默认 recursion_limit 为 10007
    （langgraph._internal._config，可用 LANGGRAPH_DEFAULT_RECURSION_LIMIT 覆盖），
    常规 max_retries 下不会触顶；这里仍显式传入按节点数推算的限制，理由：
    1) 资源上限确定化，不依赖环境变量/版本默认值；
    2) 出现失控循环 bug 时快速失败（~2 倍节点数），避免烧掉上万次真实 LLM 调用。
    按 2 倍预留图结构演进空间，并以 25 兜底。
    """
    return max(25, 2 * (_HAPPY_PATH_NODES + _RETRY_ROUND_NODES * max_retries))


class ArticleWorkflow:
    """文章生产工作流 - 编排 6 个 Agent 的协作"""

    def __init__(self, config: dict | None = None):
        self.config = config or load_config()
        # 复用进程级 LLMRouter（同一 config 对象命中缓存），降低重复初始化的开销
        cfg_id = id(self.config)
        router = _LLM_ROUTER_CACHE.get(cfg_id)
        if router is None:
            router = LLMRouter(self.config)
            _LLM_ROUTER_CACHE[cfg_id] = router
        self.llm_router = router

        # 初始化所有 Agent
        self.topic_planner = TopicPlannerAgent(self.llm_router, self.config)
        self.content_writer = ContentWriterAgent(self.llm_router, self.config)
        self.image_generator = ImageGeneratorAgent(self.llm_router, self.config)
        self.reviewer = ReviewerAgent(self.llm_router, self.config)
        self.formatter = FormatterAgent(self.llm_router, self.config)
        self.publisher = PublisherAgent(self.llm_router, self.config)

        # 内容数据库
        self.db = ContentDB(self.config.get("storage", {}).get("db_path"))

        # 通知器
        self.notifier = Notifier(self.config)

        # 构建工作流
        self.app = self._build_workflow()

    def _build_workflow(self):
        """构建 LangGraph StateGraph 工作流"""
        workflow = StateGraph(ArticleState)

        # 添加节点
        workflow.add_node("topic_planner", self.topic_planner)
        workflow.add_node("content_writer", self.content_writer)
        workflow.add_node("image_generator", self.image_generator)
        workflow.add_node("reviewer", self.reviewer)
        workflow.add_node("retry_counter", increment_retry)  # 重试计数集中在 workflow 层
        workflow.add_node("formatter", self.formatter)
        workflow.add_node("publisher", self.publisher)

        # 添加边
        workflow.add_edge(START, "topic_planner")
        workflow.add_edge("topic_planner", "content_writer")
        workflow.add_edge("content_writer", "image_generator")
        workflow.add_edge("image_generator", "reviewer")

        # 审核条件分支：不通过且未达上限 → 先计数再回退重写
        workflow.add_conditional_edges(
            "reviewer",
            review_condition,
            {
                "approved": "formatter",
                "rejected": "retry_counter",
                "max_retries": "formatter",
            },
        )
        workflow.add_edge("retry_counter", "content_writer")

        workflow.add_edge("formatter", "publisher")
        workflow.add_edge("publisher", END)

        return workflow.compile()

    def _publish_lock_timeout(self) -> float:
        """读取发布锁等待超时（wechat.publish_lock_timeout，秒）；非法值兜底为 0"""
        raw = self.config.get("wechat", {}).get("publish_lock_timeout", 0)
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            logger.warning(f"wechat.publish_lock_timeout 配置非法（{raw!r}），按 0（立即跳过）处理")
            return 0.0

    def run(
        self,
        initial_state: dict | None = None,
        progress_callback: Callable | None = None,
    ) -> ArticleState:
        """
        执行完整工作流

        Args:
            initial_state: 初始状态（可覆盖自动选题）
            progress_callback: 可选进度回调，每完成一个节点时调用 callback(node_name, state)

        Returns:
            最终状态
        """
        # 设置初始状态（统一走 create_initial_state，保证 retry_count/metadata 等默认值齐全）
        max_retries = int(self.config.get("review", {}).get("max_retries", 2) or 2)
        # 显式传入 recursion_limit：重试循环节点数会随 max_retries 增长，
        # 超过 langgraph 默认上限(25)会抛 GraphRecursionError（见 _recursion_limit）
        run_config = {"recursion_limit": _recursion_limit(max_retries)}
        state = create_initial_state(max_retries=max_retries, extra=initial_state)

        # 跨进程发布锁：Hermes /start、scheduler 定时任务、CLI run 共用同一浏览器
        # 会话，避免并发操作同一文章/会话。锁被占用时最多等待
        # wechat.publish_lock_timeout 秒，超时才跳过（0 = 立即跳过）。
        lock_timeout = self._publish_lock_timeout()
        lock = acquire_publish_lock(timeout=lock_timeout)
        # 无论成功/失败/跳过都关闭数据库连接（ContentDB 为 thread-local 持久连接）
        try:
            if lock is None:
                logger.warning(
                    f"发布锁在 {lock_timeout:.0f} 秒内未能取得（可能有发布任务正在进行），"
                    "跳过本次执行"
                )
                state["publish_result"] = {
                    "success": False,
                    "mode": self.config.get("wechat", {}).get("publish_mode", "draft"),
                    "error": f"发布锁等待 {lock_timeout:.0f} 秒后仍被占用，跳过本次执行",
                }
                return state
            try:
                return self._execute_workflow(state, progress_callback, run_config)
            finally:
                lock.release()
        finally:
            self.db.close()

    def _execute_workflow(
        self,
        state: ArticleState,
        progress_callback: Callable | None,
        run_config: dict,
    ) -> ArticleState:
        """执行工作流主体（含保存、通知、日志）"""
        logger.info("=" * 60)
        logger.info("开始执行文章生产工作流")
        logger.info("=" * 60)

        # 执行工作流（有回调时使用 stream 模式获取节点事件）
        if progress_callback:
            # 注意：stream() 默认 stream_mode="updates"，每个 chunk 是各节点返回的
            # "部分更新"，不是合并后的完整状态。这里手动累积为完整状态，
            # 否则最终状态只剩最后一个节点（publisher）返回的字段，
            # 会导致落库空记录、日志/结果卡片缺数据。
            full_state: dict = dict(state)
            for event in self.app.stream(state, config=run_config):
                for node_name, node_state in event.items():
                    if node_state:
                        # 与 schema 注解使用同一套归并器（metadata/review_history 合并/追加），
                        # 其余键直接覆盖；langgraph 1.x 对未声明键的事件值为 None，已跳过
                        for key, update in node_state.items():
                            reducer = CHANNEL_REDUCERS.get(key)
                            full_state[key] = reducer(full_state.get(key), update) if reducer else update
                    try:
                        progress_callback(node_name, full_state)
                    except Exception as e:
                        logger.warning(f"进度回调失败 [{node_name}]: {e}")
            final_state = full_state
        else:
            final_state = self.app.invoke(state, config=run_config)

        # 工作流完成后保存到数据库
        self._save_result(final_state)

        # 检查审核是否达到最大重试次数
        review_result = final_state.get("review_result", {})
        retry_count = final_state.get("retry_count", 0)
        max_retries = self.config.get("review", {}).get("max_retries", 2)
        if not review_result.get("passed", True) and retry_count >= max_retries:
            self.notifier.notify_review_failed(
                article_title=final_state.get("article_title", ""),
                retry_count=retry_count,
                feedback=review_result.get("feedback", ""),
            )

        logger.info("=" * 60)
        logger.info("工作流执行完成")
        workflow_event(
            "complete",
            article_title=final_state.get("article_title", "N/A"),
            score=final_state.get("review_result", {}).get("score"),
            retries=final_state.get("retry_count", 0),
            success=final_state.get("publish_result", {}).get("success"),
        )
        self._log_summary(final_state)
        logger.info("=" * 60)

        return final_state

    def _save_result(self, state: ArticleState):
        """保存文章结果到数据库"""
        try:
            # review_history 已提升为顶层通道（schema 归并），落库时重新嵌入
            # articles.metadata，保持与旧版（review_history 嵌在 metadata 内）一致的记录形态
            metadata = dict(state.get("metadata", {}))
            if state.get("review_history"):
                metadata["review_history"] = state["review_history"]

            article_id = self.db.save_article(
                title=state.get("article_title", ""),
                topic=state.get("topic", ""),
                summary=state.get("summary", ""),
                content=state.get("content", ""),
                metadata=metadata,
            )

            # 更新发布状态
            publish_result = state.get("publish_result", {})
            if publish_result.get("success"):
                self.db.update_article_status(
                    article_id,
                    "published" if publish_result.get("mode") == "publish" else "draft",
                )

            # 保存审核记录
            review_result = state.get("review_result", {})
            if review_result:
                self.db.save_review_log(
                    article_id,
                    review_result,
                    state.get("retry_count", 0),
                )

            logger.info(f"文章已保存到数据库: id={article_id}")
        except Exception as e:
            logger.error(f"保存文章到数据库失败: {e}")

    def _log_summary(self, state: ArticleState):
        """输出工作流执行摘要"""
        publish_result = state.get("publish_result", {})
        review_result = state.get("review_result", {})

        logger.info(f"文章标题: {state.get('article_title', 'N/A')}")
        logger.info(f"选题: {state.get('topic', 'N/A')}")
        logger.info(f"审核评分: {review_result.get('score', 'N/A')}/10")
        logger.info(f"审核通过: {review_result.get('passed', 'N/A')}")
        logger.info(f"重写次数: {state.get('retry_count', 0)}")
        logger.info(f"发布模式: {publish_result.get('mode', 'N/A')}")
        logger.info(f"发布成功: {publish_result.get('success', 'N/A')}")

        if publish_result.get("error"):
            logger.info(f"发布错误: {publish_result['error']}")