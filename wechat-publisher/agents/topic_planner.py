"""选题策划 Agent - 分析热点、生成选题方案

流水线定位：输入裁剪 → 调用加固 → 产出校验 → 失败短路

1. 输入裁剪：调用方指定选题时跳过热点搜索（省 3 次网络请求）
2. 调用加固：LLM 调用失败自动重试一次，仍失败才降级
3. 产出校验：与近期历史文章的标题/选题做相似度硬校验，重复则重选一次
4. 失败短路：降级时置 metadata.topic_degraded，由 workflow 条件边中止后续节点
"""

import logging
import time
from datetime import datetime

from agents.base import BaseAgent
from config.validation import TOPIC_PLANNING_INT_RULES
from graph.state import ArticleState
import tools.web_search as web_search_mod
from tools.web_search import DEFAULT_QUERY_SUFFIXES
import tools.sources_feed as sources_feed
from tools.content_db import ContentDB
from tools.json_utils import safe_extract_json
from tools.topic_dedup import DEFAULT_THRESHOLD, find_most_similar, normalize
from tools.topic_scorer import score_topic

logger = logging.getLogger(__name__)

# 日志中原始响应截断长度：避免日志膨胀，也避免整段内容进日志
_RESPONSE_LOG_LIMIT = 200
# 候选方案最多保留条数 / 单字段截断长度（防止 metadata 膨胀）
_MAX_CANDIDATES = 3
_FIELD_MAX_LEN = 200

# content.topic_planning 默认值：缺段、缺键、类型非法、越界均静默回落到这里
_PLANNING_DEFAULTS = {
    "history_days": 30,
    "history_limit": 10,
    "max_keywords": 3,
    "results_per_keyword": 3,
    "dedup_threshold": DEFAULT_THRESHOLD,
    "skip_search_when_specified": True,
    "trending_cache_ttl_seconds": 3600,
    "max_topic_attempts": 2,
    "llm_max_attempts": 2,
    # 搜索查询构造（修复垃圾上下文用）
    "query_suffixes": list(DEFAULT_QUERY_SUFFIXES),
    "include_domain_in_query": False,
    "max_query_variants": 2,
    "filter_aggregator_pages": True,
    # ── 运营增长：竞品模式 + 数据化选题 ──
    "competitor_enabled": True,                      # true = 候选选题时套用竞品爆款标题模式
    "competitor_max_keywords": 3,                    # 竞品采集关键词上限，1~10
    "competitor_results_per_keyword": 5,             # 每个竞品关键词保留条数，1~10
    "competitor_keywords": [],                       # 为空则复用 content.keywords
    "metrics_enabled": True,                         # true = 评分时纳入自有文章表现（若有数据）
    "score_weights": {"heat": 0.4, "pattern": 0.4, "history": 0.2},
}


def resolve_planning_config(content_config: dict) -> dict:
    """读取 content.topic_planning 并逐键钳制

    任何非法值都回落到默认并记一条 warning，绝不抛异常 —— 配置写错不应
    让整个发布流程起不来。
    """
    raw = content_config.get("topic_planning")
    if raw is None:
        return dict(_PLANNING_DEFAULTS)
    if not isinstance(raw, dict):
        logger.warning(
            f"content.topic_planning 类型非法（{type(raw).__name__}），全部回落默认值"
        )
        return dict(_PLANNING_DEFAULTS)

    resolved: dict = {}
    for key, default in _PLANNING_DEFAULTS.items():
        value = raw.get(key, default)
        # bool 是 int 的子类，需先排除，避免 `true` 被当成 1
        if isinstance(value, bool) and not isinstance(default, bool):
            value = default

        if key == "dedup_threshold":
            if not isinstance(value, (int, float)) or not (0 < value <= 1):
                value = default
            else:
                value = float(value)
        elif key == "skip_search_when_specified":
            value = bool(value) if isinstance(value, bool) else default
        elif key == "include_domain_in_query":
            value = bool(value) if isinstance(value, bool) else default
        elif key == "filter_aggregator_pages":
            value = bool(value) if isinstance(value, bool) else default
        elif key == "query_suffixes":
            if not isinstance(value, list) or not all(
                isinstance(s, str) and s.strip() for s in value
            ):
                value = default
            else:
                value = [s.strip() for s in value]
        elif key == "trending_cache_ttl_seconds":
            if not isinstance(value, int) or value < 0:
                value = default
        elif key == "max_query_variants":
            if not isinstance(value, int) or value < 1:
                value = default
        elif key == "competitor_enabled":
            value = bool(value) if isinstance(value, bool) else default
        elif key == "metrics_enabled":
            value = bool(value) if isinstance(value, bool) else default
        elif key == "competitor_max_keywords":
            if not isinstance(value, int) or not (1 <= value <= 10):
                value = default
        elif key == "competitor_results_per_keyword":
            if not isinstance(value, int) or not (1 <= value <= 10):
                value = default
        elif key == "competitor_keywords":
            if not isinstance(value, list) or not all(
                isinstance(s, str) and s.strip() for s in value
            ):
                value = default
            else:
                value = [s.strip() for s in value]
        elif key == "score_weights":
            if not isinstance(value, dict):
                value = default
            else:
                w = {}
                for wk in ("heat", "pattern", "history"):
                    wv = value.get(wk)
                    w[wk] = (
                        float(wv)
                        if isinstance(wv, (int, float))
                        else default["score_weights"][wk]
                    )
                value = w
        else:
            # 上下界都钳制：上界同样是成本/耗时守卫而非格式约束
            # （max_keywords=999 会真的发出 999 次网络请求）
            low, high = TOPIC_PLANNING_INT_RULES[key]
            if not isinstance(value, int) or not (low <= value <= high):
                value = default

        if not isinstance(default, (list, dict)) and value != raw.get(key, default):
            logger.warning(
                f"content.topic_planning.{key} 值非法（{raw.get(key)!r}），回落为 {value}"
            )
        resolved[key] = value

    return resolved


def _extract_candidates(data: dict) -> list[dict]:
    """提取候选方案（最多 3 条），仅保留标量字段并截断长度

    模型可能把 candidates 写成 dict 列表或字符串列表，两种都兼容。
    """
    raw = data.get("candidates")
    if not isinstance(raw, list):
        return []

    candidates: list[dict] = []
    for item in raw[:_MAX_CANDIDATES]:
        if isinstance(item, dict):
            candidates.append({
                "topic": str(item.get("topic", ""))[:_FIELD_MAX_LEN],
                "angle": str(item.get("angle", ""))[:_FIELD_MAX_LEN],
                "hook": str(item.get("hook", ""))[:_FIELD_MAX_LEN],
            })
        elif isinstance(item, str):
            candidates.append({"topic": item[:_FIELD_MAX_LEN], "angle": "", "hook": ""})
    return candidates


class TopicPlannerAgent(BaseAgent):
    """选题策划 Agent：综合分析热点 + 历史内容 → 生成选题方案"""

    agent_name = "topic_planner"
    prompt_file = "topic_planner.yaml"

    def __init__(self, llm_router, config: dict):
        super().__init__(llm_router, config)
        self.content_config = config.get("content", {})
        self.domain = self.content_config.get("domain", "科技")
        self.keywords = self.content_config.get("keywords", [])
        self.planning = resolve_planning_config(self.content_config)
        # 注意：不在 __init__ 持有持久 ContentDB 连接（工作流结束无人关闭，
        # 会残留 sqlite 连接），改为 run() 内按需创建并 finally 关闭。

    # ── 主流程 ──────────────────────────────────────────────

    def run(self, state: ArticleState) -> dict:
        """执行选题策划

        Returns:
            顶层选题字段（topic/outline/...）+ metadata 审计信息。
            metadata 走 schema 级归并，只需返回本节点新增字段。
        """
        # 调用方指定选题（如 main.py run --topic）时，直接采用，不再自拟；
        # 仍结合热点与历史标题生成大纲等配套字段，保证下游节点输入完整。
        specified_topic = (state.get("topic") or "").strip()
        if specified_topic:
            logger.info(f"使用调用方指定选题: {specified_topic}")

        # 1. 热点信息（指定选题时可跳过）
        trending_text, trending_sources, trending_ok, _trending_items = self._fetch_trending(
            specified_topic
        )

        # 2. 历史文章（短连接，用完即关）
        history = self._fetch_history()

        # 3. 生成 → 校验 → 必要时重选
        result, metadata = self._plan(
            specified_topic=specified_topic,
            trending_text=trending_text,
            history=history,
            trending_sources=trending_sources,
            trending_ok=trending_ok,
        )

        result["metadata"] = metadata
        return result

    # ── 输入阶段 ────────────────────────────────────────────

    def _fetch_trending(self, specified_topic: str) -> tuple[str, list[str], bool, list[dict]]:
        """获取热点信息，返回 (文本, 来源 URL 列表, 是否命中, 结构化条目)

        文本 = 搜索引擎热点（国内）+ 多源权威采集（国内/国际）合并；
        结构化条目用于选题的可信度标注与证据引用。

        调用方已指定选题时跳过搜索：定题已定，热点对本次产出无意义，
        却要付出网络请求与等待。但仍采集多源条目供证据参考（低成本、可缓存）。
        """
        # 多源采集（国际权威源 + 国内媒体），本机缓存式，失败静默跳过
        multi_items = sources_feed.collect_and_grade(topics=["ai", "robotics", "chip"])

        if specified_topic and self.planning["skip_search_when_specified"]:
            logger.info("调用方已指定选题，跳过搜索引擎热点（仍保留多源采集）")
            return (
                sources_feed.to_text(multi_items),
                [it["url"] for it in multi_items if it.get("url")],
                len(multi_items) > 0,
                multi_items,
            )

        detail = web_search_mod.search_trending_detailed(
            self.keywords,
            self.domain,
            max_keywords=self.planning["max_keywords"],
            results_per_keyword=self.planning["results_per_keyword"],
            cache_ttl_seconds=self.planning["trending_cache_ttl_seconds"],
            suffixes=self.planning["query_suffixes"],
            include_domain=self.planning["include_domain_in_query"],
            max_query_variants=self.planning["max_query_variants"],
            filter_aggregator=self.planning["filter_aggregator_pages"],
        )
        combined_text = detail["text"]
        if multi_items:
            combined_text = (
                f"{detail['text']}\n\n## 多源资讯（国内/国际，已分级）\n"
                + sources_feed.to_text(multi_items)
            )
        sources_urls = list(detail["sources"]) + [it["url"] for it in multi_items if it.get("url")]
        ok = detail["ok"] or len(multi_items) > 0
        logger.info(f"获取到热点信息: {len(combined_text)} 字，多源 {len(multi_items)} 条")
        return combined_text, sources_urls, ok, multi_items

    def _fetch_history(self) -> list[dict[str, str]]:
        """读取近期文章的 {title, topic}（短连接，用完即关）"""
        db = ContentDB(self.config.get("storage", {}).get("db_path"))
        try:
            return db.get_recent_articles_for_dedup(days=self.planning["history_days"])
        finally:
            db.close()

    # ── 生成与校验阶段 ──────────────────────────────────────

    def _plan(
        self,
        specified_topic: str,
        trending_text: str,
        history: list[dict[str, str]],
        trending_sources: list[str],
        trending_ok: bool,
    ) -> tuple[dict, dict]:
        """生成选题并在必要时重选，返回 (选题字段, 审计元数据)"""
        history_view = history[:self.planning["history_limit"]]
        history_str = self._format_history(history_view)

        max_attempts = max(1, int(self.planning["max_topic_attempts"]))
        threshold = self.planning["dedup_threshold"]

        # 指定选题是用户显式意图（补发/重发/定向创作），去重会直接违背指令
        skip_dedup = bool(specified_topic)
        dedup_skipped_reason = "调用方指定选题，跳过去重校验" if skip_dedup else ""

        dedup_checked = False
        dedup_warning = None
        candidates: list[dict] = []
        avoid_clause = ""
        attempts = 0
        degraded_reason = ""
        result: dict | None = None
        cover_prompt_derived = False

        for attempt in range(1, max_attempts + 1):
            attempts = attempt
            prompt = self._build_prompt(
                specified_topic, trending_text, history_str, avoid_clause
            )

            raw = self._invoke_with_retry(prompt)
            if raw is None:
                degraded_reason = f"LLM 调用连续失败 {self.planning['llm_max_attempts']} 次"
                break

            data = safe_extract_json(raw, default={})
            candidates = _extract_candidates(data)
            parsed, cover_prompt_derived = self._coerce_result(data, specified_topic)

            if parsed is None:
                logger.error(
                    f"选题输出缺少必需字段（topic/outline），原始响应: "
                    f"{self._truncate(raw)}"
                )
                degraded_reason = "LLM 输出缺少必需字段（topic/outline）"
                break

            if skip_dedup:
                result = parsed
                break

            # 硬校验：标题与选题都要比对，只比对标题会漏掉「换标题不换选题」
            worst = self._worst_match(parsed, history_view, threshold)
            dedup_checked = True

            if not worst["is_duplicate"]:
                result = parsed
                break

            if attempt < max_attempts:
                logger.warning(
                    f"选题与历史重复（相似度 {worst['score']:.2f}，命中「{worst['matched']}」），重选一次"
                )
                avoid_clause = self._build_avoid_clause(worst)
                continue

            # 已用尽次数：记警告放行，不阻塞发布
            dedup_warning = {
                "score": round(worst["score"], 4),
                "matched": worst["matched"][:_FIELD_MAX_LEN],
                "field": worst["field"],
                "threshold": threshold,
            }
            logger.warning(
                f"重选后仍与历史重复（相似度 {worst['score']:.2f}），记 dedup_warning 并放行"
            )
            result = parsed
            break
        else:
            # 循环体 break 全覆盖，理论上不可达；兜底避免 result 未定义
            degraded_reason = "选题生成未产出有效结果"

        if degraded_reason or result is None:
            return self._build_degraded(
                specified_topic=specified_topic,
                reason=degraded_reason or "选题生成未产出有效结果",
                attempts=attempts,
                trending_sources=trending_sources,
                trending_ok=trending_ok,
                dedup_checked=dedup_checked,
                dedup_skipped_reason=dedup_skipped_reason,
                candidates=candidates,
            )

        metadata = {
            "topic_created_at": datetime.now().isoformat(),
            "domain": self.domain,
            "topic_keywords": list(self.keywords)[:_MAX_CANDIDATES * 3],
            "trending_ok": trending_ok,
            "trending_skipped": bool(specified_topic)
            and bool(self.planning["skip_search_when_specified"]),
            "trending_sources_count": len(trending_sources),
            "topic_attempts": attempts,
            "topic_candidates": candidates,
            "dedup_checked": dedup_checked,
            "dedup_skipped_reason": dedup_skipped_reason,
            "model_provider": self._provider_name(),
        }
        if cover_prompt_derived:
            metadata["cover_prompt_derived"] = True
        if dedup_warning:
            metadata["dedup_warning"] = dedup_warning

        return result, metadata

    def _worst_match(self, parsed: dict, history_view: list[dict], threshold: float) -> dict:
        """取「标题」与「选题」两个字段中更相似的一次比对结果"""
        by_title = find_most_similar(parsed.get("article_title", ""), history_view, threshold)
        by_topic = find_most_similar(parsed.get("topic", ""), history_view, threshold)
        return by_title if by_title["score"] >= by_topic["score"] else by_topic

    def _invoke_with_retry(self, prompt: str) -> str | None:
        """调用 LLM，失败重试；全部失败返回 None（由调用方降级）

        目的：单次网络抖动不应炸掉整个 run（BaseAgent.__call__ 会把异常
        冒泡到 graph.invoke，scheduler/main/hermes 三个入口行为不一致）。
        """
        max_attempts = max(1, int(self.planning["llm_max_attempts"]))
        last_error = None

        for attempt in range(1, max_attempts + 1):
            try:
                return self.invoke(prompt, json_mode=True)
            except Exception as e:  # noqa: BLE001 - 兜住所有 LLM 客户端异常
                last_error = e
                if attempt < max_attempts:
                    logger.warning(
                        f"选题 LLM 调用失败（第 {attempt}/{max_attempts} 次），1.5s 后重试: {e}"
                    )
                    time.sleep(1.5)

        logger.error(f"选题 LLM 调用连续失败 {max_attempts} 次: {last_error}")
        return None

    def _coerce_result(self, data: dict, specified_topic: str) -> tuple[dict | None, bool]:
        """逐字段兜底：保留模型已给出的字段，仅补齐缺失项

        Returns:
            (规范化后的选题字段, cover_prompt 是否为派生值)。
            关键字段（topic/outline）缺失时返回 (None, False)，由调用方走降级分支。

            返回值只含 ArticleState 已声明的字段；派生标记等审计信息一律走
            metadata，避免向状态里塞入 schema 未声明的键。
        """
        if not isinstance(data, dict):
            return None, False

        topic = str(data.get("topic") or "").strip()
        title = str(data.get("article_title") or "").strip()
        outline = str(data.get("outline") or "").strip()

        # 关键字段：缺了就没法写正文，必须降级而不是带着占位符往下走
        if not outline or not (topic or title):
            return None, False

        topic = topic or title
        title = title or topic

        # 双重保险：即使 LLM 输出偏离，选题也以调用方指定值为准；
        # 标题若未包含选题关键词则前置选题，保证读者可见。
        if specified_topic:
            topic = specified_topic
            if specified_topic not in title:
                title = f"{specified_topic}｜{title}"

        cover_prompt = str(data.get("cover_prompt") or "").strip()
        cover_prompt_derived = False
        if not cover_prompt:
            cover_prompt = self._derive_cover_prompt(topic, title)
            cover_prompt_derived = True

        return {
            "topic": topic,
            "outline": outline,
            "target_audience": str(data.get("target_audience") or "").strip()
            or f"对{self.domain}感兴趣的读者",
            "article_title": title,
            "summary": str(data.get("summary") or "").strip() or outline[:50],
            "cover_prompt": cover_prompt,
        }, cover_prompt_derived

    def _build_degraded(
        self,
        specified_topic: str,
        reason: str,
        attempts: int,
        trending_sources: list[str],
        trending_ok: bool,
        dedup_checked: bool,
        dedup_skipped_reason: str,
        candidates: list[dict],
    ) -> tuple[dict, dict]:
        """降级产出：置 topic_degraded=True，由 workflow 条件边中止后续节点

        与旧版的区别：不再把占位垃圾（"待优化"）塞进状态一路跑到发布，
        而是明确标记失败并短路，避免白跑生成/配图/审核并占用每日发布配额。
        """
        logger.error(f"选题降级，原因: {reason}")

        topic = specified_topic or f"{self.domain}领域新视角"
        return (
            {
                "topic": topic,
                "outline": "",
                "target_audience": f"对{self.domain}感兴趣的读者",
                "article_title": specified_topic or f"{self.domain}新趋势解读",
                "summary": "",
                "cover_prompt": "",
            },
            {
                "topic_created_at": datetime.now().isoformat(),
                "domain": self.domain,
                "topic_keywords": list(self.keywords)[:_MAX_CANDIDATES * 3],
                "trending_ok": trending_ok,
                "trending_skipped": bool(specified_topic)
                and bool(self.planning["skip_search_when_specified"]),
                "trending_sources_count": len(trending_sources),
                "topic_attempts": attempts,
                "topic_candidates": candidates,
                "dedup_checked": dedup_checked,
                "dedup_skipped_reason": dedup_skipped_reason,
                "model_provider": self._provider_name(),
                # 中止信号：workflow 的 topic_condition 据此直连 END
                "topic_degraded": True,
                "topic_degraded_reason": reason,
            },
        )

    # ── 提示词与格式化 ──────────────────────────────────────

    def _build_prompt(
        self,
        specified_topic: str,
        trending_text: str,
        history_str: str,
        avoid_clause: str,
    ) -> str:
        specified_clause = (
            f"\n## 指定选题（必须严格围绕此选题策划，不得更换）\n{specified_topic}\n"
            if specified_topic else ""
        )
        trending_section = (
            f"\n## 当前热点信息\n{trending_text}\n"
            if trending_text
            else "\n## 当前热点信息\n（本次未获取热点，请基于领域知识与读者需求策划）\n"
        )
        return f"""请为「{self.domain}」领域的微信公众号策划一期选题。
{specified_clause}
{trending_section}
## 近期已发布的文章（避免重复）
{history_str}
{avoid_clause}
## 内容风格要求
{self.content_config.get("style", "专业但通俗易懂")}

请综合以上信息，策划一个有吸引力的选题。严格按照 JSON 格式输出。"""

    def _build_avoid_clause(self, match: dict) -> str:
        return (
            "\n## 重要：避免重复\n"
            f"上一轮选题与历史文章「{match.get('matched', '')}」"
            f"相似度 {match.get('score', 0):.2f}，已被判定为重复。\n"
            "请换一个明显不同的切入点重新策划，不要只做同义改写。\n"
        )

    @staticmethod
    def _format_history(history: list[dict[str, str]]) -> str:
        if not history:
            return "暂无近期文章"
        lines = [f"- {item['title']}" for item in history if item.get("title")]
        return "\n".join(lines) if lines else "暂无近期文章"

    def _derive_cover_prompt(self, topic: str, title: str) -> str:
        """cover_prompt 为空时的兜底：由标题/选题派生英文提示词

        契约：模型可以不返回 cover_prompt。若留空，image_generator 会直接
        跳过 AI 生成、退化为本地渐变兜底封面（image_generator.py:40），
        白白丢掉一次 AI 封面机会；这里派生一个可用提示词保住该路径。
        """
        base = (title or topic or self.domain).strip()
        return f"Modern editorial illustration about {base}, clean minimal style, high quality"

    # ── 候选选题批量产出（只读：不写库、不发布、不生成正文）──

    def suggest_topics(self, count: int = 12) -> list[dict]:
        """产出候选选题清单供人工审核（数据化、带评分、按分降序）

        只做「多源采集 + 一次 LLM 头脑风暴 + 评分排序」，不写 workflow、不发布。
        LLM 失败或解析失败时返回 []，绝不抛异常（候选清单是辅助决策，不可阻断）。

        评分与选题库落库均为增强项：任一环节失败都静默降级（评分记 0、不落库），
        不影响候选清单本身返回。

        Returns: [{"title","angle","why","direction","region","evidence",
                   "confidence","score","score_detail","pattern"}, ...]（按分降序）
        """
        # 多源采集（国际权威源 + 国内媒体），含可信度分级
        items = sources_feed.collect_and_grade(topics=["ai", "robotics", "chip"])
        evidence = sources_feed.to_text(items, limit=80) or "（本次未采集到多源资讯，请基于领域知识策划）"

        # 竞品爆款模式 + 自有表现（评分用），读取失败则降级为空
        patterns, metrics = self._load_growth_context()

        prompt = self._build_ideas_prompt(evidence, count, patterns)

        try:
            raw = self.invoke(prompt, json_mode=True)
        except Exception as e:  # noqa: BLE001 - 候选产出失败不应炸
            logger.error(f"候选选题 LLM 调用失败: {e}")
            return []

        data = safe_extract_json(raw, default={})
        raw_list = data.get("topics")
        if not isinstance(raw_list, list):
            logger.error("候选选题解析失败：未找到 topics 列表")
            return []

        seen_titles: set[str] = set()
        result: list[dict] = []
        for item in raw_list:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            if not title:
                continue
            key = normalize(title)[:_FIELD_MAX_LEN]
            if key in seen_titles:
                continue
            seen_titles.add(key)
            result.append({
                "title": title[:_FIELD_MAX_LEN],
                "angle": str(item.get("angle") or "").strip()[:_FIELD_MAX_LEN],
                "why": str(item.get("why") or "").strip()[:_FIELD_MAX_LEN],
                "direction": str(item.get("direction") or "").strip()[:_FIELD_MAX_LEN],
                "region": str(item.get("region") or "any").strip()[:20],
                "evidence": str(item.get("evidence") or "").strip()[:_FIELD_MAX_LEN],
                "confidence": str(item.get("confidence") or "medium").strip()[:20],
            })

        # 评分 + 按分降序（失败降级：评分记 0，仍返回候选）
        result = self._enrich_and_rank(result, items, patterns, metrics)
        # 选题库落库（best-effort，失败不阻断）
        self._persist_topic_bank(result)

        logger.info(f"候选选题产出 {len(result)} 条")
        return result

    def _load_growth_context(self) -> tuple[list[dict], list[dict]]:
        """读取竞品标题模式库与自有表现数据（评分用）

        数据来自 research 命令沉淀的 DB 表；本环节任何失败都降级为空，
        不影响候选选题产出。
        """
        patterns: list[dict] = []
        metrics: list[dict] = []
        db_path = self.config.get("storage", {}).get("db_path")
        if not db_path:
            return patterns, metrics
        try:
            with ContentDB(db_path) as db:
                if self.planning.get("competitor_enabled"):
                    patterns = db.get_title_patterns()
                if self.planning.get("metrics_enabled"):
                    metrics = db.get_article_metrics()
        except Exception as e:  # noqa: BLE001
            logger.debug(f"读取增长上下文失败（降级为空）: {e}")
        return patterns, metrics

    def _enrich_and_rank(
        self,
        result: list[dict],
        evidence_items: list[dict],
        patterns: list[dict],
        metrics: list[dict],
    ) -> list[dict]:
        """给候选选题打分、附加模式命中，并按分数降序"""
        enriched: list[dict] = []
        for r in result:
            score, detail = score_topic(
                r,
                evidence_items=evidence_items,
                patterns=patterns,
                history_metrics=metrics,
                weights=self.planning.get("score_weights"),
            )
            item = dict(r)
            item["score"] = score
            item["score_detail"] = detail
            item["pattern"] = "; ".join(detail.get("matched_patterns", []))[:200]
            enriched.append(item)
        enriched.sort(key=lambda x: x.get("score", 0.0), reverse=True)
        return enriched

    def _persist_topic_bank(self, topics: list[dict]) -> None:
        """把评分后的候选选题落库（best-effort，失败静默不阻断）"""
        db_path = self.config.get("storage", {}).get("db_path")
        if not db_path or not topics:
            return
        try:
            with ContentDB(db_path) as db:
                db.save_topic_bank([
                    {
                        "title": t.get("title", ""),
                        "angle": t.get("angle", ""),
                        "why": t.get("why", ""),
                        "direction": t.get("direction", ""),
                        "score": t.get("score", 0.0),
                        "score_detail": t.get("score_detail", {}),
                        "pattern": t.get("pattern", ""),
                        "evidence": t.get("evidence", ""),
                        "confidence": t.get("confidence", "medium"),
                    }
                    for t in topics
                ])
        except Exception as e:  # noqa: BLE001
            logger.debug(f"选题库落库失败（不阻断）: {e}")

    def _build_ideas_prompt(self, evidence: str, count: int, patterns: list[dict] | None = None) -> str:
        """构造候选选题 prompt（模板从 config/prompts/topic_ideas.yaml 读取）

        patterns: 已验证的竞品爆款标题模式库；为空时给通用爆款原则提示。
        """
        import yaml
        import os

        prompt_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "config", "prompts", "topic_ideas.yaml",
        )
        template = "请基于以下资讯策划 {count} 个公众号选题。"
        try:
            with open(prompt_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            template = data.get("system_prompt", template)
        except Exception as e:
            logger.debug(f"topic_ideas 模板读取失败，使用内置模板: {e}")

        # 组装「已验证爆款标题模式」段落（注入 prompt，强制 LLM 套用）
        if patterns:
            lines = [f"- [{p.get('pattern_type')}] {p.get('pattern')}（命中 {p.get('hit_count')} 次，样例：{p.get('sample')}）"
                     for p in patterns[:12]]
            patterns_block = "已验证的爆款标题模式（请尽量套用其句式/钩子）：\n" + "\n".join(lines)
        else:
            patterns_block = ("暂无已积累的竞品爆款模式，请基于通用爆款原则设计标题："
                              "① 含具体数字/年份；② 用疑问引发好奇；③ 制造反差/对比；"
                              "④ 盘点式清单（十大/榜单）；⑤ 带时效或稀缺钩子（最新/必看）。")

        try:
            return template.format(
                count=count,
                domain=self.domain,
                style=self.content_config.get("style", "专业但通俗易懂"),
                evidence=evidence,
                patterns=patterns_block,
            )
        except (KeyError, IndexError):
            # 模板未含 {patterns} 占位时退回不注入模式
            return template.format(
                count=count,
                domain=self.domain,
                style=self.content_config.get("style", "专业但通俗易懂"),
                evidence=evidence,
            )

    def _provider_name(self) -> str:
        return (
            self.llm_router.config.get("models", {})
            .get(self.agent_name, {})
            .get("provider", "unknown")
        )

    @staticmethod
    def _truncate(text: str) -> str:
        if not text:
            return ""
        return text if len(text) <= _RESPONSE_LOG_LIMIT else text[:_RESPONSE_LOG_LIMIT] + "…"
