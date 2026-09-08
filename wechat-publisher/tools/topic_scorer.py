"""选题评分（纯函数，零 IO，便于单测）

综合三维度给候选选题打分（0~100），输出可解释明细：
- heat      行业热度：基于多源资讯条目数与权威度（evidence_items）。
- pattern   爆款标题模式：候选标题命中竞品模式库的程度（match_patterns）。
- history   自有历史表现：账号已发文章阅读量规模（article_metrics）。

任一维度数据缺失时按 0 计入并自动降权（权重不变但该项贡献为 0），
保证不会因空数据而放大噪声。权重可配，默认 heat 0.4 / pattern 0.4 / history 0.2
（自有数据样本小，仅作辅助加权，随积累可逐步提权）。
"""

from __future__ import annotations

import math

from tools.title_patterns import match_patterns
from tools.topic_dedup import normalize, similarity

DEFAULT_WEIGHTS = {"heat": 0.4, "pattern": 0.4, "history": 0.2}


def _score_heat(items: list[dict]) -> float:
    """行业热度分（0~1）：资讯覆盖度 + 权威度

    - 覆盖度：条目数归一（10 条即视为饱和）。
    - 权威度：authoritative 级 / high 置信条目占比。
    """
    if not items:
        return 0.0
    n = len(items)
    coverage = min(1.0, n / 10.0)
    auth = sum(
        1 for it in items
        if it.get("tier") == "authoritative" or it.get("confidence") == "high"
    )
    quality = auth / n
    return 0.5 * coverage + 0.5 * quality


def _score_pattern(title: str, patterns: list[dict]) -> float:
    """爆款标题模式分（0~1）：命中模式越多越接近 1，最多按 2 个封顶"""
    if not title or not patterns:
        return 0.0
    matched = match_patterns(title, patterns)
    if not matched:
        return 0.0
    return min(1.0, len(matched) / 2.0)


def _score_history(title: str, metrics: list[dict]) -> float:
    """自有历史表现分（0~1）：与高阅读历史标题相似 → 已验证方向加成

    同时用账号整体阅读量规模做「数据是否充足」的弱信号：
    样本充足且量级大 → 加成；样本为空 → 0（不放大噪声）。
    """
    if not metrics:
        return 0.0

    reads = [int(m.get("read_count") or 0) for m in metrics]
    top = sorted(reads, reverse=True)[:max(1, len(reads) // 3)]
    avg_top = sum(top) / len(top)

    # 量级归一（log 压缩）：1 万阅读 → ~0.8，10 万 → ~0.95
    scale = math.log10(avg_top + 1) / 5.0 if avg_top > 0 else 0.0

    # 与高阅读历史标题相似 → 该方向已被验证，给加成
    similarity_boost = 0.0
    if title:
        top_titles = [
            m.get("title", "") for m in metrics
            if int(m.get("read_count") or 0) >= (avg_top if avg_top else 0)
        ]
        best = max(
            (similarity(title, t) for t in top_titles),
            default=0.0,
        )
        similarity_boost = max(0.0, best - 0.5) * 2.0  # 仅在 >0.5 时给加成

    return min(1.0, 0.6 * scale + 0.4 * similarity_boost)


def score_topic(
    topic: dict,
    evidence_items: list[dict] | None = None,
    patterns: list[dict] | None = None,
    history_metrics: list[dict] | None = None,
    weights: dict | None = None,
) -> tuple[float, dict]:
    """给单个候选选题打分（0~100），返回 (score, score_detail)

    score_detail = {
        "heat": float, "pattern": float, "history": float,
        "weights": dict, "matched_patterns": list[str],
    }
    各项分值为 0~100 的展示值；内部权重计算基于 0~1 归一分。
    """
    w = dict(DEFAULT_WEIGHTS)
    if weights:
        for k in w:
            if isinstance(weights.get(k), (int, float)):
                w[k] = float(weights[k])
    # 权重归一，避免外部传入非归一值导致越界
    total_w = sum(w.values()) or 1.0
    w = {k: v / total_w for k, v in w.items()}

    heat_n = _score_heat(evidence_items or [])
    pattern_n = _score_pattern(topic.get("title", ""), patterns or [])
    history_n = _score_history(topic.get("title", ""), history_metrics or [])

    matched = (
        match_patterns(topic.get("title", ""), patterns or [])
        if patterns else []
    )

    score = (heat_n * w["heat"] + pattern_n * w["pattern"] + history_n * w["history"]) * 100.0

    detail = {
        "heat": round(heat_n * 100, 1),
        "pattern": round(pattern_n * 100, 1),
        "history": round(history_n * 100, 1),
        "weights": {k: round(v, 3) for k, v in w.items()},
        "matched_patterns": matched,
    }
    return round(score, 1), detail
