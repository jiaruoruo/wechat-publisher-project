"""公众号素材管理辅助 - 图片上传等"""

import os
import logging
from typing import Any

logger = logging.getLogger(__name__)


class WechatAPIHelper:
    """微信公众号素材管理辅助工具（用于非浏览器自动化场景）"""

    def __init__(self, config: dict):
        self.config = config

    async def upload_image(self, image_path: str) -> str:
        """
        上传图片到公众号素材库（备用方案）

        注意：此功能需要公众号 API 权限，主要使用浏览器自动化方式。
        此方法保留用于将来扩展。

        Args:
            image_path: 本地图片路径

        Returns:
            图片在公众号中的 media_id 或 URL
        """
        logger.warning("upload_image 需要公众号 API 权限，当前使用浏览器自动化方式")
        raise NotImplementedError(
            "请使用浏览器自动化方式上传图片。"
            "此方法保留用于将来通过官方 API 上传的扩展。"
        )

    def format_image_for_wechat(self, image_path: str) -> str:
        """
        将本地图片格式化为公众号编辑器可用的格式

        在浏览器自动化模式下，图片通过文件上传对话框直接上传，
        此方法主要用于准备图片信息。

        Args:
            image_path: 本地图片路径

        Returns:
            图片的绝对路径（用于 Playwright 文件上传）
        """
        abs_path = os.path.abspath(image_path)
        if not os.path.exists(abs_path):
            raise FileNotFoundError(f"图片文件不存在: {abs_path}")
        return abs_path
