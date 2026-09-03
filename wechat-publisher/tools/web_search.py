"""联网搜索工具 - 供选题 Agent 使用，获取热点趋势"""

import hashlib
import json
import logging
import os
import re
import time
import unicodedata
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlparse

import requests
from bs4 import BeautifulSoup

from config.paths import STORAGE_DIR
from tools.topic_dedup import normalize

logger = logging.getLogger(__name__)

# 热点搜索结果缓存文件（storage/ 下，已被 .gitignore 忽略）
CACHE_PATH = os.path.join(STORAGE_DIR, "trending_cache.json")

# ── 关键词同义词组 ──────────────────────────────────────────
# 目的：配置里常出现语义高度重叠的关键词（如 ["AI", "人工智能", "大模型"]），
# 逐个搜索会发出几乎等价的重复请求。这里把已知同义写法归一到同一查询。
# 组内第一个词为规范形式；未命中的关键词保持原样（不做猜测性合并）。
# 用户若确实想分别搜索，只要不使用同义写法即可。
_SYNONYM_GROUPS = (
    ("ai", "人工智能", "artificialintelligence", "机器智能", "ai技术"),
    ("llm", "大模型", "大语言模型", "largelanguagemodel", "llms"),
    ("ml", "机器学习", "machinelearning"),
    ("dl", "深度学习", "deeplearning"),
    ("gc", "生成式ai", "生成式人工智能", "aigc", "generativeai"),
)

_SYNONYM_CANON: dict[str, str] = {
    term: group[0] for group in _SYNONYM_GROUPS for term in group
}

# 标题去重时截取的归一化长度上限
_TITLE_KEY_LEN = 80

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


def _canonical_keyword(keyword: str) -> str:
    """关键词归一：全角转半角、转小写、去空白与分隔符，再映射同义词"""
    key = unicodedata.normalize("NFKC", keyword).strip().lower()
    key = re.sub(r"[\s_\-]+", "", key)
    return _SYNONYM_CANON.get(key, key)


def dedupe_keywords(keywords: list[str] | None, max_keywords: int = 3) -> list[str]:
    """关键词去同义 + 去重，保留原始顺序与原始写法

    ["AI", "人工智能", "大模型"] → ["AI", "大模型"]（"人工智能" 与 "AI" 同义，合并）
    """
    seen: set[str] = set()
    selected: list[str] = []

    for kw in keywords or []:
        if not isinstance(kw, str):
            continue
        raw = kw.strip()
        if not raw:
            continue
        key = _canonical_keyword(raw)
        if key in seen:
            logger.debug(f"关键词 '{raw}' 与已选关键词同义/重复，跳过")
            continue
        seen.add(key)
        selected.append(raw)
        if len(selected) >= max_keywords:
            break

    return selected


# ── 聚合页识别 ──────────────────────────────────────────────
# 聚合页 = 资讯入口而非具体报道（如「XX 最新资讯」「XX 日报」「XX 导航」）。
# 实测：这类结果会抹平关键词差异 —— 「芯片」搜出来全是聚合页，
# 与「人工智能」结果雷同后被跨关键词去重整段丢弃。
_AGGREGATOR_TOKENS = re.compile(
    r"资讯|日报|周报|月报|导航|聚合|大全|汇总|实时滚动|小时报|首页|频道|工具集|"
    r"官网|_百度百科|百度知道|知识百科"
)
# 具体性信号：含年份或具体日期的标题（如「机器人行业日报 (08.31)」）仍有价值，不判为聚合页
_SPECIFIC_SIGNAL = re.compile(r"(20\d{2})|(\d{2}\.\d{2})")

# 默认后缀候选链：实测不存在全局最优后缀
# （「人工智能 最新消息」出真实新闻，「芯片 行业动态」出深度观察）
DEFAULT_QUERY_SUFFIXES = ["最新消息", "行业动态", "新闻"]


def is_aggregator(title: str) -> bool:
    """标题疑似聚合页（资讯入口而非具体报道）

    含具体日期/年份者放宽：如「机器人行业日报 (08.31)」虽含「日报」但指向具体一期。
    """
    if not title:
        return False
    if not _AGGREGATOR_TOKENS.search(title):
        return False
    return not bool(_SPECIFIC_SIGNAL.search(title))


def build_query(keyword: str, suffix: str, domain: str = "", include_domain: bool = False) -> str:
    """构造单条查询

    默认不拼 domain：实测加「科技」前缀后 Bing 返回门户首页，质量明显劣化
    （「科技 人工智能 最新消息」全部命中聚合页，而「人工智能 最新消息」出真实新闻）。
    """
    parts = [domain, keyword] if (include_domain and domain) else [keyword]
    parts.append(suffix)
    return " ".join(p for p in parts if p)


def _quality_ok(results: list[dict], needed: int) -> bool:
    """该关键词的搜索结果质量是否达标

    判定：过滤聚合页后至少有 1 条有效结果即视为达标，不再重试。
    理由：search_trending 每个关键词最多返回 results_per_keyword 条，
    若要求「达到 results_per_keyword 条才算达标」，则单条有效结果也会
    误触发重试（实测导致缓存命中测试失败、无效请求翻倍）。真正的重试信号
    是「全部命中聚合页 / 零有效结果」。
    """
    if not results:
        return False
    good = [r for r in results if not is_aggregator((r.get("title") or ""))]
    return len(good) >= 1


def _merge_results(base: list[dict], extra: list[dict]) -> list[dict]:
    """合并两轮结果，按归一化标题去重，保持顺序"""
    merged: list[dict] = []
    seen: set[str] = set()
    for r in [*base, *extra]:
        key = normalize((r.get("title") or ""))[:_TITLE_KEY_LEN]
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        merged.append(r)
    return merged


def _cache_key(queries: list[str], results_per_keyword: int) -> str:
    """缓存键：只保留查询列表的哈希摘要，不落原始关键词明文"""
    raw = "|".join(sorted(queries)) + f"|n={results_per_keyword}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _cache_read(queries: list[str], results_per_keyword: int, ttl: int) -> dict | None:
    """读取未过期的缓存；任何异常一律降级为「未命中」，绝不阻断选题"""
    if ttl <= 0:
        return None
    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            store = json.load(f)
        entry = store.get(_cache_key(queries, results_per_keyword))
        if not isinstance(entry, dict):
            return None
        if time.time() - float(entry.get("ts", 0)) > ttl:
            return None
        return {
            "text": entry.get("text", ""),
            "sources": list(entry.get("sources", [])),
            "ok": bool(entry.get("ok")),
        }
    except Exception as e:
        logger.debug(f"热点缓存读取失败（降级为不缓存）: {e}")
        return None


def _cache_write(queries: list[str], results_per_keyword: int, payload: dict, ttl: int):
    """写入缓存（tmp + os.replace 原子替换，避免并发/中断留下半截 JSON）"""
    if ttl <= 0:
        return
    try:
        try:
            with open(CACHE_PATH, "r", encoding="utf-8") as f:
                store = json.load(f)
            if not isinstance(store, dict):
                store = {}
        except (FileNotFoundError, ValueError):
            store = {}

        now = time.time()
        # 顺带清理过期条目，避免缓存文件随关键词变化无限增长
        store = {
            k: v for k, v in store.items()
            if isinstance(v, dict) and now - float(v.get("ts", 0)) <= ttl
        }
        store[_cache_key(queries, results_per_keyword)] = {
            "ts": now,
            "text": payload.get("text", ""),
            "sources": payload.get("sources", []),
            "ok": payload.get("ok", False),
        }

        os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
        tmp_path = f"{CACHE_PATH}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(store, f, ensure_ascii=False)
        os.replace(tmp_path, CACHE_PATH)
    except Exception as e:
        logger.debug(f"热点缓存写入失败（降级为不缓存）: {e}")


def search_trending_detailed(
    keywords: list[str],
    domain: str = "",  # noqa: ARG001 - 保留参数位以维持调用方兼容；默认不再拼进查询

    max_keywords: int = 3,
    results_per_keyword: int = 3,
    cache_ttl_seconds: int = 3600,
    suffixes: list[str] | None = None,
    include_domain: bool = False,
    max_query_variants: int = 2,
    filter_aggregator: bool = True,
) -> dict:
    """搜索热点趋势，返回结构化结果

    在旧版 search_trending 基础上增加：
    - 关键词去同义，避免语义重叠的关键词发出等价请求
    - 跨关键词按标题去重，避免同一条热点重复占用上下文
    - TTL 缓存落盘，同一天内重复运行不再重复请求

    Args:
        keywords: 关键词列表
        domain: 内容领域
        max_keywords: 参与搜索的关键词数量上限
        results_per_keyword: 每个关键词保留的结果条数
        cache_ttl_seconds: 缓存秒数，0 = 不缓存

    Returns:
        {"text": str, "sources": list[str], "ok": bool}
        ok=False 表示所有通道均无结果（text 为提示文案，非异常）。
        仅 ok=True 时写缓存，避免一次失败被缓存住一整个 TTL。
    """
    suffix_list = [s for s in (suffixes or DEFAULT_QUERY_SUFFIXES) if isinstance(s, str) and s.strip()]
    if not suffix_list:
        suffix_list = list(DEFAULT_QUERY_SUFFIXES)

    selected = dedupe_keywords(keywords, max_keywords)
    if not selected:
        logger.warning("未配置有效关键词，跳过热点搜索")
        return {"text": "未配置有效关键词，无法搜索热点，请基于领域知识进行选题。", "sources": [], "ok": False}

    # 缓存键覆盖全部候选后缀，换后缀会自然换键，不会误命中旧缓存
    all_queries = [
        build_query(kw, sfx, domain, include_domain)
        for kw in selected for sfx in suffix_list
    ]
    cached = _cache_read(all_queries, results_per_keyword, cache_ttl_seconds)
    if cached is not None:
        logger.info(f"热点搜索命中缓存（{len(all_queries)} 个查询，未发起网络请求）")
        return cached

    # ── 第 1 轮：所有关键词并发跑首选后缀 ──
    pending: dict[str, dict] = {kw: {"raw": [], "tried": 0} for kw in selected}
    round_queries = [build_query(kw, suffix_list[0], domain, include_domain) for kw in selected]

    with ThreadPoolExecutor(max_workers=len(round_queries)) as pool:
        futures = [pool.submit(web_search, q, results_per_keyword) for q in round_queries]
        for kw, fut in zip(selected, futures):
            try:
                pending[kw]["raw"] = fut.result() or []
            except Exception as e:
                logger.debug(f"关键词 '{kw}' 搜索失败: {e}")
                pending[kw]["raw"] = []
            pending[kw]["tried"] = 1

    # ── 第 2 轮起：仅对质量不合格的关键词换后缀（定向重试，非常态翻倍）──
    max_variants = max(1, min(max_query_variants, len(suffix_list)))
    while True:
        retry_kws = [
            kw for kw in selected
            if pending[kw]["tried"] < max_variants and not _quality_ok(pending[kw]["raw"], results_per_keyword)
        ]
        if not retry_kws:
            break
        retry_queries = [
            build_query(kw, suffix_list[pending[kw]["tried"]], domain, include_domain)
            for kw in retry_kws
        ]
        with ThreadPoolExecutor(max_workers=len(retry_queries)) as pool:
            futures = [pool.submit(web_search, q, results_per_keyword) for q in retry_queries]
            for kw, fut in zip(retry_kws, futures):
                try:
                    extra = fut.result() or []
                except Exception as e:
                    logger.debug(f"关键词 '{kw}' 重试失败: {e}")
                    extra = []
                pending[kw]["raw"] = _merge_results(pending[kw]["raw"], extra)
                pending[kw]["tried"] += 1

    lines: list[str] = []
    sources: list[str] = []
    seen_titles: set[str] = set()

    for kw in selected:
        results = pending[kw]["raw"]
        # 过滤聚合页；过滤后为空则回退到未过滤结果，绝不丢空
        filtered = [r for r in results if not is_aggregator((r.get("title") or ""))]
        if not filter_aggregator:
            filtered = results
        elif not filtered:
            if results:
                logger.debug(f"关键词 '{kw}' 过滤聚合页后为空，回退未过滤结果")
            filtered = results

        block: list[str] = []
        for r in filtered[:results_per_keyword]:
            title = (r.get("title") or "").strip()
            # 标题去重：不同关键词常搜出同一条热点，重复计入只会浪费上下文
            title_key = normalize(title)[:_TITLE_KEY_LEN]
            if title_key:
                if title_key in seen_titles:
                    continue
                seen_titles.add(title_key)
            block.append(f"- **{title}**: {r.get('snippet', '')}")
            url = (r.get("url") or "").strip()
            if url:
                sources.append(url)
        if block:
            lines.append(f"\n### 关键词: {kw}")
            lines.extend(block)

    if not lines:
        logger.warning("所有关键词均未搜索到结果，搜索服务可能不可用")
        return {
            "text": "未找到相关热点信息，请基于领域知识进行选题。（提示：网络搜索功能可能暂时不可用，检查网络连接）",
            "sources": [],
            "ok": False,
        }

    payload = {"text": "\n".join(lines), "sources": sources, "ok": True}
    _cache_write(all_queries, results_per_keyword, payload, cache_ttl_seconds)
    return payload


def search_trending(keywords: list[str], domain: str = "") -> str:
    """
    搜索热点趋势，返回格式化的搜索结果摘要

    保持既有契约（签名与返回类型均不变），内部委托给 search_trending_detailed。
    """
    return search_trending_detailed(keywords, domain)["text"]
