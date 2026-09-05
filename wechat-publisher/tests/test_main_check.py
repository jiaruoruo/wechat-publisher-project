"""tests for main.py cmd_check 的截图保留清理

cmd_check 除了展示配置，还会按 browser.screenshot_retention_days
实际执行 cleanup_old_screenshots，防止截图无限堆积。
"""

import io
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import main as main_mod
    MAIN_AVAILABLE = True
except ImportError:
    MAIN_AVAILABLE = False


# 测试期间创建的临时目录，由 tearDownModule 统一清理（避免在项目根目录堆积 tmp*）
_TMPDIRS: list[str] = []


def _config(retention=30, lock_timeout=0):
    """构造测试用 config；screenshot_dir 用临时目录，登记后由 tearDownModule 统一清理"""
    tmpdir = tempfile.mkdtemp()
    _TMPDIRS.append(tmpdir)
    return {
        "models": {},
        "content": {},
        "wechat": {"publish_mode": "draft", "publish_lock_timeout": lock_timeout},
        "browser": {
            "screenshot_dir": tmpdir,
            "screenshot_retention_days": retention,
        },
        "feishu": {},
    }


def tearDownModule():
    """统一清理测试期间创建的临时目录，避免在项目根目录堆积 tmp*"""
    while _TMPDIRS:
        shutil.rmtree(_TMPDIRS.pop(), True)


@unittest.skipUnless(MAIN_AVAILABLE, "运行时依赖缺失")
class CmdCheckCleanupTest(unittest.TestCase):
    def _run_check(self, config, selector_issues=()):
        buf = io.StringIO()
        with mock.patch.object(main_mod, "load_config", return_value=config), \
             mock.patch.object(main_mod, "validate_config", return_value=[]), \
             mock.patch.object(main_mod, "validate_selectors_config", return_value=list(selector_issues)), \
             redirect_stdout(buf):
            main_mod.cmd_check(mock.Mock())
        return buf.getvalue()

    def test_check_reports_selector_config_issues(self):
        out = self._run_check(
            _config(30),
            selector_issues=[
                ("warning", "选择器配置缺少分组 toast（该分组回退内置默认值）"),
                ("error", "选择器分组 title_inputs 为空列表（该交互将无法执行）"),
            ],
        )
        self.assertIn("[警告] 选择器配置缺少分组", out)
        self.assertIn("[错误] 选择器分组 title_inputs 为空列表", out)

    def test_check_selector_config_ok_line(self):
        out = self._run_check(_config(30))
        self.assertIn("选择器配置校验通过（config/selectors.yaml）", out)

    def test_check_runs_cleanup_and_reports_removed(self):
        with mock.patch("main.cleanup_old_screenshots", return_value=3) as cleanup:
            out = self._run_check(_config(30))
        cleanup.assert_called_once()
        args = cleanup.call_args[0]
        self.assertEqual(args[1], 30)  # retention 透传
        self.assertTrue(os.path.isabs(args[0]))  # 截图目录转绝对路径
        self.assertIn("删除 3 个过期日期目录/文件", out)

    def test_check_cleanup_noop_prints_nothing_to_clean(self):
        with mock.patch("main.cleanup_old_screenshots", return_value=0) as cleanup:
            out = self._run_check(_config(30))
        cleanup.assert_called_once()
        self.assertIn("无需清理", out)

    def test_check_cleanup_disabled_when_retention_zero(self):
        with mock.patch("main.cleanup_old_screenshots", return_value=0) as cleanup:
            out = self._run_check(_config(0))
        cleanup.assert_called_once()
        self.assertEqual(cleanup.call_args[0][1], 0)
        self.assertIn("截图保留策略: 0 天", out)

    def test_check_cleanup_failure_does_not_crash(self):
        with mock.patch("main.cleanup_old_screenshots", side_effect=OSError("磁盘错误")) as cleanup:
            out = self._run_check(_config(30))
        cleanup.assert_called_once()
        self.assertIn("截图保留清理失败", out)

    def test_check_shows_publish_lock_timeout(self):
        out = self._run_check(_config(30, lock_timeout=30))
        self.assertIn("发布锁等待超时: 30 秒", out)
        self.assertIn("0 = 立即跳过", out)

    def test_check_invalid_publish_lock_timeout_falls_back(self):
        out = self._run_check(_config(30, lock_timeout="bad"))
        self.assertIn("发布锁等待超时: 配置非法", out)
        self.assertIn("按 0 秒（立即跳过）处理", out)


if __name__ == "__main__":
    unittest.main()
