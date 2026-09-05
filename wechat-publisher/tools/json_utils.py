"""JSON 解析工具 — 从 LLM 响应中提取 JSON 块"""

import json
import logging

logger = logging.getLogger(__name__)


def extract_json_from_response(response: str) -> dict:
    """从 LLM 文本响应中提取 JSON 内容
    
    处理常见的 LLM 输出格式：
    - 纯 JSON 文本
    - ```json ... ``` 包裹
    - ``` ... ``` 包裹（无语言标记）
    """
    text = response.strip()

    # 尝试提取 JSON 代码块
    if "```json" in text:
        parts = text.split("```json", 1)
        if len(parts) > 1:
            text = parts[1].split("```", 1)[0].strip()
    elif "```" in text:
        parts = text.split("```", 1)
        if len(parts) > 1:
            text = parts[1].split("```", 1)[0].strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # LLM 常在 JSON 前后附带说明文字，尝试从文本中定位第一个 JSON 对象
        decoder = json.JSONDecoder()
        start = text.find("{")
        while start != -1:
            try:
                obj, _ = decoder.raw_decode(text[start:])
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                pass
            start = text.find("{", start + 1)
        raise


def safe_extract_json(response: str, default: dict | None = None) -> dict:
    """安全提取 JSON，解析失败时返回默认值"""
    try:
        return extract_json_from_response(response)
    except (json.JSONDecodeError, ValueError, IndexError) as e:
        logger.warning(f"JSON 提取失败: {e}")
        return default if default is not None else {}
