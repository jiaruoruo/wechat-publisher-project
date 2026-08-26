"""选题策划 Agent - 分析热点、生成选题方案"""

import json
import logging
from datetime import datetime

from agents.base import BaseAgent
from graph.state import ArticleState
from tools.web_search import search_trending
from tools.content_db import ContentDB
from tools.json_utils import extract_json_from_response

logger = logging.getLogger(__name__)


class TopicPlannerAgent(BaseAgent):
    """选题策划 Agent：综合分析热点 + 历史内容 → 生成选题方案"""

    agent_name = "topic_planner"
    prompt_file = "topic_planner.yaml"

    def __init__(self, llm_router, config: dict):
        super().__init__(llm_router, config)
        self.content_config = config.get("content", {})
        self.domain = self.content_config.get("domain", "科技")
        self.keywords = self.content_config.get("keywords", [])
        # 注意：不在 __init__ 持有持久 ContentDB 连接（工作流结束无人关闭，
        # 会残留 sqlite 连接），改为 run() 内按需创建并 finally 关闭。

    def run(self, state: ArticleState) -> dict:
        """执行选题策划"""
        # 1. 获取热点信息
        trending_info = search_trending(self.keywords, self.domain)
        logger.info(f"获取到热点信息: {len(trending_info)} 字")

        # 2. 获取历史文章标题（避免重复）——短连接，用完即关
        db = ContentDB(self.config.get("storage", {}).get("db_path"))
        try:
            recent_titles = db.get_recent_titles(days=30)
        finally:
            db.close()
        recent_titles_str = "\n".join(f"- {t}" for t in recent_titles[:10]) if recent_titles else "暂无近期文章"

        # 3. 构建 prompt
        prompt = f"""请为「{self.domain}」领域的微信公众号策划一期选题。

## 当前热点信息
{trending_info}

## 近期已发布的文章（避免重复）
{recent_titles_str}

## 内容风格要求
{self.content_config.get("style", "专业但通俗易懂")}

请综合以上信息，策划一个有吸引力的选题。严格按照 JSON 格式输出。"""

        # 4. 调用 LLM（json_mode 强制 JSON 输出，减少解析失败）
        response = self.invoke(prompt, json_mode=True)

        # 5. 解析结果
        try:
            result = self._parse_response(response)
        except (json.JSONDecodeError, KeyError) as e:
            logger.error(f"解析选题结果失败: {e}, 原始响应: {response}")
            # 降级处理：使用默认值
            result = {
                "topic": f"{self.domain}领域新视角",
                "outline": "待优化",
                "target_audience": f"对{self.domain}感兴趣的读者",
                "article_title": f"{self.domain}新趋势解读",
                "summary": "深度解析最新趋势",
                "cover_prompt": f"Modern {self.domain} concept illustration",
            }

        # 6. 更新状态（metadata 走 schema 级归并，只需返回本节点新增字段）
        result["metadata"] = {
            "topic_created_at": datetime.now().isoformat(),
            "domain": self.domain,
        }
        result["retry_count"] = 0

        return result

    def _parse_response(self, response: str) -> dict:
        """解析 LLM 返回的 JSON 结果"""
        data = extract_json_from_response(response)
        return {
            "topic": data["topic"],
            "outline": data["outline"],
            "target_audience": data["target_audience"],
            "article_title": data["article_title"],
            "summary": data["summary"],
            "cover_prompt": data.get("cover_prompt", ""),
        }