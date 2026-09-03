"""选择器命中率台账 — 把 editor.selector_stats 事件持久化为按日期归档的 JSONL

每次发布（publish_actions._emit_selector_stats）追加一行：
    storage/selector_stats/{YYYYMMDD}.jsonl
    {"ts": "...", "groups_attempted": n, "total_hits": n,
     "hits_by_group": {...}, "never_hit": {...}, "all_missed": [...]}

统计与自动精简建议（发布多篇后运行）：
    python -m browser.selector_ledger [--days 30] [--min-publishes 2]
    python main.py selectors [--days 30] [--min-publishes 2]

- 按 (交互分组, 选择器) 汇总长期命中率；
- 0 命中 → 建议删除；主选择器未命中且兜底明显有效 → 建议提前；
- 输出按命中次数降序的建议 selectors.yaml 分组顺序（可直接覆盖 config/selectors.yaml）。
"""

import json
import logging
import os
import threading
from datetime import datetime, timedelta

from config.paths import SELECTOR_STATS_DIR
from browser import wechat_selectors as sel

logger = logging.getLogger(__name__)

# 台账写入加锁：Hermes 多线程（Webhook worker / 工作流线程）并发发布时防止 JSONL 行交错
_LEDGER_LOCK = threading.Lock()


def serialize(report: dict, now: datetime | None = None) -> str:
    """把一次发布的命中统计转成 JSONL 一行（含时间戳）"""
    ts = (now or datetime.now()).isoformat()
    return json.dumps({"ts": ts, **report}, ensure_ascii=False)


def cleanup_old_stats(directory: str, retention_days: int = 30) -> int:
    """删除过期台账：按日期归档天然分片，只需按保留期回收旧 {YYYYMMDD}.jsonl

    Args:
        directory: 台账目录（storage/selector_stats）
        retention_days: 保留天数；<=0 表示不清理

    Returns:
        删除的文件数
    """
    if retention_days <= 0 or not os.path.isdir(directory):
        return 0
    cutoff = (datetime.now() - timedelta(days=retention_days)).strftime("%Y%m%d")
    removed = 0
    for name in os.listdir(directory):
        if not (name.endswith(".jsonl") and len(name) == 13):
            continue
        day = name[:-6]
        if not day.isdigit():
            continue
        # 文件名即日期，字典序比较等价于时间比较
        if day < cutoff:
            try:
                os.remove(os.path.join(directory, name))
                removed += 1
            except OSError as e:
                logger.warning(f"清理过期台账失败 {name}: {e}")
    if removed:
        logger.info(f"已清理 {retention_days} 天前的台账 {removed} 个文件（保留 {retention_days} 天）")
    return removed


def parse_line(line: str) -> dict | None:
    """解析 JSONL 一行；空行/损坏行/非对象返回 None"""
    line = line.strip()
    if not line:
        return None
    try:
        data = json.loads(line)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def append_selector_stats(report: dict, ledger_dir: str | None = None) -> str:
    """把一次发布的命中统计追加到当日 JSONL 台账

    失败只记日志不抛异常（台账故障不能影响发布流程）。

    Returns:
        写入的文件路径（失败返回空串）
    """
    directory = ledger_dir or SELECTOR_STATS_DIR
    try:
        with _LEDGER_LOCK:
            os.makedirs(directory, exist_ok=True)
            day = datetime.now().strftime("%Y%m%d")
            path = os.path.join(directory, f"{day}.jsonl")
            with open(path, "a", encoding="utf-8") as f:
                f.write(serialize(report) + "\n")
        return path
    except Exception as e:
        logger.warning(f"选择器台账写入失败（忽略）: {e}")
        return ""


def load_selector_stats(days: int = 30, ledger_dir: str | None = None) -> list[dict]:
    """读取最近 N 天的台账记录（由近及远）"""
    directory = ledger_dir or SELECTOR_STATS_DIR
    if not os.path.isdir(directory):
        return []
    today = datetime.now().date()
    reports: list[dict] = []
    for offset in range(days):
        day = (today - timedelta(days=offset)).strftime("%Y%m%d")
        path = os.path.join(directory, f"{day}.jsonl")
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    rec = parse_line(line)
                    if rec:
                        reports.append(rec)
        except Exception as e:
            logger.warning(f"读取台账失败（跳过）: {path}: {e}")
    return reports


def aggregate(reports: list[dict]) -> dict:
    """汇总多篇发布的命中情况

    Returns:
        {
            "publishes": n,
            "groups": {
                label: {
                    "publishes": n,
                    "selectors": { selector: {"hits": n, "rate": float} },
                }
            }
        }
    """
    groups: dict[str, dict] = {}
    for report in reports:
        hits_by_group = report.get("hits_by_group") or {}
        never_hit = report.get("never_hit") or {}
        for label in set(hits_by_group) | set(never_hit):
            g = groups.setdefault(label, {"publishes": 0, "selectors": {}})
            g["publishes"] += 1
            for selector, count in (hits_by_group.get(label) or {}).items():
                try:
                    count = int(count)
                except (TypeError, ValueError):
                    continue  # 人为改坏的台账行：跳过该条计数，不崩分析
                entry = g["selectors"].setdefault(selector, {"hits": 0, "rate": 0.0})
                entry["hits"] += count
            # 从未命中的选择器补 0 命中项（保证全量清单可见）
            for selector in never_hit.get(label) or []:
                if selector not in g["selectors"]:
                    g["selectors"][selector] = {"hits": 0, "rate": 0.0}
    for g in groups.values():
        n = g["publishes"]
        for entry in g["selectors"].values():
            entry["rate"] = round(entry["hits"] / n, 3) if n else 0.0
    return {"publishes": len(reports), "groups": groups}


def suggest_trimming(agg: dict, min_publishes: int = 2) -> dict:
    """基于台账自动生成精简建议

    - remove：N 次发布（>= min_publishes）0 命中 → 候选删除；
    - promote：主选择器未命中、兜底选择器明显有效（命中 > 0 且位置 > 1）→ 建议提前。

    Returns:
        {
            "suggestions": [{label, publishes, suggestions: [{selector, kind, hits, rate, position}]}],
            "reordered": {label: [按命中次数降序的选择器列表]},  # 可直接生成 selectors.yaml
        }
    """
    suggestions = []
    reordered: dict[str, list[str]] = {}
    current = sel.get_selectors()
    for label, g in sorted(agg.get("groups", {}).items()):
        if g["publishes"] < min_publishes:
            continue
        key = sel.SELECTOR_GROUP_KEYS.get(label)
        full = current.get(key) if key else None
        if isinstance(full, str):
            full = [full]
        if not isinstance(full, list) or not full:
            continue  # 未知分组/无生效清单，跳过
        order = {s: i for i, s in enumerate(full)}
        # 对当前生效清单整体重排：无台账数据的新选择器按 0 命中 + 原位置参与排序（不丢失、不新增）
        ordered = sorted(
            full,
            key=lambda s: (-(g["selectors"].get(s, {}).get("hits", 0)), order[s]),
        )
        reordered[label] = ordered

        group_sugs = []
        for s in ordered:
            info = g["selectors"].get(s)
            if info is None:
                continue  # 台账里没有数据（新加入当前配置）：不参与建议
            if info["hits"] == 0:
                group_sugs.append({
                    "selector": s,
                    "kind": "remove",
                    "hits": 0,
                    "rate": info["rate"],
                    "position": order[s] + 1,
                })
        best = ordered[0] if ordered else None
        best_info = g["selectors"].get(best) if best else None
        if best_info and order[best] + 1 > 1 and best_info["hits"] > 0:
            group_sugs.append({
                "selector": best,
                "kind": "promote",
                "hits": best_info["hits"],
                "rate": best_info["rate"],
                "position": order[best] + 1,
            })
        if group_sugs:
            suggestions.append({
                "label": label,
                "publishes": g["publishes"],
                "suggestions": group_sugs,
            })
    return {"suggestions": suggestions, "reordered": reordered}


def analyze(days: int = 30, min_publishes: int = 2, ledger_dir: str | None = None) -> dict:
    """完整流水线：加载 → 聚合 → 精简建议"""
    reports = load_selector_stats(days, ledger_dir)
    agg = aggregate(reports)
    trimming = suggest_trimming(agg, min_publishes)
    return {"aggregated": agg, **trimming}


def print_report(days: int = 30, min_publishes: int = 2, ledger_dir: str | None = None):
    """打印台账统计与精简建议（纯文本，GBK 控制台安全）"""
    result = analyze(days, min_publishes, ledger_dir)
    agg = result["aggregated"]
    print(f"选择器命中率台账（最近 {days} 天，共 {agg['publishes']} 次发布）")
    if not agg["publishes"]:
        print("暂无记录：发布过文章后这里才会有统计。")
        return
    for label, g in sorted(agg["groups"].items()):
        print(f"\n[{label}] {g['publishes']} 次发布")
        items = sorted(
            g["selectors"].items(),
            key=lambda kv: (-kv[1]["hits"], kv[0]),
        )
        for selector, info in items:
            rate = f"{info['rate'] * 100:.0f}%"
            print(f"  {selector}: 命中 {info['hits']} 次 ({rate})")

    if result["suggestions"]:
        print("\n[精简建议]")
        for item in result["suggestions"]:
            for s in item["suggestions"]:
                if s["kind"] == "remove":
                    print(
                        f"  删除 [{item['label']}] 中的 {s['selector']} "
                        f"（{item['publishes']} 次发布 0 命中，位置 {s['position']}）"
                    )
                else:
                    print(
                        f"  提前 [{item['label']}] 中的 {s['selector']} "
                        f"（命中 {s['hits']} 次，当前位置 {s['position']}）"
                    )

    if result["reordered"]:
        print("\n[建议的 selectors.yaml 分组排序（按命中次数降序，可整体替换对应分组）]")
        for label, ordered in result["reordered"].items():
            key = sel.SELECTOR_GROUP_KEYS.get(label)
            if not key:
                continue
            print(f"{key}:")
            for s in ordered:
                # JSON 双引号转义是合法 YAML，且对含引号的选择器安全
                print(f"  - {json.dumps(s, ensure_ascii=False)}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="选择器命中率台账与精简建议")
    parser.add_argument("--days", type=int, default=30, help="统计最近 N 天（默认 30）")
    parser.add_argument("--min-publishes", type=int, default=2, help="参与建议的最少发布次数（默认 2）")
    args = parser.parse_args()
    print_report(days=args.days, min_publishes=args.min_publishes)
