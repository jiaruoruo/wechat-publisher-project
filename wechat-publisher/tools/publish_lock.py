"""跨进程发布互斥锁 — 防止多个入口并发操作同一浏览器会话/同一篇文章

背景：Hermes /start 后台线程、scheduler 定时任务、CLI `run` 都会执行
ArticleWorkflow → PublisherAgent（复用已登录的 WechatSession）。它们可能同时
运行，当前只有 Hermes 内的 `running` 标志，scheduler/CLI 不受其约束。

实现：基于文件锁（storage/.publish.lock）的互斥，并在其上叠加**票据队列**：
- POSIX 用 fcntl.flock(LOCK_EX|LOCK_NB)，Windows 用 msvcrt.locking(LK_NBLCK)；
- acquire(timeout)：无竞争时直接取锁（快速路径）；有竞争且 timeout>0 时，
  各等待者注册一张「到达顺序」票据（storage/.publish.lock.queue/*.tkt），
  只有队首票据才有资格取锁——**FIFO 公平，而非轮询竞争**；
- 超时/放弃时移除自身票据；崩溃遗留的票据按票据内记录的 deadline 被后续
  等待者自动回收（不会永久卡住队列）；
- 进程退出/句柄关闭自动释放实际锁，无陈旧锁问题；
- 平台无锁支持或锁文件不可用时退化为无锁执行（放行，不阻断发布）；
  票据队列不可用（目录不可写）时退化为普通轮询（仍保证互斥，仅失去公平）。

用法（见 graph/workflow.py）：
    lock = acquire_publish_lock(timeout=30)
    if lock is None:
        # 等待 30s 后仍被占用，跳过
    try:
        ...
    finally:
        lock.release()
"""

import itertools
import logging
import os
import time

from config.paths import PUBLISH_LOCK_PATH
from config.structured_logging import log_event

logger = logging.getLogger(__name__)

try:
    import fcntl

    _HAS_FCNTL = True
except ImportError:  # pragma: no cover - Windows
    _HAS_FCNTL = False

try:
    import msvcrt

    _HAS_MSVCRT = True
except ImportError:  # pragma: no cover - POSIX
    _HAS_MSVCRT = False

# 等待轮询间隔（秒）：足够小以便及时感知释放，又不至于空转耗 CPU
_POLL_INTERVAL = 0.2

# 进程内票据序号（保证同一进程内票据文件名唯一；跨进程由 pid 区分）
_TICKET_SEQ = itertools.count()


class _LockUnavailable(Exception):
    """实际锁文件无法建立（如目录不可写）——调用方应退化为无锁执行而非等待"""


class _QueueUnavailable(Exception):
    """票据队列目录/文件无法建立——调用方退化为普通轮询（失去 FIFO 公平）"""


class PublishLock:
    """基于文件锁的跨进程互斥（有限等待 + FIFO 公平排队）"""

    def __init__(self, path: str | None = None):
        self.path = path or PUBLISH_LOCK_PATH
        self.queue_dir = self.path + ".queue"  # 票据队列目录
        self._fd: int | None = None
        self._acquire_start: float | None = None  # acquire() 调用时刻，用于计算等待/持有耗时

    def acquire(self, timeout: float = 0.0) -> bool:
        """尝试获取锁；已被占用时最多等待 timeout 秒，超时才返回 False

        有竞争且 timeout>0 时按到达顺序（FIFO）排队，先到者先得。

        Args:
            timeout: 等待秒数（0 = 非阻塞立即返回）。负数按 0 处理。

        Returns:
            True 已取得锁（或锁机制不可用、选择放行）；False 等待超时仍被占用。
        """
        self._acquire_start = time.time()

        if not _HAS_FCNTL and not _HAS_MSVCRT:
            logger.warning("当前平台无文件锁支持，退化为无锁执行")
            log_event(
                "publish_lock.unavailable",
                level="warning",
                reason="no_file_lock_support",
                timeout=timeout,
            )
            return True

        # 快速路径：无竞争直接取锁；顺带清理遗留的空队列目录
        try:
            if self._try_acquire_once():
                self._cleanup_queue_if_empty()
                waited_ms = (time.time() - self._acquire_start) * 1000
                log_event(
                    "publish_lock.acquired",
                    mode="fast",
                    waited_ms=round(waited_ms, 1),
                    timeout=timeout,
                )
                return True
        except _LockUnavailable:
            self._cleanup_queue_if_empty()
            log_event(
                "publish_lock.unavailable",
                level="warning",
                reason="lock_file_unavailable",
                timeout=timeout,
            )
            return True  # 锁机制不可用 → 放行

        if timeout <= 0:
            return False

        # 竞争路径：FIFO 票据排队；队列不可用时退化为普通轮询（仍保证互斥）
        try:
            return self._acquire_fifo(timeout)
        except _QueueUnavailable as e:
            logger.warning(f"{e}，退化为普通轮询等待（失去 FIFO 公平性）")
            log_event(
                "publish_lock.waiting",
                level="warning",
                mode="polling_fallback",
                timeout=timeout,
                reason=str(e),
            )
            return self._acquire_polling(timeout)

    # ── 实际文件锁 ──────────────────────────────────────────────

    def _try_acquire_once(self) -> bool:
        """单次非阻塞尝试：成功返回 True；锁被占用返回 False；锁机制不可用抛 _LockUnavailable"""
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o644)
        except OSError as e:
            raise _LockUnavailable("打开发布锁文件失败") from e

        try:
            if _HAS_FCNTL:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            elif _HAS_MSVCRT:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except (BlockingIOError, OSError):
            # 锁已被其他进程持有
            os.close(fd)
            return False

        self._fd = fd
        return True

    def release(self) -> None:
        """释放锁并关闭句柄；若队列已空则清扫队列目录"""
        if self._fd is None:
            return
        held_ms = (
            (time.time() - self._acquire_start) * 1000
            if self._acquire_start is not None
            else 0
        )
        try:
            if _HAS_FCNTL:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            elif _HAS_MSVCRT:
                os.lseek(self._fd, 0, os.SEEK_SET)
                msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        finally:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
            self._cleanup_queue_if_empty()
        log_event(
            "publish_lock.released",
            held_ms=round(held_ms, 1),
        )

    # ── FIFO 票据队列 ──────────────────────────────────────────

    def _acquire_fifo(self, timeout: float) -> bool:
        """FIFO 排队获取锁：队首才有资格取锁，先到先得"""
        deadline = time.time() + timeout
        ticket = self._register_ticket(deadline)
        position = self._ticket_position(ticket)
        queue_depth = len(self._list_tickets())
        log_event(
            "publish_lock.waiting",
            mode="fifo",
            ticket=ticket,
            position=position,
            queue_depth=queue_depth,
            timeout=timeout,
        )
        logger.info(
            f"发布锁被占用，进入 FIFO 排队（位置 {position}/{queue_depth}，"
            f"最多等待 {timeout:.0f} 秒）"
        )
        try:
            while True:
                self._reap_stale_heads()
                head = self._head_ticket()
                if head == ticket:
                    try:
                        if self._try_acquire_once():
                            self._remove_ticket(ticket)
                            waited_ms = (time.time() - self._acquire_start) * 1000  if self._acquire_start else 0
                            log_event(
                                "publish_lock.acquired",
                                mode="fifo",
                                waited_ms=round(waited_ms, 1),
                                ticket=ticket,
                                timeout=timeout,
                            )
                            logger.info(f"发布锁已释放，取得执行权（等待 {waited_ms/1000:.1f}s）")
                            return True
                    except _LockUnavailable:
                        self._remove_ticket(ticket)
                        log_event(
                            "publish_lock.unavailable",
                            level="warning",
                            reason="lock_file_unavailable_during_fifo",
                            ticket=ticket,
                        )
                        return True  # 锁机制不可用 → 放行
                remaining = deadline - time.time()
                if remaining <= 0:
                    self._remove_ticket(ticket)
                    waited_ms = (deadline - (deadline - timeout)) * 1000
                    queue_depth = len(self._list_tickets())
                    log_event(
                        "publish_lock.timeout",
                        level="warning",
                        waited_ms=round(waited_ms, 1),
                        ticket=ticket,
                        queue_depth_at_timeout=queue_depth,
                        timeout=timeout,
                    )
                    return False
                time.sleep(min(_POLL_INTERVAL, remaining))
        except Exception:
            self._remove_ticket(ticket)
            raise

    def _acquire_polling(self, timeout: float) -> bool:
        """普通轮询（无 FIFO 公平），作为票据队列不可用时的兜底"""
        deadline = time.monotonic() + timeout
        while True:
            try:
                if self._try_acquire_once():
                    waited_ms = (time.time() - self._acquire_start) * 1000 if self._acquire_start else 0
                    log_event(
                        "publish_lock.acquired",
                        mode="polling",
                        waited_ms=round(waited_ms, 1),
                        timeout=timeout,
                    )
                    return True
            except _LockUnavailable:
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                waited_ms = timeout * 1000
                log_event(
                    "publish_lock.timeout",
                    level="warning",
                    waited_ms=round(waited_ms, 1),
                    mode="polling",
                    timeout=timeout,
                )
                return False
            time.sleep(min(_POLL_INTERVAL, remaining))

    def _register_ticket(self, deadline: float) -> str:
        """注册一张票据，返回票据文件名；文件名按到达时间排序即 FIFO 顺序"""
        try:
            os.makedirs(self.queue_dir, exist_ok=True)
        except OSError as e:
            raise _QueueUnavailable("创建发布队列目录失败") from e

        # 文件名 = 到达时间(纳秒) + pid + 进程内序号；字典序 == 到达顺序
        name = f"{time.time_ns():020d}_{os.getpid():06d}_{next(_TICKET_SEQ):08d}.tkt"
        try:
            with open(os.path.join(self.queue_dir, name), "w", encoding="utf-8") as f:
                f.write(str(deadline))
        except OSError as e:
            raise _QueueUnavailable("写入发布队列票据失败") from e
        return name

    def _list_tickets(self) -> list[str]:
        if not os.path.isdir(self.queue_dir):
            return []
        try:
            return sorted(n for n in os.listdir(self.queue_dir) if n.endswith(".tkt"))
        except OSError:
            return []

    def _head_ticket(self) -> str | None:
        tickets = self._list_tickets()
        return tickets[0] if tickets else None

    def _ticket_position(self, ticket: str) -> int:
        """返回指定票据在队列中的位置（1-indexed）；不在队列返回 -1"""
        tickets = self._list_tickets()
        try:
            return tickets.index(ticket) + 1
        except ValueError:
            return -1

    def _ticket_deadline(self, name: str) -> float | None:
        """读取票据记录的 deadline（epoch 秒）；不可读/损坏返回 None（视为过期）"""
        try:
            with open(os.path.join(self.queue_dir, name), "r", encoding="utf-8") as f:
                return float(f.read().strip())
        except (OSError, ValueError):
            return None

    def _reap_stale_heads(self) -> None:
        """回收队首过期票据（崩溃遗留的等待者），避免永久卡住队列"""
        while True:
            head = self._head_ticket()
            if head is None:
                return
            deadline = self._ticket_deadline(head)
            if deadline is None or time.time() > deadline:
                self._remove_ticket(head)
                continue
            return

    def _remove_ticket(self, name: str) -> None:
        try:
            os.remove(os.path.join(self.queue_dir, name))
        except OSError:
            pass

    def _cleanup_queue_if_empty(self) -> bool:
        """若队列目录存在且为空则删除；返回 True 表示执行了清理"""
        if not os.path.isdir(self.queue_dir):
            return False
        try:
            entries = os.listdir(self.queue_dir)
        except OSError:
            return False
        if entries:
            return False  # 仍有等待者票据，不删
        try:
            os.rmdir(self.queue_dir)
            logger.debug("已清理空置的票据队列目录")
            return True
        except OSError:
            return False

    def _cleanup_stale_queue(self, idle_hours: float = 24.0) -> tuple[int, bool]:
        """清理长时间（idle_hours）无活动的票据队列

        返回 (stale_tickets_removed, dir_removed)。
        仅当目录 mtime 超过 idle_hours 且无活跃票据（票据 deadline 均已过期）时才清理。
        """
        if not os.path.isdir(self.queue_dir):
            return 0, False
        try:
            mtime = os.path.getmtime(self.queue_dir)
        except OSError:
            return 0, False
        age_hours = (time.time() - mtime) / 3600
        if age_hours < idle_hours:
            return 0, False

        # 检查是否有未过期的票据（有效等待者）
        removed = 0
        has_active = False
        for name in self._list_tickets():
            deadline = self._ticket_deadline(name)
            if deadline is not None and time.time() <= deadline:
                has_active = True
                continue  # 该等待者仍在有效期，保留
            self._remove_ticket(name)
            removed += 1

        if has_active:
            return removed, False

        # 没有活跃票据：删除整个队列目录
        try:
            os.rmdir(self.queue_dir)
            logger.info(
                f"票据队列已空闲 {age_hours:.1f} 小时，已回收 {removed} 张过期票据并删除队列目录"
            )
            return removed, True
        except OSError:
            return removed, False


def acquire_publish_lock(path: str | None = None, timeout: float = 0.0) -> PublishLock | None:
    """尝试获取发布锁；等待 timeout 秒后仍被占用时返回 None（调用方跳过本次执行）"""
    lock = PublishLock(path)
    return lock if lock.acquire(timeout) else None


def cleanup_publish_lock_queue(path: str | None = None, idle_hours: float = 24.0) -> tuple[int, bool]:
    """清扫长时间空置的票据队列目录（供 main.py check 等定期调用）

    Returns:
        (stale_tickets_removed, dir_removed)
    """
    lock = PublishLock(path)
    return lock._cleanup_stale_queue(idle_hours)
