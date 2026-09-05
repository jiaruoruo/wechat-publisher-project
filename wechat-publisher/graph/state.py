"""全局状态定义 - LangGraph StateGraph 共享状态"""

from datetime import datetime
from typing import Annotated, Any, TypedDict


# ── 通道归并器（单一来源）────────────────────────────────
# 同时用于：1) TypedDict 的 Annotated 注解（LangGraph invoke/stream 内部归并）
#          2) workflow.run() 的 stream 累积（与 schema 语义保持一致，避免漂移）


def merge_metadata(current: dict | None, update: dict) -> dict:
    """metadata 通道归并器：节点返回部分 metadata 时与已有值合并（同名键后者覆盖）"""
    return {**(current or {}), **(update or {})}


def append_review_history(current: list | None, update: list) -> list:
    """review_history 通道归并器：节点返回的审核记录追加到历史"""
    return [*(current or []), *(update or [])]


CHANNEL_REDUCERS: dict[str, Any] = {
    "metadata": merge_metadata,
    "review_history": append_review_history,
}


class ArticleState(TypedDict, total=False):
    """文章生产流程的全局状态，所有 Agent 共享

    total=False：流水线各阶段产出各自的字段，缺失字段由节点用 .get() 安全读取，
    或由 create_initial_state() 提供默认值。

    带 Annotated 归并器的通道（metadata / review_history）：
    - 节点只需返回本阶段新增的部分字段，LangGraph 在 schema 层自动合并，
      不再要求每个节点手写 {**state.get("metadata", {}), ...}。
    """

    # 选题阶段输出
    topic: str                    # 选题标题
    outline: str                  # 文章大纲
    target_audience: str          # 目标受众描述

    # 内容创作阶段输出
    content: str                  # Markdown 格式正文
    article_title: str            # 文章标题（最终版）
    summary: str                  # 文章摘要

    # 配图阶段输出
    cover_prompt: str             # 封面图生成提示词
    cover_image_path: str         # 封面图本地路径
    inline_images: list[str]      # 文内插图路径列表

    # 审核阶段输出
    review_result: dict[str, Any] # {"passed": bool, "feedback": str, "score": int}
    retry_count: int              # 当前重写次数
    review_history: Annotated[list[dict[str, Any]], append_review_history]  # 每次审核记录，自动追加

    # 排版阶段输出
    formatted_html: str           # 公众号兼容的富文本 HTML

    # 发布阶段输出
    publish_result: dict[str, Any]  # {"success": bool, "url": str, "error": str}

    # 元数据（各节点部分更新自动合并）
    metadata: Annotated[dict[str, Any], merge_metadata]


def create_initial_state(
    *,
    max_retries: int = 2,
    extra: dict | None = None,
) -> ArticleState:
    """构建带默认值的初始状态

    统一入口，避免各调用方手动拼装导致字段缺失（如 retry_count 未初始化、
    inline_images 缺失等）。extra 中的 metadata 会与默认 metadata 深度合并，
    不会覆盖 max_retries/started_at。

    Args:
        max_retries: 审核最大重试次数（写入 metadata）
        extra: 调用方传入的额外初始字段（如 topic、triggered_by 等）
    """
    state: ArticleState = {
        "retry_count": 0,
        "review_result": {},
        "publish_result": {},
        "inline_images": [],
        "review_history": [],
        "metadata": {
            "max_retries": max_retries,
            "started_at": datetime.now().isoformat(),
        },
    }

    if extra:
        if "metadata" in extra:
            state["metadata"] = {**state["metadata"], **extra["metadata"]}
            extra = {k: v for k, v in extra.items() if k != "metadata"}
        state.update(extra)

    return state
