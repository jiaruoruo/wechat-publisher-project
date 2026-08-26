"""图片生成模型 - 支持通义万相 / DALL-E"""

import os
import base64
import uuid
from pathlib import Path

import requests
from openai import OpenAI

from config.paths import BASE_DIR, IMAGES_DIR as DEFAULT_IMAGES_DIR
from models.image_sizes import closest_size, DALLE_VALID_SIZES, DASHSCOPE_VALID_SIZES


class ImageGenerator:
    """图片生成器，支持多种图片模型"""

    # 进程级 OpenAI 客户端缓存：避免每次 generate 都新建客户端（建连开销）。
    # key 为 api_key，key 变化（如测试换 key）时自动重建。
    _openai_clients: dict[str, "OpenAI"] = {}

    def __init__(self, config: dict):
        """
        Args:
            config: 图片模型配置，如 {"provider": "dashscope", "model": "wanx-v1"}
        """
        self.provider = config.get("provider", "dashscope")
        self.model = config.get("model", "wanx-v1")
        # 优先使用配置的 storage.images_dir，其次默认 storage/images
        images_dir = config.get("images_dir", "") or DEFAULT_IMAGES_DIR
        if not os.path.isabs(images_dir):
            images_dir = os.path.join(BASE_DIR, images_dir)
        self.images_dir = Path(images_dir)
        self.images_dir.mkdir(parents=True, exist_ok=True)

    def generate(
        self,
        prompt: str,
        size: str = "1024x768",
        output_name: str | None = None,
    ) -> str:
        """
        生成图片并保存到本地

        Args:
            prompt: 图片描述提示词
            size: 图片尺寸，如 "1024x768"（自动适配到 provider 支持的最接近尺寸）
            output_name: 输出文件名（不含扩展名），默认自动生成

        Returns:
            保存的图片文件路径
        """
        # 将请求尺寸映射到 provider 支持的有效尺寸
        if self.provider == "openai":
            size = closest_size(size, DALLE_VALID_SIZES, "DALL-E 3")
        elif self.provider == "dashscope":
            size = closest_size(size, DASHSCOPE_VALID_SIZES, "DashScope")

        if self.provider == "dashscope":
            image_data = self._generate_dashscope(prompt, size)
        elif self.provider == "openai":
            image_data = self._generate_openai(prompt, size)
        else:
            raise ValueError(f"不支持的图片模型 provider: {self.provider}")

        # 保存图片
        if not output_name:
            output_name = f"img_{uuid.uuid4().hex[:8]}"
        file_path = self.images_dir / f"{output_name}.png"

        with open(file_path, "wb") as f:
            f.write(image_data)

        return str(file_path)

    def _generate_dashscope(self, prompt: str, size: str) -> bytes:
        """通过通义万相生成图片"""
        try:
            import dashscope
            from dashscope import ImageSynthesis
        except ImportError:
            raise RuntimeError(
                "dashscope 未安装，请运行: pip install dashscope>=1.14\n"
                "或切换图片模型 provider 为 openai"
            )

        api_key = os.environ.get("DASHSCOPE_API_KEY", "")
        if api_key:
            dashscope.api_key = api_key

        # 转换尺寸格式
        width, height = size.split("x")
        rsp = ImageSynthesis.call(
            model=self.model,
            prompt=prompt,
            n=1,
            size=f"{width}*{height}",
        )

        if rsp.status_code != 200:
            raise RuntimeError(f"通义万相生成图片失败: {rsp.message}")

        # 下载图片
        image_url = rsp.output.results[0].url
        response = requests.get(image_url, timeout=60)
        response.raise_for_status()
        return response.content

    def _generate_openai(self, prompt: str, size: str) -> bytes:
        """通过 DALL-E 生成图片"""
        api_key = os.environ.get("OPENAI_API_KEY", "")
        # 复用进程级客户端（按 api_key 缓存），避免重复建连
        client = self._openai_clients.get(api_key)
        if client is None:
            client = OpenAI(api_key=api_key)
            self._openai_clients[api_key] = client

        response = client.images.generate(
            model=self.model,
            prompt=prompt,
            size=size,
            quality="standard",
            n=1,
            response_format="b64_json",
        )

        return base64.b64decode(response.data[0].b64_json)

    @staticmethod
    def image_to_base64(image_path: str) -> str:
        """将图片文件转为 base64 编码字符串"""
        with open(image_path, "rb") as f:
            return base64.b64encode(f.read()).decode("utf-8")

    @staticmethod
    def image_to_data_url(image_path: str) -> str:
        """将图片文件转为 data URL"""
        b64 = ImageGenerator.image_to_base64(image_path)
        return f"data:image/png;base64,{b64}"
