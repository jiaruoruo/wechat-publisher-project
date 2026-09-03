"""配图生成 Agent - 生成封面图 + 文内插图"""

import json
import re
import logging
import uuid
from datetime import datetime

from agents.base import BaseAgent
from graph.state import ArticleState
from models.image_model import ImageGenerator

logger = logging.getLogger(__name__)


class ImageGeneratorAgent(BaseAgent):
    """配图生成 Agent：解析文章中的图片标记，生成封面图和文内插图"""

    agent_name = "image_generator"
    prompt_file = "image_generator.yaml"

    def __init__(self, llm_router, config: dict):
        super().__init__(llm_router, config)
        image_config = dict(llm_router.get_image_model_config())
        # 图片保存目录统一走 storage.images_dir 配置
        image_config.setdefault("images_dir", config.get("storage", {}).get("images_dir", ""))
        self.image_generator = ImageGenerator(image_config)

    def run(self, state: ArticleState) -> dict:
        """执行配图生成"""
        content = state.get("content", "")
        cover_prompt_cn = state.get("cover_prompt", "")

        # 1. 解析文章中的 [IMAGE: ...] 标记
        inline_descriptions = self._extract_image_markers(content)
        logger.info(f"解析到 {len(inline_descriptions)} 个插图标记")

        # 2. 生成封面图
        cover_image_path = ""
        if cover_prompt_cn:
            try:
                enhanced_prompt = self._enhance_prompt(cover_prompt_cn, "cover")
                cover_image_path = self.image_generator.generate(
                    prompt=enhanced_prompt,
                    size="900x383",  # 公众号封面比例 2.35:1
                    output_name=f"cover_{uuid.uuid4().hex[:8]}",
                )
                logger.info(f"封面图已生成: {cover_image_path}")
            except Exception as e:
                logger.error(f"封面图生成失败: {e}")

        # 封面兜底：云端图片生成不可用（未配置 API Key/欠费/网络异常）时，
        # 本地渲染一张兜底封面。公众号「发表」强制要求封面，无封面只能存草稿，
        # 兜底封面保证文章仍能真实发表（用户可在后台替换为更精美的封面）。
        if not cover_image_path:
            try:
                fallback_title = (
                    state.get("article_title") or cover_prompt_cn or "文章封面"
                )
                cover_image_path = self._local_fallback_cover(fallback_title)
                if cover_image_path:
                    logger.warning(
                        f"图片生成服务不可用，已使用本地渲染兜底封面: {cover_image_path}"
                    )
            except Exception as e:
                logger.error(f"兜底封面生成也失败: {e}")

        # 3. 生成文内插图
        inline_images = []
        for i, desc in enumerate(inline_descriptions):
            try:
                enhanced_prompt = self._enhance_prompt(desc, "inline")
                image_path = self.image_generator.generate(
                    prompt=enhanced_prompt,
                    size="1024x768",
                    output_name=f"inline_{uuid.uuid4().hex[:8]}",
                )
                inline_images.append(image_path)
                logger.info(f"插图 {i+1}/{len(inline_descriptions)} 已生成: {image_path}")
            except Exception as e:
                logger.error(f"插图 {i+1} 生成失败: {e}")

        # 4. 替换文章中的 [IMAGE: ...] 标记为占位符
        updated_content = self._replace_image_markers(content, inline_images)

        return {
            "content": updated_content,
            "cover_image_path": cover_image_path,
            "inline_images": inline_images,
            # metadata 走 schema 级归并，只需返回新增字段
            "metadata": {
                "images_generated_at": datetime.now().isoformat(),
                "inline_images_count": len(inline_images),
            },
        }

    def _local_fallback_cover(self, text: str) -> str:
        """本地渲染兜底封面：渐变背景 + 标题文字（900x383，公众号封面比例）

        不依赖任何外部 API，保证图片生成服务不可用时封面硬性要求仍能满足。
        字体缺失时退化为 PIL 默认字体（中文可能显示为方块，但发布不受阻）。
        """
        from PIL import Image, ImageDraw, ImageFont

        width, height = 900, 383
        img = Image.new("RGB", (width, height))
        draw = ImageDraw.Draw(img)

        # 竖向渐变背景（深蓝 → 青绿）
        top, bottom = (24, 60, 110), (10, 140, 120)
        for y in range(height):
            ratio = y / height
            color = tuple(
                int(top[i] + (bottom[i] - top[i]) * ratio) for i in range(3)
            )
            draw.line([(0, y), (width, y)], fill=color)

        # 标题：去空白截断，避免超出画面
        title = "".join(text.split())[:22]
        font = None
        for fp in (
            r"C:\Windows\Fonts\msyh.ttc",
            r"C:\Windows\Fonts\msyh.ttf",
            r"C:\Windows\Fonts\simhei.ttf",
        ):
            try:
                font = ImageFont.truetype(fp, 46)
                break
            except Exception:
                continue
        if font is None:
            font = ImageFont.load_default()

        try:
            bbox = draw.textbbox((0, 0), title, font=font)
            tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        except Exception:
            tw, th = len(title) * 46, 46
        draw.text(
            ((width - tw) // 2, (height - th) // 2),
            title,
            fill=(255, 255, 255),
            font=font,
        )

        path = self.image_generator.images_dir / f"cover_fallback_{uuid.uuid4().hex[:8]}.png"
        img.save(path)
        return str(path)

    def _extract_image_markers(self, content: str) -> list[str]:
        """提取文章中所有 [IMAGE: ...] 标记的描述"""
        pattern = r"\[IMAGE:\s*(.*?)\]"
        matches = re.findall(pattern, content)
        return [m.strip() for m in matches if m.strip()]

    def _enhance_prompt(self, description: str, image_type: str) -> str:
        """调用 LLM 将中文描述增强为英文图片生成提示词"""
        prompt = f"""请将以下中文图片描述转化为高质量的英文 AI 图片生成提示词。

图片类型：{"封面图（宽屏 2.35:1 比例，大气有冲击力）" if image_type == "cover" else "文内插图（清晰专业，与文章内容相关）"}

中文描述：{description}

请只输出增强后的英文提示词，不要包含其他内容。"""

        try:
            enhanced = self.invoke(prompt)
            return enhanced.strip().strip('"').strip("'")
        except Exception as e:
            logger.warning(f"提示词增强失败，使用原始描述: {e}")
            return description

    def _replace_image_markers(self, content: str, image_paths: list[str]) -> str:
        """将文章中的 [IMAGE: ...] 标记替换为占位符"""
        counter = [0]

        def replacer(match):
            idx = counter[0]
            counter[0] += 1
            if idx < len(image_paths):
                placeholder = "{{IMAGE_PATH_" + str(idx + 1) + "}}"
                return "![插图" + str(idx + 1) + "](" + placeholder + ")"
            return match.group(0)  # 保留原标记

        return re.sub(r"\[IMAGE:\s*.*?\]", replacer, content)