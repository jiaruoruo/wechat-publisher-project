"""tests for browser/image_uploader.py 纯函数部分

CDN 上传本身依赖真实浏览器会话（uploadimg2cdn），此处测试可离线验证的：
token 提取（页面 JS 上下文优先 / URL 回退）、响应解析、占位符解析（含
逐张 base64 兜底、已成功 CDN 保留、统计）。
"""

import io
import json
import logging
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from browser.image_uploader import (
        ImageUploadStats,
        extract_token,
        read_token_from_page,
        parse_upload_response,
        resolve_image_placeholders,
    )
    UPLOADER_AVAILABLE = True
except ImportError:
    UPLOADER_AVAILABLE = False


@unittest.skipUnless(UPLOADER_AVAILABLE, "需要 playwright 等运行时依赖")
class TestExtractToken(unittest.TestCase):
    def test_editor_url(self):
        url = "https://mp.weixin.qq.com/cgi-bin/appmsg?t=media/appmsg_edit&action=edit&token=AbC123&lang=zh_CN"
        self.assertEqual(extract_token(url), "AbC123")

    def test_no_token(self):
        self.assertEqual(extract_token("https://mp.weixin.qq.com/"), "")

    def test_empty(self):
        self.assertEqual(extract_token(""), "")


@unittest.skipUnless(UPLOADER_AVAILABLE, "需要 playwright 等运行时依赖")
class TestReadTokenFromPage(unittest.TestCase):
    def _fake_page(self, js_value: str, url: str):
        class FakePage:
            def __init__(self, js_value, url):
                self._js_value = js_value
                self.url = url

            def evaluate(self, *args, **kwargs):
                return self._js_value

        return FakePage(js_value, url)

    def test_from_page_js_context(self):
        page = self._fake_page("AbC-from-page", "https://mp.weixin.qq.com/cgi-bin/appmsg?token=AbC-url")
        self.assertEqual(read_token_from_page(page), "AbC-from-page")

    def test_url_fallback_when_js_empty(self):
        page = self._fake_page("", "https://mp.weixin.qq.com/cgi-bin/appmsg?token=AbC-url")
        self.assertEqual(read_token_from_page(page), "AbC-url")

    def test_evaluate_exception_falls_back_to_url(self):
        class BoomPage:
            url = "https://mp.weixin.qq.com/cgi-bin/appmsg?token=AbC-url"

            def evaluate(self, *a, **k):
                raise RuntimeError("no js")

        self.assertEqual(read_token_from_page(BoomPage()), "AbC-url")


@unittest.skipUnless(UPLOADER_AVAILABLE, "需要 playwright 等运行时依赖")
class TestParseUploadResponse(unittest.TestCase):
    def test_url_shape(self):
        text = '{"base_resp": {"ret": 0, "err_msg": "ok"}, "url": "https://mmbiz.qpic.cn/mmbiz_png/abc"}'
        self.assertEqual(parse_upload_response(text), "https://mmbiz.qpic.cn/mmbiz_png/abc")

    def test_cdn_url_shape(self):
        self.assertEqual(
            parse_upload_response('{"cdn_url": "https://mmbiz.qpic.cn/x"}'),
            "https://mmbiz.qpic.cn/x",
        )

    def test_content_with_img(self):
        text = '{"content": "<img src=\\"https://mmbiz.qpic.cn/y\\" alt=\\"\\"/>", "base_resp": {"ret": 0}}'
        self.assertEqual(parse_upload_response(text), "https://mmbiz.qpic.cn/y")

    def test_non_json_with_img(self):
        text = '<html><body><img src="https://mmbiz.qpic.cn/z"></body></html>'
        self.assertEqual(parse_upload_response(text), "https://mmbiz.qpic.cn/z")

    def test_garbage(self):
        self.assertEqual(parse_upload_response("server error"), "")
        self.assertEqual(parse_upload_response(""), "")

    def test_no_url_keys(self):
        self.assertEqual(parse_upload_response('{"base_resp": {"ret": -1}}'), "")


@unittest.skipUnless(UPLOADER_AVAILABLE, "需要 playwright 等运行时依赖")
class TestResolveImagePlaceholders(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.img1 = os.path.join(self.tmp, "a.png")
        self.img2 = os.path.join(self.tmp, "b.png")
        for p in (self.img1, self.img2):
            with open(p, "wb") as f:
                f.write(b"\x89PNG\r\n\x1a\n" + b"0" * 32)

    def test_all_succeed_cdn_mode(self):
        html = '<img src="{{IMAGE_PATH_1}}"><img src="{{IMAGE_PATH_2}}">'
        out, stats = resolve_image_placeholders(
            html, [self.img1, self.img2],
            lambda path: f"https://cdn.example/{os.path.basename(path)}",
        )
        self.assertIn('src="https://cdn.example/a.png"', out)
        self.assertIn('src="https://cdn.example/b.png"', out)
        self.assertEqual(stats.total, 2)
        self.assertEqual(stats.cdn_ok, 2)
        self.assertEqual(stats.base64_fallback, 0)
        self.assertEqual(stats.mode, "cdn")

    def test_partial_failure_keeps_successful_cdn(self):
        # 一张失败一张成功：已成功的 CDN URL 必须保留（不再整篇重新 base64 化），
        # 失败的那张单独回退 base64 → 结果 mixed。
        html = '<img src="{{IMAGE_PATH_1}}"><img src="{{IMAGE_PATH_2}}">'

        def uploader(path):
            return "" if path == self.img1 else "https://cdn.example/b.png"

        out, stats = resolve_image_placeholders(html, [self.img1, self.img2], uploader)
        self.assertIn("https://cdn.example/b.png", out)  # 成功保持 CDN
        self.assertEqual(out.count("data:image/png;base64,"), 1)  # 失败单独 base64
        self.assertEqual(stats.cdn_ok, 1)
        self.assertEqual(stats.base64_fallback, 1)
        self.assertEqual(stats.mode, "mixed")
        self.assertFalse(stats.all_failed)

    def test_all_failed_falls_back_whole_base64(self):
        # 全部失败 → 结果自然全 base64（统计如实记录）
        html = '<img src="{{IMAGE_PATH_1}}"><img src="{{IMAGE_PATH_2}}">'
        out, stats = resolve_image_placeholders(html, [self.img1, self.img2], lambda p: "")
        self.assertEqual(out.count("data:image/png;base64,"), 2)
        self.assertEqual(stats.mode, "base64")
        self.assertTrue(stats.all_failed)

    def test_uploader_exception_falls_back(self):
        html = '<img src="{{IMAGE_PATH_1}}">'
        out, stats = resolve_image_placeholders(
            html, [self.img1], lambda path: (_ for _ in ()).throw(RuntimeError("boom"))
        )
        self.assertTrue(out.startswith('<img src="data:image/png;base64,'))
        self.assertEqual(stats.mode, "base64")

    def test_missing_path_replaced_with_empty(self):
        html = '<img src="{{IMAGE_PATH_2}}">'  # 只有 1 张图，占位符 2 越界
        out, stats = resolve_image_placeholders(html, [self.img1], lambda path: "https://x")
        self.assertEqual(out, '<img src="">')
        self.assertEqual(stats.missing, 1)
        self.assertEqual(stats.mode, "none")

    def test_no_placeholders_unchanged_and_stats_empty(self):
        html = "<p>纯文本，无图片</p>"
        out, stats = resolve_image_placeholders(html, [self.img1], lambda p: "https://x")
        self.assertEqual(out, html)
        self.assertEqual(stats.total, 0)
        self.assertEqual(stats.mode, "none")


@unittest.skipUnless(UPLOADER_AVAILABLE, "需要 playwright 等运行时依赖")
class TestImageUploadStats(unittest.TestCase):
    def test_all_failed_property(self):
        self.assertTrue(ImageUploadStats(total=2, cdn_ok=0, base64_fallback=2).all_failed)
        self.assertFalse(ImageUploadStats(total=2, cdn_ok=1, base64_fallback=1).all_failed)
        self.assertFalse(ImageUploadStats(total=0).all_failed)


@unittest.skipUnless(UPLOADER_AVAILABLE, "需要 playwright 等运行时依赖")
class TestStructuredLogging(unittest.TestCase):
    def test_image_upload_event_emitted(self):
        import tempfile as _tf

        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        logger = logging.getLogger("config.structured_logging")
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        try:
            tmp = _tf.mkdtemp()
            img = os.path.join(tmp, "a.png")
            with open(img, "wb") as f:
                f.write(b"\x89PNG\r\n\x1a\n" + b"0" * 8)
            resolve_image_placeholders(
                '<img src="{{IMAGE_PATH_1}}">', [img],
                lambda path: "https://cdn.example/a.png",
            )
            events = [
                json.loads(line)
                for line in stream.getvalue().strip().splitlines()
                if line.strip()
            ]
            self.assertTrue(events, "应至少有一条结构化日志")
            ev = next(e for e in events if e["event"] == "image_upload")
            self.assertEqual(ev["data"]["cdn_ok"], 1)
            self.assertEqual(ev["data"]["mode"], "cdn")
        finally:
            logger.removeHandler(handler)
            logger.propagate = True


if __name__ == "__main__":
    unittest.main()
