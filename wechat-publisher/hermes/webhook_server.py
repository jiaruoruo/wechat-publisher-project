"""飞书 Webhook 服务器 - 接收飞书事件回调（消息、卡片交互等）

安全加固：
- 默认绑定 127.0.0.1（经反向代理对外），避免对公网裸暴露；
- 可选 IP 白名单 webhook_allow_from（支持 IP / CIDR），拒绝非白名单来源；
- 可选 require_encrypt 强制加密：开启后未加密的请求一律拒绝。
"""

import json
import base64
import hashlib
import logging
import threading
from http.server import HTTPServer, ThreadingHTTPServer, BaseHTTPRequestHandler
from threading import Thread
from typing import Callable

logger = logging.getLogger(__name__)


class DaemonThreadingHTTPServer(ThreadingHTTPServer):
    """请求处理线程为 daemon 的 ThreadingHTTPServer

    ThreadingHTTPServer 默认 daemon_threads=False：请求处理线程在进程退出时
    会阻塞解释器关闭（等待所有请求完成）。设为 daemon 后，主进程退出时
    未完成的请求线程随进程终止，避免 Ctrl+C / stop() 卡住。
    """

    daemon_threads = True

# ── 解密失败计数（跨请求累计，暴露到 /health，便于发现 encrypt_key 变更等问题）──
_decrypt_fail_count = 0
_decrypt_fail_lock = threading.Lock()


def _increment_decrypt_fail() -> None:
    global _decrypt_fail_count
    with _decrypt_fail_lock:
        _decrypt_fail_count += 1


def get_decrypt_fail_count() -> int:
    """累计解密失败次数（供 /health 与测试使用）"""
    with _decrypt_fail_lock:
        return _decrypt_fail_count


def reset_decrypt_fail_count() -> None:
    """重置解密失败计数（测试用）"""
    global _decrypt_fail_count
    with _decrypt_fail_lock:
        _decrypt_fail_count = 0


def ip_allowed(peer_ip: str, allow_from: list[str]) -> bool:
    """判断来源 IP 是否在白名单内（支持 IP 或 CIDR；白名单为空 = 全部放行）"""
    if not allow_from:
        return True
    if not peer_ip:
        return False
    try:
        from ipaddress import ip_address, ip_network

        addr = ip_address(peer_ip)
    except ValueError:
        return False
    for entry in allow_from:
        try:
            if addr in ip_network(entry, strict=False):
                return True
        except ValueError:
            continue
    return False


def decrypt_event(data: dict, encrypt_key: str) -> dict | None:
    """解密飞书事件体（encrypt_key 模式，AES-256-CBC）

    飞书加密算法：key = md5(encrypt_key) 的 hex 前 16 字符（即 16 字节），
    IV 与 key 相同；密文为 base64。URL 验证挑战同样走加密。

    Returns:
        解密后的 dict；失败返回 None（并累计失败计数）。
    """
    encrypted = data.get("encrypt", "")
    if not encrypted:
        logger.warning("已配置 encrypt_key，但事件体缺少 encrypt 字段")
        _increment_decrypt_fail()
        return None
    try:
        from Crypto.Cipher import AES
        from Crypto.Util.Padding import unpad

        key = hashlib.md5(encrypt_key.encode("utf-8")).hexdigest()[:16].encode("utf-8")
        cipher = AES.new(key, AES.MODE_CBC, key)
        plaintext = unpad(cipher.decrypt(base64.b64decode(encrypted)), AES.block_size)
        return json.loads(plaintext.decode("utf-8"))
    except Exception as e:
        logger.error(f"飞书事件解密失败: {e}")
        _increment_decrypt_fail()
        return None


def process_event(
    body: bytes,
    *,
    verification_token: str = "",
    encrypt_key: str = "",
    allow_from: list[str] | None = None,
    require_encrypt: bool = False,
    peer_ip: str = "",
    on_event: Callable | None = None,
) -> tuple[int, dict]:
    """处理一条 webhook 请求体，返回 (HTTP status, JSON 响应 dict)

    抽出为纯函数便于单测；do_POST 只负责读 body 与写响应。
    处理顺序：JSON 解析 → IP 白名单 → 解密 → url_verification → token 校验 →
    v2.0 / v1.0 / card_action 事件。
    """
    try:
        data = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return 400, {"error": "Invalid JSON"}

    # IP 白名单（第一道防线，先于一切业务逻辑）
    if not ip_allowed(peer_ip, allow_from or []):
        logger.warning(f"拒绝非白名单来源的 webhook 请求: {peer_ip}")
        return 403, {"error": "Forbidden"}

    # 事件体解密（require_encrypt 强制加密；encrypt_key 配置了则始终解密）
    if require_encrypt or encrypt_key:
        if not encrypt_key:
            logger.warning("require_encrypt 开启但未配置 encrypt_key，拒绝未加密请求")
            return 403, {"error": "Encryption required but no encrypt_key configured"}
        if "encrypt" not in data:
            logger.warning("require_encrypt 开启但请求未加密，拒绝")
            return 403, {"error": "Encryption required"}
        data = decrypt_event(data, encrypt_key)
        if data is None:
            return 400, {"error": "Decrypt failed"}

    # URL 验证挑战（飞书首次配置回调时的验证）
    if data.get("type") == "url_verification":
        challenge = data.get("challenge", "")
        logger.info(f"收到飞书 URL 验证挑战: {str(challenge)[:20]}...")
        return 200, {"challenge": challenge}

    # 验证 token
    token = data.get("token", "")
    if verification_token and token != verification_token:
        logger.warning("飞书 verification_token 验证失败")
        return 403, {"error": "Invalid token"}

    # 处理 v2.0 事件格式
    if data.get("schema") == "2.0":
        header = data.get("header", {})
        event = data.get("event", {})
        event_type = header.get("event_type", "")

        # 提取消息事件
        if event_type == "im.message.receive_v1":
            message = event.get("message", {})
            sender = event.get("sender", {}).get("sender_id", {})

            msg_type = message.get("message_type", "")
            message_id = message.get("message_id", "")
            chat_id = message.get("chat_id", "")
            user_id = sender.get("open_id", "")

            if msg_type == "text":
                # 飞书富文本/文件/图片等事件的 content 可能不是合法 JSON，
                # 必须做容错，否则 json.loads 抛异常会冒泡到 do_POST 导致 500。
                try:
                    raw_content = message.get("content", "{}")
                    if isinstance(raw_content, str):
                        content = json.loads(raw_content or "{}")
                    elif isinstance(raw_content, dict):
                        content = raw_content
                    else:
                        content = {}
                    text = content.get("text", "")
                except (json.JSONDecodeError, TypeError, ValueError) as e:
                    logger.error(
                        f"解析 v2.0 message.content 失败（非文本内容已忽略）: {e}; "
                        f"content={message.get('content')!r}"
                    )
                    text = ""

                logger.info(f"收到飞书消息: user={user_id}, text={text[:50]}")

                if on_event:
                    on_event({
                        "type": "message",
                        "text": text,
                        "message_id": message_id,
                        "chat_id": chat_id,
                        "user_id": user_id,
                    })

    # 处理 v1.0 事件格式（兼容）
    elif data.get("type") == "event_callback":
        event = data.get("event", {})
        event_type = data.get("event", {}).get("type", "")

        if event_type == "message":
            msg_type = event.get("message_type", "")
            if msg_type == "text":
                text = event.get("text", "")
                message_id = event.get("message_id", "")
                chat_id = event.get("chat_id", "")
                user_id = event.get("open_id", "")

                logger.info(f"收到飞书消息(v1): user={user_id}, text={text[:50]}")

                if on_event:
                    on_event({
                        "type": "message",
                        "text": text,
                        "message_id": message_id,
                        "chat_id": chat_id,
                        "user_id": user_id,
                    })

    # 处理卡片交互回调
    elif data.get("type") == "card_action":
        action = data.get("action", {})
        logger.info(f"收到飞书卡片交互: {action}")
        if on_event:
            on_event({
                "type": "card_action",
                "action": action,
                "data": data,
            })

    return 200, {"code": 0}


class FeishuWebhookHandler(BaseHTTPRequestHandler):
    """飞书事件回调 HTTP Handler"""

    # 由 FeishuWebhookServer 注入
    on_event: Callable | None = None
    verification_token: str = ""
    encrypt_key: str = ""
    allow_from: list[str] = []
    require_encrypt: bool = False

    def do_POST(self):
        """处理 POST 请求（飞书事件回调）"""
        content_length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(content_length) if content_length else b""

        try:
            status, resp = process_event(
                body,
                verification_token=self.verification_token,
                encrypt_key=self.encrypt_key,
                allow_from=self.allow_from,
                require_encrypt=self.require_encrypt,
                peer_ip=self.client_address[0],
                # 用 type(self).on_event 取类属性原值，避免函数被描述符协议
                # 绑定到 handler 实例（on_event 是普通函数时会变成 2 参数调用）
                on_event=type(self).on_event,
            )
        except Exception as e:
            # 业务回调 on_event 抛异常不应冒泡为 500 断连；记录日志并返回 200，
            # 避免飞书因连续非 2xx 而重试/封禁回调地址。
            logger.exception(f"处理飞书事件回调异常（已吞掉，返回 200）: {e}")
            status, resp = 200, {"code": 0, "warning": "event handled with internal error"}
        self._respond(status, resp)

    def do_GET(self):
        """健康检查"""
        if self.path == "/health":
            self._respond(200, {
                "status": "ok",
                "service": "hermes-agent",
                "decrypt_fail_count": get_decrypt_fail_count(),
            })
        else:
            self._respond(404, {"error": "Not found"})

    def _respond(self, status: int, body: dict):
        """发送 HTTP 响应"""
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body, ensure_ascii=False).encode("utf-8"))

    def log_message(self, format, *args):
        """覆盖默认日志"""
        logger.debug(f"WebhookServer: {format % args}")


class FeishuWebhookServer:
    """飞书 Webhook HTTP 服务器（后台线程运行）"""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 9000,
        on_event: Callable | None = None,
        verification_token: str = "",
        encrypt_key: str = "",
        allow_from: list[str] | None = None,
        require_encrypt: bool = False,
    ):
        self.host = host
        self.port = port
        self.on_event = on_event
        self.verification_token = verification_token
        self.encrypt_key = encrypt_key
        self.allow_from = [a.strip() for a in (allow_from or []) if a.strip()]
        self.require_encrypt = require_encrypt
        self._server: HTTPServer | None = None
        self._thread: Thread | None = None

    def start(self):
        """启动 Webhook 服务器（后台线程）"""
        # 注入回调和配置到 Handler 类
        FeishuWebhookHandler.on_event = self.on_event
        FeishuWebhookHandler.verification_token = self.verification_token
        FeishuWebhookHandler.encrypt_key = self.encrypt_key
        FeishuWebhookHandler.allow_from = self.allow_from
        FeishuWebhookHandler.require_encrypt = self.require_encrypt

        # 使用 ThreadingHTTPServer，避免单线程串行阻塞（飞书 API 调用慢时回调排队）；
        # daemon_threads=True 确保进程退出时请求处理线程不阻塞关闭
        self._server = DaemonThreadingHTTPServer((self.host, self.port), FeishuWebhookHandler)
        self._thread = Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

        logger.info(f"Hermes Webhook 服务器已启动: http://{self.host}:{self.port}")

    def stop(self):
        """停止 Webhook 服务器"""
        if self._server:
            self._server.shutdown()
            logger.info("Hermes Webhook 服务器已停止")

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()
