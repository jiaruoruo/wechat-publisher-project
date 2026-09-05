"""竞品爆款标题采集（搜索引擎）与模式库提炼

- 复用 tools.web_search.web_search（Bing RSS 主 + DuckDuckGo 兜底）采集竞品/
  热门文章标题（微信不开放竞品阅读量 API，这里只取标题，信号为「句式/钩子」层面）。
- 标题去重（复用 topic_dedup.normalize）后落库 competitor_titles。
- 用 title_patterns.extract_patterns 提炼模式库，落库 title_patterns。

所有采集失败均静默降级（返回空列表），绝不抛异常、绝不阻断主流程。
"""

from __future__ import annotations

import logging

import tools.web_search as web_search_mod
import tools.title_patterns as title_patterns_mod
from tools.topic_dedup import normalize
from tools.content_db import ContentDB

logger = logging.getLogger(__name__)

# 竞品采集查询后缀：引导搜索引擎返回「已发布且较热」的公众号文章标题
_QUERY_SUFFIX = "公众号 热门文章 爆款"


def collect_titles(
    keywords: list[str],
    results_per_keyword: int = 5,
    max_keywords: int = 3,
) -> list[dict]:
    """搜索引擎采集竞品/热门文章标题

    Returns: [{"title","source","keyword","url"}, ...]（已去重）
    空输入或搜索不可用返回 []。
    """
    # 通过模块属性访问，便于测试 mock（直接 from import 会绑定引用，patch 失效）
    selected = web_search_mod.dedupe_keywords(keywords or [], max_keywords)
    if not selected:
        logger.warning("竞品采集：未提供有效关键词，跳过")
        return []

    rows: list[dict] = []
    seen: set[str] = set()
    for kw in selected:
        query = f"{kw} {_QUERY_SUFFIX}"
        try:
            results = web_search_mod.web_search(query, results_per_keyword) or []
        except Exception as e:  # noqa: BLE001 - 采集失败不应炸
            logger.debug(f"竞品标题搜索失败({kw}): {e}")
            results = []

        for r in results:
            title = (r.get("title") or "").strip()
            if not title:
                continue
            key = normalize(title)[:80]
            if key and key in seen:
                continue
            seen.add(key)
            rows.append({
                "title": title,
                "source": "web_search",
                "keyword": kw,
                "url": (r.get("url") or "").strip(),
            })

    logger.info(f"竞品标题采集完成: {len(rows)} 条（来自 {len(selected)} 个关键词）")
    return rows


def build_competitor_bank(
    db: ContentDB,
    keywords: list[str],
    max_keywords: int = 3,
    results_per_keyword: int = 5,
) -> tuple[list[dict], list[dict]]:
    """采集竞品标题 + 提炼模式库 + 落库。返回 (titles, patterns)

    失败时各自降级为空列表，不影响调用方。
    """
    titles = collect_titles(keywords, results_per_keyword, max_keywords)
    patterns: list[dict] = []
    if titles:
        try:
            db.save_competitor_titles(titles)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"竞品标题落库失败（不阻断）: {e}")
        try:
            patterns = title_patterns_mod.extract_patterns([t["title"] for t in titles])
            if patterns:
                db.save_title_patterns(patterns)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"标题模式提炼/落库失败（不阻断）: {e}")
    else:
        logger.warning("未采集到竞品标题（搜索服务可能不可用），跳过模式库更新")
    return titles, patterns
