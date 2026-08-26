"""Agent 标签映射 — 统一的 Agent 名称→中文标签转换"""

AGENT_LABELS: dict[str, str] = {
    "topic_planner": "选题策划",
    "content_writer": "内容创作",
    "image_generator": "配图生成",
    "reviewer": "审核校对",
    "retry_counter": "审核重试",
    "formatter": "排版美化",
    "publisher": "发布执行",
}

STAGE_ICONS: dict[str, str] = {
    "pending": "⬜",
    "running": "🔄",
    "completed": "✅",
    "failed": "❌",
    "skipped": "⏭️",
}
