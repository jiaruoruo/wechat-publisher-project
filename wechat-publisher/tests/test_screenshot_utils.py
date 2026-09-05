"""tests for browser/screenshot_utils.py 与 wechat_session.take_screenshot 命名

覆盖：
- screenshot_path：日期子目录、时间戳后缀、目录自动创建、两次调用不重名
- publish_actions / wechat_session 共用同一规范（命名一致）
- cleanup_old_screenshots：按保留天数清理过期日期目录与遗留根目录 PNG
"""

import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from browser.screenshot_utils import screenshot_path, cleanup_old_screenshots

try:
    from browser.wechat_session import WechatSession
    WS_AVAILABLE = True
except ImportError:
    WS_AVAILABLE = False


class ScreenshotPathTest(unittest.TestCase):
    def test_path_has_date_subdir_and_timestamp(self):
        with tempfile.TemporaryDirectory() as d:
            path = screenshot_path(d, "pre_publish_preview")
            # tmpdir/YYYYMMDD/name_YYYYMMDD_HHMMSS_ffffff.png
            day = datetime.now().strftime("%Y%m%d")
            self.assertEqual(os.path.dirname(os.path.dirname(path)), d)
            self.assertEqual(os.path.basename(os.path.dirname(path)), day)
            self.assertRegex(
                os.path.basename(path),
                r"^pre_publish_preview_\d{8}_\d{6}_\d{6}\.png$",
            )

    def test_directory_auto_created(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        base = os.path.join(tmp, "not", "exists", "shots")
        path = screenshot_path(base, "a")
        self.assertTrue(os.path.isdir(os.path.dirname(path)))
        self.assertTrue(os.path.isdir(base))

    def test_two_calls_do_not_collide(self):
        with tempfile.TemporaryDirectory() as d:
            p1 = screenshot_path(d, "draft_saved")
            p2 = screenshot_path(d, "draft_saved")
            self.assertNotEqual(p1, p2)


class CleanupOldScreenshotsTest(unittest.TestCase):
    """cleanup_old_screenshots 保留策略"""

    @staticmethod
    def _day_dir(d, offset):
        day = (datetime.now().date() - timedelta(days=offset)).strftime("%Y%m%d")
        path = os.path.join(d, day)
        os.makedirs(path, exist_ok=True)
        with open(os.path.join(path, "x.png"), "w", encoding="utf-8") as f:
            f.write("x")
        return path

    def test_removes_old_dirs_keeps_recent(self):
        with tempfile.TemporaryDirectory() as d:
            old = self._day_dir(d, 40)
            new = self._day_dir(d, 1)
            removed = cleanup_old_screenshots(d, retention_days=30)
            self.assertEqual(removed, 1)
            self.assertFalse(os.path.exists(old))
            self.assertTrue(os.path.exists(new))

    def test_boundary_day_is_kept(self):
        # 恰好 retention_days 天前（cutoff 当天）应保留：day < cutoff 才删
        with tempfile.TemporaryDirectory() as d:
            boundary = self._day_dir(d, 30)
            self.assertEqual(cleanup_old_screenshots(d, retention_days=30), 0)
            self.assertTrue(os.path.exists(boundary))

    def test_non_date_dir_untouched(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "misc"))
            self.assertEqual(cleanup_old_screenshots(d, 30), 0)
            self.assertTrue(os.path.exists(os.path.join(d, "misc")))

    def test_disabled_when_non_positive(self):
        with tempfile.TemporaryDirectory() as d:
            old = self._day_dir(d, 100)
            self.assertEqual(cleanup_old_screenshots(d, 0), 0)
            self.assertEqual(cleanup_old_screenshots(d, -5), 0)
            self.assertTrue(os.path.exists(old))

    def test_removes_old_root_png_keeps_recent(self):
        with tempfile.TemporaryDirectory() as d:
            old_path = os.path.join(d, "old_legacy.png")
            new_path = os.path.join(d, "new_legacy.png")
            for p in (old_path, new_path):
                with open(p, "w", encoding="utf-8") as f:
                    f.write("x")
            old_ts = (datetime.now() - timedelta(days=40)).timestamp()
            os.utime(old_path, (old_ts, old_ts))
            removed = cleanup_old_screenshots(d, 30)
            self.assertEqual(removed, 1)
            self.assertFalse(os.path.exists(old_path))
            self.assertTrue(os.path.exists(new_path))

    def test_missing_dir_returns_zero(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        self.assertEqual(
            cleanup_old_screenshots(os.path.join(tmp, "nope"), 30), 0
        )

    def test_invalid_retention_config_safe(self):
        with tempfile.TemporaryDirectory() as d:
            self._day_dir(d, 100)
            self.assertEqual(cleanup_old_screenshots(d, "abc"), 0)


class _FakePage:
    def __init__(self):
        self.paths = []

    def screenshot(self, path=None, full_page=False):
        self.paths.append(path)
        return path


@unittest.skipUnless(WS_AVAILABLE, "需要 playwright 等运行时依赖")
class WechatSessionScreenshotTest(unittest.TestCase):
    """wechat_session.take_screenshot 与 publish_actions 命名统一（日期归档 + 时间戳）"""

    def test_take_screenshot_timestamped_and_archived(self):
        with tempfile.TemporaryDirectory() as d:
            session = WechatSession({"browser": {"screenshot_dir": d}})
            fake = _FakePage()
            session._page = fake
            path = session.take_screenshot("final_state")
            self.assertEqual(fake.paths, [path])
            self.assertEqual(os.path.dirname(os.path.dirname(path)), d)
            self.assertRegex(
                os.path.basename(path),
                r"^final_state_\d{8}_\d{6}_\d{6}\.png$",
            )

    def test_take_screenshot_requires_started_browser(self):
        with tempfile.TemporaryDirectory() as d:
            session = WechatSession({"browser": {"screenshot_dir": d}})
            with self.assertRaises(RuntimeError):
                session.take_screenshot("x")


if __name__ == "__main__":
    unittest.main()
