"""多源信息采集：并发抓取定向权威源 + 国内源，统一解析与可信度分级

为什么需要它（实测结论，2026-09-02）：
- 搜索引擎（Bing）在地理定位下即使设 mkt=en-US 仍返回中文结果，
  无法稳定获取国际内容。国际资讯只能靠「定向权威源」抓取。
- 因此这里维护一份可配置源清单（config/sources.yaml），按 region/tier
  标注，失效源默认禁用，抓取时单源失败静默跳过、绝不阻断选题。

可信度分级（防假信息）：
- authoritative（厂商官网 / IEEE / arXiv / GitHub / MIT TR / Ars 等）：单条即可信
- general（国内科技媒体 / HN 等）：需 ≥1 个其他来源在标题层面印证，否则标「待核实」
- 跨源印证用标题相似度（tools.topic_dedup.similarity），阈值 0.5
"""

import logging
import os
import re
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import requests

from config.paths import CONFIG_DIR
from tools.topic_dedup import normalize, similarity

logger = logging.getLogger(__name__)

_SOURCES_PATH = os.path.join(CONFIG_DIR, "sources.yaml")

# 请求头：部分站点对默认 UA 直接 403
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}

# 标签清洗：RSS 描述常含 HTML 标签与转义
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
# 发布时间归一化简写（仅保留日期部分用于展示）
_ISO_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _clean(text: str | None) -> str:
    if not text:
        return ""
    text = _TAG_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text).strip()
    return text


def _first_date(text: str) -> str:
    m = _ISO_RE.search(text or "")
    return m.group(0) if m else ""


def _local_name(elem) -> str:
    """去掉命名空间前缀，取本地标签名（RSS/Atom 通用）"""
    return elem.tag.split("}")[-1]


def _parse_feed(text: str) -> list[dict]:
    """解析 RSS 或 Atom，统一为 {title, url, snippet, published}"""
    items: list[dict] = []
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        logger.debug(f"feed 解析失败: {e}")
        return items

    for node in root.iter():
        if _local_name(node) not in ("item", "entry"):
            continue
        fields: dict[str, str] = {}
        for child in node:
            name = _local_name(child)
            if name == "link":
                # RSS: <link>text</link>；Atom: <link href="..."/>
                href = child.get("href") or child.text or ""
                fields.setdefault("link", href.strip())
            else:
                fields.setdefault(name, _clean(child.text or ""))
        title = fields.get("title", "")
        # Atom 用 summary，RSS 用 description
        snippet = fields.get("description") or fields.get("summary") or ""
        published = fields.get("pubDate") or fields.get("published") or fields.get("updated") or ""
        url = fields.get("link") or fields.get("guid") or ""
        if title:
            items.append({
                "title": title,
                "url": url,
                "snippet": snippet,
                "published": _first_date(published),
            })
    return items


def _parse_json_github(text: str) -> list[dict]:
    import json

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    out = []
    for repo in data.get("items", [])[:8]:
        out.append({
            "title": repo.get("full_name", ""),
            "url": repo.get("html_url", ""),
            "snippet": _clean(repo.get("description", "")),
            "published": _first_date(repo.get("pushed_at", "")),
        })
    return out


def _parse_json_hn(text: str) -> list[dict]:
    import json

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    out = []
    for hit in data.get("hits", [])[:8]:
        url = hit.get("url") or f"https://news.ycombinator.com/item?id={hit.get('objectID', '')}"
        out.append({
            "title": _clean(hit.get("title", "")),
            "url": url,
            "snippet": _clean(hit.get("story_text", ""))[:300],
            "published": _first_date(hit.get("created_at", "")),
        })
    return out


def _fetch_one(source: dict) -> list[dict]:
    """抓取单个源并解析，任何异常都返回 []（静默跳过）"""
    name = source.get("name", "?")
    url = source.get("url", "")
    stype = source.get("type", "feed")
    timeout = int(source.get("timeout", 15))
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=timeout)
        if resp.status_code != 200:
            logger.debug(f"源 {name} 返回 {resp.status_code}，跳过")
            return []
        # 部分源返回 gzip/编码异常，统一用 apparent_encoding 兜底
        resp.encoding = resp.apparent_encoding or resp.encoding
        body = resp.text
    except Exception as e:
        logger.debug(f"源 {name} 请求失败: {e}")
        return []

    if stype == "json":
        if "github" in url:
            items = _parse_json_github(body)
        elif "hn.algolia" in url or "hacker" in url:
            items = _parse_json_hn(body)
        else:
            logger.debug(f"源 {name} 类型 json 但无对应解析器，跳过")
            return []
    else:  # feed
        items = _parse_feed(body)

    for it in items:
        it["source"] = name
        it["region"] = source.get("region", "intl")
        it["tier"] = source.get("tier", "general")
        it["topics"] = source.get("topics", [])
    logger.debug(f"源 {name} 采集到 {len(items)} 条")
    return items


def load_sources(path: str = _SOURCES_PATH) -> list[dict]:
    """读取 config/sources.yaml 的源清单（不抛异常，缺文件返回空）"""
    import yaml

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except FileNotFoundError:
        logger.warning(f"源配置不存在: {path}")
        return []
    except Exception as e:
        logger.warning(f"源配置解析失败: {e}")
        return []
    sources = data.get("sources", [])
    return [s for s in sources if s.get("enabled")]


def collect(
    topics: list[str] | None = None,
    region: str | None = None,
    max_per_source: int = 8,
    sources_path: str = _SOURCES_PATH,
) -> list[dict]:
    """并发抓取所有启用源，返回条目列表（已按 topic/region 过滤）

    每条: {title, url, snippet, published, source, region, tier, topics}
    单源失败不影响其他源。
    """
    sources = load_sources(sources_path)
    if not sources:
        return []

    with ThreadPoolExecutor(max_workers=min(len(sources), 10)) as pool:
        results = pool.map(_fetch_one, sources)

    items: list[dict] = []
    for src_items in results:
        if not src_items:
            continue
        for it in src_items[:max_per_source]:
            # 主题过滤：源声明的 topics 与请求 topics 有交集才保留
            if topics:
                src_topics = it.get("topics", [])
                if src_topics and not (set(src_topics) & set(topics)):
                    continue
            # 国内外过滤
            if region and it.get("region") != region:
                continue
            items.append(it)

    logger.info(f"多源采集完成：{len(sources)} 个启用源，共 {len(items)} 条")
    return items


def grade(items: list[dict]) -> list[dict]:
    """可信度分级：authoritative 单源采信；general 需跨源印证

    就地给每条 item 追加 confidence（high/medium）与 verified（bool）。
    """
    for i, it in enumerate(items):
        if it.get("tier") == "authoritative":
            it["confidence"] = "high"
            it["verified"] = True
            continue
        # general：寻找另一个不同源的相似标题作印证
        best = 0.0
        for j, other in enumerate(items):
            if j == i or other.get("source") == it.get("source"):
                continue
            score = similarity(it.get("title", ""), other.get("title", ""))
            best = max(best, score)
        if best >= 0.5:
            it["confidence"] = "high"
            it["verified"] = True
        else:
            it["confidence"] = "medium"
            it["verified"] = False
    return items


def to_text(items: list[dict], limit: int = 60) -> str:
    """把条目渲染成选题 prompt 可消费的文本块（含来源与可信度标记）"""
    lines: list[str] = []
    for it in items[:limit]:
        region_tag = "国内" if it.get("region") == "domestic" else "国际"
        verified = "[已核实]" if it.get("verified") else "[待核实]"
        snippet = (it.get("snippet") or "")[:120]
        lines.append(
            f"- 【{region_tag}/{it.get('source', '?')}{verified}】{it.get('title', '')}"
            + (f"：{snippet}" if snippet else "")
        )
    return "\n".join(lines)


def collect_and_grade(
    topics: list[str] | None = None,
    region: str | None = None,
) -> list[dict]:
    """采集 + 分级 一站式入口"""
    items = collect(topics=topics, region=region)
    return grade(items)
