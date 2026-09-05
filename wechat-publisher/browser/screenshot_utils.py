"""截图路径工具 — publish_actions / wechat_session 共用

统一命名规范：
    {screenshot_dir}/{YYYYMMDD}/{name}_{YYYYMMDD_HHMMSS_微秒}.png

- 按日期归档到 YYYYMMDD 子目录，便于按天排查；
- 文件名带时间戳后缀，多次发布/运行不会覆盖同名截图；
- 提供保留期清理（cleanup_old_screenshots），防止截图无限堆积。
"""

import os
import shutil
from datetime import datetime, time, timedelta


def screenshot_path(screenshot_dir: str, name: str) -> str:
    """生成截图完整路径并按日期归档（自动创建 YYYYMMDD 子目录）"""
    now = datetime.now()
    day = now.strftime("%Y%m%d")
    ts = now.strftime("%Y%m%d_%H%M%S_%f")
    sub = os.path.join(screenshot_dir, day)
    os.makedirs(sub, exist_ok=True)
    return os.path.join(sub, f"{name}_{ts}.png")


def cleanup_old_screenshots(screenshot_dir: str, retention_days: int = 30) -> int:
    """清理超过保留期的截图，返回删除的目录/文件数

    - 按日期归档的 YYYYMMDD 子目录：日期早于（今天 - retention_days）的整目录删除；
    - 根目录遗留的旧版 .png（时间戳化改造前）：按文件修改时间清理；
    - 非日期命名的目录一律不动（可能是手工放置的素材）。

    retention_days <= 0 时禁用清理（返回 0）。
    """
    try:
        retention_days = int(retention_days)
    except (TypeError, ValueError):
        return 0
    if retention_days <= 0 or not os.path.isdir(screenshot_dir):
        return 0

    today = datetime.now().date()
    cutoff_date = today - timedelta(days=retention_days)
    cutoff_ts = datetime.combine(cutoff_date, time.min).timestamp()

    removed = 0
    for entry in os.listdir(screenshot_dir):
        path = os.path.join(screenshot_dir, entry)
        if os.path.isdir(path):
            try:
                day = datetime.strptime(entry, "%Y%m%d").date()
            except ValueError:
                continue  # 非日期目录，不动
            if day < cutoff_date:
                shutil.rmtree(path)
                removed += 1
        elif os.path.isfile(path) and entry.lower().endswith(".png"):
            try:
                if os.path.getmtime(path) < cutoff_ts:
                    os.remove(path)
                    removed += 1
            except OSError:
                continue
    return removed
