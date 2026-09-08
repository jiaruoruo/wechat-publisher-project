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
        # 轻量 schema 迁移：已有库文件补列（历史库建表时没有 html 列）
        self._ensure_column(conn, "articles", "html", "html TEXT")
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
        # ── 运营增长能力（选题库 / 竞品模式 / 表现归因）──
        # 全部 CREATE TABLE IF NOT EXISTS：对历史库零破坏，首次运行自动建表。
        conn.execute("""
            CREATE TABLE IF NOT EXISTS competitor_titles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                source TEXT,
                keyword TEXT,
                url TEXT,
                collected_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS title_patterns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pattern_type TEXT NOT NULL,   -- numeric/question/contrast/list/urgency
                pattern TEXT NOT NULL,
                hit_count INTEGER DEFAULT 0,
                sample TEXT,
                collected_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS article_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                article_id INTEGER,
                title TEXT,
                publish_url TEXT,
                read_count INTEGER DEFAULT 0,
                share_count INTEGER DEFAULT 0,
                like_count INTEGER DEFAULT 0,
                collected_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS topic_bank (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                angle TEXT,
                why TEXT,
                direction TEXT,
                score REAL DEFAULT 0,
                score_detail TEXT,
                pattern TEXT,
                evidence TEXT,
                confidence TEXT,
                used INTEGER DEFAULT 0,
                created_at TEXT NOT NULL
            )
        """)
        conn.commit()
        logger.info(f"数据库初始化完成: {self.db_path}")

    @staticmethod
    def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str):
        """列不存在则 ALTER TABLE 添加（兼容历史库文件，无需重建表）"""
        cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")
            logger.info(f"数据库迁移：{table} 新增列 {column}")

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

    def delete_orphan_drafts(self, days: int = 7) -> int:
        """删除 N 天前仍未发表的孤儿草稿（发布失败/中断残留），返回删除条数

        这些行是「运行了但没发出去」的残留（如封面缺失降级、发布失败中断），
        保留会干扰选题去重与统计。已发表/近期记录不受影响。
        """
        cutoff_iso = (datetime.now() - timedelta(days=days)).isoformat()
        conn = self._get_conn()
        cur = conn.execute(
            "DELETE FROM articles WHERE status = 'draft' AND created_at < ?",
            (cutoff_iso,),
        )
        conn.commit()
        deleted = cur.rowcount or 0
        if deleted:
            logger.info(f"已清理 {days} 天前的孤儿草稿 {deleted} 条")
        return deleted

    def update_article_html(self, article_id: int, html: str):
        """回写最终 HTML（图片占位符已解析为 CDN 外链的成品），实现「所见即所存」

        库里原本只存 content（Markdown 正文），而真正发出去的是解析后的
        formatted_html；不回写会导致无法审计/重发实际发布的内容。
        """
        if not html:
            return
        conn = self._get_conn()
        conn.execute("UPDATE articles SET html=? WHERE id=?", (html, article_id))
        conn.commit()
        logger.info(f"最终 HTML 已回写落库: id={article_id} ({len(html)} 字符)")

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

    def get_recent_articles_for_dedup(self, days: int = 30) -> list[dict[str, str]]:
        """获取最近 N 天的 {title, topic} 列表（选题去重用）

        与 get_recent_titles 的区别：同时返回 topic 列。选题 Agent 需要对
        「标题」与「选题」两个字段都做相似度比对 —— 只比对标题会漏掉
        「标题不同但选题相同」的重复，只比对 topic 会漏掉改了选题措辞的重写。

        不修改 get_recent_titles 的返回类型：hermes/agent.py 依赖其 list[str] 契约。
        articles.topic 列在建表时即存在，无需 schema 迁移。
        """
        cutoff_iso = (datetime.now() - timedelta(days=days)).isoformat()
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT title, topic, created_at FROM articles "
            "WHERE created_at >= ? ORDER BY created_at DESC",
            (cutoff_iso,),
        ).fetchall()
        # 与 get_recent_titles 一致：兜底修正含时区导致字典序偏差的行
        cutoff = datetime.fromisoformat(cutoff_iso)
        return [
            {"title": row[0] or "", "topic": row[1] or ""}
            for row in rows
            if (created := _parse_dt(row[2])) is not None and created >= cutoff
        ]

    def get_article_count(self, days: int = 7, status: str | None = None) -> int:
        """获取最近 N 天的文章数量

        Args:
            days: 统计窗口天数
            status: 可选状态过滤（如 "published" = 只统计已发表）。
                None = 统计全部行（含未发表的草稿/失败运行）。

        计数下推到 SQL（WHERE created_at >= ?），避免拉全表后在 Python 端统计。

        注意：调度器的「每日发布上限」必须用 status="published" 调用，
        否则失败/草稿运行会占用配额，导致当天后续定时任务被静默跳过。
        """
        cutoff_iso = (datetime.now() - timedelta(days=days)).isoformat()
        conn = self._get_conn()
        # COUNT 直接由 SQLite 在索引上完成，无需把每行 created_at 拉回 Python。
        if status:
            return conn.execute(
                "SELECT COUNT(*) FROM articles WHERE created_at >= ? AND status = ?",
                (cutoff_iso, status),
            ).fetchone()[0]
        return conn.execute(
            "SELECT COUNT(*) FROM articles WHERE created_at >= ?",
            (cutoff_iso,),
        ).fetchone()[0]

    # ── 运营增长：竞品标题 ───────────────────────────────
    def save_competitor_titles(self, rows: list[dict]) -> int:
        """批量写入竞品标题（调用方已去重）。返回写入条数。

        rows: [{"title","source","keyword","url"}]
        """
        if not rows:
            return 0
        now = datetime.now().isoformat()
        conn = self._get_conn()
        inserted = 0
        for r in rows:
            title = (r.get("title") or "").strip()
            if not title:
                continue
            conn.execute(
                "INSERT INTO competitor_titles (title, source, keyword, url, collected_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    title[:500],
                    str(r.get("source") or "")[:120],
                    str(r.get("keyword") or "")[:120],
                    str(r.get("url") or "")[:500],
                    now,
                ),
            )
            inserted += 1
        conn.commit()
        logger.info(f"竞品标题已入库: {inserted} 条")
        return inserted

    def get_competitor_titles(self, limit: int = 200) -> list[dict]:
        """读取竞品标题池（按采集时间倒序）"""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT title, source, keyword, url, collected_at "
            "FROM competitor_titles ORDER BY collected_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ── 运营增长：标题模式库 ─────────────────────────────
    def save_title_patterns(self, rows: list[dict]) -> int:
        """批量写入标题模式库（调用方已做类型唯一去重）。返回写入条数。

        rows: [{"pattern_type","pattern","hit_count","sample"}]
        """
        if not rows:
            return 0
        now = datetime.now().isoformat()
        conn = self._get_conn()
        inserted = 0
        for r in rows:
            ptype = str(r.get("pattern_type") or "").strip()
            pattern = str(r.get("pattern") or "").strip()
            if not ptype or not pattern:
                continue
            conn.execute(
                "INSERT INTO title_patterns "
                "(pattern_type, pattern, hit_count, sample, collected_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    ptype[:40],
                    pattern[:120],
                    int(r.get("hit_count") or 0),
                    str(r.get("sample") or "")[:300],
                    now,
                ),
            )
            inserted += 1
        conn.commit()
        logger.info(f"标题模式已入库: {inserted} 条")
        return inserted

    def get_title_patterns(self, limit: int = 200) -> list[dict]:
        """读取标题模式库（按命中次数倒序），供选题评分器使用"""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT pattern_type, pattern, hit_count, sample "
            "FROM title_patterns ORDER BY hit_count DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ── 运营增长：自有文章表现 ───────────────────────────
    def save_article_metrics(self, rows: list[dict]) -> int:
        """批量写入自有文章表现数据（阅读/分享/点赞）。返回写入条数。

        rows: [{"article_id","title","publish_url","read_count",
                "share_count","like_count"}]
        """
        if not rows:
            return 0
        now = datetime.now().isoformat()
        conn = self._get_conn()
        inserted = 0
        for r in rows:
            title = (r.get("title") or "").strip()
            if not title and not r.get("publish_url"):
                continue
            conn.execute(
                "INSERT INTO article_metrics "
                "(article_id, title, publish_url, read_count, share_count, "
                "like_count, collected_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    r.get("article_id"),
                    title[:500],
                    str(r.get("publish_url") or "")[:500],
                    int(r.get("read_count") or 0),
                    int(r.get("share_count") or 0),
                    int(r.get("like_count") or 0),
                    now,
                ),
            )
            inserted += 1
        conn.commit()
        logger.info(f"自有文章表现数据已入库: {inserted} 条")
        return inserted

    def get_article_metrics(self, limit: int = 500) -> list[dict]:
        """读取自有文章表现数据（按采集时间倒序）"""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT title, publish_url, read_count, share_count, like_count, "
            "collected_at FROM article_metrics ORDER BY collected_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ── 运营增长：选题库 ─────────────────────────────────
    def save_topic_bank(self, rows: list[dict]) -> int:
        """批量写入评分后的候选选题库。返回写入条数。

        rows: [{"title","angle","why","direction","score","score_detail",
                "pattern","evidence","confidence"}]
        """
        if not rows:
            return 0
        now = datetime.now().isoformat()
        conn = self._get_conn()
        inserted = 0
        for r in rows:
            title = (r.get("title") or "").strip()
            if not title:
                continue
            conn.execute(
                "INSERT INTO topic_bank "
                "(title, angle, why, direction, score, score_detail, pattern, "
                "evidence, confidence, used, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
                (
                    title[:300],
                    str(r.get("angle") or "")[:500],
                    str(r.get("why") or "")[:500],
                    str(r.get("direction") or "")[:40],
                    float(r.get("score") or 0.0),
                    json.dumps(r.get("score_detail") or {}, ensure_ascii=False)[:1000],
                    str(r.get("pattern") or "")[:200],
                    str(r.get("evidence") or "")[:500],
                    str(r.get("confidence") or "medium")[:20],
                    now,
                ),
            )
            inserted += 1
        conn.commit()
        logger.info(f"选题库已入库: {inserted} 条")
        return inserted

    def get_top_topics(self, limit: int = 10) -> list[dict]:
        """按评分降序取选题库 Top N（扫描未使用优先，再按分数）"""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT id, title, angle, direction, score, pattern, evidence, "
            "confidence, created_at FROM topic_bank "
            "ORDER BY used ASC, score DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def mark_topic_used(self, topic_id: int) -> None:
        """标记某选题已被采用（used=1），避免重复推荐"""
        conn = self._get_conn()
        conn.execute("UPDATE topic_bank SET used=1 WHERE id=?", (topic_id,))
        conn.commit()
