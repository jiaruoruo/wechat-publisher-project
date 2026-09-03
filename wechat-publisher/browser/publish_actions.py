"""公众号后台操作封装 - 新建、编辑、发布图文"""

import os
import json
import time
import logging
from typing import Any

from playwright.sync_api import Page

from browser.screenshot_utils import screenshot_path, cleanup_old_screenshots
from browser.selector_ledger import append_selector_stats

# 选择器通过模块引用读取（sel.XXX），支持 reload_selectors() 热加载而不改代码
import browser.wechat_selectors as sel

logger = logging.getLogger(__name__)

try:
    from config.structured_logging import log_event as _log_event
except ImportError:
    def _log_event(event: str, **kwargs: Any) -> None:  # type: ignore[misc]
        pass


WECHAT_MP_URL = "https://mp.weixin.qq.com"

# 发表记录页候选入口（发布后复核用）。
# 实测：后台内部页面必须拼 &token=...&lang=zh_CN（_mp_token 从当前 URL 提取），
# 否则被重定向到首页/登录页；会话失效时复核会如实返回失败。
PUBLISHED_LIST_URLS = (
    "https://mp.weixin.qq.com/cgi-bin/appmsgpublish?sub=list&begin=0&count=50",
    "https://mp.weixin.qq.com/cgi-bin/appmsg?t=media/appmsg_list&action=list&type=10",
)

# 发表记录页：按标题文本匹配文章链接（列表标题可能被截断，故双向包含匹配）
_PUBLISHED_LINK_JS = """(args) => {
    const norm = (s) => (s || '').replace(/\\s+/g, '');
    const n = norm(args.needle);
    if (!n) return '';
    const as = Array.from(document.querySelectorAll('a[href]'));
    for (const a of as) {
        const t = norm(a.textContent || '');
        if (!t) continue;
        if (t.indexOf(n) >= 0 || n.indexOf(t) >= 0) return a.href;
    }
    return '';
}"""


class PublishActions:
    """微信公众号后台操作封装"""

    def __init__(self, page: Page, config: dict, session=None):
        self.page = page
        self.config = config
        # session 引用：编辑器可能在新标签页打开，切换页面时同步 session._page，
        # 确保发布后 take_screenshot / close() 操作的是正确的页面
        self._session = session
        self.publish_mode = config.get("wechat", {}).get("publish_mode", "draft")
        self.screenshot_dir = config.get("browser", {}).get("screenshot_dir", "storage/screenshots")
        os.makedirs(self.screenshot_dir, exist_ok=True)
        # 截图保留天数（0 = 不清理）；每次发布开始时清理过期日期目录，防止无限堆积
        self.retention_days = int(
            config.get("browser", {}).get("screenshot_retention_days", 30)
        )
        # 本次发布的选择器命中统计：{label: {selector: 命中次数}}（execute_publish 开始时重置）
        self._selector_stats: dict[str, dict[str, int]] = {}

    def _switch_page(self, new_page):
        """切换到指定页面（编辑器标签页），并同步 session 内部状态"""
        self.page = new_page
        if self._session is not None and hasattr(self._session, "_page"):
            self._session._page = new_page
            logger.info("已同步 WechatSession 到编辑器标签页")

    # ── 核心选择器探测基础设施 ────────────────────────────────────────

    def _try_select(
        self,
        selectors: list[str],
        label: str,
        *,
        timeout_per: int = 2000,
        screenshot_name: str = "",
        dom_area_selectors: list[str] | None = None,
    ) -> tuple[Any, str] | None:
        """尝试多个选择器，返回 (element, matched_selector) 或 None

        - 按顺序探测每个选择器，首个可见即返回
        - 命中时记录结构化日志（含位置、总数），便于编辑器改版后优先调整回退顺序
        - 全部未命中时截图 + DOM 诊断
        """
        total = len(selectors)
        for i, selector in enumerate(selectors):
            try:
                el = self.page.locator(selector).first
                if el.is_visible(timeout=timeout_per):
                    _log_event(
                        "editor.selector_match",
                        label=label,
                        selector=selector,
                        position=i + 1,
                        total=total,
                    )
                    stats = self._selector_stats.setdefault(label, {})
                    stats[selector] = stats.get(selector, 0) + 1
                    logger.info(f"[{label}] 命中选择器 ({i + 1}/{total}): {selector}")
                    return el, selector
            except Exception:
                continue

        # 全部未命中（记录该分组被探测过且 0 命中，供命中率统计）
        self._selector_stats.setdefault(label, {})
        if screenshot_name:
            self._screenshot(screenshot_name)
        if dom_area_selectors:
            self._diagnose_dom(label, dom_area_selectors)
        _log_event(
            "editor.selector_all_missed",
            label=label,
            total=total,
            selectors=selectors,
        )
        logger.warning(f"[{label}] 所有 {total} 个选择器均未匹配")
        return None

    def _try_click_selectors(
        self,
        selectors: list[str],
        label: str,
        timeout_per: int = 2000,
        *,
        screenshot_name: str = "",
        dom_area_selectors: list[str] | None = None,
    ) -> bool:
        """点击首个可见选择器（含结构化日志 + 失败诊断）

        Returns:
            True 如果成功点击了某个选择器
        """
        result = self._try_select(
            selectors,
            label,
            timeout_per=timeout_per,
            screenshot_name=screenshot_name,
            dom_area_selectors=dom_area_selectors,
        )
        if result is None:
            return False
        el, _ = result
        el.click()
        return True

    def _try_fill(
        self,
        selectors: list[str],
        label: str,
        value: str,
        *,
        timeout_per: int = 2000,
        screenshot_name: str = "",
    ) -> bool:
        """找到首个可见选择器并 fill（含结构化日志）

        Returns:
            True 如果成功填写
        """
        result = self._try_select(
            selectors,
            label,
            timeout_per=timeout_per,
            screenshot_name=screenshot_name,
        )
        if result is None:
            return False
        el, _ = result
        el.click()
        el.fill(value)
        self.page.wait_for_timeout(300)
        return True

    def _diagnose_dom(self, label: str, area_selectors: list[str] | None = None):
        """输出指定区域 DOM 片段，便于编辑器改版后人工定位选择器"""
        selectors = area_selectors or []
        if not selectors:
            return
        try:
            js_list = json.dumps(selectors)
            snippet = self.page.evaluate(
                f"""() => {{
                    for (const sel of {js_list}) {{
                        const el = document.querySelector(sel);
                        if (el) return el.outerHTML.slice(0, 800);
                    }}
                    return 'NO_AREA_FOUND';
                }}"""
            )
            logger.warning(f"[{label}] DOM 片段: {snippet}")
        except Exception as e:
            logger.debug(f"[{label}] 获取 DOM 片段失败: {e}")

    # ── 导航 ──────────────────────────────────────────────────────────

    def navigate_to_editor(self):
        """导航到图文消息编辑页面（兼容新版编辑器在新标签页打开）"""
        logger.info("导航到图文消息编辑页面...")

        # 进入公众号后台
        self.page.goto(WECHAT_MP_URL, wait_until="networkidle", timeout=30000)
        self.page.wait_for_timeout(1500)

        # 记录当前标签页数量，用于检测新标签页
        base_pages = len(self.page.context.pages)

        result = self._try_select(
            sel.NAV_NEW_ARTICLE,
            "新建图文入口",
            timeout_per=3000,
            screenshot_name="nav_editor_no_entry",
            dom_area_selectors=[".new-creation-accordion", ".weui-desktop-btn_wrp"],
        )
        if result:
            el, _ = result
            # 新版后台：编辑器在新标签页（popup）打开（appmsg_edit_v2）
            # 用 expect_popup 捕获新标签页（比轮询 ctx.pages 更可靠）
            try:
                with self.page.expect_popup(timeout=15000) as popup_info:
                    el.click()
                editor_page = popup_info.value
                self._switch_page(editor_page)
                logger.info(f"已切换到编辑器标签页: {editor_page.url[:100]}")
                # 等待编辑器加载（v2 编辑器初始化较慢）
                try:
                    self.page.wait_for_selector(sel.NAV_EDITOR_LOAD, timeout=30000)
                    logger.info("编辑器已加载")
                except Exception:
                    logger.warning("编辑器加载等待超时，继续执行")
            except Exception as e:
                logger.warning(f"等待编辑器标签页失败: {e}")
        else:
            logger.warning("未找到新建图文入口，尝试直接导航...")
            self.page.goto(
                f"{WECHAT_MP_URL}/cgi-bin/appmsg?t=media/appmsg_edit_v2&action=edit&isNew=1&type=77",
                wait_until="networkidle",
                timeout=30000,
            )
            self.page.wait_for_timeout(3000)

        self._screenshot("after_navigate_editor")

    # ── 标题 ──────────────────────────────────────────────────────────

    def fill_title(self, title: str):
        """填写文章标题（兼容新版 ProseMirror 标题编辑器）"""
        logger.info(f"填写标题: {title}")

        # 新版编辑器：标题是第一个可见的 ProseMirror（高度最小），#title 为隐藏字段
        # 必须用真实键盘输入（keyboard.type）触发 ProseMirror 输入事件更新文档状态。
        # execCommand/innerText 不会更新 ProseMirror 内部 state，保存后标题为空。
        try:
            focused = self.page.evaluate(
                f"""() => {{
                    const title = {json.dumps(title)};
                    const editors = Array.from(document.querySelectorAll('.ProseMirror, [contenteditable="true"]'))
                        .filter(el => {{
                            const r = el.getBoundingClientRect();
                            return r.width > 0 && r.height > 0;
                        }});
                    if (editors.length === 0) return false;
                    // 取第一个 ProseMirror（标题区），或高度最小的
                    editors.sort((a, b) => a.getBoundingClientRect().height - b.getBoundingClientRect().height);
                    const el = editors[0];
                    el.focus();
                    return true;
                }}"""
            )
            if focused:
                # 真实键盘输入（触发 ProseMirror 状态更新）
                self.page.keyboard.type(title, delay=10)
                self.page.wait_for_timeout(600)
                # 验证标题确实写入
                verify = self.page.evaluate(
                    """() => {
                        const eds = Array.from(document.querySelectorAll('.ProseMirror, [contenteditable="true"]'))
                            .filter(el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; });
                        eds.sort((a, b) => a.getBoundingClientRect().height - b.getBoundingClientRect().height);
                        return eds.length > 0 ? eds[0].innerText : '';
                    }"""
                )
                if verify.strip():
                    logger.info(f"标题已填写（键盘输入）: {verify[:30]}")
                    return
                logger.warning("键盘输入后标题仍为空，尝试回退方式")
        except Exception as e:
            logger.warning(f"ProseMirror 标题填写失败: {e}")

        # 回退：传统 input 方式
        if not self._try_fill(
            sel.TITLE_INPUTS,
            "标题输入",
            title,
            timeout_per=3000,
            screenshot_name="fill_title_failed",
        ):
            raise RuntimeError("未找到标题输入框")

    # ── 图片替换 ──────────────────────────────────────────────────────

    def replace_inline_images(self, html_content: str, inline_images: list[str]) -> str:
        """把 HTML 中的 {{IMAGE_PATH_N}} 占位符替换为微信 CDN 外链

        - wechat.external_images=true（默认）：上传到微信 CDN 拿外链，逐张独立
          兜底——失败回退 base64，已成功的 CDN URL 保留；
        - wechat.external_images=false：直接 base64 内嵌（旧行为）。
        """
        from browser.image_uploader import (
            resolve_image_placeholders,
            upload_image_to_cdn,
            read_token_from_page,
        )

        use_cdn = self.config.get("wechat", {}).get("external_images", True)
        token = read_token_from_page(self.page) if use_cdn else ""

        def uploader(path: str) -> str:
            if not use_cdn:
                return ""  # 关闭外链时返回空串 → resolve 兜底 base64
            return upload_image_to_cdn(self.page, path, token)

        resolved, stats = resolve_image_placeholders(html_content, inline_images, uploader)
        if stats.total > 0:
            logger.info(
                f"图片占位符解析完成: total={stats.total}, cdn={stats.cdn_ok}, "
                f"base64={stats.base64_fallback}, missing={stats.missing}, mode={stats.mode}"
            )
        return resolved

    # ── 正文 ──────────────────────────────────────────────────────────

    def fill_content_html(self, html_content: str):
        """填写文章正文（注入 HTML，兼容新版 ProseMirror 编辑器）"""
        logger.info("填写文章正文...")

        # 方式零（新版编辑器）：ProseMirror 正文区剪贴板粘贴
        # 注意：直接 innerHTML 不会更新 ProseMirror 文档状态，保存时内容为空。
        # 必须通过剪贴板粘贴（触发 paste 事件）或键盘输入来更新编辑器状态。
        try:
            injected = self.page.evaluate(
                f"""async () => {{
                    const html = {json.dumps(html_content)};
                    const editors = Array.from(document.querySelectorAll('.ProseMirror, [contenteditable="true"]'))
                        .filter(el => {{
                            const r = el.getBoundingClientRect();
                            return r.width > 100 && r.height > 50;
                        }});
                    if (editors.length === 0) return false;
                    // 取高度最大的（正文区）
                    editors.sort((a, b) => b.getBoundingClientRect().height - a.getBoundingClientRect().height);
                    const el = editors[0];
                    el.focus();
                    // 写剪贴板（HTML），然后 Ctrl+V 触发 ProseMirror paste 处理
                    const blob = new Blob([html], {{ type: 'text/html' }});
                    const item = new ClipboardItem({{ 'text/html': blob }});
                    await navigator.clipboard.write([item]);
                    return true;
                }}"""
            )
            if injected:
                self.page.keyboard.press("Control+v")
                self.page.wait_for_timeout(1000)
                self._verify_content_injected(html_content[:100])
                logger.info("通过 ProseMirror 剪贴板粘贴内容成功")
                return
        except Exception as e:
            logger.warning(f"ProseMirror 剪贴板注入失败: {e}")

        # 方式一：通过编辑器的 API 注入（旧版 UEditor）
        try:
            self.page.evaluate(f"""
                (() => {{
                    const editor = document.querySelector('.edui-body-container');
                    if (editor) {{
                        editor.innerHTML = {json.dumps(html_content)};
                        editor.dispatchEvent(new Event('input', {{ bubbles: true }}));
                        return true;
                    }}
                    return false;
                }})()
            """)
            logger.info("通过编辑器 API 注入内容成功")
            self._verify_content_injected(html_content[:100])
            return
        except Exception as e:
            logger.warning(f"编辑器 API 注入失败: {e}")

        # 方式二：通过剪贴板粘贴（多选择器回退）
        try:
            result = self._try_select(
                sel.CONTENT_EDITORS,
                "正文编辑器",
                timeout_per=3000,
                screenshot_name="fill_content_editor_not_found",
            )
            if result:
                editor, matched = result
                editor.click()
                self.page.wait_for_timeout(200)

                self.page.evaluate(f"""
                    async () => {{
                        const html = {json.dumps(html_content)};
                        const blob = new Blob([html], {{ type: 'text/html' }});
                        const clipboardItem = new ClipboardItem({{ 'text/html': blob }});
                        await navigator.clipboard.write([clipboardItem]);
                    }}
                """)
                self.page.keyboard.press("Control+v")
                self._verify_content_injected(html_content[:100])
                logger.info(f"通过剪贴板粘贴内容成功: {matched}")
                return
        except Exception as e:
            logger.warning(f"剪贴板粘贴失败: {e}")

        # 方式三：直接设置 innerHTML
        try:
            self.page.evaluate(f"""
                (() => {{
                    const editors = document.querySelectorAll('[contenteditable="true"]');
                    for (const editor of editors) {{
                        editor.innerHTML = {json.dumps(html_content)};
                        editor.dispatchEvent(new Event('input', {{ bubbles: true }}));
                        editor.dispatchEvent(new Event('change', {{ bubbles: true }}));
                    }}
                }})()
            """)
            logger.info("通过 innerHTML 直接注入成功")
            self._verify_content_injected(html_content[:100])
        except Exception as e:
            logger.error(f"所有内容注入方式均失败: {e}")
            self._screenshot("fill_content_failed")
            raise RuntimeError("无法注入文章内容")

    # ── 封面上传（文件方式） ──────────────────────────────────────────

    def upload_cover_image(self, image_path: str):
        """上传封面图（文件上传方式，CDN 失败后的回退路径）

        新版编辑器实测要点：
        - 点击封面按钮（.js_cover_btn_area「拖拽或选择封面」）直接唤起系统文件选择器；
        - 菜单内的 input[type=file] 是模板元素（隐藏），对它 set_input_files 不会
          触发平台上传逻辑（封面仍为空，点「发表」会被「必须插入一张图片」拦截）；
        - 因此优先走 expect_file_chooser 注入，结果用 _wait_cover_preview 严格验证
          （file_id 隐藏字段/非空 background-image），杜绝假阳性。
        """
        if not image_path or not os.path.exists(image_path):
            logger.warning(f"封面图不存在: {image_path}")
            return

        logger.info(f"上传封面图: {image_path}")

        # 方式一（新版编辑器，最可靠）：点封面按钮/上传入口唤起原生文件选择器注入文件
        if self._upload_cover_via_file_chooser(image_path):
            self._verify_and_report_cover("文件选择器方式")
            return

        # 方式二（旧版编辑器）：对 file input 直接 set_input_files（先展开封面菜单）
        self._try_click_selectors(
            sel.COVER_AREA,
            "封面区域",
            timeout_per=2000,
            dom_area_selectors=sel.COVER_AREA,
        )
        self.page.wait_for_timeout(600)
        try:
            file_input = self.page.locator(", ".join(sel.COVER_FILE_INPUTS)).first
            file_input.set_input_files(image_path)
            logger.info("封面文件已设置（file input set_input_files）")
        except Exception as e:
            logger.warning(f"file input 设置封面文件失败: {e}")

        self._verify_and_report_cover("file input 方式")

    def _upload_cover_via_file_chooser(self, image_path: str) -> bool:
        """点击会唤起原生文件选择器的入口，用 expect_file_chooser 注入封面文件

        候选入口（按优先级）：
        1. 封面按钮本身（.js_cover_btn_area / .select-cover__btn，点击即唤起选择器）；
        2. 展开菜单后可见的「上传/上传图片/本地上传/选择图片」文本入口。
        """
        # 入口 1：封面按钮本身（先确保封面区域可见）
        try:
            cover_btn = self.page.locator(".js_cover_btn_area, .select-cover__btn").first
            if cover_btn.is_visible(timeout=1500):
                try:
                    with self.page.expect_file_chooser(timeout=5000) as fc_info:
                        cover_btn.click()
                    fc_info.value.set_files(image_path)
                    logger.info("封面按钮唤起文件选择器，文件已注入")
                    return True
                except Exception:
                    logger.debug("封面按钮点击未唤起文件选择器，尝试菜单内上传入口")
        except Exception:
            pass

        # 入口 2：展开封面菜单，找可见的「上传」文本入口（JS 标记后由 Playwright 点击）
        self._try_click_selectors(
            sel.COVER_AREA,
            "封面区域",
            timeout_per=2000,
        )
        self.page.wait_for_timeout(600)
        entry_info = ""
        try:
            entry_info = str(self.page.evaluate("""() => {
                const vis = (el) => {
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    if (r.width === 0 || r.height === 0) return false;
                    const s = getComputedStyle(el);
                    return s.display !== 'none' && s.visibility !== 'hidden';
                };
                const texts = ['上传图片', '本地上传', '选择图片', '上传'];
                const cands = Array.from(document.querySelectorAll('a, button, span, div, label'))
                    .filter(el => {
                        if (!vis(el)) return false;
                        const t = (el.innerText || '').trim();
                        if (!texts.includes(t)) return false;
                        // 取叶子节点：没有同样命中的可见后代，避免点到外层容器
                        return !Array.from(el.querySelectorAll('a, button, span, div, label'))
                            .some(c => vis(c) && texts.includes((c.innerText || '').trim()));
                    });
                if (!cands.length) return '';
                cands[0].setAttribute('data-cover-upload-probe', '1');
                return cands[0].tagName + '.' + String(cands[0].className).slice(0, 80);
            }"""))
        except Exception as e:
            logger.debug(f"探测封面上传入口异常: {e}")
        if not entry_info:
            logger.debug("封面菜单内未找到可见上传入口")
            return False
        try:
            entry = self.page.locator('[data-cover-upload-probe="1"]').first
            with self.page.expect_file_chooser(timeout=5000) as fc_info:
                entry.click()
            fc_info.value.set_files(image_path)
            logger.info(f"封面上传入口已点击（{entry_info}），文件已注入")
            return True
        except Exception as e:
            logger.debug(f"文件选择器方式注入封面文件失败（{entry_info}）: {e}")
            return False
        finally:
            try:
                self.page.evaluate(
                    "() => document.querySelectorAll('[data-cover-upload-probe]')"
                    ".forEach(e => e.removeAttribute('data-cover-upload-probe'))"
                )
            except Exception:
                pass

    def _verify_and_report_cover(self, method: str) -> None:
        """封面上传后验证结果并如实记录（失败则截图 + DOM 状态，不做假阳性放行）"""
        if self._wait_cover_preview(timeout=15000):
            logger.info(f"封面设置验证通过（{method}：file_id/封面预览已确认）")
            return
        state = self._cover_state()
        logger.warning(
            f"封面设置验证未通过（{method}）: {state}"
            "——发表将被后台以「必须插入一张图片」拦截"
        )
        self._screenshot("cover_upload_not_verified")
        self._diagnose_dom("cover_upload_not_verified", sel.COVER_AREA)

    def _cover_state(self) -> str:
        """封面当前状态（诊断用，返回 JSON 字符串）"""
        try:
            return str(self.page.evaluate("""() => {
                const fid = document.querySelector('.js_file_id, input[name="file_id"]');
                const prev = document.querySelector(
                    '.js_cover_preview_new, .select-cover__preview');
                return JSON.stringify({
                    file_id: fid ? String(fid.value || '').slice(0, 40) : 'no-input',
                    preview_display: prev ? getComputedStyle(prev).display : 'no-el',
                    preview_bg: prev
                        ? (getComputedStyle(prev).backgroundImage || '').slice(0, 80) : '',
                });
            }"""))
        except Exception as e:
            return f"state-query-failed: {e}"

    # ── 正文上传图片（新版编辑器，封面设置的前置步骤） ───────────────────

    def upload_body_image(self, image_path: str) -> bool:
        """通过正文工具栏「图片 → 上传图片」注入图片（新版编辑器实测路径）

        实测要点：
        - 工具栏图片下拉菜单用 JS mouseenter+click 展开（Playwright 原生 click 不展开）；
        - 下拉菜单第一个 tpl_dropdown_menu_item（无 js_img_from_* class）即「上传图片」，
          内含隐藏 file input；必须用 Playwright 原生点击唤起 filechooser 事件；
        - 上传成功后正文出现 mmbiz.qpic.cn CDN 图，可作为封面「从正文选择」的素材。

        Returns:
            True 如果正文出现新图片
        """
        if not image_path or not os.path.exists(image_path):
            logger.warning(f"图片文件不存在: {image_path}")
            return False
        try:
            # 1. JS 展开工具栏图片下拉菜单
            opened = self.page.evaluate("""() => {
                const cands = Array.from(document.querySelectorAll('li, a, button, span, div'))
                    .filter(el => {
                        const cls = String(el.className || '');
                        return cls.includes('jsInsertIcon') && cls.includes('img')
                            && el.offsetParent !== null;
                    });
                if (!cands.length) return false;
                cands[0].dispatchEvent(new MouseEvent('mouseenter', {bubbles: true}));
                cands[0].click();
                return true;
            }""")
            if not opened:
                logger.warning("未找到正文工具栏「图片」按钮")
                return False
            self.page.wait_for_timeout(1200)

            # 2. 标记「上传图片」菜单项（无 js_img_from_* class 的首项）
            marked = self.page.evaluate("""() => {
                const cands = Array.from(
                    document.querySelectorAll('ul.js_img_dropdown_menu li'))
                    .filter(li => li.offsetParent !== null
                        && !String(li.className).includes('js_img_from_'));
                if (!cands.length) return false;
                cands[0].setAttribute('data-body-upload', '1');
                return true;
            }""")
            if not marked:
                logger.warning("图片下拉菜单中未找到「上传图片」项")
                return False

            # 3. Playwright 原生点击菜单项 → filechooser → 注入文件
            before_count = self.page.evaluate(
                "() => document.querySelectorAll('.ProseMirror img').length"
            )
            try:
                with self.page.expect_file_chooser(timeout=6000) as fc_info:
                    self.page.locator('[data-body-upload="1"]').first.click()
                fc_info.value.set_files(image_path)
                logger.info("正文图片文件已注入（工具栏「上传图片」）")
            except Exception as e:
                logger.warning(f"正文图片上传（file chooser）失败: {e}")
                return False
            finally:
                try:
                    self.page.evaluate(
                        "() => document.querySelectorAll('[data-body-upload]')"
                        ".forEach(e => e.removeAttribute('data-body-upload'))"
                    )
                except Exception:
                    pass

            # 4. 等待正文出现新图（CDN URL 且可见尺寸）
            try:
                self.page.wait_for_function(
                    """(before) => {
                        const imgs = Array.from(
                            document.querySelectorAll('.ProseMirror img'))
                            .filter(img => (img.getAttribute('src') || '')
                                && img.getBoundingClientRect().width > 10);
                        return imgs.length > before;
                    }""",
                    arg=before_count,
                    timeout=15000,
                )
                logger.info("正文图片上传成功（已出现 CDN 图）")
                return True
            except Exception:
                logger.warning("正文图片上传后未检测到新图（可能上传失败）")
                return False
        except Exception as e:
            logger.warning(f"正文图片上传异常: {e}")
            return False

    def set_cover_from_body_v2(self) -> bool:
        """新版编辑器「从正文选择」设封面（真实鼠标事件弹菜单，实测路径）

        与 set_cover_from_body 的区别：新版封面空态菜单 #js_cover_null 的显示
        依赖 Playwright 原生鼠标事件（JS click 不改变其 visibility），因此：
        1) Playwright 点击封面按钮（正文图片列表 ul.appmsg_content_img_list
           可能遮挡，force 兜底）；
        2) 等待菜单弹出后 JS 点击「从正文选择」；
        3) 在「选择图片」对话框确认（_confirm_cover_dialog）。
        """
        try:
            # 1. Playwright 点击封面按钮弹菜单（真实鼠标事件）
            try:
                self.page.locator(sel.COVER_AREA[0]).first.click(timeout=8000)
            except Exception:
                self.page.locator(sel.COVER_AREA[0]).first.click(timeout=8000, force=True)
            self.page.wait_for_timeout(1200)

            # 2. 等待封面菜单弹出并点击可见的「从正文选择」（轮询最多 8 秒）
            #    注意：菜单可能为 fixed 定位，offsetParent 恒为 null，必须用
            #    rect + computedStyle 判断可见性（否则菜单已弹出也会误判为未弹出）
            clicked = False
            for _ in range(16):
                clicked = self.page.evaluate("""() => {
                    const vis = (el) => {
                        if (!el) return false;
                        const r = el.getBoundingClientRect();
                        if (r.width === 0 || r.height === 0) return false;
                        const s = getComputedStyle(el);
                        return s.display !== 'none' && s.visibility !== 'hidden';
                    };
                    const cands = Array.from(
                        document.querySelectorAll('a.js_selectCoverFromContent'))
                        .filter(a => vis(a));
                    if (!cands.length) return false;
                    cands[0].click();
                    return true;
                }""")
                if clicked:
                    break
                self.page.wait_for_timeout(500)
            if not clicked:
                logger.warning("封面菜单未弹出或「从正文选择」不可见")
                self._screenshot("cover_menu_not_shown")
                return False
            logger.info("已点击「从正文选择」")
        except Exception as e:
            logger.warning(f"点击封面菜单失败: {e}")
            return False

        # 3. 「选择图片」对话框确认（多图时可能需点选/裁剪）
        return self._confirm_cover_dialog()

    def _visible_dialog(self):
        """返回当前可见的 .weui-desktop-dialog（页面可能存在多个隐藏模板实例）

        实测：编辑器 DOM 中同时存在模板对话框与实例对话框，直接取 .first
        会命中隐藏模板（is_visible=False），因此必须遍历取可见实例。
        """
        dialogs = self.page.locator(".weui-desktop-dialog")
        n = dialogs.count()
        for i in range(n):
            d = dialogs.nth(i)
            try:
                if d.is_visible(timeout=1200):
                    return d
            except Exception:
                continue
        return None

    def _confirm_cover_dialog(self) -> bool:
        """在「选择图片」→「编辑封面」两级对话框中完成封面确认（实测路径）

        实测交互链（新版编辑器）：
        - 「选择图片」页：正文图片列表 li.appmsg_content_img_item，必须先点击
          图片项选中（「下一步」按钮由 disabled 变 enabled），再点「下一步」；
        - 「编辑封面」页（裁剪）：点「确认」（weui-desktop-btn_primary）即完成。
        """
        try:
            dlg = self._visible_dialog()
            if dlg is None:
                logger.warning("未出现「选择图片」对话框（从正文选择未生效）")
                return False

            # 1. 选择图片页：点击图片项选中（已默认选中时点击无副作用）
            try:
                dlg.locator('.appmsg_content_img_item').first.click(timeout=5000)
                self.page.wait_for_timeout(1200)
            except Exception:
                pass

            # 2. 点「下一步」进入「编辑封面」裁剪页
            try:
                dlg.locator('button:has-text("下一步")').first.click(timeout=5000)
                logger.info("封面对话框已进入「编辑封面」裁剪页")
                self.page.wait_for_timeout(2000)
            except Exception as e:
                logger.warning(f"点击「下一步」失败: {e}")
                return False

            # 3. 编辑封面页：点「确认」完成设置
            try:
                dlg.locator('button:has-text("确认")').first.click(timeout=5000)
                logger.info("封面对话框已确认（选择图片 → 编辑封面 → 确认）")
                # 确认后封面预览更新可能较慢（v2 编辑器加载慢时尤其明显），放宽到 15 秒
                return self._wait_cover_preview(timeout=15000)
            except Exception as e:
                logger.warning(f"点击「确认」失败: {e}")
                self._screenshot("cover_dialog_confirm_missing")
                return False
        except Exception as e:
            logger.warning(f"封面对话框操作异常: {e}")
            return False

    # ── 封面 CDN 上传 ─────────────────────────────────────────────────

    def upload_cover_to_cdn(self, cover_path: str) -> str:
        """先上传封面到微信 CDN 拿外链 URL（与文内图片同一条 uploadimg2cdn 路径）

        Returns:
            CDN URL；路径无效或上传失败返回空串（由调用方回退旧流程）
        """
        from browser.image_uploader import upload_image_to_cdn

        if not cover_path or not os.path.exists(cover_path):
            return ""
        # 不显式传 token：upload_image_to_cdn 内部走 read_token_from_page（页面优先）
        return upload_image_to_cdn(self.page, cover_path)

    def prepend_cover_image(self, html_content: str, cover_url: str) -> str:
        """把封面图（CDN URL）作为正文首图注入，供「从正文选择」设为封面"""
        cover_img = (
            f'<section style="text-align:center;">'
            f'<img src="{cover_url}" style="width:100%;"/></section>'
        )
        logger.info(f"封面图已作为正文首图注入: {cover_url[:60]}...")
        return cover_img + html_content

    # ── 封面「从正文选择」 ────────────────────────────────────────────

    def set_cover_from_body(self) -> bool:
        """通过编辑器「从正文选择」把正文首图（封面 CDN URL）设为封面

        多轮选择器回退以兼容编辑器 DOM 改版：
        1) 点击封面区域（多选择器）展开封面设置面板；
        2) 点击「从正文选择」入口（多选择器，兼容文案/结构变化）；
        3) 部分版本点入口后还需在子面板点选正文首图；
        4) 等待并验证封面预览出现。

        任一步失败都会截图 + 输出封面区域 DOM 片段，便于诊断编辑器改版。

        Returns:
            True 如果成功设置封面；否则 False（调用方回退旧的文件上传）
        """
        # 1. 点击封面区域，展开封面设置面板（多选择器回退）
        self._try_click_selectors(
            sel.COVER_AREA,
            "封面区域",
            timeout_per=2000,
            dom_area_selectors=sel.COVER_AREA,
        )
        self.page.wait_for_timeout(300)

        # 2. 点击「从正文选择」入口（多轮选择器回退，兼容文案/结构改版）
        result = self._try_select(
            sel.COVER_SELECT_FROM_BODY,
            "从正文选择",
            timeout_per=1500,
            screenshot_name="cover_from_body_no_entry",
            dom_area_selectors=sel.COVER_AREA,
        )
        if result is None:
            return False
        el, _ = result
        el.click()

        self.page.wait_for_timeout(500)

        # 3. 部分新版编辑器点「从正文选择」后需在子面板点选正文首图
        self._try_click_selectors(
            sel.COVER_BODY_FIRST_IMAGE,
            "正文首图",
            timeout_per=1500,
        )

        # 4. 等待并验证封面预览出现，失败截图 + DOM 诊断
        if self._wait_cover_preview():
            logger.info("封面已通过「从正文选择」设置（CDN URL）")
            return True

        self._screenshot("cover_from_body_failed")
        logger.warning("「从正文选择」后未检测到封面预览，视为失败（回退文件上传）")
        self._diagnose_dom("cover_from_body", sel.COVER_AREA)
        return False

    # 封面设置成功信号（严格版，杜绝假阳性）：
    # 1) 隐藏字段 file_id 非空 = 封面已登记到后台（最可靠）；
    # 2) 预览容器可见且 background-image 为非空 URL。
    # 历史教训：空占位 `url("")` 曾被误判为已设置，导致点「发表」被后台拦截。
    _COVER_SET_JS = """() => {
        const fid = document.querySelector('.js_file_id, input[name="file_id"]');
        if (fid && String(fid.value || '').trim()) return true;
        const prev = document.querySelector(
            '.js_cover_preview_new, .select-cover__preview, .cover_preview, .js_cover_preview');
        if (prev && prev.style.display !== 'none') {
            // 注意：预览容器可能为 fixed 定位，offsetParent 恒为 null，
            // 必须用 rect + computedStyle 判断可见性（否则封面已设置也会误判为失败）
            const r = prev.getBoundingClientRect();
            if (r.width === 0 || r.height === 0) return false;
            const s = getComputedStyle(prev);
            if (s.display === 'none' || s.visibility === 'hidden') return false;
            const bg = s.backgroundImage || '';
            const m = bg.match(/url\\(["']?([^"')]+)["']?\\)/);
            if (m && m[1]) return true;
        }
        return false;
    }"""

    def _wait_cover_preview(self, timeout: int = 8000) -> bool:
        """等待封面设置成功（严格验证，无假阳性）

        - 新版：轮询 file_id 隐藏字段非空 / 预览容器获得非空 background-image；
          注意空占位 `url("")` 不算已设置；
        - 旧版：兜底等待 .cover_preview / .js_cover_preview 元素出现。
        """
        try:
            self.page.wait_for_function(self._COVER_SET_JS, timeout=timeout)
            return True
        except Exception:
            pass
        # 旧版编辑器：等待封面预览元素出现（存在即成功信号）
        try:
            self.page.wait_for_selector(sel.COVER_PREVIEW, timeout=2000)
            return True
        except Exception:
            return False

    # ── 摘要 ──────────────────────────────────────────────────────────

    def fill_summary(self, summary: str):
        """填写文章摘要"""
        if not summary:
            return

        logger.info(f"填写摘要: {summary}")

        result = self._try_select(
            sel.SUMMARY_INPUTS,
            "摘要输入",
            timeout_per=3000,
            screenshot_name="fill_summary_failed",
        )
        if result is None:
            self._diagnose_dom("摘要输入", sel.SUMMARY_INPUTS)
            logger.warning("未找到摘要输入框（可能不影响发布）")
            return

        el, matched = result
        el.fill(summary)
        self.page.wait_for_timeout(200)

    # ── 保存草稿 ──────────────────────────────────────────────────────

    def dismiss_dialogs(self):
        """关闭可能拦截点击的弹窗（教育弹窗/新手引导等）

        安全约束：绝不点击「确定/确认」类按钮——发布确认对话框的确认
        只能由 publish() 在监控窗口内显式点击，否则可能在监控外真实发布。
        """
        try:
            closed = self.page.evaluate("""() => {
                let count = 0;
                // 关闭可见弹窗的「我知道了/关闭」按钮（排除确定/确认）
                const dismissBtns = Array.from(document.querySelectorAll(
                    '.education-dialog button, .weui-desktop-dialog button, ' +
                    '.weui-desktop-dialog__close-btn, [class*=dialog] .weui-desktop-btn'
                )).filter(el => {
                    // 注意：对话框为 position:fixed，offsetParent 恒为 null，
                    // 必须用 rect + computedStyle 判断可见性（否则漏掉弹窗内按钮）
                    const r = el.getBoundingClientRect();
                    if (r.width === 0 || r.height === 0) return false;
                    const s = getComputedStyle(el);
                    return s.display !== 'none' && s.visibility !== 'hidden';
                });
                for (const btn of dismissBtns) {
                    const text = (btn.textContent || '').trim();
                    const cls = btn.className.toString();
                    if (/确定|确认/.test(text)) continue;  // 绝不点确认（防误发布）
                    if (text.includes('我知道了') || text.includes('关闭') ||
                        cls.includes('close') || text.includes('跳过') || text.includes('知道了')) {
                        btn.click();
                        count++;
                    }
                }
                return count;
            }""")
            if closed:
                logger.info(f"已关闭 {closed} 个弹窗")
                self.page.wait_for_timeout(500)
            return closed
        except Exception as e:
            logger.debug(f"关闭弹窗异常: {e}")
            return 0

    def _cancel_visible_dialog(self) -> bool:
        """取消当前可见的对话框（点「取消」；无取消按钮则点关闭图标）

        用于发布重试前清理迟到的确认对话框，绝不用「确定」关闭。
        """
        try:
            cancelled = self.page.evaluate("""() => {
                const btns = Array.from(document.querySelectorAll('button, a'))
                    .filter(b => b.offsetParent !== null &&
                        /^(取消|关闭)$/.test((b.innerText || '').trim()));
                if (btns.length) { btns[0].click(); return 'cancel'; }
                const closes = Array.from(document.querySelectorAll(
                    '.weui-desktop-dialog__close-btn, [class*="dialog"] [class*="close"]'
                )).filter(el => el.offsetParent !== null);
                if (closes.length) { closes[0].click(); return 'close-icon'; }
                return '';
            }""")
            if cancelled:
                logger.info(f"已取消可见对话框（{cancelled}）")
                self.page.wait_for_timeout(400)
                return True
            return False
        except Exception as e:
            logger.debug(f"取消对话框异常: {e}")
            return False

    def save_draft(self):
        """保存为草稿"""
        logger.info("保存为草稿...")

        # 先关闭可能拦截的弹窗
        self.dismiss_dialogs()

        result = self._try_select(
            sel.DRAFT_BUTTONS,
            "保存草稿",
            timeout_per=3000,
            screenshot_name="save_draft_failed",
            dom_area_selectors=["#js_send", ".weui-desktop-btn"],
        )
        if result is None:
            return False

        el, matched = result
        # 用 JS 直接触发点击（绕过 Playwright 点击的遮挡/视口检查，
        # 以及 span 无 onclick 事件绑定导致点击无效的问题）
        try:
            js_clicked = self.page.evaluate("""() => {
                const spans = Array.from(document.querySelectorAll('span, button, a'))
                    .filter(s => (s.textContent || '').trim() === '保存为草稿');
                if (!spans.length) return false;
                const s = spans[0];
                let el = s;
                for (let i = 0; i < 4 && el; i++) {
                    if (el.tagName === 'BUTTON' || el.tagName === 'A') {
                        el.click(); return true;
                    }
                    el = el.parentElement;
                }
                s.click();
                return true;
            }""")
            if js_clicked:
                logger.info("保存为草稿（JS 点击）")
                # 等待保存完成：可靠信号是 URL 从 isNew=1 变为 appmsgid=...
                # （编辑器内"正文字数"等文本一直存在，不能作为保存成功依据）
                saved = False
                try:
                    self.page.wait_for_function(
                        """() => {
                            const u = window.location.href || '';
                            return u.includes('appmsgid=') && !u.includes('isNew=1');
                        }""",
                        timeout=10000,
                    )
                    saved = True
                    logger.info("草稿保存确认：URL 已携带 appmsgid（保存成功）")
                except Exception:
                    logger.warning("保存 URL 确认超时，尝试二次点击")
                if not saved:
                    # 二次尝试：可能第一次点击未生效
                    try:
                        self.page.evaluate("""() => {
                            const spans = Array.from(document.querySelectorAll('span, button, a'))
                                .filter(s => (s.textContent || '').trim() === '保存为草稿');
                            if (!spans.length) return false;
                            const s = spans[0];
                            let el = s;
                            for (let i = 0; i < 4 && el; i++) {
                                if (el.tagName === 'BUTTON' || el.tagName === 'A') { el.click(); return true; }
                                el = el.parentElement;
                            }
                            s.click();
                            return true;
                        }""")
                        self.page.wait_for_function(
                            """() => {
                                const u = window.location.href || '';
                                return u.includes('appmsgid=') && !u.includes('isNew=1');
                            }""",
                            timeout=8000,
                        )
                        logger.info("二次点击后保存成功")
                        saved = True
                    except Exception:
                        logger.error("草稿保存失败：URL 未变为 appmsgid")
                self._screenshot("draft_saved")
                return saved
        except Exception as e:
            logger.warning(f"JS 点击保存失败: {e}")

        # 回退：Playwright 原生点击
        try:
            el.click(timeout=5000)
        except Exception:
            logger.warning("保存按钮被遮挡，尝试 force 点击")
            self.dismiss_dialogs()
            self.page.wait_for_timeout(300)
            el.click(timeout=5000, force=True)
        try:
            self.page.wait_for_selector(sel.TOAST, timeout=8000)
        except Exception:
            self.page.wait_for_timeout(1500)
        self._screenshot("draft_saved")
        return True

    # ── 发布 ──────────────────────────────────────────────────────────

    def publish(self, title: str = "") -> bool:
        """发布文章（群发）

        判定纪律（防假阳性）：
        - 必须点到确认对话框并确认后，才进入成功信号等待；
        - 必须检测到真实成功信号（页面跳转/成功提示）才返回 True；
        - 点击后无确认对话框、或确认后无成功信号，一律判失败并留截图证据。

        新版流程实测（2026-08-31）：点击编辑器「发表」后，v2 编辑器会先自动保存
        并跳转旧版编辑器（URL 出现 appmsgid），随后自动弹出确认对话框①（标题「发表」，
        含「群发通知/定时发表」选项，按钮「发表」「取消」）——确认按钮文本是
        「发表」而非「确定/确认」；点击「发表」后还会弹出确认框②
        （「已开启群发通知…继续发表 取消」），必须再点「继续发表」发布才真正提交。
        跳转+加载需要数秒，等待窗口放宽到 15 秒（两级确认都在其中完成）。
        """
        logger.info("发布文章...")

        # 先关闭可能拦截的弹窗
        self.dismiss_dialogs()

        # 记录点击前 URL，用于判定发布后是否发生页面跳转（强成功信号）
        pre_url = self._current_url()

        # 点击发表并等待确认对话框（最多 2 轮；v2→旧版跳转后需重新定位按钮）
        dialog_confirmed = False
        for attempt in (1, 2):
            result = self._try_select(
                sel.PUBLISH_BUTTONS,
                "发布",
                timeout_per=3000,
                screenshot_name="publish_failed",
                dom_area_selectors=["#js_send", ".weui-desktop-btn"],
            )
            if result is None:
                # 找不到发表按钮（可能已跳转/对话框遮挡），直接等待确认对话框
                if self._wait_publish_confirm_dialog(timeout_ms=15000):
                    dialog_confirmed = True
                break
            el, matched = result
            try:
                el.click(timeout=5000)
            except Exception:
                logger.warning("发布按钮被遮挡，尝试 force 点击")
                self.dismiss_dialogs()
                self.page.wait_for_timeout(300)
                try:
                    el.click(timeout=5000, force=True)
                except Exception as e:
                    logger.warning(f"第 {attempt} 次点击发布按钮失败: {e}")
                    continue
            if self._wait_publish_confirm_dialog(timeout_ms=15000):
                dialog_confirmed = True
                break
            logger.warning(
                f"第 {attempt} 次点击发表后未出现确认对话框"
                "（可能页面在 v2→旧版编辑器跳转中，或被表单校验拦截，如缺少封面图/摘要）"
            )
            self._screenshot(f"publish_no_dialog_{attempt}")
            # 竞态防护：若确认对话框在等待超时后才出现，先「取消」它——
            # 绝不能用 dismiss_dialogs（它会点「确定」导致在监控窗口外真实发布）
            self._cancel_visible_dialog()
            self.page.wait_for_timeout(800)

        if not dialog_confirmed:
            # 历史假阳性根因：此处曾把「无确认对话框」当作「已直接发布」返回 True。
            # 后台点击「发表」必经确认对话框；对话框未出现 = 发布动作未成立。
            logger.error(
                "发布失败：两次点击发表按钮后确认对话框均未出现，"
                "文章未发布（最可能被表单校验拦截，如缺少封面图）。"
                "证据截图: publish_no_dialog_*"
            )
            self._diagnose_dom("publish_no_dialog", ["#js_send", ".weui-desktop-dialog"])
            return False

        # 确认后必须等到真实成功信号（成功提示/页面跳转）；无信号即失败
        ok, evidence = self._wait_publish_outcome(pre_url)
        if ok:
            logger.info(f"文章已发布（成功信号: {evidence}）")
            self._screenshot("published")
            return True

        logger.error(f"发布失败：确认发表后未检测到真实成功信号: {evidence}")
        self._screenshot("publish_no_success_signal")
        return False

    def _wait_publish_confirm_dialog(self, timeout_ms: int = 15000) -> bool:
        """等待发表确认对话框并逐级确认（实测为两级确认流程）

        流程实测（2026-08-31，probe-post-confirm 60 秒采样证实）：
        1. 确认框①（标题「发表」，含「群发通知/定时发表」选项，按钮「发表」「取消」）
           ——点击「发表」后并不会直接提交发布；
        2. 确认框②（「已开启群发通知…查看详情 继续发表 取消」）
           ——必须再点「继续发表」，发布动作才真正提交。

        按钮匹配限定在可见 .weui-desktop-dialog 内、文本精确匹配
        （「继续发表」先于「发表」判定），避免误点页面底部工具栏的「发表」按钮。

        Returns:
            True 表示确认完成（两级都已点击，或第二级未出现/已关闭——
            最终是否成功由 _wait_publish_outcome 的真实成功信号判定）；
            False 表示确认框从未出现（发布动作未成立）。
        """
        deadline = time.time() + timeout_ms / 1000.0
        stage = 0  # 0=尚未点第一级；1=已点第一级，等待第二级
        while time.time() < deadline:
            try:
                hit = self.page.evaluate("""() => {
                    const vis = (el) => {
                        if (!el) return false;
                        const r = el.getBoundingClientRect();
                        if (r.width === 0 || r.height === 0) return false;
                        const s = getComputedStyle(el);
                        return s.display !== 'none' && s.visibility !== 'hidden';
                    };
                    const dlg = Array.from(
                        document.querySelectorAll('.weui-desktop-dialog')).find(vis);
                    if (!dlg) return 'no-dialog';
                    // 第二级：继续发表（文本精确匹配，先于第一级判定）
                    const cont = Array.from(dlg.querySelectorAll('button, a'))
                        .find(b => vis(b) && (b.innerText || '').trim() === '继续发表');
                    if (cont) { cont.click(); return 'continue-confirmed'; }
                    // 第一级：发表
                    const target = Array.from(dlg.querySelectorAll('button, a'))
                        .find(b => vis(b) && (b.innerText || '').trim() === '发表');
                    if (!target) return 'dialog-no-publish-btn';
                    target.click();
                    return 'confirmed';
                }""")
                if hit == "continue-confirmed":
                    logger.info("已点击第二级确认框的「继续发表」按钮，发布动作已提交")
                    return True
                if hit == "confirmed":
                    logger.info("已点击确认框①的「发表」按钮，等待第二级确认（如有）")
                    stage = 1
                    continue
                if hit == "dialog-no-publish-btn":
                    # 非发表确认对话框（可能是其他拦截框），取消避免干扰主流程
                    self._cancel_visible_dialog()
            except Exception:
                pass
            self.page.wait_for_timeout(500)
        return stage == 1

    # ── 发布结果验证 ──────────────────────────────────────────────────

    def _current_url(self) -> str:
        """当前页面 URL（异常时返回空串，不中断发布流程）"""
        try:
            return self.page.url or ""
        except Exception:
            return ""

    def _mp_token(self) -> str:
        """从当前页面 URL 提取后台 token（内部页面必须带 token 才能访问）"""
        import re as _re

        m = _re.search(r"token=(\d+)", self._current_url())
        if m:
            return m.group(1)
        try:
            return str(self.page.evaluate(
                "() => (location.href.match(/token=(\\d+)/) || [])[1] || ''"
            ))
        except Exception:
            return ""

    def _wait_publish_outcome(self, pre_url: str, timeout_ms: int = 0) -> tuple[bool, str]:
        """点击确认发布后，等待真实的发布结果信号

        成功信号（任一即可）：
        1. 页面跳转：URL 变化且不再是编辑器页（appmsg_edit）——发布成功后后台会离开编辑器；
        2. 页面出现成功提示文本（selectors 的 publish_success_texts）。
        失败信号：页面出现表单校验/发送失败文本（publish_fail_texts）→ 提前判失败。

        特殊状态（实测 2026-08-31）：群发提交后可能触发微信风控——弹出
        「微信验证」弹窗，要求管理员扫码验证。此时不判失败：等待用户扫码，
        验证通过后发布自动继续；等待窗口在首次检测到时延长 300 秒（默认超时 20 秒
        是给扫码留时间），仍无信号才判失败。

        Returns:
            (是否成功, 证据描述)
        """
        if timeout_ms <= 0:
            timeout_ms = int(
                self.config.get("wechat", {}).get("publish_outcome_timeout_ms", 20000)
            )
        success_texts = list(sel.get_selectors().get("publish_success_texts") or [])
        fail_texts = list(sel.get_selectors().get("publish_fail_texts") or [])
        js = f"""() => {{
            const text = (document.body && document.body.innerText) || '';
            for (const t of {json.dumps(fail_texts, ensure_ascii=False)})
                if (text.includes(t)) return 'FAIL:' + t;
            for (const t of {json.dumps(success_texts, ensure_ascii=False)})
                if (text.includes(t)) return 'OK:' + t;
            return '';
        }}"""
        verify_js = """() => {
            const t = (document.body && document.body.innerText) || '';
            // 微信验证弹窗特征文本（需管理员扫码）；与正文含「验证」字样区分：
            // 用完整片段「扫码后，请联系管理员进行验证」匹配
            return t.includes('扫码后，请联系管理员进行验证');
        }"""
        deadline = time.time() + timeout_ms / 1000.0
        verify_seen = False
        while time.time() < deadline:
            # 信号 1：页面已离开编辑器（最强成功信号）
            url = self._current_url()
            if url and pre_url and url != pre_url and "appmsg_edit" not in url:
                return True, f"页面已跳转: {url[:100]}"
            # 信号 2/3：成功或校验失败文本
            try:
                hit = self.page.evaluate(js)
                if isinstance(hit, str):
                    if hit.startswith("FAIL:"):
                        return False, f"页面出现校验失败提示「{hit[5:]}」（发布被后台拦截）"
                    if hit.startswith("OK:"):
                        return True, f"页面出现成功提示「{hit[3:]}」"
            except Exception:
                pass
            # 特殊状态：微信验证弹窗（群发风控，需管理员扫码）
            if not verify_seen:
                try:
                    if self.page.evaluate(verify_js):
                        verify_seen = True
                        logger.warning(
                            "检测到「微信验证」弹窗：群发被风控拦截，需管理员扫码验证。"
                            "请在浏览器窗口中完成扫码，验证通过后发布将自动继续"
                        )
                        self._screenshot("wechat_verify_waiting")
                        # 延长等待窗口：给足人工扫码时间（首次检测时生效一次）
                        verify_deadline = time.time() + 300
                        if verify_deadline > deadline:
                            deadline = verify_deadline
                except Exception:
                    pass
            self.page.wait_for_timeout(500)
        return False, f"{timeout_ms // 1000} 秒内无成功信号且页面未跳转（可能仍停留在编辑器）"

    def verify_published_online(self, title: str, timeout_each: int = 30000) -> bool:
        """发布后复核：到公众号后台「发表记录」核实文章是否真实存在

        判定发布真实成功的最终关口——拦截「点击流程看似完成、
        但文章实际未发表」的假阳性。命中规则：发表记录页文本中
        包含标题前缀（列表中可能被截断显示）。
        """
        if not title:
            logger.warning("发布复核中止：标题为空")
            return False
        needle = self._title_needle(title)
        session_lost = False

        # 路线 1：访问发表记录页（多候选 URL）。
        # 实测：后台内部页面必须带 token 参数，否则被重定向到首页/登录页；
        # 先进后台首页（自动带上 token），再拼接 token 访问目标页。
        try:
            self.page.goto(WECHAT_MP_URL, wait_until="domcontentloaded", timeout=timeout_each)
            self.page.wait_for_timeout(2500)
        except Exception as e:
            logger.warning(f"后台首页导航失败: {e}")
        if self._on_login_page():
            session_lost = True
            logger.warning("后台首页为登录页（会话失效），发表记录复核不可用")
        token = self._mp_token()
        for url in PUBLISHED_LIST_URLS:
            if session_lost:
                break
            target = url + (f"&token={token}&lang=zh_CN" if token else "")
            try:
                self.page.goto(target, wait_until="domcontentloaded", timeout=timeout_each)
                self.page.wait_for_timeout(3500)
            except Exception as e:
                logger.warning(f"发表记录页导航失败: {target}: {e}")
                continue
            if self._on_login_page():
                # 被重定向到登录页/首页：会话失效或路由被拦截，不能误报为「文章不存在」
                session_lost = True
                logger.warning(f"发表记录页跳转后被重定向（会话失效或路由拦截）: {url}")
                continue
            if self._page_contains_text(needle):
                logger.info(f"发布复核通过：在发表记录中找到文章（{url}）")
                self._screenshot("verified_in_published_list")
                # 提取文章链接供落库（best-effort，取不到保持空串）
                self.last_publish_url = self._extract_published_url(needle)
                if self.last_publish_url:
                    logger.info(f"已捕获发表文章链接: {self.last_publish_url}")
                return True
            logger.warning(f"发表记录页未找到文章: {url}")

        # 路线 2：回后台首页，经侧边栏「发表记录」菜单进入（兼容 URL 改版）
        try:
            self.page.goto(WECHAT_MP_URL, wait_until="domcontentloaded", timeout=timeout_each)
            self.page.wait_for_timeout(1500)
            if self._on_login_page():
                session_lost = True
                logger.warning("后台首页被重定向到登录页（会话失效），侧边栏路线不可用")
            else:
                for entry in sel.get_selectors().get("published_list_entries") or []:
                    try:
                        el = self.page.locator(entry).first
                        if not el.is_visible(timeout=2000):
                            continue
                        el.click()
                        self.page.wait_for_timeout(2500)
                        # 部分版本需在「草稿箱/发表记录」页内切换到「已发表」标签
                        for tab in sel.get_selectors().get("published_tab") or []:
                            try:
                                t = self.page.locator(tab).first
                                if t.is_visible(timeout=1500):
                                    t.click()
                                    self.page.wait_for_timeout(1500)
                                    break
                            except Exception:
                                continue
                        if self._page_contains_text(needle):
                            logger.info("发布复核通过：经侧边栏进入发表记录后找到文章")
                            self._screenshot("verified_in_published_list")
                            self.last_publish_url = self._extract_published_url(needle)
                            if self.last_publish_url:
                                logger.info(f"已捕获发表文章链接: {self.last_publish_url}")
                            return True
                    except Exception:
                        continue
        except Exception as e:
            logger.warning(f"侧边栏路线复核异常: {e}")

        if session_lost:
            logger.error(
                f"发布复核失败：会话已失效（被重定向到登录页），无法确认文章「{title[:30]}」是否已发表"
                "——请重新登录（python main.py login）后人工核实"
            )
        else:
            logger.error(f"发布复核失败：所有发表记录入口均未找到文章「{title[:30]}」")
        self._screenshot("verify_published_not_found")
        return False

    def _extract_published_url(self, needle: str) -> str:
        """在发表记录页按标题匹配提取文章链接

        best-effort：列表标题可能被截断，故用双向包含匹配；
        取不到时返回空串（不影响复核判定，仅影响 publish_url 落库）。
        """
        try:
            url = self.page.evaluate(_PUBLISHED_LINK_JS, {"needle": needle}) or ""
            return str(url)
        except Exception as e:
            logger.debug(f"提取发表文章链接失败（忽略）: {e}")
            return ""

    def _on_login_page(self) -> bool:
        """当前页面是否为登录页/未登录状态（复核时区分「会话失效」与「文章不存在」）"""
        url = self._current_url()
        if "login" in url or "wx_open" in url:
            return True
        # 新版后台：部分页面不重定向，直接在页面上展示登录入口（无侧边栏/管理面板）
        try:
            has_login_entry = bool(self.page.evaluate("""() => {
                const links = Array.from(document.querySelectorAll('a[href*="loginpage"], a[href*="login"]'))
                    .filter(a => a.offsetParent !== null);
                return links.length > 0;
            }"""))
            has_admin_panel = bool(self.page.evaluate("""() => {
                return !!document.querySelector(
                    '.weui-desktop-menu, .menu_container, [class*="new-creation"], .appmsg_editor');
            }"""))
            return has_login_entry and not has_admin_panel
        except Exception:
            return False

    @staticmethod
    def _title_needle(title: str) -> str:
        """标题匹配片段：去空白取前 20 字（兼容列表截断/空白差异）"""
        return "".join(title.split())[:20]

    def _page_contains_text(self, text: str) -> bool:
        """当前页面可见文本是否包含指定片段（去空白比对）"""
        try:
            found = self.page.evaluate(
                f"""() => {{
                    const body = document.body ? (document.body.innerText || '') : '';
                    return body.replace(/\\s+/g, '').includes({json.dumps(text, ensure_ascii=False)});
                }}"""
            )
            return bool(found)
        except Exception:
            return False

    # ── 预览截图 ──────────────────────────────────────────────────────

    def preview_before_publish(self) -> str:
        """发布前截图预览 - 保存当前编辑状态的截图供人工确认"""
        logger.info("发布前截图预览...")
        path = self._screenshot("pre_publish_preview")
        logger.info(f"发布前预览截图已保存: {path}")
        return path

    # ── 完整发布流程 ──────────────────────────────────────────────────

    def execute_publish(
        self,
        title: str,
        html_content: str,
        cover_path: str,
        summary: str,
        inline_images: list[str] | None = None,
    ) -> dict:
        """
        执行完整的发布流程

        Args:
            inline_images: 文内插图路径列表（与 HTML 中 {{IMAGE_PATH_N}} 占位符对应）；
                发布前会把占位符替换为微信 CDN 外链（失败回退 base64）

        配置项（wechat.*）：
            - external_images: true（默认）= 封面/文内图片走 CDN 外链；false = 封面走旧文件上传、文内 base64
            - cover_in_body: true（默认）= 封面 CDN URL 作为正文首图注入并通过「从正文选择」设为封面；
              false = 封面仍走 CDN 上传拿 URL，但不注入正文、不用「从正文选择」，设置封面走旧文件上传

        Returns:
            {"success": bool, "mode": str, "error": str, "preview_path": str, "url": str}

            url 为发表后的文章链接（复核页按标题匹配提取，取不到为空串）；
            最终注入编辑器的 HTML 另存于 self.last_published_html（占位符已解析）。
        """
        # 每次发布重置产物（最终 HTML / 发表后 URL），避免跨次串味
        self.last_published_html = ""
        self.last_publish_url = ""
        try:
            # 每次发布独立统计（避免跨次累计）
            self._selector_stats = {}

            # 0. 清理过期截图（保留 retention_days 天，防止时间戳化后无限堆积）
            try:
                removed = cleanup_old_screenshots(self.screenshot_dir, self.retention_days)
                if removed:
                    logger.info(f"截图清理: 删除 {removed} 个过期日期目录/文件（保留 {self.retention_days} 天）")
            except Exception as e:
                logger.debug(f"截图清理失败（忽略）: {e}")

            # 1. 导航到编辑器
            self.navigate_to_editor()

            # 2. 统一图片路径：封面先传 CDN 拿 URL（与文内图片同链路）
            use_cdn = self.config.get("wechat", {}).get("external_images", True)
            cover_in_body = self.config.get("wechat", {}).get("cover_in_body", True)
            cover_url = self.upload_cover_to_cdn(cover_path) if (use_cdn and cover_path) else ""

            # 3. 填写标题
            self.fill_title(title)

            # 4. 解析文内图片占位符（CDN 外链优先，base64 兜底）
            #    注意：inline_images 为空时（图片生成失败），必须移除 {{IMAGE_PATH_N}}
            #    占位符，否则 ProseMirror 粘贴含占位符内容后保存会被阻塞（URL 不变）
            if inline_images:
                html_content = self.replace_inline_images(html_content, inline_images)
            else:
                import re as _re
                # 移除 {{IMAGE_PATH_N}} 占位符，并清理因此产生的空 img 标签
                # （空 src 的 img 会被 ProseMirror 当作待上传图片，触发图片对话框阻塞保存）
                cleaned = _re.sub(r"\{\{IMAGE_PATH_\d+\}\}", "", html_content)
                cleaned = _re.sub(r"<img[^>]*src=\"\"[^>]*>", "", cleaned)
                if cleaned != html_content:
                    logger.warning("未生成插图，已移除文内图片占位符及空图片标签")
                    html_content = cleaned

            # 5. 封面 CDN URL 作为正文首图注入（仅当 cover_in_body=true），统一注入正文
            if cover_url and cover_in_body:
                html_content = self.prepend_cover_image(html_content, cover_url)
            self.fill_content_html(html_content)
            # 记录最终注入编辑器的 HTML（占位符已解析为 CDN 外链、封面已注入），
            # 供 publisher/workflow 落库，实现「所见即所存」。
            self.last_published_html = html_content

            # 6. 设置封面：
            #    - cover_in_body=true（默认）：优先「从正文选择」（封面已在正文首图），失败回退新路径：
            #      正文工具栏上传图片 + 封面「从正文选择」（新版编辑器实测链路），再失败才走旧文件上传；
            #    - cover_in_body=false：封面仍走 CDN 上传拿 URL（统一图片路径），但不注入正文、
            #      不用「从正文选择」，设置封面直接走旧的文件上传。
            cover_set = False
            if cover_path:
                if not (cover_in_body and cover_url and self.set_cover_from_body()):
                    # 新路径（新版编辑器实测）：正文工具栏上传图片 → 封面「从正文选择」
                    if not self.upload_body_image(cover_path):
                        logger.warning("正文图片上传失败，回退旧文件上传封面")
                        self.upload_cover_image(cover_path)
                    elif not self.set_cover_from_body_v2():
                        # 偶发时序问题（页面加载慢/对话框切换延迟）导致首次失败时重试一次
                        logger.warning("「从正文选择」设封面失败，重试一次")
                        if not self.set_cover_from_body_v2():
                            logger.warning("「从正文选择」设封面重试仍失败，回退旧文件上传封面")
                            self.upload_cover_image(cover_path)
                # 封面是「发表」的硬性要求，这里验证设置结果（而非假设成功）
                try:
                    cover_set = self._wait_cover_preview(timeout=5000)
                except Exception:
                    cover_set = False
                if not cover_set:
                    logger.warning("封面设置结果未验证到封面预览，发表可能被后台拦截")

            # 7. 填写摘要
            self.fill_summary(summary)

            # 8. 发布前截图预览（安全机制）
            preview_path = self.preview_before_publish()

            # 9. 保存草稿或发布
            if self.publish_mode == "draft":
                success = self.save_draft()
                result = {
                    "success": success,
                    "mode": "draft",
                    "error": "" if success else "保存草稿失败",
                    "preview_path": preview_path,
                    "url": "",
                }
            elif not cover_set:
                # 公众号要求「发表」必须设置封面，缺失时点击发表会被后台拦截。
                # 降级保存草稿：文章不丢失，人工补封面后可再发表；明确报告降级事实。
                logger.error("封面未设置：公众号要求发表必须设置封面，降级为保存草稿（防文章丢失）")
                saved = self.save_draft()
                result = {
                    "success": saved,
                    "mode": "draft",
                    "error": "缺少封面图，无法直接发表，已降级保存为草稿"
                    + ("" if saved else "；且草稿保存也失败，请人工检查编辑器状态"),
                    "preview_path": preview_path,
                    "url": "",
                }
            else:
                success = self.publish(title=title)
                error = "" if success else "发布动作未检测到成功信号（证据截图: publish_no_*）"

                # 发布失败时先存草稿，防止后续复核导航离开编辑器导致文章内容丢失
                if not success:
                    try:
                        if self.save_draft():
                            logger.info("发布失败，文章已降级保存为草稿（防内容丢失）")
                    except Exception as e:
                        logger.warning(f"发布失败后保存草稿异常（忽略）: {e}")

                # 发布后复核（防假阳性/假阴性的最终关口）：到「发表记录」核实文章真实存在。
                # - publish 判成功但复核找不到 → 推翻为失败（拦截假阳性）；
                # - publish 判失败但复核找到 → 改判成功（容忍成功信号选择器过期的假阴性）。
                if self.config.get("wechat", {}).get("verify_published", True):
                    verified = self.verify_published_online(title)
                    if verified:
                        if not success:
                            logger.warning("发布动作信号异常，但发表记录复核找到文章，判定为已真实发布")
                        success = True
                        error = ""
                    elif success:
                        success = False
                        error = "发布结果复核失败：发表记录中未找到该文章（发布未真实生效）"
                        logger.error(error)

                result = {
                    "success": success,
                    "mode": "publish",
                    "error": error,
                    "preview_path": preview_path,
                    # 发表后的文章链接（复核页按标题匹配提取；取不到为空串，不影响判定）
                    "url": self.last_publish_url if success else "",
                }

            # 10. 输出本次发布的选择器命中率统计
            self._emit_selector_stats()
            return result

        except Exception as e:
            logger.error(f"发布流程出错: {e}")
            self._screenshot("publish_error")
            # 异常中断也输出已发生的命中统计（便于排查卡在哪一步）
            self._emit_selector_stats()
            return {
                "success": False,
                "mode": self.publish_mode,
                "error": str(e),
                "preview_path": "",
                "url": "",
            }

    # ── 辅助方法 ──────────────────────────────────────────────────────

    def _verify_content_injected(self, expected_snippet: str = ""):
        """验证内容是否已成功注入到编辑器中"""
        try:
            self.page.wait_for_timeout(500)
            has_text = self.page.evaluate(
                f"""() => {{
                    const editors = document.querySelectorAll({json.dumps(sel.CONTENT_VERIFY_EDITOR)});
                    for (const ed of editors) {{
                        const text = ed.innerText || ed.textContent || "";
                        if (text.trim().length > 50) return true;
                    }}
                    return false;
                }}"""
            )
            if has_text:
                logger.info("内容注入验证通过：编辑器中有可见文本")
            else:
                logger.warning("内容注入验证失败：编辑器可能为空，请检查发布前截图")
                self._screenshot("content_verify_warning")
        except Exception as e:
            logger.warning(f"内容注入验证异常: {e}")

    def _screenshot(self, name: str) -> str:
        """截图并返回路径（按日期归档 + 时间戳后缀，避免多次发布覆盖同名截图）"""
        try:
            path = screenshot_path(self.screenshot_dir, name)
            self.page.screenshot(path=path)
            logger.debug(f"截图: {path}")
            return path
        except Exception as e:
            logger.debug(f"截图失败: {e}")
            return ""

    # ── 选择器命中率统计 ─────────────────────────────────────────────

    def _position_in_group(self, label: str, selector: str) -> int:
        """返回选择器在所属分组生效清单中的位置（1 起）；未知分组/不在清单返回 0"""
        key = sel.SELECTOR_GROUP_KEYS.get(label)
        if not key:
            return 0
        full = sel.get_selectors().get(key)
        if isinstance(full, str):
            full = [full]
        if not isinstance(full, list):
            return 0
        try:
            return full.index(selector) + 1
        except ValueError:
            return 0

    def _selector_stats_report(self) -> dict:
        """汇总当前发布的选择器命中情况（结构化日志事件负载）"""
        groups = sel.get_selectors()
        hits_by_group = {}
        never_hit = {}
        all_missed = []
        total_hits = 0
        for label, hits in self._selector_stats.items():
            if hits:
                hits_by_group[label] = dict(hits)
                total_hits += sum(hits.values())
            else:
                all_missed.append(label)
            key = sel.SELECTOR_GROUP_KEYS.get(label)
            full = groups.get(key) if key else None
            if isinstance(full, str):
                full = [full]
            if isinstance(full, list) and full:
                missed = [s for s in full if s not in hits]
                if missed:
                    never_hit[label] = missed
        return {
            "groups_attempted": len(self._selector_stats),
            "groups_hit": len(hits_by_group),
            "total_hits": total_hits,
            "hits_by_group": hits_by_group,
            "never_hit": never_hit,
            "all_missed": all_missed,
        }

    def _emit_selector_stats(self):
        """发布结束后输出选择器命中率统计，辅助精简 selectors.yaml 回退列表

        - 命中位置 > 1 → 主选择器可能已失效，建议提前该兜底选择器
        - 从未命中的选择器 → 候选删除
        """
        if not self._selector_stats:
            return
        report = self._selector_stats_report()
        _log_event("editor.selector_stats", **report)
        # 持久化到本地 JSONL 台账（按日期归档），供长期命中率统计与精简建议
        append_selector_stats(report)
        if report["total_hits"] == 0:
            logger.warning(
                f"[选择器命中统计] 本次发布 0 次命中（{report['groups_attempted']} 个分组全部未匹配）"
            )
            return

        for label in sorted(self._selector_stats):
            hits = self._selector_stats[label]
            if not hits:
                logger.warning(f"[选择器命中统计] {label}: 0 次命中（全部选择器均未匹配）")
                continue
            parts = []
            for selector, count in hits.items():
                pos = self._position_in_group(label, selector)
                parts.append(f'"{selector}"x{count}' + (f"(位置{pos})" if pos else ""))
                if pos and pos > 1:
                    logger.warning(
                        f"[选择器命中统计] {label}: 主选择器未命中，"
                        f"兜底命中「{selector}」(位置 {pos})——建议在 selectors.yaml 中提前"
                    )
            logger.info(
                f"[选择器命中统计] {label}: {sum(hits.values())} 次命中 -> " + ", ".join(parts)
            )

        if report["never_hit"]:
            lines = []
            for label, missed in sorted(report["never_hit"].items()):
                shown = missed[:8]
                suffix = f" 等 {len(missed)} 个" if len(missed) > 8 else ""
                lines.append(f"{label}: " + ", ".join(repr(s) for s in shown) + suffix)
            logger.warning(
                "[选择器命中统计] 从未命中的选择器（候选精简）: " + "; ".join(lines)
            )
