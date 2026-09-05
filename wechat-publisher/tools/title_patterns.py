"""标题模式提炼与命中判定（纯函数，零 IO，便于单测）

从竞品爆款标题中提炼可复用的「句式结构 + 情绪钩子」模式，并给出命中判定，
供选题评分器打分、以及候选选题 prompt 强制套用，从而提升爆款率。

模式类型（pattern_type）：
- numeric  数字型：标题含具体数字（年份/百分比/倍数/排名/数量）
- question 疑问型：以问号结尾或含疑问词（为什么/怎么/如何/吗…）
- contrast 反差型：含对立/转折词（vs/对比/却/反而/颠覆/逆袭…）
- list     盘点型：枚举式清单（十大/排行榜/合集/盘点/指南…）
- urgency  紧迫型：时效/稀缺钩子（最新/突发/刚刚/必看/重磅…）

命中判定基于正则 + 关键词，轻量且可解释；数字型用「含任意数字」泛化，
其余类型按类型匹配，避免把单一具体词语写死。
"""

from __future__ import annotations

import re
from collections import Counter

PATTERN_TYPES = ("numeric", "question", "contrast", "list", "urgency")

# 数字型：年份 / 数字 + 可选单位（倍、%、个、万、亿、年…）
_NUMERIC_RE = re.compile(r"(?:19|20)\d{2}|\d+(?:\.\d+)?\s*(?:%|倍|个|条|篇|大|强|万|亿|年)?")
# 疑问型：问号
_QUESTION_RE = re.compile(r"[?？]")
_QUESTION_WORDS = ("为什么", "怎么", "如何", "吗", "什么", "哪些", "怎样", "为何")
_CONTRAST_WORDS = ("vs", "对比", "却", "反而", "颠覆", "逆袭", "反超", "而是")
_LIST_WORDS = ("十大", "排行榜", "排名", "合集", "盘点", "清单", "系列", "榜单", "攻略", "指南")
_URGENCY_WORDS = ("最新", "突发", "刚刚", "即将", "必看", "重磅", "独家", "一文读懂", "彻底")


def _norm(title: str) -> str:
    return (title or "").lower()


def classify_patterns(title: str) -> list[str]:
    """返回标题命中哪些模式类型（可多个）"""
    t = _norm(title)
    hits: list[str] = []
    if _NUMERIC_RE.search(t):
        hits.append("numeric")
    if _QUESTION_RE.search(t) or any(w in t for w in _QUESTION_WORDS):
        hits.append("question")
    if any(w in t for w in _CONTRAST_WORDS):
        hits.append("contrast")
    if any(w in t for w in _LIST_WORDS):
        hits.append("list")
    if any(w in t for w in _URGENCY_WORDS):
        hits.append("urgency")
    return hits


def _pattern_token(ptype: str, title: str) -> str:
    """把一个模式类型映射为可读 pattern 文本（用于展示与 LLM 套用）"""
    if ptype == "numeric":
        m = _NUMERIC_RE.search(_norm(title))
        fragment = m.group(0) if m else "含数字"
        return f"数字型:{fragment}"
    return {
        "question": "疑问型:用问句或疑问词引发好奇",
        "contrast": "反差型:制造对立或反转预期",
        "list": "盘点型:枚举式清单(十大/榜单/指南)",
        "urgency": "紧迫型:时效或稀缺钩子(最新/必看/重磅)",
    }[ptype]


def extract_patterns(titles: list[str], top_n: int = 12) -> list[dict]:
    """从竞品标题提炼模式库

    Returns: [{"pattern_type","pattern","hit_count","sample"}, ...]
    空输入返回 []，绝不抛异常。
    """
    if not titles:
        return []

    counter: Counter = Counter()
    samples: dict = {}
    for title in titles:
        for ptype in classify_patterns(title):
            token = _pattern_token(ptype, title)
            key = (ptype, token)
            counter[key] += 1
            if key not in samples:
                samples[key] = title

    return [
        {
            "pattern_type": ptype,
            "pattern": token,
            "hit_count": cnt,
            "sample": samples[(ptype, token)],
        }
        for (ptype, token), cnt in counter.most_common(top_n)
    ]


def match_patterns(title: str, patterns: list[dict]) -> list[str]:
    """判定标题命中哪些已有模式，返回命中 pattern 字符串列表

    空标题或空模式库返回 []。数字型按「含任意数字」泛化命中，
    其余按类型匹配，保证对同一类型的不同具体表述都能命中。
    """
    if not title or not patterns:
        return []
    title_types = set(classify_patterns(title))
    hits: list[str] = []
    for p in patterns:
        ptype = p.get("pattern_type")
        pattern = p.get("pattern", "")
        if ptype in title_types and pattern:
            hits.append(pattern)
    return hits
