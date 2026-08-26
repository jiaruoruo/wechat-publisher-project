"""图片素材外链 - 上传本地图片到微信 CDN，返回可外链 URL

实现：复用已登录的 mp.weixin.qq.com 会话，通过编辑器图片上传接口
`uploadimg2cdn`（编辑器粘贴/上传图片的同一条链路）把图片上传到微信素材
CDN（mmbiz.qpic.cn），返回可外链 URL。该接口长期稳定，无需额外凭证；
上传失败时按降级策略回退 base64 内嵌（旧行为）。

设计：HTML 中的图片使用 {{IMAGE_PATH_N}} 占位符（image_generator 生成，
formatter 保留），发布前在此模块解析为外链，实现「图片素材外链分离」，
避免多图文章 HTML 膨胀到数 MB。

降级策略：逐张独立兜底——每张图 CDN 优先，失败回退 base64。
已成功上传的 CDN URL 始终保留（不再整篇重新 base64 化，避免浪费已成功的
上传与丢弃可用外链）。若全部失败则自然全部回退 base64。

每次解析产出 ImageUploadStats（total/cdn_ok/base64_fallback/missing/mode）
并写入结构化日志（event=image_upload），便于排查 CDN 通道问题。
"""

import os
import re
import json
import base64
import logging
from dataclasses import dataclass
from typing import Callable

from playwright.sync_api import Page

from models.image_model import ImageGenerator  # base64 兜底
from config.structured_logging import log_event

logger = logging.getLogger(__name__)

# 编辑器图片上传接口（与粘贴图片同链路）
UPLOAD_ENDPOINT = "https://mp.weixin.qq.com/cgi-bin/uploadimg2cdn"

# 图片占位符：{{IMAGE_PATH_1}}、{{IMAGE_PATH_2}} ...
IMAGE_PLACEHOLDER_RE = re.compile(r"\{\{IMAGE_PATH_(\d+)\}\}")


@dataclass
class ImageUploadStats:
    """一次占位符解析的失败统计"""

    total: int = 0            # 需要解析的占位符数
    cdn_ok: int = 0           # CDN 上传成功
    base64_fallback: int = 0  # base64 兜底
    missing: int = 0          # 路径缺失/越界（替换为空）
    mode: str = "none"        # cdn / mixed / base64 / none

    @property
    def failed(self) -> int:
        """上传失败（触发 base64 兜底）的图片数"""
        return self.base64_fallback

    @property
    def all_failed(self) -> bool:
        return self.total > 0 and self.cdn_ok == 0 and self.base64_fallback == self.total


def extract_token(url: str) -> str:
    """从 mp.weixin.qq.com 页面 URL 提取 token（编辑器接口的鉴权参数）

    登录后编辑器 URL 形如 .../appmsg?action=edit&token=XXXX&lang=zh_CN。
    仅作为 read_token_from_page() 的最后回退；直接拼 token 到 URL 会留痕。
    """
    m = re.search(r"[?&]token=([0-9a-zA-Z_-]+)", url or "")
    return m.group(1) if m else ""


def read_token_from_page(page: Page) -> str:
    """优先从页面 JS 上下文读取上传鉴权 token，避免解析 URL 中明文 token

    微信编辑器通常把 token 存在 window 全局或隐藏输入字段中；这里按常见位置
    探测，找不到再回退 URL 提取（URL 中的 token 会随浏览器地址栏/历史/请求
    URL 留痕，回退时告警）。
    """
    try:
        token = page.evaluate(
            """() => {
                const el = document.querySelector(
                    'input[name="token"], input[name="token_id"], input[name="ticket"]'
                );
                if (el && el.value) return el.value;
                if (typeof window.token === 'string' && window.token) return window.token;
                if (typeof window.appmsg_token === 'string' && window.appmsg_token) return window.appmsg_token;
                for (const k of ['token', 'appmsg_token', 'wx_token']) {
                    try {
                        const v = localStorage.getItem(k);
                        if (v) return v;
                    } catch (e) {}
                }
                return '';
            }"""
        )
        if token:
            return token
    except Exception as e:
        logger.debug(f"从页面 JS 上下文读取 token 失败: {e}")
    token = extract_token(page.url)
    if token:
        logger.warning(
            "token 仅能从页面 URL 提取（会随请求 URL 留痕），建议确认编辑器页面结构，"
            "改用隐藏字段/JS-SDK 传递以消除明文 token 暴露"
        )
    return token


def parse_upload_response(text: str) -> str:
    """解析 uploadimg2cdn 响应，返回图片 CDN URL；无法解析返回 ""

    兼容多种返回形态：
    - {"url": "https://mmbiz.qpic.cn/..."}（最常见）
    - {"cdn_url": "..."} / {"cdnUrl": "..."}
    - {"content": "<img src=\"...\">"}（content 内嵌 img 标签）
    - 非 JSON（JSONP / HTML）时正则提取 img src
    """
    if not text:
        return ""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', text)
        return m.group(1) if m else ""

    for key in ("url", "cdn_url", "cdnUrl"):
        val = data.get(key)
        if isinstance(val, str) and val.startswith("http"):
            return val

    content = data.get("content", "")
    if isinstance(content, str):
        m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', content)
        if m:
            return m.group(1)
    return ""


def upload_image_to_cdn(page: Page, image_path: str, token: str = "") -> str:
    """上传单张图片到微信 CDN，返回外链 URL；失败返回 ""（由调用方兜底）"""
    if not os.path.exists(image_path):
        logger.warning(f"图片不存在，跳过 CDN 上传: {image_path}")
        return ""
    if not token:
        token = read_token_from_page(page)
    if not token:
        logger.warning("无法从页面 URL 提取 token，跳过 CDN 上传")
        return ""

    try:
        with open(image_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")

        text = page.evaluate(
            """async (args) => {
                const url = args.endpoint + '?token=' + encodeURIComponent(args.token)
                    + '&lang=zh_CN&f=json&ajax=1&random=' + Math.random();
                const bin = atob(args.b64);
                const bytes = new Uint8Array(bin.length);
                for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
                const blob = new Blob([bytes], { type: 'image/png' });
                const form = new FormData();
                form.append('img', blob, args.filename);
                const resp = await fetch(url, {
                    method: 'POST',
                    body: form,
                    credentials: 'include',
                    signal: AbortSignal.timeout(30000),
                });
                return await resp.text();
            }""",
            {
                "endpoint": UPLOAD_ENDPOINT,
                "token": token,
                "b64": b64,
                "filename": os.path.basename(image_path),
            },
        )

        url = parse_upload_response(text)
        if url:
            logger.info(f"图片已上传到微信 CDN: {os.path.basename(image_path)} -> {url[:60]}...")
        else:
            logger.warning(f"CDN 上传返回无法解析: {text[:120]}")
        return url
    except Exception as e:
        logger.warning(f"CDN 上传失败 {image_path}: {e}")
        return ""


def _resolve_one(path: str, uploader: Callable[[str], str]) -> tuple[str, str]:
    """解析单张图片：返回 (kind, value)，kind ∈ {cdn, base64, missing}"""
    if not path or not os.path.exists(path):
        return "missing", ""
    try:
        url = uploader(path)
        if url:
            return "cdn", url
    except Exception as e:
        logger.warning(f"图片外链解析失败 {path}: {e}")
    try:
        return "base64", ImageGenerator.image_to_data_url(path)
    except Exception as e:
        logger.warning(f"base64 兜底失败 {path}: {e}")
        return "missing", ""


def resolve_image_placeholders(
    html: str,
    image_paths: list[str],
    uploader: Callable[[str], str],
) -> tuple[str, ImageUploadStats]:
    """把 HTML 中的 {{IMAGE_PATH_N}} 占位符替换为外链 URL

    逐张独立兜底：每张图 CDN 优先，失败回退 base64；已成功的 CDN URL 保留，
    不再整篇重新 base64 化（避免丢弃可用外链、浪费已成功的上传）。

    Args:
        html: 含 {{IMAGE_PATH_N}} 占位符的 HTML（N 对应 image_paths[N-1]）
        image_paths: 文内插图路径列表（与占位符编号一一对应）
        uploader: 上传回调，入参为图片路径，返回外链 URL；返回空串时回退 base64

    Returns:
        (解析后的 HTML, ImageUploadStats)
    """
    placeholders = list(IMAGE_PLACEHOLDER_RE.finditer(html))
    if not placeholders:
        return html, ImageUploadStats()

    resolved: list[tuple[re.Match, str, str]] = []
    for m in placeholders:
        idx = int(m.group(1))
        path = image_paths[idx - 1] if 0 <= idx - 1 < len(image_paths) else ""
        kind, value = _resolve_one(path, uploader)
        resolved.append((m, kind, value))

    # 组装最终 HTML（不再二次 base64 化，已 cdn 的保留）
    for m, kind, value in resolved:
        html = html.replace(m.group(0), value)

    stats = ImageUploadStats(
        total=len(placeholders),
        cdn_ok=sum(1 for _, k, _ in resolved if k == "cdn"),
        base64_fallback=sum(1 for _, k, _ in resolved if k == "base64"),
        missing=sum(1 for _, k, _ in resolved if k == "missing"),
    )
    if stats.cdn_ok > 0 and stats.base64_fallback == 0:
        stats.mode = "cdn"
    elif stats.cdn_ok > 0 and stats.base64_fallback > 0:
        stats.mode = "mixed"
    elif stats.base64_fallback > 0:
        stats.mode = "base64"
    else:
        stats.mode = "none"

    log_event(
        "image_upload",
        total=stats.total,
        cdn_ok=stats.cdn_ok,
        base64_fallback=stats.base64_fallback,
        missing=stats.missing,
        mode=stats.mode,
    )
    return html, stats
