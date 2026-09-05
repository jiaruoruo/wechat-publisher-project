"""Playwright 浏览器会话管理 - Cookie 持久化"""

import os
import time
import logging

from playwright.sync_api import sync_playwright, Browser, BrowserContext, Page

from config.paths import BASE_DIR, SESSION_PATH as DEFAULT_SESSION_FILE
from browser.screenshot_utils import screenshot_path
from browser import wechat_selectors as sel

logger = logging.getLogger(__name__)
WECHAT_MP_URL = "https://mp.weixin.qq.com"

# 已登录后台的特征选择器（纳入 wechat_selectors 配置体系，编辑器改版时一处维护）。
# 从 selectors 读取；缺失时回退到原硬编码值，保证向后兼容。
LOGIN_INDICATOR_SELECTORS = sel.get_selector_list("login_indicators") or [
    ".weui-desktop-panel",
]


class WechatSession:
    """微信公众号浏览器会话管理"""

    def __init__(self, config: dict):
        browser_config = config.get("browser", {})
        self.headless = browser_config.get("headless", False)
        self.session_file = browser_config.get("session_file", DEFAULT_SESSION_FILE)
        self.screenshot_dir = browser_config.get("screenshot_dir", "storage/screenshots")

        # 确保路径为绝对路径（防止工作目录不同导致路径错误）
        if not os.path.isabs(self.session_file):
            self.session_file = os.path.join(BASE_DIR, self.session_file)
        if not os.path.isabs(self.screenshot_dir):
            self.screenshot_dir = os.path.join(BASE_DIR, self.screenshot_dir)

        # 确保目录存在
        os.makedirs(os.path.dirname(self.session_file), exist_ok=True)
        os.makedirs(self.screenshot_dir, exist_ok=True)

        self._playwright = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    @property
    def page(self) -> Page | None:
        """公开的 Page 对象（替代直接访问 _page）"""
        return self._page

    def start(self) -> Page:
        """启动浏览器并返回 Page 对象"""
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(
            headless=self.headless,
            args=["--disable-blink-features=AutomationControlled"],
        )

        # 加载已保存的会话状态
        if os.path.exists(self.session_file):
            logger.info(f"加载已保存的会话: {self.session_file}")
            self._context = self._browser.new_context(
                storage_state=self.session_file,
                viewport={"width": 1280, "height": 800},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                # 剪贴板权限：正文注入使用 ClipboardItem + Ctrl+V 触发 ProseMirror paste
                permissions=["clipboard-read", "clipboard-write"],
            )
        else:
            logger.info("无已保存的会话，创建新会话")
            self._context = self._browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                permissions=["clipboard-read", "clipboard-write"],
            )

        self._page = self._context.new_page()
        return self._page

    def save_session(self):
        """保存当前会话状态（Cookie 等）"""
        if self._context:
            self._context.storage_state(path=self.session_file)
            logger.info(f"会话已保存: {self.session_file}")

    def _on_login_shell(self) -> bool:
        """页面是否为未登录的「登录壳页」

        新版后台未登录时部分页面（如 cgi-bin/home）不重定向到 login 页，
        而是直接展示扫码登录壳——仅凭 URL 含 cgi-bin 会误判为已登录。
        特征：存在可见的 loginpage 链接 **且** 页面没有管理后台特征。
        （已登录管理页也可能有指向 loginpage 的「退出/切换账号」链接，
        因此必须同时确认无后台特征才判为登录壳，否则会把已登录误判为未登录）
        """
        try:
            return bool(self._page.evaluate("""() => {
                const links = Array.from(document.querySelectorAll('a[href*="loginpage"]'))
                    .filter(a => a.offsetParent !== null);
                if (!links.length) return false;
                // 登录壳页必有「扫码登录」文案；已登录管理页不会有（「扫码预览」等不算）
                const body = ((document.body && document.body.innerText) || '').replace(/\\s+/g, '');
                return /扫码登录|扫码后可登录/.test(body);
            }"""))
        except Exception:
            return False

    def check_login(self) -> bool:
        """检查是否已登录公众号后台"""
        if not self._page:
            return False

        try:
            self._page.goto(WECHAT_MP_URL, wait_until="networkidle", timeout=30000)
            # 检查是否跳转到了登录页面
            current_url = self._page.url
            if "login" in current_url or "wx_open" in current_url:
                logger.warning("未登录或登录已过期")
                return False
            # 未重定向但页面是登录壳（可见登录入口）：同样视为未登录
            if self._on_login_shell():
                logger.warning("未登录或登录已过期（页面为登录壳页）")
                return False
            # 检查页面是否有公众号后台的特征元素（选择器来自 selectors 配置）
            for indicator in LOGIN_INDICATOR_SELECTORS:
                if self._page.locator(indicator).count() > 0:
                    logger.info("已登录公众号后台")
                    return True
            # 备用判断：URL 中包含 cgi-bin（登录壳已在前面排除）
            if "cgi-bin" in current_url:
                logger.info("已登录公众号后台（通过 URL 判断）")
                return True
            return False
        except Exception as e:
            logger.error(f"检查登录状态失败: {e}")
            return False

    def login_interactive(self, timeout: int = 120):
        """
        交互式登录：打开浏览器让用户手动扫码登录

        Args:
            timeout: 等待登录的超时时间（秒）
        """
        if not self._page:
            raise RuntimeError("浏览器未启动，请先调用 start()")

        logger.info("请在浏览器中扫码登录公众号...")
        self._page.goto(WECHAT_MP_URL)

        # 等待用户扫码登录：URL 进入后台 **且** 页面不再是登录壳页（防误报）。
        # 历史缺陷：仅等 URL 匹配 **/cgi-bin/**，未登录时 cgi-bin/home 登录壳页也匹配，
        # 会在用户未扫码的情况下误报「登录成功」。
        deadline = time.time() + timeout
        logged_in = False
        while time.time() < deadline:
            try:
                url = self._page.url
                if "cgi-bin" in url and "login" not in url and not self._on_login_shell():
                    logged_in = True
                    break
            except Exception:
                pass
            self._page.wait_for_timeout(1000)

        if not logged_in:
            logger.error(f"登录超时（{timeout}秒），请重试")
            raise RuntimeError("登录超时")
        logger.info("登录成功！")
        self.save_session()

    def take_screenshot(self, name: str = "screenshot") -> str:
        """截图并保存（按日期归档 + 时间戳后缀，与 publish_actions 命名一致）"""
        if not self._page:
            raise RuntimeError("浏览器未启动")

        path = screenshot_path(self.screenshot_dir, name)
        self._page.screenshot(path=path, full_page=True)
        logger.info(f"截图已保存: {path}")
        return path

    def close(self):
        """关闭浏览器"""
        try:
            if self._context:
                self.save_session()
                self._context.close()
            if self._browser:
                self._browser.close()
            if self._playwright:
                self._playwright.stop()
            logger.info("浏览器已关闭")
        except Exception as e:
            logger.error(f"关闭浏览器时出错: {e}")

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()