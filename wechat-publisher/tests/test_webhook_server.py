"""tests for hermes/webhook_server.py — 安全边界单测

覆盖 do_POST 抽出后的纯函数 process_event / decrypt_event / ip_allowed：
- JSON 解析、IP 白名单、url_verification、verification_token 校验
- v2.0 / v1.0 / card_action 事件解析
- require_encrypt 强制加密、encrypt_key 解密（成功 + 失败计数）
另含一个真实 HTTPServer 集成冒烟（POST + /health）。
"""

import json
import os
import sys
import unittest
import base64 as _b64
import hashlib as _hashlib
import http.client
from threading import Thread

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hermes.webhook_server import (
    FeishuWebhookHandler,
    FeishuWebhookServer,
    decrypt_event,
    get_decrypt_fail_count,
    ip_allowed,
    process_event,
    reset_decrypt_fail_count,
)

try:
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import pad

    CRYPTO_AVAILABLE = True
except ImportError:
    CRYPTO_AVAILABLE = False


def _encrypt(data: dict, encrypt_key: str) -> dict:
    """按飞书算法加密 payload（AES-256-CBC，key=IV=md5(encrypt_key)[:16]）"""
    key = _hashlib.md5(encrypt_key.encode("utf-8")).hexdigest()[:16].encode("utf-8")
    cipher = AES.new(key, AES.MODE_CBC, key)
    plaintext = json.dumps(data, ensure_ascii=False).encode("utf-8")
    return {"encrypt": _b64.b64encode(cipher.encrypt(pad(plaintext, AES.block_size))).decode("utf-8")}


class TestIpAllowed(unittest.TestCase):
    def test_empty_whitelist_allows_all(self):
        self.assertTrue(ip_allowed("1.2.3.4", []))
        self.assertTrue(ip_allowed("", []))

    def test_exact_match(self):
        self.assertTrue(ip_allowed("1.2.3.4", ["1.2.3.4"]))
        self.assertFalse(ip_allowed("1.2.3.5", ["1.2.3.4"]))

    def test_cidr_match(self):
        self.assertTrue(ip_allowed("10.0.0.7", ["10.0.0.0/24"]))
        self.assertFalse(ip_allowed("10.0.1.7", ["10.0.0.0/24"]))

    def test_invalid_peer_or_entries(self):
        self.assertFalse(ip_allowed("not-an-ip", ["10.0.0.0/24"]))
        self.assertFalse(ip_allowed("1.2.3.4", ["garbage", "10.0.0.0/24"]))


class TestDecryptEvent(unittest.TestCase):
    def setUp(self):
        reset_decrypt_fail_count()

    def test_missing_encrypt_field(self):
        self.assertIsNone(decrypt_event({}, "k"))
        self.assertEqual(get_decrypt_fail_count(), 1)

    def test_invalid_base64(self):
        self.assertIsNone(decrypt_event({"encrypt": "!!not-base64!!"}, "k"))
        self.assertEqual(get_decrypt_fail_count(), 1)

    @unittest.skipUnless(CRYPTO_AVAILABLE, "需要 pycryptodome")
    def test_valid_roundtrip(self):
        payload = {"type": "url_verification", "challenge": "abc"}
        encrypted = _encrypt(payload, "secret-key")
        out = decrypt_event(encrypted, "secret-key")
        self.assertEqual(out, payload)
        self.assertEqual(get_decrypt_fail_count(), 0)


class TestProcessEvent(unittest.TestCase):
    def setUp(self):
        reset_decrypt_fail_count()

    def _events(self):
        captured = []

        def on_event(event):
            captured.append(event)

        return captured, on_event

    def test_invalid_json(self):
        status, resp = process_event(b"not json")
        self.assertEqual(status, 400)

    def test_ip_whitelist_deny(self):
        status, resp = process_event(
            b'{"type": "url_verification", "challenge": "c"}',
            allow_from=["10.0.0.0/24"],
            peer_ip="1.2.3.4",
        )
        self.assertEqual(status, 403)
        self.assertEqual(resp["error"], "Forbidden")

    def test_url_verification(self):
        status, resp = process_event(b'{"type": "url_verification", "challenge": "ch123"}')
        self.assertEqual(status, 200)
        self.assertEqual(resp["challenge"], "ch123")

    def test_token_mismatch(self):
        status, resp = process_event(
            b'{"token": "wrong", "type": "event_callback", "event": {}}',
            verification_token="right",
        )
        self.assertEqual(status, 403)
        self.assertEqual(resp["error"], "Invalid token")

    def test_token_match_passes(self):
        captured, on_event = self._events()
        body = json.dumps({
            "token": "right",
            "type": "card_action",
            "action": {"value": {"k": "v"}},
        }).encode("utf-8")
        status, resp = process_event(body, verification_token="right", on_event=on_event)
        self.assertEqual(status, 200)
        self.assertEqual(captured[0]["type"], "card_action")

    def test_v2_message(self):
        captured, on_event = self._events()
        body = json.dumps({
            "schema": "2.0",
            "header": {"event_type": "im.message.receive_v1"},
            "event": {
                "sender": {"sender_id": {"open_id": "ou_1"}},
                "message": {
                    "message_type": "text",
                    "message_id": "om_1",
                    "chat_id": "oc_1",
                    "content": json.dumps({"text": "hello"}),
                },
            },
        }).encode("utf-8")
        status, resp = process_event(body, on_event=on_event)
        self.assertEqual(status, 200)
        self.assertEqual(captured[0]["type"], "message")
        self.assertEqual(captured[0]["text"], "hello")
        self.assertEqual(captured[0]["user_id"], "ou_1")

    def test_v2_message_non_json_content_does_not_crash(self):
        """P0#1 回归：飞书富文本/文件/图片事件的 content 非 JSON 时不应抛异常"""
        captured, on_event = self._events()
        body = json.dumps({
            "schema": "2.0",
            "header": {"event_type": "im.message.receive_v1"},
            "event": {
                "sender": {"sender_id": {"open_id": "ou_1"}},
                "message": {
                    "message_type": "file",  # 非 text 事件，content 不是合法 JSON
                    "message_id": "om_2",
                    "chat_id": "oc_1",
                    "content": "{这不是合法JSON",
                },
            },
        }).encode("utf-8")
        # 即使 content 解析失败，也不应冒泡异常（返回 200，text 为空）
        status, resp = process_event(body, on_event=on_event)
        self.assertEqual(status, 200)
        # file 类型事件不会触发 on_event，但绝不应 500
        self.assertEqual(len(captured), 0)

    def test_v1_message(self):
        captured, on_event = self._events()
        body = json.dumps({
            "type": "event_callback",
            "event": {
                "type": "message",
                "message_type": "text",
                "text": "hi",
                "message_id": "m1",
                "chat_id": "c1",
                "open_id": "u1",
            },
        }).encode("utf-8")
        status, resp = process_event(body, on_event=on_event)
        self.assertEqual(status, 200)
        self.assertEqual(captured[0]["type"], "message")
        self.assertEqual(captured[0]["text"], "hi")

    def test_require_encrypt_without_key_rejected(self):
        status, resp = process_event(
            b'{"type": "url_verification", "challenge": "c"}',
            require_encrypt=True,
            encrypt_key="",
        )
        self.assertEqual(status, 403)
        self.assertIn("encrypt_key", resp["error"])

    def test_require_encrypt_plain_request_rejected(self):
        status, resp = process_event(
            b'{"type": "url_verification", "challenge": "c"}',
            require_encrypt=True,
            encrypt_key="secret",
        )
        self.assertEqual(status, 403)
        self.assertEqual(resp["error"], "Encryption required")

    @unittest.skipUnless(CRYPTO_AVAILABLE, "需要 pycryptodome")
    def test_encrypted_request_decrypted_and_processed(self):
        captured, on_event = self._events()
        payload = {"type": "card_action", "action": {"value": {"k": "v"}}}
        body = json.dumps(_encrypt(payload, "secret")).encode("utf-8")
        status, resp = process_event(body, encrypt_key="secret", on_event=on_event)
        self.assertEqual(status, 200)
        self.assertEqual(captured[0]["type"], "card_action")

    @unittest.skipUnless(CRYPTO_AVAILABLE, "需要 pycryptodome")
    def test_decrypt_fail_returns_400_and_counts(self):
        status, resp = process_event(
            json.dumps({"encrypt": "!!bad!!"}).encode("utf-8"),
            encrypt_key="secret",
        )
        self.assertEqual(status, 400)
        self.assertEqual(resp["error"], "Decrypt failed")
        self.assertEqual(get_decrypt_fail_count(), 1)


class TestWebhookServerIntegration(unittest.TestCase):
    """真实 HTTPServer 冒烟：do_POST 接线 + /health 解密失败计数"""

    def setUp(self):
        reset_decrypt_fail_count()
        self._saved = (
            FeishuWebhookHandler.on_event,
            FeishuWebhookHandler.verification_token,
            FeishuWebhookHandler.encrypt_key,
            FeishuWebhookHandler.allow_from,
            FeishuWebhookHandler.require_encrypt,
        )

    def tearDown(self):
        (
            FeishuWebhookHandler.on_event,
            FeishuWebhookHandler.verification_token,
            FeishuWebhookHandler.encrypt_key,
            FeishuWebhookHandler.allow_from,
            FeishuWebhookHandler.require_encrypt,
        ) = self._saved

    def test_do_post_and_health(self):
        captured = []

        server = FeishuWebhookServer(
            host="127.0.0.1",
            port=0,
            on_event=lambda e: captured.append(e),
            verification_token="tok",
        )
        # 手动注入并起线程（FeishuWebhookServer.start 内部用 ThreadingHTTPServer）
        FeishuWebhookHandler.on_event = server.on_event
        FeishuWebhookHandler.verification_token = server.verification_token
        FeishuWebhookHandler.encrypt_key = server.encrypt_key
        FeishuWebhookHandler.allow_from = server.allow_from
        FeishuWebhookHandler.require_encrypt = server.require_encrypt
        server._server = __import__("http.server").server.ThreadingHTTPServer(
            ("127.0.0.1", 0), FeishuWebhookHandler
        )
        port = server._server.server_address[1]
        thread = Thread(target=server._server.serve_forever, daemon=True)
        thread.start()
        try:
            # 错误 token → 403
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("POST", "/", body=json.dumps({"token": "bad"}), headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            self.assertEqual(resp.status, 403)
            resp.read()
            conn.close()

            # 正确 token 的 card_action → 200 且 on_event 被调用
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            body = json.dumps({"token": "tok", "type": "card_action", "action": {"value": {}}})
            conn.request("POST", "/", body=body, headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            self.assertEqual(resp.status, 200)
            resp.read()
            conn.close()
            self.assertEqual(len(captured), 1)
            self.assertEqual(captured[0]["type"], "card_action")

            # /health 返回解密失败计数
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", "/health")
            resp = conn.getresponse()
            self.assertEqual(resp.status, 200)
            health = json.loads(resp.read().decode("utf-8"))
            self.assertIn("decrypt_fail_count", health)
            conn.close()
        finally:
            server._server.shutdown()
            server._server.server_close()


if __name__ == "__main__":
    unittest.main()
