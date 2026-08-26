"""tests for tools/publish_lock.py - FIFO + queue cleanup"""

import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.publish_lock import (
    PublishLock,
    acquire_publish_lock,
    cleanup_publish_lock_queue,
)


class TestPublishLock(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), ".publish.lock")

    def _wait_tickets(self, lock, n, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if len(lock._list_tickets()) >= n:
                return
            time.sleep(0.01)
        self.fail(f"wait {n} tickets timeout, got {lock._list_tickets()}")

    # ---- basic ----

    def test_acquire_and_release(self):
        lock = PublishLock(self.path)
        self.assertTrue(lock.acquire())
        lock.release()
        self.assertTrue(lock.acquire())
        lock.release()

    def test_second_acquire_fails_nonblocking(self):
        l1 = PublishLock(self.path)
        self.assertTrue(l1.acquire())
        try:
            l2 = PublishLock(self.path)
            self.assertFalse(l2.acquire(timeout=0))
        finally:
            l1.release()

    def test_wait_timeout_expires_then_returns_false(self):
        l1 = PublishLock(self.path)
        self.assertTrue(l1.acquire())
        try:
            l2 = PublishLock(self.path)
            start = time.monotonic()
            self.assertFalse(l2.acquire(timeout=0.4))
            elapsed = time.monotonic() - start
            self.assertGreaterEqual(elapsed, 0.3)
        finally:
            l1.release()

    def test_wait_succeeds_after_release(self):
        released = threading.Event()
        acquired = threading.Event()
        holder = PublishLock(self.path)

        def hold_then_release():
            self.assertTrue(holder.acquire())
            acquired.set()
            released.wait(timeout=5)
            time.sleep(0.3)
            holder.release()

        t = threading.Thread(target=hold_then_release)
        t.start()
        try:
            self.assertTrue(acquired.wait(timeout=5))
            waiter = PublishLock(self.path)
            released.set()
            self.assertTrue(waiter.acquire(timeout=5))
            waiter.release()
        finally:
            released.set()
            t.join(timeout=5)

    def test_negative_timeout_treated_as_nonblocking(self):
        l1 = PublishLock(self.path)
        self.assertTrue(l1.acquire())
        try:
            l2 = PublishLock(self.path)
            start = time.monotonic()
            self.assertFalse(l2.acquire(timeout=-1))
            self.assertLess(time.monotonic() - start, 1.0)
        finally:
            l1.release()

    def test_acquire_publish_lock_helper(self):
        lock = acquire_publish_lock(self.path, timeout=0)
        self.assertIsNotNone(lock)
        self.assertIsNone(acquire_publish_lock(self.path, timeout=0))
        lock.release()
        again = acquire_publish_lock(self.path, timeout=0)
        self.assertIsNotNone(again)
        again.release()

    # ---- FIFO ----

    def test_fast_path_creates_no_queue(self):
        lock = PublishLock(self.path)
        self.assertTrue(lock.acquire())
        self.assertFalse(os.path.exists(lock.queue_dir))
        lock.release()

    def test_fifo_order_first_arrival_wins(self):
        holder = PublishLock(self.path)
        self.assertTrue(holder.acquire())
        order = []
        w1 = PublishLock(self.path)
        w2 = PublishLock(self.path)

        def waiter(lock, label):
            if lock.acquire(timeout=10):
                order.append(label)
                lock.release()

        t1 = threading.Thread(target=waiter, args=(w1, "first"))
        t1.start()
        self._wait_tickets(w1, 1)
        t2 = threading.Thread(target=waiter, args=(w2, "second"))
        t2.start()
        self._wait_tickets(w1, 2)
        holder.release()
        t1.join(timeout=10)
        t2.join(timeout=10)
        self.assertEqual(order, ["first", "second"])

    def test_fifo_timeout_removes_own_ticket(self):
        holder = PublishLock(self.path)
        self.assertTrue(holder.acquire())
        try:
            waiter = PublishLock(self.path)
            self.assertFalse(waiter.acquire(timeout=0.3))
            self.assertEqual(len(waiter._list_tickets()), 0)
        finally:
            holder.release()

    def test_stale_head_reaped(self):
        lock = PublishLock(self.path)
        os.makedirs(lock.queue_dir, exist_ok=True)
        with open(os.path.join(lock.queue_dir, "0001.tkt"), "w") as f:
            f.write(str(time.time() - 10))
        with open(os.path.join(lock.queue_dir, "0002.tkt"), "w") as f:
            f.write(str(time.time() + 60))
        lock._reap_stale_heads()
        self.assertEqual(lock._list_tickets(), ["0002.tkt"])

    def test_unreadable_ticket_treated_as_stale(self):
        lock = PublishLock(self.path)
        os.makedirs(lock.queue_dir, exist_ok=True)
        with open(os.path.join(lock.queue_dir, "0001.tkt"), "w") as f:
            f.write("not-a-float")
        lock._reap_stale_heads()
        self.assertEqual(lock._list_tickets(), [])

    # ---- queue cleanup ----

    def test_cleanup_empty_queue_on_release(self):
        lock = PublishLock(self.path)
        self.assertTrue(lock.acquire())
        lock.release()
        self.assertFalse(os.path.exists(lock.queue_dir))

    def test_cleanup_empty_queue_on_fast_path(self):
        lock = PublishLock(self.path)
        os.makedirs(lock.queue_dir, exist_ok=True)
        self.assertTrue(lock.acquire())
        self.assertFalse(os.path.exists(lock.queue_dir))
        lock.release()

    def test_cleanup_queue_not_empty_not_cleaned(self):
        lock = PublishLock(self.path)
        os.makedirs(lock.queue_dir, exist_ok=True)
        with open(os.path.join(lock.queue_dir, "0001.tkt"), "w") as f:
            f.write(str(time.time() + 60))
        self.assertTrue(lock.acquire(timeout=0))
        self.assertTrue(os.path.isdir(lock.queue_dir))
        lock.release()

    def test_cleanup_publish_lock_queue_helper(self):
        lock = PublishLock(self.path)
        os.makedirs(lock.queue_dir, exist_ok=True)
        with open(os.path.join(lock.queue_dir, "0001.tkt"), "w") as f:
            f.write(str(time.time() - 3600))
        with open(os.path.join(lock.queue_dir, "0002.tkt"), "w") as f:
            f.write(str(time.time() - 7200))
        os.utime(lock.queue_dir, (time.time() - 25 * 3600, time.time() - 25 * 3600))
        stale, dir_removed = cleanup_publish_lock_queue(self.path, idle_hours=24.0)
        self.assertEqual(stale, 2)
        self.assertTrue(dir_removed)
        self.assertFalse(os.path.exists(lock.queue_dir))

    def test_cleanup_queue_with_active_tickets_not_removed(self):
        lock = PublishLock(self.path)
        os.makedirs(lock.queue_dir, exist_ok=True)
        with open(os.path.join(lock.queue_dir, "0001.tkt"), "w") as f:
            f.write(str(time.time() - 3600))
        with open(os.path.join(lock.queue_dir, "0002.tkt"), "w") as f:
            f.write(str(time.time() + 3600))
        os.utime(lock.queue_dir, (time.time() - 25 * 3600, time.time() - 25 * 3600))
        stale, dir_removed = cleanup_publish_lock_queue(self.path, idle_hours=24.0)
        self.assertEqual(stale, 1)
        self.assertFalse(dir_removed)
        self.assertTrue(os.path.isdir(lock.queue_dir))
        self.assertEqual(lock._list_tickets(), ["0002.tkt"])

    def test_cleanup_queue_not_idle_enough_skipped(self):
        lock = PublishLock(self.path)
        os.makedirs(lock.queue_dir, exist_ok=True)
        with open(os.path.join(lock.queue_dir, "0001.tkt"), "w") as f:
            f.write(str(time.time() - 3600))
        os.utime(lock.queue_dir, (time.time() - 3600, time.time() - 3600))
        stale, dir_removed = cleanup_publish_lock_queue(self.path, idle_hours=24.0)
        self.assertEqual(stale, 0)
        self.assertFalse(dir_removed)

    def test_cleanup_queue_dir_not_exist(self):
        stale, dir_removed = cleanup_publish_lock_queue(self.path, idle_hours=24.0)
        self.assertEqual(stale, 0)
        self.assertFalse(dir_removed)


if __name__ == "__main__":
    unittest.main()