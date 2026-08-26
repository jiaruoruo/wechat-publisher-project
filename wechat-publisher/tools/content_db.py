"""历史内容存储 - 避免重复选题，记录发布历史"""

import os
import sqlite3
import json
import logging
import threading
from datetime import datetime, timedelta
from typing import Any

from config.paths import BASE_DIR, DB_PATH as DEFAULT_DB_PATH

logger = logging.getLogger(__name__)


def _parse_dt(value: str | None) -> datetime | None:
    """解析存储的 ISO 时间字符串为本地 naive datetime（解析失败返回 None）"""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)  # 统一转本地 naive 便于比较
    return dt


class ContentDB:
    """文章内容数据库管理

    线程安全：每个线程使用独立的 sqlite3 连接（threading.local），
    避免持久连接跨线程使用导致 ProgrammingError。
    时间比较：统一在 Python 侧解析为 datetime 后比较，不依赖字符串字典序。
    """

    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or DEFAULT_DB_PATH
        # 确保路径为绝对路径
        if not os.path.isabs(self.db_path):
            self.db_path = os.path.join(BASE_DIR, self.db_path)
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._local = threading.local()
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        """获取当前线程的数据库连接（懒创建，每线程一条）"""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")  # 写 ahead logging 提高并发
            self._local.conn = conn
        return conn

    def close(self):
        """关闭当前线程的数据库连接"""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def __enter__(self):
        """上下文管理器：`with ContentDB(...) as db:` 退出时自动关闭当前线程连接"""
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    def _init_db(self):
        """初始化数据库表"""
        conn = self._get_conn()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS articles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                topic TEXT,
                summary TEXT,
                content TEXT,
                status TEXT DEFAULT 'draft',
                created_at TEXT NOT NULL,
                published_at TEXT,
                publish_url TEXT,
                metadata TEXT
            )
        """)
        # 为 created_at 建索引：避免 get_recent_titles / get_article_count
        # 在文章积累后做全表扫描（WHERE created_at >= ? 命中索引）。
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_articles_created_at "
            "ON articles(created_at)"
        )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS review_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                article_id INTEGER,
                review_result TEXT,
                retry_count INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                FOREIGN KEY (article_id) REFERENCES articles(id)
            )
        """)
        conn.commit()
        logger.info(f"数据库初始化完成: {self.db_path}")

    def save_article(
        self,
        title: str,
        topic: str,
        summary: str,
        content: str,
        metadata: dict | None = None,
    ) -> int:
        """保存文章记录，返回文章 ID"""
        now = datetime.now().isoformat()
        conn = self._get_conn()
        cursor = conn.execute(
            """INSERT INTO articles
               (title, topic, summary, content, status, created_at, metadata)
               VALUES (?, ?, ?, ?, 'draft', ?, ?)""",
            (title, topic, summary, content, now, json.dumps(metadata or {}, ensure_ascii=False)),
        )
        conn.commit()
        article_id = cursor.lastrowid
        logger.info(f"文章已保存: id={article_id}, title={title}")
        return article_id

    def update_article_status(
        self,
        article_id: int,
        status: str,
        publish_url: str = "",
    ):
        """更新文章状态"""
        now = datetime.now().isoformat()
        conn = self._get_conn()
        if status == "published":
            conn.execute(
                "UPDATE articles SET status=?, published_at=?, publish_url=? WHERE id=?",
                (status, now, publish_url, article_id),
            )
        else:
            conn.execute(
                "UPDATE articles SET status=? WHERE id=?",
                (status, article_id),
            )
        conn.commit()

    def save_review_log(
        self,
        article_id: int,
        review_result: dict,
        retry_count: int,
    ):
        """保存审核记录"""
        now = datetime.now().isoformat()
        conn = self._get_conn()
        conn.execute(
            """INSERT INTO review_logs
               (article_id, review_result, retry_count, created_at)
               VALUES (?, ?, ?, ?)""",
            (article_id, json.dumps(review_result, ensure_ascii=False), retry_count, now),
        )
        conn.commit()

    def get_recent_titles(self, days: int = 30) -> list[str]:
        """获取最近 N 天的文章标题列表（用于避免重复选题）

        过滤下推到 SQL（WHERE created_at >= ?）并命中 idx_articles_created_at
        索引，避免文章积累后拉全表在 Python 端过滤。
        """
        cutoff_iso = (datetime.now() - timedelta(days=days)).isoformat()
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT title, created_at FROM articles "
            "WHERE created_at >= ? ORDER BY created_at DESC",
            (cutoff_iso,),
        ).fetchall()
        # 兜底：若 created_at 含时区导致字典序偏差，仍做一次时间比较修正。
        cutoff = datetime.fromisoformat(cutoff_iso)
        return [
            row[0]
            for row in rows
            if (created := _parse_dt(row[1])) is not None and created >= cutoff
        ]

    def get_article_count(self, days: int = 7) -> int:
        """获取最近 N 天的文章数量

        计数下推到 SQL（WHERE created_at >= ?），避免拉全表后在 Python 端统计。
        """
        cutoff_iso = (datetime.now() - timedelta(days=days)).isoformat()
        conn = self._get_conn()
        # COUNT 直接由 SQLite 在索引上完成，无需把每行 created_at 拉回 Python。
        return conn.execute(
            "SELECT COUNT(*) FROM articles WHERE created_at >= ?",
            (cutoff_iso,),
        ).fetchone()[0]
