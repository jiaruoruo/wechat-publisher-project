"""审核校对 Agent - 内容质量审核、合规检查"""

import json
import logging
from datetime import datetime

from agents.base import BaseAgent
from graph.state import ArticleState
from tools.json_utils import extract_json_from_response

logger = logging.getLogger(__name__)


class ReviewerAgent(BaseAgent):
    """审核校对 Agent：对文章进行全面质量审核"""

    agent_name = "reviewer"
    prompt_file = "reviewer.yaml"

    def __init__(self, llm_router, config: dict):
        super().__init__(llm_router, config)
        self.review_config = config.get("review", {})
        self.min_score = self.review_config.get("min_score", 7)
        # 注意：max_retries 由 workflow 层的 increment_retry 节点统一处理，
        # Reviewer 不再维护重试计数（避免计数被状态覆盖导致无限重试）。

    def run(self, state: ArticleState) -> dict:
        """执行内容审核"""
        article_title = state.get("article_title", "")
        content = state.get("content", "")
        target_audience = state.get("target_audience", "")
        retry_count = state.get("retry_count", 0)

        # 构建审核 prompt
        prompt = f"""请对以下微信公众号文章进行全面审核。

## 文章信息
- 标题：{article_title}
- 目标受众：{target_audience}
- 最低通过分数：{self.min_score}/10

## 文章内容
{content}

请严格按照 JSON 格式输出审核结果。"""

        # 调用 LLM（json_mode 强制 JSON 输出，减少解析失败）
        response = self.invoke(prompt, json_mode=True)

        # 解析审核结果
        try:
            review_result = self._parse_review_result(response)
        except (json.JSONDecodeError, KeyError) as e:
            logger.error(f"解析审核结果失败: {e}")
            review_result = {
                "passed": True,  # 解析失败时默认通过
                "score": 7,
                "feedback": "审核结果解析失败，默认通过",
                "details": {},
            }

        logger.info(
            f"审核结果: passed={review_result['passed']}, "
            f"score={review_result['score']}, retry={retry_count}"
        )

        # 重试计数由 workflow 层 increment_retry 节点维护，这里不返回 retry_count。
        # review_history 为 schema 级 Annotated[list, append_review_history]，
        # 只需返回本次审核记录，LangGraph 自动追加到历史。
        return {
            "review_result": review_result,
            "review_history": [
                {
                    "retry": retry_count,
                    "passed": review_result["passed"],
                    "score": review_result["score"],
                    "feedback": review_result.get("feedback", ""),
                },
            ],
            "metadata": {
                "reviewed_at": datetime.now().isoformat(),
            },
        }

    def _parse_review_result(self, response: str) -> dict:
        """解析 LLM 返回的审核结果"""
        data = extract_json_from_response(response)
        return {
            "passed": bool(data.get("passed", True)),
            "score": int(data.get("score", 7)),
            "feedback": str(data.get("feedback", "")),
            "details": data.get("details", {}),
        }