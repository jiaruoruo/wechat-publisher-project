"""联网搜索工具 - 供选题 Agent 使用，获取热点趋势"""

import logging
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlparse

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# Bing RSS 接口（无需 API Key，返回干净的结构化结果）
BING_RSS_URL = "https://www.bing.com/search"

# ─── DuckDuckGo HTML 兜底：CSS 选择器回退列表（应对 HTML 结构变更）─────
_RESULT_SELECTORS = [
    ".result",                    # 当前结构
    ".result__body",              # 备选结构
    ".web-result",                # 备选结构
    "article[data-testid='result']",  # 备选结构
]

_TITLE_SELECTORS = [
    ".result__title a",
    ".result__a",
    "a.result__a",
    "a[data-testid='result-title-a']",
]

_SNIPPET_SELECTORS = [
    ".result__snippet",
    "span.result__snippet",
]


def _clean_redirect_url(url: str) -> str:
    """解码搜索跳转链接（DuckDuckGo / Bing 的转发 URL）为原始地址"""
    url = url.strip()
    parsed = urlparse(url)
    if "duckduckgo.com" in parsed.netloc and parsed.path.startswith("/l/"):
        params = parse_qs(parsed.query)
        if "uddg" in params:
            return params["uddg"][0]
    if "bing.com" in parsed.netloc and parsed.path.startswith("/ck/a"):
        params = parse_qs(parsed.query)
        if "u" in params:
            return params["u"][0]
    return url


def _parse_bing_rss(text: str, max_results: int) -> list[dict[str, str]]:
    """解析 Bing RSS 搜索结果（XML 中的 HTML 实体已转义，需还原）"""
    results = []
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        logger.warning(f"Bing RSS 解析失败: {e}")
        return results

    for item in root.iter("item"):
        title_el = item.find("title")
        link_el = item.find("link")
        if title_el is None or link_el is None or not (title_el.text or "").strip():
            continue
        title = title_el.text.strip()
        link = (link_el.text or "").strip()
        snippet = ""
        desc_el = item.find("description")
        if desc_el is not None and desc_el.text:
            snippet = BeautifulSoup(desc_el.text, "html.parser").get_text(" ", strip=True)
        results.append({"title": title, "snippet": snippet, "url": link})
        if len(results) >= max_results:
            break
    return results


def _search_bing(query: str, max_results: int) -> list[dict[str, str]]:
    """Bing RSS 搜索（主通道，比 DuckDuckGo HTML 抓取稳定）"""
    try:
        resp = requests.get(
            BING_RSS_URL,
            params={"format": "rss", "q": query},
            headers={"User-Agent": _UA},
            timeout=15,
        )
        resp.raise_for_status()
        results = _parse_bing_rss(resp.text, max_results)
        for r in results:
            r["url"] = _clean_redirect_url(r["url"])
        if results:
            logger.info(f"Bing RSS 搜索 '{query}' 找到 {len(results)} 条结果")
        return results
    except Exception as e:
        logger.warning(f"Bing RSS 搜索失败: {e}")
        return []


def _search_duckduckgo(query: str, max_results: int) -> list[dict[str, str]]:
    """DuckDuckGo HTML 搜索（兜底通道）"""
    try:
        response = requests.post(
            "https://html.duckduckgo.com/html/",
            data={"q": query},
            headers={"User-Agent": _UA},
            timeout=15,
        )
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")

        # 尝试多种选择器模式（适配 DuckDuckGo HTML 变更）
        for result_sel in _RESULT_SELECTORS:
            items = soup.select(result_sel)
            if items:
                logger.debug(f"使用结果选择器: {result_sel}，匹配 {len(items)} 项")
                break
        else:
            items = []

        results = []
        for item in items:
            title_el = None
            for ts in _TITLE_SELECTORS:
                title_el = item.select_one(ts)
                if title_el:
                    break
            if not title_el:
                continue

            title = title_el.get_text(strip=True)
            link = title_el.get("href", "")

            snippet_el = None
            for ss in _SNIPPET_SELECTORS:
                snippet_el = item.select_one(ss)
                if snippet_el:
                    break
            snippet = snippet_el.get_text(strip=True) if snippet_el else ""

            results.append({
                "title": title,
                "snippet": snippet,
                "url": _clean_redirect_url(link),
            })

            if len(results) >= max_results:
                break

        if results:
            logger.info(f"DuckDuckGo 搜索 '{query}' 找到 {len(results)} 条结果")
        return results

    except Exception as e:
        logger.warning(f"DuckDuckGo 搜索失败: {e}")
        return []


def web_search(query: str, max_results: int = 5) -> list[dict[str, str]]:
    """
    联网搜索（无需 API Key）

    优先使用 Bing RSS，失败或结果为空时回退 DuckDuckGo HTML 抓取。

    Args:
        query: 搜索关键词
        max_results: 最大返回结果数

    Returns:
        搜索结果列表，每项包含 {"title": ..., "snippet": ..., "url": ...}
    """
    results = _search_bing(query, max_results)
    if results:
        return results
    return _search_duckduckgo(query, max_results)


def search_trending(keywords: list[str], domain: str = "") -> str:
    """
    搜索热点趋势，返回格式化的搜索结果摘要

    多个关键词并行请求，避免串行导致的最坏 45s+ 等待。

    Args:
        keywords: 关键词列表
        domain: 内容领域

    Returns:
        格式化的搜索结果文本
    """
    all_results: list[str] = []

    def _search_one(kw: str) -> tuple[str, list[dict[str, str]]]:
        query = f"{domain} {kw} 最新" if domain else f"{kw} 最新"
        return kw, web_search(query, max_results=3)

    selected = keywords[:3]
    with ThreadPoolExecutor(max_workers=len(selected) or 1) as pool:
        futures = [pool.submit(_search_one, kw) for kw in selected]

    for future in futures:
        kw, results = future.result()
        if results:
            all_results.append(f"\n### 关键词: {kw}")
            for r in results:
                all_results.append(f"- **{r['title']}**: {r['snippet']}")

    if not all_results:
        logger.warning("所有关键词均未搜索到结果，搜索服务可能不可用")
        return "未找到相关热点信息，请基于领域知识进行选题。（提示：网络搜索功能可能暂时不可用，检查网络连接）"

    return "\n".join(all_results)
