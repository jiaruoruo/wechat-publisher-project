"""共享路径配置 — 统一管理项目中的绝对路径"""

import os

# 项目根目录（wechat-publisher/）
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 配置目录
CONFIG_DIR = os.path.join(BASE_DIR, "config")
PROMPTS_DIR = os.path.join(CONFIG_DIR, "prompts")

# 存储目录
STORAGE_DIR = os.path.join(BASE_DIR, "storage")
IMAGES_DIR = os.path.join(STORAGE_DIR, "images")
SCREENSHOTS_DIR = os.path.join(STORAGE_DIR, "screenshots")
SELECTOR_STATS_DIR = os.path.join(STORAGE_DIR, "selector_stats")  # 选择器命中率台账（按日期 JSONL）

# 关键文件路径
SETTINGS_PATH = os.path.join(CONFIG_DIR, "settings.yaml")
DB_PATH = os.path.join(STORAGE_DIR, "articles.db")
SESSION_PATH = os.path.join(STORAGE_DIR, "browser_session.json")
NOTIFICATION_LOG_PATH = os.path.join(STORAGE_DIR, "notifications.log")
PAUSE_FLAG_PATH = os.path.join(STORAGE_DIR, ".scheduler_paused")
PUBLISH_LOCK_PATH = os.path.join(STORAGE_DIR, ".publish.lock")  # 跨进程发布互斥锁（Hermes/调度器/CLI 共用）


def ensure_dirs():
    """确保所有必要的目录存在"""
    for d in [STORAGE_DIR, IMAGES_DIR, SCREENSHOTS_DIR]:
        os.makedirs(d, exist_ok=True)
