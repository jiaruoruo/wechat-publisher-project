"""选题去重工具 - 判断候选选题与历史文章是否重复

纯函数实现，零第三方依赖（标准库 difflib + re），便于单独单测。

相似度 = max(序列比, 关键词 Dice)：
- 序列比（difflib.SequenceMatcher）：捕捉「换字/增删」类重复，但对语序敏感
  （"AI 大模型趋势" vs "大模型 AI 趋势" 仅得 0.71）。
- 关键词 Dice（2|A∩B|/(|A|+|B|)）：基于特征集合，天然忽略语序。
  这里用 Dice 而非 Jaccard：Jaccard 对长度差更严苛，短标题换序时得分
  只有 0.80，低于默认阈值 0.82，会让「换序检测」实际失效。
取两者最大值可同时覆盖「换序」与「换词」两类重复。

特征集合构造：拉丁字母/数字按词切分；CJK 取二元组（bigram），
避免引入 jieba 等分词依赖仍能对中文有合理的粒度。
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

# 归一化后删除：空白、标点、下划线（\W 在 Unicode 模式下保留 CJK 汉字）
_PUNCT_RE = re.compile(r"[\s\W_]+", re.UNICODE)
# 拉丁字母/数字词
_LATIN_WORD_RE = re.compile(r"[a-z0-9]+")
# 连续 CJK 汉字段（CJK 扩展 A + 基本区，覆盖常用中文）
_CJK_RUN_RE = re.compile(r"[㐀-鿿]+")

# 默认去重阈值：低于此值视为不重复
DEFAULT_THRESHOLD = 0.82


def normalize(text: str | None) -> str:
    """归一化标题文本：全角转半角、转小写、去除空白与标点

    空值安全：None / 空串 / 非字符串均返回 ""。
    """
    if not text:
        return ""
    # NFKC 将全角字母/数字/标点统一为半角，减少「ＡＩ」与「AI」的误判
    folded = unicodedata.normalize("NFKC", str(text)).lower()
    return _PUNCT_RE.sub("", folded)


def _tokens(text: str | None) -> set[str]:
    """切分为特征集合：拉丁词 + CJK 二元组（单字则自身成词）"""
    body = normalize(text)
    if not body:
        return set()

    tokens = set(_LATIN_WORD_RE.findall(body))
    for run in _CJK_RUN_RE.findall(body):
        if len(run) == 1:
            tokens.add(run)
            continue
        tokens.update(run[i:i + 2] for i in range(len(run) - 1))
    return tokens


def _dice(a: set[str], b: set[str]) -> float:
    """集合 Dice 系数（无交集直接返回 0，避免空集除零）

    相比 Jaccard，Dice 对集合大小差异更宽容，适合长度不一的中文短标题比对。
    """
    if not a or not b:
        return 0.0
    intersection = len(a & b)
    if not intersection:
        return 0.0
    return 2 * intersection / (len(a) + len(b))


def similarity(a: str | None, b: str | None) -> float:
    """计算两个标题的相似度，取值 [0.0, 1.0]

    空串与任何文本相似度均为 0.0（避免空历史标题误判为重复）。
    """
    norm_a, norm_b = normalize(a), normalize(b)
    if not norm_a or not norm_b:
        return 0.0
    if norm_a == norm_b:
        return 1.0

    ratio = SequenceMatcher(None, norm_a, norm_b).ratio()
    dice = _dice(_tokens(a), _tokens(b))
    return max(ratio, dice)


def find_most_similar(
    candidate: str | None,
    history,
    threshold: float = DEFAULT_THRESHOLD,
) -> dict:
    """在历史文章中找出与候选选题最相似的一项

    Args:
        candidate: 候选选题（或文章标题）
        history: 历史记录，元素可为 str（视为标题），
                 或含 "title" / "topic" 键的 dict（两字段均参与比对）
        threshold: 判定重复的阈值，score >= threshold 即视为重复

    Returns:
        {
            "score": float,        # 最高相似度，无历史时为 0.0
            "matched": str,        # 命中的历史文本，无历史时为 ""
            "field": str,          # 命中的字段：title / topic，无历史时为 ""
            "is_duplicate": bool,  # score >= threshold
        }
    """
    best = {"score": 0.0, "matched": "", "field": ""}

    for item in history or []:
        if isinstance(item, dict):
            candidates = (("title", item.get("title")), ("topic", item.get("topic")))
        else:
            candidates = (("title", item),)

        for field, value in candidates:
            score = similarity(candidate, value)
            if score > best["score"]:
                best = {"score": score, "matched": value or "", "field": field}

    best["is_duplicate"] = best["score"] >= threshold
    return best
