"""自有文章表现采集（阅读/分享/点赞）与爆款归因

- collect_metrics：复用已登录的公众号后台会话（WechatSession）抓取已发文章的
  阅读/分享/点赞。后台统计页选择器**未经验证**，任何异常均静默降级返回 []，
  绝不阻断主流程。同时提供手动补录入口（CSV/JSON）以便选择器失效时仍可沉淀数据。
- attribute_performance：纯函数，把表现数据与标题模式库做关联，输出可读归因报告
  （哪类模式/方向阅读更高），供 report 命令展示。

注意：微信不开放竞品阅读量 API，本模块只针对「自有账号」的表现数据。
"""

from __future__ import annotations

import csv
import json
import logging

from browser.publish_actions import PUBLISHED_LIST_URLS
from browser.image_uploader import read_token_from_page
from tools.title_patterns import match_patterns

logger = logging.getLogger(__name__)


def collect_metrics(session, config: dict, limit: int = 50) -> list[dict]:
    """抓取自有已发文章的表现数据（阅读/分享/点赞）

    复用已登录会话与经实测可用的后台列表 URL + token 拼法（publish_actions 同款）。
    后台统计页的具体选择器**未验证**，故整段包在 try/except 中：任一环节失败
    （未登录 / 重定向 / 解析失败）一律返回 [] 并打印 [WARNING]，不影响其他流程。

    Returns: [{"title","publish_url","read_count","share_count","like_count"}, ...]
    """
    try:
        if session is None or not getattr(session, "check_login", lambda: False)():
            logger.warning("[WARNING] 未登录公众号会话，跳过表现数据采集")
            return []
        page = getattr(session, "page", None)
        if page is None:
            logger.warning("[WARNING] 会话无可用页面，跳过表现数据采集")
            return []

        url = f"{PUBLISHED_LIST_URLS[0]}&token={read_token_from_page(page)}&lang=zh_CN"
        page.goto(url, wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(2000)

        # 后台统计结构未验证：用通用 JS 试探，解析失败即降级为空。
        # 真实选择器需经「截图 + DOM 片段」探索后回填，此处仅做尽力而为的提取。
        rows = page.evaluate(
            """() => {
                const out = [];
                try {
                    const items = document.querySelectorAll('.appmsg_item, .weui-desktop-card, li');
                    items.forEach(el => {
                        const text = el.innerText || '';
                        const titleEl = el.querySelector('h4, .title, strong');
                        const title = titleEl ? titleEl.innerText.trim() : '';
                        // 仅在含数字与阅读/分享/点赞字样时视为有效行
                        if (title && /(\\d|阅读|分享|点赞)/.test(text)) {
                            const nums = (text.match(/[\\d,]+/g) || []).map(s => parseInt(s.replace(/,/g,''),10)||0);
                            out.push({title, nums});
                        }
                    });
                } catch (e) {}
                return out;
            }"""
        )
        if not rows:
            logger.warning("[WARNING] 后台统计页结构未匹配，表现数据采集成空（可改用手动补录）")
            return []

        metrics = []
        for r in rows[:limit]:
            nums = r.get("nums") or [0, 0, 0]
            metrics.append({
                "title": r.get("title", "")[:500],
                "publish_url": "",
                "read_count": nums[0] if len(nums) > 0 else 0,
                "share_count": nums[1] if len(nums) > 1 else 0,
                "like_count": nums[2] if len(nums) > 2 else 0,
            })
        logger.info(f"自有文章表现数据采集: {len(metrics)} 条")
        return metrics
    except Exception as e:  # noqa: BLE001 - 抓取失败绝不应阻断
        logger.warning(f"[WARNING] 表现数据采集失败（已降级为空）: {e}")
        return []


def import_metrics_from_csv(path: str) -> list[dict]:
    """手动补录入口：从 CSV 导入自有文章表现（选择器失效时的兜底）"""
    rows = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for r in reader:
                rows.append({
                    "title": (r.get("title") or "").strip()[:500],
                    "publish_url": (r.get("publish_url") or "").strip(),
                    "read_count": int(r.get("read_count") or 0),
                    "share_count": int(r.get("share_count") or 0),
                    "like_count": int(r.get("like_count") or 0),
                })
        logger.info(f"手动补录表现数据: {len(rows)} 条（来自 {path}）")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[WARNING] 表现数据 CSV 导入失败: {e}")
    return rows


def attribute_performance(
    metrics: list[dict],
    patterns: list[dict] | None = None,
    top_n: int = 10,
) -> list[dict]:
    """爆款归因（纯函数）：按标题命中的模式类型聚合表现

    返回按平均阅读降序的归因报告：
    [{"segment","count","avg_read","avg_share","avg_like"}, ...]
    segment 为「模式类型」或「无模式匹配」。

    数据不足（< 3 条）时仍返回聚合结果，由调用方决定是否提示样本不足。
    """
    if not metrics:
        return []

    buckets: dict[str, list[dict]] = {}
    for m in metrics:
        title = m.get("title", "")
        if patterns:
            hits = match_patterns(title, patterns)
            segs = [h.split(":", 1)[0] for h in hits] or ["无模式匹配"]
        else:
            segs = ["全部"]
        for seg in segs:
            buckets.setdefault(seg, []).append(m)

    report = []
    for seg, items in buckets.items():
        reads = [int(x.get("read_count") or 0) for x in items]
        shares = [int(x.get("share_count") or 0) for x in items]
        likes = [int(x.get("like_count") or 0) for x in items]
        report.append({
            "segment": seg,
            "count": len(items),
            "avg_read": round(sum(reads) / len(reads), 1) if reads else 0,
            "avg_share": round(sum(shares) / len(shares), 1) if shares else 0,
            "avg_like": round(sum(likes) / len(likes), 1) if likes else 0,
        })

    report.sort(key=lambda r: r["avg_read"], reverse=True)
    return report[:top_n]
