"""tests for tools/notifier.py bridge registration"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.notifier import Notifier


class TestNotifierBridge(unittest.TestCase):
    def test_bridge_called_even_when_disabled(self):
        # 通知关闭时，桥接回调（如飞书推送）仍应收到事件
        n = Notifier({"notification": {"enabled": False}})
        calls = []
        n.register_bridge(lambda title, message, level="info": calls.append((title, message, level)))
        n._send_notification("测试标题", "测试内容", "warning")
        self.assertEqual(calls, [("测试标题", "测试内容", "warning")])

    def test_bridge_not_called_after_unregister(self):
        n = Notifier({"notification": {"enabled": True}})
        calls = []

        def bridge(title, message, level="info"):
            calls.append(title)

        n.register_bridge(bridge)
        n.unregister_bridge(bridge)
        n._send_notification("标题", "内容")
        self.assertEqual(calls, [])

    def test_duplicate_registration_ignored(self):
        n = Notifier({"notification": {"enabled": True}})
        calls = []

        def bridge(title, message, level="info"):
            calls.append(title)

        n.register_bridge(bridge)
        n.register_bridge(bridge)
        n._send_notification("标题", "内容")
        self.assertEqual(len(calls), 1)

    def test_bridge_exception_does_not_break_send(self):
        n = Notifier({"notification": {"enabled": True}})

        def bad_bridge(title, message, level="info"):
            raise RuntimeError("bridge failure")

        n.register_bridge(bad_bridge)
        # 不应抛出异常
        n._send_notification("标题", "内容")


if __name__ == "__main__":
    unittest.main()
