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
        """上传封面图（文件上传方式，CDN 失败后的回退路径）"""
        if not image_path or not os.path.exists(image_path):
            logger.warning(f"封面图不存在: {image_path}")
            return

        logger.info(f"上传封面图: {image_path}")

        try:
            result = self._try_select(
                sel.COVER_FILE_INPUTS,
                "封面文件上传",
                timeout_per=3000,
                screenshot_name="upload_cover_failed",
                dom_area_selectors=[".cover_area", ".cover-upload", ".weui-desktop-form__upload"],
            )
            if result:
                file_input, matched = result
                file_input.set_input_files(image_path)
                logger.info(f"封面图已上传: {matched}")
                try:
                    self.page.wait_for_selector(sel.COVER_PREVIEW, timeout=8000)
                except Exception:
                    self.page.wait_for_timeout(1500)
                return

            # 兜底：点击上传按钮触发文件选择器
            try:
                upload_btn = self.page.locator(sel.COVER_UPLOAD_BTN).first
                if upload_btn.is_visible(timeout=3000):
                    with self.page.expect_file_chooser() as fc_info:
                        upload_btn.click()
                    file_chooser = fc_info.value
                    file_chooser.set_files(image_path)
                    logger.info("通过文件选择器上传封面图")
                    try:
                        self.page.wait_for_selector(sel.COVER_PREVIEW, timeout=8000)
                    except Exception:
                        self.page.wait_for_timeout(1500)
                    return
            except Exception:
                pass

            logger.warning("未找到封面图上传入口")

        except Exception as e:
            logger.error(f"上传封面图失败: {e}")
            self._screenshot("upload_cover_error")

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

    def _wait_cover_preview(self, timeout: int = 8000) -> bool:
        """等待封面预览出现（兼容新旧版编辑器 DOM）

        - 旧版：等待 .cover_preview / .js_cover_preview 元素出现即视为成功；
        - 新版：无 preview 类名时，退化为等待封面区域内出现带非空 src 的 img。
        """
        try:
            self.page.wait_for_selector(sel.COVER_PREVIEW, timeout=timeout)
        except Exception:
            try:
                self.page.wait_for_function(
                    """() => {
                        const el = document.querySelector('.js_cover_area, .cover_area, .cover-panel');
                        const img = el && el.querySelector('img');
                        return !!(img && img.src && img.src.trim());
                    }""",
                    timeout=timeout,
                )
                return True
            except Exception:
                return False
        # preview 已出现（旧版成功信号）；img src 可能延迟加载，等待片刻并记录诊断
        try:
            self.page.wait_for_timeout(800)
            has_src = self.page.evaluate(
                """() => {
                    const el = document.querySelector('.cover_preview, .js_cover_preview');
                    const img = el && el.querySelector('img');
                    return !!(img && img.src && img.src.trim());
                }"""
            )
            if not has_src:
                logger.warning("封面预览已出现但未检测到图片 src（可能仍在加载，按成功处理）")
        except Exception as e:
            logger.debug(f"封面预览校验异常（按成功处理）: {e}")
        return True

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
        """关闭可能拦截点击的弹窗（教育弹窗/新手引导等）"""
        try:
            closed = self.page.evaluate("""() => {
                let count = 0;
                // 关闭可见弹窗的「我知道了/关闭」按钮
                const dismissBtns = Array.from(document.querySelectorAll(
                    '.education-dialog button, .weui-desktop-dialog button, ' +
                    '.weui-desktop-dialog__close-btn, [class*=dialog] .weui-desktop-btn'
                )).filter(el => {
                    const s = getComputedStyle(el);
                    return s.display !== 'none' && el.offsetParent !== null;
                });
                for (const btn of dismissBtns) {
                    const text = (btn.textContent || '').trim();
                    const cls = btn.className.toString();
                    if (text.includes('我知道了') || text.includes('关闭') || text.includes('确定') ||
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

    def publish(self):
        """发布文章（群发）"""
        logger.info("发布文章...")

        # 先关闭可能拦截的弹窗
        self.dismiss_dialogs()

        result = self._try_select(
            sel.PUBLISH_BUTTONS,
            "发布",
            timeout_per=3000,
            screenshot_name="publish_failed",
            dom_area_selectors=["#js_send", ".weui-desktop-btn"],
        )
        if result is None:
            return False

        el, matched = result
        # 先尝试正常点击，若被遮挡则 force 点击
        try:
            el.click(timeout=5000)
        except Exception:
            logger.warning("发布按钮被遮挡，尝试 force 点击")
            self.dismiss_dialogs()
            self.page.wait_for_timeout(300)
            el.click(timeout=5000, force=True)
        # 等待确认对话框
        try:
            confirm_btn = self.page.locator(sel.CONFIRM_BUTTON).first
            confirm_btn.wait_for(state="visible", timeout=5000)
            confirm_btn.click()
            try:
                self.page.wait_for_selector(sel.TOAST, timeout=10000)
            except Exception:
                self.page.wait_for_timeout(2000)
        except Exception:
            logger.info("无确认对话框，可能已直接发布")
            self.page.wait_for_timeout(1500)

        logger.info(f"文章已发布: {matched}")
        self._screenshot("published")
        return True

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
            {"success": bool, "mode": str, "error": str, "preview_path": str}
        """
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

            # 6. 设置封面：
            #    - cover_in_body=true（默认）：优先「从正文选择」（封面已在正文首图），失败回退旧文件上传；
            #    - cover_in_body=false：封面仍走 CDN 上传拿 URL（统一图片路径），但不注入正文、
            #      不用「从正文选择」，设置封面直接走旧的文件上传。
            if cover_path:
                if not (cover_in_body and cover_url and self.set_cover_from_body()):
                    self.upload_cover_image(cover_path)

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
                }
            else:
                success = self.publish()
                result = {
                    "success": success,
                    "mode": "publish",
                    "error": "" if success else "发布失败",
                    "preview_path": preview_path,
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
