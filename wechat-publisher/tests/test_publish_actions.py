"""tests for browser/publish_actions.py

- TestCoverCdnPure: 纯方法（prepend_cover_image、upload_cover_to_cdn 路径校验）
- TestExecutePublishCoverInBody: mock page 交互后 execute_publish 的 cover_in_body 分支
- TestSetCoverFromBody: set_cover_from_body 的多轮选择器回退与失败诊断
- TestTrySelectInfrastructure: _try_select / _try_fill / _diagnose_dom 核心基础设施
"""

import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from browser.publish_actions import PublishActions
    PA_AVAILABLE = True
except ImportError:
    PA_AVAILABLE = False


@unittest.skipUnless(PA_AVAILABLE, "需要 playwright 等运行时依赖")
class TestCoverCdnPure(unittest.TestCase):
    def setUp(self):
        self.pa = PublishActions(None, {})

    def test_prepend_cover_image(self):
        html = "<section><p>正文</p></section>"
        out = self.pa.prepend_cover_image(html, "https://mmbiz.qpic.cn/cover")
        self.assertTrue(out.startswith('<section style="text-align:center;">'), out)
        self.assertIn('src="https://mmbiz.qpic.cn/cover"', out)
        self.assertTrue(out.endswith(html), out)

    def test_prepend_cover_image_empty_url(self):
        out = self.pa.prepend_cover_image("<p>正文</p>", "https://cdn.example/x")
        self.assertIn('src="https://cdn.example/x"', out)

    def test_upload_cover_to_cdn_empty_path(self):
        self.assertEqual(self.pa.upload_cover_to_cdn(""), "")

    def test_upload_cover_to_cdn_missing_file(self):
        missing = os.path.join(tempfile.mkdtemp(), "no_such_cover.png")
        self.addCleanup(shutil.rmtree, os.path.dirname(missing), True)
        self.assertEqual(self.pa.upload_cover_to_cdn(missing), "")


def _make_pa(wechat_cfg: dict):
    """构造一个所有 page 交互都被 mock 掉的 PublishActions（只测分支逻辑）

    返回 (pa, tmpdir)：调用方负责注册 shutil.rmtree 清理，避免临时目录泄漏。
    """
    tmpdir = tempfile.mkdtemp()
    pa = PublishActions(
        None,
        {"wechat": wechat_cfg, "browser": {"screenshot_dir": tmpdir}},
    )
    pa.navigate_to_editor = mock.Mock()
    pa.upload_cover_to_cdn = mock.Mock(return_value="https://mmbiz.qpic.cn/cover")
    pa.fill_title = mock.Mock()
    pa.replace_inline_images = mock.Mock(side_effect=lambda h, imgs: h)
    pa.prepend_cover_image = mock.Mock(
        side_effect=lambda h, u: "<section>COVER</section>" + h
    )
    pa.fill_content_html = mock.Mock()
    pa.set_cover_from_body = mock.Mock(return_value=True)
    pa.upload_cover_image = mock.Mock()
    pa.fill_summary = mock.Mock()
    pa.preview_before_publish = mock.Mock(return_value="")
    pa.save_draft = mock.Mock(return_value=True)
    pa.publish = mock.Mock(return_value=True)
    return pa, tmpdir


@unittest.skipUnless(PA_AVAILABLE, "需要 playwright 等运行时依赖")
class TestExecutePublishCoverInBody(unittest.TestCase):
    """cover_in_body 配置对 execute_publish 封面流程的分支影响"""

    def _run(self, wechat_cfg: dict, cover_path: str = "cover.png"):
        pa, tmpdir = _make_pa(wechat_cfg)
        self.addCleanup(shutil.rmtree, tmpdir, True)
        res = pa.execute_publish("标题", "<p>正文</p>", cover_path, "摘要", ["a.png"])
        return pa, res

    def test_default_true_prepend_and_select_from_body(self):
        pa, res = self._run({"external_images": True})
        self.assertTrue(res["success"])
        pa.prepend_cover_image.assert_called_once()
        self.assertEqual(
            pa.prepend_cover_image.call_args[0][1], "https://mmbiz.qpic.cn/cover"
        )
        pa.set_cover_from_body.assert_called_once()
        pa.upload_cover_image.assert_not_called()

    def test_true_falls_back_to_file_upload(self):
        pa, tmpdir = _make_pa({"external_images": True, "cover_in_body": True})
        self.addCleanup(shutil.rmtree, tmpdir, True)
        pa.set_cover_from_body = mock.Mock(return_value=False)
        res = pa.execute_publish("标题", "<p>正文</p>", "cover.png", "摘要", ["a.png"])
        self.assertTrue(res["success"])
        pa.prepend_cover_image.assert_called_once()
        pa.set_cover_from_body.assert_called_once()
        pa.upload_cover_image.assert_called_once_with("cover.png")

    def test_false_still_cdn_upload_but_old_cover_flow(self):
        pa, res = self._run({"external_images": True, "cover_in_body": False})
        self.assertTrue(res["success"])
        pa.upload_cover_to_cdn.assert_called_once()
        pa.prepend_cover_image.assert_not_called()
        pa.set_cover_from_body.assert_not_called()
        pa.upload_cover_image.assert_called_once_with("cover.png")

    def test_false_no_cover_path(self):
        pa, res = self._run(
            {"external_images": True, "cover_in_body": False}, cover_path=""
        )
        self.assertTrue(res["success"])
        pa.upload_cover_to_cdn.assert_not_called()
        pa.prepend_cover_image.assert_not_called()
        pa.set_cover_from_body.assert_not_called()
        pa.upload_cover_image.assert_not_called()

    def test_external_images_false_uses_file_upload(self):
        pa, res = self._run({"external_images": False})
        self.assertTrue(res["success"])
        pa.upload_cover_to_cdn.assert_not_called()
        pa.prepend_cover_image.assert_not_called()
        pa.set_cover_from_body.assert_not_called()
        pa.upload_cover_image.assert_called_once_with("cover.png")


# ── Fake Page / Locator 基础设施（供下方测试使用） ────────────────────


class _FakeLocator:
    """极简 Playwright Locator 桩：按 page 的可见性规则决定行为"""

    def __init__(self, page, selector):
        self.page = page
        self.selector = selector
        self.first = self

    def is_visible(self, timeout=0):
        return self.page._visible(self.selector)

    def click(self, timeout=None, force=False):
        if not self.page._visible(self.selector):
            raise Exception(f"{self.selector} 不可见")
        self.page._clicked.append(self.selector)

    def fill(self, value):
        if not self.page._visible(self.selector):
            raise Exception(f"{self.selector} 不可见")
        self.page._filled[self.selector] = value

    def wait_for(self, state="visible", timeout=0):
        if not self.page._visible(self.selector):
            raise Exception(f"{self.selector} 超时不可见")

    def set_input_files(self, path):
        if not self.page._visible(self.selector):
            raise Exception(f"{self.selector} 不可见")
        self.page._uploaded.append((self.selector, path))


class _FakePage:
    """极简 Page 桩：visible 为 selector->bool 的映射；preview 控制封面预览是否出现；
    evaluate_result 控制 evaluate 的返回值；url 可被测试改写以模拟发布后跳转"""

    # 与 config/selectors.yaml 的 confirm_button 保持一致（兼容「确定/确认」）
    CONFIRM_SEL = 'button:has-text("确定"), button:has-text("确认")'

    def __init__(self, visible: dict, preview: bool = True, evaluate_result=True):
        self._visible_map = visible
        self._clicked = []
        self._filled = {}
        self._uploaded = []
        self.preview = preview
        self.screenshots = []
        self.evaluations = []
        self.evaluate_result = evaluate_result
        # 默认停留在编辑器页（URL 含 appmsg_edit，未发生发布后跳转）
        self.url = "https://mp.weixin.qq.com/cgi-bin/appmsg?t=media/appmsg_edit_v2&action=edit&isNew=1"
        self.goto_history = []

    def _visible(self, selector):
        return self._visible_map.get(selector, False)

    def locator(self, selector):
        return _FakeLocator(self, selector)

    def goto(self, url, **kwargs):
        self.goto_history.append(url)
        self.url = url

    def wait_for_selector(self, selector, timeout=0):
        if "toast" in selector.lower():
            return  # 保存草稿/发布的 toast 等待：失败路径会安静超时，不影响判定
        if not self.preview:
            raise Exception("封面预览未出现")

    def wait_for_function(self, js, timeout=0):
        if not self.preview:
            raise Exception("封面区域内无图片")

    def wait_for_timeout(self, ms):
        pass

    def evaluate(self, js):
        self.evaluations.append(js)
        return self.evaluate_result

    def screenshot(self, path=None):
        self.screenshots.append(path)
        return path


# ── set_cover_from_body 测试 ──────────────────────────────────────────


@unittest.skipUnless(PA_AVAILABLE, "需要 playwright 等运行时依赖")
class TestSetCoverFromBody(unittest.TestCase):
    """set_cover_from_body 的多轮选择器回退与失败诊断"""

    def _run(self, page):
        tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmpdir, True)
        pa = PublishActions(page, {"browser": {"screenshot_dir": tmpdir}})
        return pa.set_cover_from_body(), page

    def test_primary_selector_path(self):
        page = _FakePage({".js_cover_area": True, "text=从正文选择": True})
        ok, page = self._run(page)
        self.assertTrue(ok)
        self.assertIn("text=从正文选择", page._clicked)

    def test_fallback_selector_used_when_primary_missing(self):
        visible = {
            ".js_cover_area": True,
            'li:has-text("从正文选择")': True,
        }
        page = _FakePage(visible)
        ok, page = self._run(page)
        self.assertTrue(ok)
        self.assertNotIn("text=从正文选择", page._clicked)
        self.assertIn('li:has-text("从正文选择")', page._clicked)

    def test_no_entry_screenshot_diagnosis(self):
        page = _FakePage({".js_cover_area": True}, preview=False)
        ok, page = self._run(page)
        self.assertFalse(ok)
        self.assertTrue(
            any("cover_from_body_no_entry" in p for p in page.screenshots),
            page.screenshots,
        )

    def test_entry_clicked_but_preview_never_appears(self):
        page = _FakePage({"text=从正文选择": True}, preview=False)
        ok, page = self._run(page)
        self.assertFalse(ok)
        self.assertIn("text=从正文选择", page._clicked)
        self.assertTrue(
            any("cover_from_body_failed" in p for p in page.screenshots),
            page.screenshots,
        )

    def test_body_first_image_subpanel_clicked(self):
        visible = {
            ".js_cover_area": True,
            "text=从正文选择": True,
            ".js_cover_from_body img": True,
        }
        page = _FakePage(visible)
        ok, page = self._run(page)
        self.assertTrue(ok)
        self.assertIn(".js_cover_from_body img", page._clicked)


# ── _try_select / _try_fill / _diagnose_dom 核心基础设施测试 ──────────


@unittest.skipUnless(PA_AVAILABLE, "需要 playwright 等运行时依赖")
class TestTrySelectInfrastructure(unittest.TestCase):
    """_try_select / _try_fill / _diagnose_dom 核心工具方法的直接测试"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir, True)

    def _make(self, page):
        return PublishActions(page, {"browser": {"screenshot_dir": self.tmpdir}})

    # ── _try_select ──────────────────────────────────────────────────

    def test_try_select_returns_element_and_selector(self):
        page = _FakePage({"#a": True, "#b": False})
        pa = self._make(page)
        result = pa._try_select(["#a", "#b"], "test")
        self.assertIsNotNone(result)
        el, matched = result
        self.assertEqual(matched, "#a")

    def test_try_select_returns_none_on_all_miss(self):
        page = _FakePage({"#a": False, "#b": False})
        pa = self._make(page)
        result = pa._try_select(["#a", "#b"], "test")
        self.assertIsNone(result)

    def test_try_select_returns_second_when_first_misses(self):
        page = _FakePage({"#a": False, "#b": True})
        pa = self._make(page)
        result = pa._try_select(["#a", "#b"], "test")
        self.assertIsNotNone(result)
        el, matched = result
        self.assertEqual(matched, "#b")

    def test_try_select_screenshot_on_failure(self):
        page = _FakePage({"#a": False})
        pa = self._make(page)
        pa._try_select(["#a"], "test", screenshot_name="test_miss")
        self.assertTrue(
            any("test_miss" in p for p in page.screenshots),
            page.screenshots,
        )

    def test_try_select_no_screenshot_when_not_requested(self):
        page = _FakePage({"#a": False})
        pa = self._make(page)
        pa._try_select(["#a"], "test")
        self.assertEqual(page.screenshots, [])

    def test_try_select_calls_diagnose_dom_on_failure(self):
        page = _FakePage({"#a": False})
        pa = self._make(page)
        pa._try_select(
            ["#a"], "test",
            dom_area_selectors=[".parent"],
        )
        # _diagnose_dom calls page.evaluate
        self.assertTrue(len(page.evaluations) > 0)

    def test_try_select_empty_list(self):
        page = _FakePage({})
        pa = self._make(page)
        result = pa._try_select([], "test")
        self.assertIsNone(result)

    # ── _try_click_selectors ─────────────────────────────────────────

    def test_try_click_returns_true_and_clicks(self):
        page = _FakePage({"#a": True})
        pa = self._make(page)
        ok = pa._try_click_selectors(["#a"], "test")
        self.assertTrue(ok)
        self.assertIn("#a", page._clicked)

    def test_try_click_returns_false_on_miss(self):
        page = _FakePage({"#a": False})
        pa = self._make(page)
        ok = pa._try_click_selectors(["#a"], "test")
        self.assertFalse(ok)
        self.assertEqual(page._clicked, [])

    # ── _try_fill ────────────────────────────────────────────────────

    def test_try_fill_fills_and_returns_true(self):
        page = _FakePage({"#input": True})
        pa = self._make(page)
        ok = pa._try_fill(["#input"], "test", "hello")
        self.assertTrue(ok)
        self.assertEqual(page._filled.get("#input"), "hello")

    def test_try_fill_returns_false_on_miss(self):
        page = _FakePage({"#input": False})
        pa = self._make(page)
        ok = pa._try_fill(["#input"], "test", "hello")
        self.assertFalse(ok)
        self.assertEqual(page._filled, {})

    # ── _diagnose_dom ────────────────────────────────────────────────

    def test_diagnose_dom_calls_evaluate(self):
        page = _FakePage({})
        pa = self._make(page)
        pa._diagnose_dom("label", [".area1", ".area2"])
        self.assertEqual(len(page.evaluations), 1)
        self.assertIn(".area1", page.evaluations[0])

    def test_diagnose_dom_noop_when_no_selectors(self):
        page = _FakePage({})
        pa = self._make(page)
        pa._diagnose_dom("label", None)
        pa._diagnose_dom("label", [])
        self.assertEqual(page.evaluations, [])

    # ── fill_title 集成 ─────────────────────────────────────────────

    def test_fill_title_uses_try_fill(self):
        page = _FakePage({"#title": True})
        pa = self._make(page)
        pa.fill_title("测试标题")
        self.assertEqual(page._filled.get("#title"), "测试标题")

    def test_fill_title_raises_on_miss(self):
        page = _FakePage({"#title": False})
        pa = self._make(page)
        with self.assertRaises(RuntimeError):
            pa.fill_title("测试标题")
        self.assertTrue(
            any("fill_title_failed" in p for p in page.screenshots),
            page.screenshots,
        )

    # ── fill_summary 集成 ───────────────────────────────────────────

    def test_fill_summary_fills_on_match(self):
        page = _FakePage({"#js_description": True})
        pa = self._make(page)
        pa.fill_summary("测试摘要")
        self.assertEqual(page._filled.get("#js_description"), "测试摘要")

    def test_fill_summary_noop_on_miss(self):
        page = _FakePage({"#js_description": False})
        pa = self._make(page)
        # 不应抛异常（摘要缺失只是 warning）
        pa.fill_summary("测试摘要")
        self.assertEqual(page._filled, {})

    # ── save_draft 集成 ─────────────────────────────────────────────

    def test_save_draft_clicks_and_returns_true(self):
        page = _FakePage({'button:has-text("保存草稿")': True})
        pa = self._make(page)
        ok = pa.save_draft()
        self.assertTrue(ok)
        self.assertIn('button:has-text("保存草稿")', page._clicked)

    def test_save_draft_returns_false_on_miss(self):
        page = _FakePage({})
        pa = self._make(page)
        ok = pa.save_draft()
        self.assertFalse(ok)
        self.assertTrue(
            any("save_draft_failed" in p for p in page.screenshots),
            page.screenshots,
        )

    # ── 截图时间戳 ──────────────────────────────────────────────────

    def test_screenshot_names_include_timestamp(self):
        page = _FakePage({})
        pa = self._make(page)
        p1 = pa._screenshot("pre_publish_preview")
        p2 = pa._screenshot("pre_publish_preview")
        self.assertIn("pre_publish_preview", p1)
        # 两次截图文件名不同 → 不会互相覆盖
        self.assertNotEqual(p1, p2)
        # 命名规范：{screenshot_dir}/{YYYYMMDD}/{name}_{时间戳}.png（与 wechat_session 一致）
        self.assertEqual(os.path.dirname(os.path.dirname(p1)), self.tmpdir)
        self.assertRegex(
            os.path.basename(p1),
            r"pre_publish_preview_\d{8}_\d{6}_\d{6}\.png$",
        )

    # ── publish 集成 ────────────────────────────────────────────────

    def test_publish_clicks_and_returns_true(self):
        # publish() 现在要求：确认对话框 + 真实成功信号才返回 True；
        # 新版对话框确认按钮文本是「发表」（v2 跳转旧版后自动弹出）
        page = _FakePage({'button:has-text("群发")': True})
        pa = self._make(page)
        pa._wait_publish_confirm_dialog = mock.Mock(return_value=True)
        pa._wait_publish_outcome = mock.Mock(return_value=(True, "页面出现成功提示「发表成功」"))
        ok = pa.publish(title="测试")
        self.assertTrue(ok)
        self.assertIn('button:has-text("群发")', page._clicked)
        pa._wait_publish_confirm_dialog.assert_called_once()

    def test_publish_returns_false_on_miss(self):
        page = _FakePage({})
        pa = self._make(page)
        pa._wait_publish_confirm_dialog = mock.Mock(return_value=False)
        ok = pa.publish()
        self.assertFalse(ok)
        self.assertTrue(
            any("publish_failed" in p for p in page.screenshots),
            page.screenshots,
        )


# ── 发布结果验证（防假阳性）测试 ────────────────────────────────


@unittest.skipUnless(PA_AVAILABLE, "需要 playwright 等运行时依赖")
class TestPublishVerification(unittest.TestCase):
    """publish() 假阳性回归、_wait_publish_outcome 信号判定、
    verify_published_online 发表记录复核、execute_publish publish 模式复核集成"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir, True)

    def _make(self, page, wechat=None):
        return PublishActions(
            page,
            {"wechat": wechat or {}, "browser": {"screenshot_dir": self.tmpdir}},
        )

    # ── publish() 假阳性回归 ──────────────────────────────────────────

    def test_publish_no_dialog_never_reports_success(self):
        """历史假阳性回归：确认对话框未出现时绝不能报发布成功"""
        page = _FakePage({'button:has-text("发表")': True})  # 确认对话框始终不出现
        pa = self._make(page)
        pa._wait_publish_confirm_dialog = mock.Mock(return_value=False)
        pa._wait_publish_outcome = mock.Mock()
        ok = pa.publish(title="测试")
        self.assertFalse(ok)
        pa._wait_publish_outcome.assert_not_called()
        self.assertTrue(
            any("publish_no_dialog" in p for p in page.screenshots),
            page.screenshots,
        )

    def test_publish_dialog_confirmed_but_no_success_signal(self):
        page = _FakePage({'button:has-text("发表")': True})
        pa = self._make(page)
        pa._wait_publish_confirm_dialog = mock.Mock(return_value=True)
        pa._wait_publish_outcome = mock.Mock(return_value=(False, "超时无信号"))
        ok = pa.publish(title="测试")
        self.assertFalse(ok)
        self.assertTrue(
            any("publish_no_success_signal" in p for p in page.screenshots),
            page.screenshots,
        )

    def test_publish_success_requires_dialog_and_signal(self):
        page = _FakePage({'button:has-text("发表")': True})
        pa = self._make(page)
        pa._wait_publish_confirm_dialog = mock.Mock(return_value=True)
        pa._wait_publish_outcome = mock.Mock(return_value=(True, "页面已跳转"))
        ok = pa.publish(title="测试")
        self.assertTrue(ok)
        pa._wait_publish_confirm_dialog.assert_called_once()

    # ── _wait_publish_outcome 信号判定 ────────────────────────────────

    def test_outcome_url_navigation_is_success(self):
        page = _FakePage({})
        page.url = "https://mp.weixin.qq.com/cgi-bin/home?t=home"  # 已离开编辑器
        pa = self._make(page)
        ok, ev = pa._wait_publish_outcome(
            "https://mp.weixin.qq.com/cgi-bin/appmsg?t=media/appmsg_edit_v2",
            timeout_ms=2000,
        )
        self.assertTrue(ok)
        self.assertIn("页面已跳转", ev)

    def test_outcome_still_in_editor_is_failure(self):
        page = _FakePage({}, evaluate_result="")
        pa = self._make(page)
        ok, ev = pa._wait_publish_outcome(page.url, timeout_ms=300)
        self.assertFalse(ok)
        self.assertIn("无成功信号", ev)

    def test_outcome_fail_text_is_failure(self):
        page = _FakePage({}, evaluate_result="FAIL:不能为空")
        pa = self._make(page)
        ok, ev = pa._wait_publish_outcome(page.url, timeout_ms=2000)
        self.assertFalse(ok)
        self.assertIn("不能为空", ev)

    def test_outcome_success_text_is_success(self):
        page = _FakePage({}, evaluate_result="OK:发表成功")
        pa = self._make(page)
        ok, _ = pa._wait_publish_outcome(page.url, timeout_ms=2000)
        self.assertTrue(ok)

    # ── verify_published_online 发表记录复核 ──────────────────────────

    def test_verify_found_in_published_list(self):
        page = _FakePage({}, evaluate_result=True)
        pa = self._make(page)
        ok = pa.verify_published_online("智能体操作系统（AgentOS/AOS）详解｜AI的下一个战场")
        self.assertTrue(ok)
        self.assertTrue(page.goto_history, "复核必须真实导航到发表记录页")

    def test_verify_not_found_returns_false(self):
        page = _FakePage({}, evaluate_result=False)
        pa = self._make(page)
        ok = pa.verify_published_online("不存在的文章标题")
        self.assertFalse(ok)
        self.assertTrue(
            any("verify_published_not_found" in p for p in page.screenshots),
            page.screenshots,
        )

    def test_verify_empty_title_returns_false(self):
        pa = self._make(_FakePage({}))
        self.assertFalse(pa.verify_published_online(""))

    def test_title_needle_strips_whitespace_and_truncates(self):
        self.assertEqual(
            PublishActions._title_needle("智能体操作系统（AgentOS/AOS）详解｜智能体操作系统：AI的下一个战场"),
            "智能体操作系统（AgentOS/AOS）详解｜智能"[:20],
        )

    # ── execute_publish publish 模式复核集成 ──────────────────────────

    def _make_execute_pa(self, page, wechat):
        pa = PublishActions(
            page,
            {"wechat": wechat, "browser": {"screenshot_dir": self.tmpdir}},
        )
        pa.navigate_to_editor = mock.Mock()
        pa.upload_cover_to_cdn = mock.Mock(return_value="https://mmbiz.qpic.cn/cover")
        pa.fill_title = mock.Mock()
        pa.replace_inline_images = mock.Mock(side_effect=lambda h, imgs: h)
        pa.prepend_cover_image = mock.Mock(side_effect=lambda h, u: h)
        pa.fill_content_html = mock.Mock()
        pa.set_cover_from_body = mock.Mock(return_value=True)
        pa.upload_cover_image = mock.Mock()
        pa._wait_cover_preview = mock.Mock(return_value=True)
        pa.fill_summary = mock.Mock()
        pa.preview_before_publish = mock.Mock(return_value="")
        pa.publish = mock.Mock(return_value=True)
        pa.save_draft = mock.Mock(return_value=True)
        pa.verify_published_online = mock.Mock(return_value=True)
        return pa

    def test_publish_success_and_verified(self):
        pa = self._make_execute_pa(_FakePage({}), {"publish_mode": "publish"})
        res = pa.execute_publish("标题", "<p>正文</p>", "cover.png", "摘要", ["a.png"])
        self.assertTrue(res["success"])
        self.assertEqual(res["mode"], "publish")
        pa.verify_published_online.assert_called_once_with("标题")
        pa.save_draft.assert_not_called()

    def test_publish_true_but_verify_false_overturns(self):
        """假阳性拦截：发布动作报成功，但发表记录复核找不到 → 必须判失败"""
        pa = self._make_execute_pa(_FakePage({}), {"publish_mode": "publish"})
        pa.verify_published_online = mock.Mock(return_value=False)
        res = pa.execute_publish("标题", "<p>正文</p>", "cover.png", "摘要", ["a.png"])
        self.assertFalse(res["success"])
        self.assertIn("复核失败", res["error"])

    def test_publish_false_but_verify_true_rescues(self):
        """假阴性容忍：发布动作信号异常，但发表记录找到文章 → 改判成功"""
        pa = self._make_execute_pa(_FakePage({}), {"publish_mode": "publish"})
        pa.publish = mock.Mock(return_value=False)
        res = pa.execute_publish("标题", "<p>正文</p>", "cover.png", "摘要", ["a.png"])
        self.assertTrue(res["success"])
        pa.save_draft.assert_called_once()  # 复核导航前先降级存草稿防丢失

    def test_publish_false_and_verify_false_stays_failed(self):
        pa = self._make_execute_pa(_FakePage({}), {"publish_mode": "publish"})
        pa.publish = mock.Mock(return_value=False)
        pa.verify_published_online = mock.Mock(return_value=False)
        res = pa.execute_publish("标题", "<p>正文</p>", "cover.png", "摘要", ["a.png"])
        self.assertFalse(res["success"])
        pa.save_draft.assert_called_once()

    def test_verify_disabled_by_config(self):
        pa = self._make_execute_pa(
            _FakePage({}), {"publish_mode": "publish", "verify_published": False}
        )
        res = pa.execute_publish("标题", "<p>正文</p>", "cover.png", "摘要", ["a.png"])
        self.assertTrue(res["success"])
        pa.verify_published_online.assert_not_called()

    def test_publish_without_cover_downgrades_to_draft(self):
        """无封面：发表必被后台拦截 → 降级存草稿防文章丢失，不尝试点击发表"""
        pa = self._make_execute_pa(_FakePage({}), {"publish_mode": "publish"})
        res = pa.execute_publish("标题", "<p>正文</p>", "", "摘要", ["a.png"])
        self.assertTrue(res["success"])  # 草稿保存成功
        self.assertEqual(res["mode"], "draft")
        self.assertIn("封面", res["error"])
        pa.publish.assert_not_called()
        pa.save_draft.assert_called_once()

    def test_publish_cover_check_failed_downgrades_to_draft(self):
        """有封面文件但设置结果未验证到 → 同样降级存草稿"""
        pa = self._make_execute_pa(_FakePage({}), {"publish_mode": "publish"})
        pa._wait_cover_preview = mock.Mock(return_value=False)
        res = pa.execute_publish("标题", "<p>正文</p>", "cover.png", "摘要", ["a.png"])
        self.assertEqual(res["mode"], "draft")
        pa.publish.assert_not_called()


# ── 选择器命中率统计测试 ────────────────────────────────────────────


@unittest.skipUnless(PA_AVAILABLE, "需要 playwright 等运行时依赖")
class TestSelectorStats(unittest.TestCase):
    """_try_select 命中记录、_selector_stats_report 汇总、execute_publish 统计输出"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir, True)

    def _make(self, page):
        return PublishActions(page, {"browser": {"screenshot_dir": self.tmpdir}})

    # ── _try_select 命中记录 ────────────────────────────────────────

    def test_try_select_records_hits_and_counts(self):
        page = _FakePage({"#a": True, "#b": True})
        pa = self._make(page)
        pa._try_select(["#a", "#b"], "标题输入")
        pa._try_select(["#a", "#b"], "标题输入")
        pa._try_select(["#a", "#b"], "保存草稿")  # 同选择器、不同标签分开统计
        self.assertEqual(pa._selector_stats["标题输入"], {"#a": 2})
        self.assertEqual(pa._selector_stats["保存草稿"], {"#a": 1})

    def test_all_miss_records_group_with_empty_hits(self):
        page = _FakePage({"#a": False})
        pa = self._make(page)
        pa._try_select(["#a"], "标题输入")
        self.assertEqual(pa._selector_stats["标题输入"], {})

    # ── _position_in_group / _selector_stats_report ──────────────────

    def test_position_in_group(self):
        pa = self._make(_FakePage({}))
        self.assertEqual(pa._position_in_group("标题输入", "#title"), 1)
        self.assertEqual(pa._position_in_group("标题输入", 'input[name="title"]'), 5)
        self.assertEqual(pa._position_in_group("未知分组", "#title"), 0)
        self.assertEqual(pa._position_in_group("标题输入", "#not-in-list"), 0)

    def test_stats_report_never_hit_and_totals(self):
        pa = self._make(_FakePage({}))
        pa._selector_stats = {"标题输入": {"#title": 2}, "保存草稿": {}}
        report = pa._selector_stats_report()
        self.assertEqual(report["total_hits"], 2)
        self.assertEqual(report["groups_attempted"], 2)
        self.assertEqual(report["groups_hit"], 1)
        self.assertEqual(report["all_missed"], ["保存草稿"])
        # 未命中的 4 个标题选择器全部列出
        self.assertEqual(
            report["never_hit"]["标题输入"],
            [s for s in [
                'input[placeholder*="标题"]',
                'textarea[placeholder*="标题"]',
                ".title_editor .weui-desktop-form__input input",
                'input[name="title"]',
            ]],
        )

    # ── execute_publish 统计输出 ─────────────────────────────────────

    def _make_execute_pa(self, page):
        """除 fill_title / save_draft 外全部 mock，让真实 _try_select 产生命中统计"""
        pa = PublishActions(
            page,
            {
                "wechat": {"publish_mode": "draft"},
                "browser": {"screenshot_dir": self.tmpdir},
            },
        )
        pa.navigate_to_editor = mock.Mock()
        pa.upload_cover_to_cdn = mock.Mock(return_value="")
        pa.replace_inline_images = mock.Mock(side_effect=lambda h, imgs: h)
        pa.fill_content_html = mock.Mock()
        pa.set_cover_from_body = mock.Mock(return_value=True)
        pa.upload_cover_image = mock.Mock()
        pa.fill_summary = mock.Mock()
        pa.preview_before_publish = mock.Mock(return_value="")
        return pa

    @staticmethod
    def _stats_events(mock_log):
        return [
            c.kwargs
            for c in mock_log.call_args_list
            if c.args and c.args[0] == "editor.selector_stats"
        ]

    def test_execute_publish_emits_selector_stats(self):
        page = _FakePage({"#title": True, 'button:has-text("保存草稿")': True}, preview=True)
        pa = self._make_execute_pa(page)
        with mock.patch("browser.publish_actions._log_event") as m, mock.patch(
            "browser.publish_actions.append_selector_stats") as ledger:
            res = pa.execute_publish("标题", "<p>正文</p>", "", "摘要", None)
        self.assertTrue(res["success"])
        events = self._stats_events(m)
        self.assertEqual(len(events), 1, events)
        payload = events[0]
        self.assertEqual(payload["total_hits"], 2)
        self.assertEqual(payload["hits_by_group"]["标题输入"], {"#title": 1})
        self.assertEqual(payload["hits_by_group"]["保存草稿"], {
            'button:has-text("保存草稿")': 1,
        })
        self.assertEqual(payload["all_missed"], [])
        self.assertEqual(len(payload["never_hit"]["标题输入"]), 4)
        self.assertEqual(len(payload["never_hit"]["保存草稿"]), 3)

    def test_execute_publish_stats_reset_between_runs(self):
        page = _FakePage({"#title": True, 'button:has-text("保存草稿")': True}, preview=True)
        pa = self._make_execute_pa(page)
        with mock.patch("browser.publish_actions._log_event") as m, mock.patch(
            "browser.publish_actions.append_selector_stats"):
            pa.execute_publish("标题", "<p>正文</p>", "", "摘要", None)
            pa.execute_publish("标题", "<p>正文</p>", "", "摘要", None)
        events = self._stats_events(m)
        # 两次发布各自独立统计，不跨次累计
        self.assertEqual([e["total_hits"] for e in events], [2, 2], events)

    def test_execute_publish_error_path_emits_partial_stats(self):
        page = _FakePage({})  # 标题输入框找不到 → fill_title 抛异常
        pa = self._make_execute_pa(page)
        with mock.patch("browser.publish_actions._log_event") as m, mock.patch(
            "browser.publish_actions.append_selector_stats"):
            res = pa.execute_publish("标题", "<p>正文</p>", "", "摘要", None)
        self.assertFalse(res["success"])
        events = self._stats_events(m)
        self.assertEqual(len(events), 1, events)
        self.assertEqual(events[0]["total_hits"], 0)
        self.assertEqual(events[0]["all_missed"], ["标题输入"])

    def test_emit_selector_stats_noop_when_no_attempts(self):
        pa = self._make(_FakePage({}))
        with mock.patch("browser.publish_actions._log_event") as m:
            pa._emit_selector_stats()
        m.assert_not_called()

    # ── 截图保留策略接线 ────────────────────────────────────────────

    def test_execute_publish_runs_screenshot_cleanup(self):
        page = _FakePage({"#title": True, 'button:has-text("保存草稿")': True}, preview=True)
        pa = self._make_execute_pa(page)
        with mock.patch("browser.publish_actions.cleanup_old_screenshots", return_value=0) as m:
            res = pa.execute_publish("标题", "<p>正文</p>", "", "摘要", None)
        self.assertTrue(res["success"])
        m.assert_called_once_with(pa.screenshot_dir, pa.retention_days)

    def test_retention_days_config(self):
        d1, d2 = tempfile.mkdtemp(), tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d1, True)
        self.addCleanup(shutil.rmtree, d2, True)
        pa = PublishActions(None, {
            "browser": {"screenshot_dir": d1, "screenshot_retention_days": 7},
        })
        self.assertEqual(pa.retention_days, 7)
        pa2 = PublishActions(None, {"browser": {"screenshot_dir": d2}})
        self.assertEqual(pa2.retention_days, 30)

    def test_execute_publish_preview_path_is_timestamped(self):
        page = _FakePage({"#title": True, 'button:has-text("保存草稿")': True}, preview=True)
        pa = self._make_execute_pa(page)
        del pa.preview_before_publish  # 用真实方法（内部走 _screenshot 生成时间戳路径）
        with mock.patch("browser.publish_actions.append_selector_stats"):
            res = pa.execute_publish("标题", "<p>正文</p>", "", "摘要", None)
        self.assertTrue(res["success"])
        self.assertRegex(res["preview_path"], r"pre_publish_preview_\d{8}_\d{6}_\d{6}\.png$")


if __name__ == "__main__":
    unittest.main()
