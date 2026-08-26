"""图片尺寸适配工具 - 将请求尺寸映射到各 Provider 支持的最接近尺寸"""

# DALL-E 3 仅支持这三种尺寸
DALLE_VALID_SIZES = ["1024x1024", "1792x1024", "1024x1792"]

# DashScope wanx-v1 支持的尺寸
DASHSCOPE_VALID_SIZES = ["1024*1024", "1280*720", "720*1280"]


def closest_size(requested: str, valid_sizes: list[str], provider: str) -> str:
    """将请求的尺寸映射到 provider 支持的最接近尺寸（按宽高比匹配）"""
    import logging
    _log = logging.getLogger(__name__)

    try:
        w, h = requested.replace("*", "x").split("x")
        target_ratio = int(w) / int(h)
    except (ValueError, ZeroDivisionError):
        _log.warning(f"无法解析尺寸 '{requested}'，使用默认尺寸")
        return valid_sizes[0]

    best = None
    best_diff = float("inf")
    for vs in valid_sizes:
        vw, vh = vs.replace("*", "x").split("x")
        vr = int(vw) / int(vh)
        diff = abs(target_ratio - vr)
        if diff < best_diff:
            best_diff = diff
            best = vs

    if best and best != requested:
        _log.info(f"尺寸 {requested} → {best}（{provider} 适配）")
    return best or valid_sizes[0]
