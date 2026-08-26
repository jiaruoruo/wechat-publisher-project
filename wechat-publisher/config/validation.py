"""配置校验 - 供 CLI check / Hermes / 调度器共用的统一校验逻辑

返回 (severity, message) 列表，severity 为 "error" 或 "warning"。
CLI 中显示为 ❌/⚠️，Hermes/调度器启动时记录日志。
"""

import os
from typing import Any

# Agent -> 期望的模型配置键
REQUIRED_AGENT_MODELS = [
    "topic_planner",
    "content_writer",
    "image_generator",
    "reviewer",
    "formatter",
    "publisher",
]

# Provider -> 环境变量名
PROVIDER_ENV_MAP = {
    "openai": "OPENAI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "dashscope": "DASHSCOPE_API_KEY",
}


def validate_config(config: dict | None) -> list[tuple[str, str]]:
    """校验配置，返回 (severity, message) 列表

    - error:   会导致功能不可用的缺失（模型未配置、API Key 全缺等）
    - warning: 有风险但可运行的配置（明文密钥、通知关闭、未开加密等）
    """
    config = config or {}
    issues: list[tuple[str, str]] = []

    # ── 模型配置 ──────────────────────────────────────────────
    models = config.get("models", {})
    if not models:
        issues.append(("error", "未配置任何模型（models 为空）"))
    else:
        missing = [a for a in REQUIRED_AGENT_MODELS if a not in models]
        if missing:
            issues.append(("error", f"缺少模型配置: {', '.join(missing)}"))

    # ── API Key ───────────────────────────────────────────────
    api_keys = config.get("api_keys", {})
    for provider, env_var in PROVIDER_ENV_MAP.items():
        has_env = bool(os.environ.get(env_var))
        has_plain = bool(api_keys.get(provider))
        if not has_env and not has_plain:
            issues.append(
                ("error", f"{provider} API Key 未配置（环境变量 {env_var} 或 settings.yaml）")
            )
        elif not has_env and has_plain:
            issues.append(
                ("warning", f"{provider} 正在使用 settings.yaml 明文 API Key，建议改用环境变量 {env_var}")
            )

    # ── 飞书（Hermes）─────────────────────────────────────────
    feishu = config.get("feishu", {})
    if feishu.get("app_id"):
        if not feishu.get("app_secret"):
            issues.append(("error", "飞书已配置 app_id 但缺少 app_secret"))
        if not feishu.get("verification_token"):
            issues.append(("warning", "飞书未配置 verification_token，事件回调将不做校验"))
        if not feishu.get("encrypt_key"):
            issues.append(("warning", "飞书未配置 encrypt_key，事件以明文传输（建议生产环境开启加密）"))

        # Webhook 绑定与来源校验（安全加固）
        webhook_host = feishu.get("webhook_host", "127.0.0.1")
        allow_from = feishu.get("webhook_allow_from", [])
        if isinstance(allow_from, str):
            allow_from = [s for s in allow_from.split(",") if s.strip()]
        if webhook_host in ("0.0.0.0", "::"):
            if allow_from:
                issues.append((
                    "warning",
                    "飞书 webhook 绑定 0.0.0.0（对所有网卡开放），已配置 IP 白名单，"
                    "仍建议改用反向代理或绑定 127.0.0.1",
                ))
            else:
                issues.append((
                    "error",
                    "飞书 webhook 绑定 0.0.0.0 且未配置 webhook_allow_from IP 白名单，"
                    "回调端口对公网裸暴露，任何人均可构造 POST 触发命令",
                ))
        if feishu.get("require_encrypt") and not feishu.get("encrypt_key"):
            issues.append((
                "error",
                "feishu.require_encrypt 已开启但未配置 encrypt_key，Hermes 将拒绝启动",
            ))

    # ── 通知 ──────────────────────────────────────────────────
    notification = config.get("notification", {})
    if not notification.get("enabled"):
        issues.append(("warning", "通知未启用（notification.enabled=false）：审核失败/发布失败仅记录日志文件"))

    # ── 内容 ──────────────────────────────────────────────────
    content = config.get("content", {})
    if not content.get("domain"):
        issues.append(("error", "内容领域未配置（content.domain）"))

    # ── 审核（重试成本守卫）───────────────────────────────────
    review = config.get("review", {})
    max_retries = review.get("max_retries", 2)
    if not isinstance(max_retries, int) or isinstance(max_retries, bool) or max_retries < 0:
        issues.append(("error", f"review.max_retries 必须是非负整数，当前值: {max_retries!r}"))
    elif max_retries > 4:
        # 每轮重试 = retry_counter + content_writer + image_generator + reviewer 4 节点，
        # 即 1 次完整重写 + 1 次配图 + 1 次审核的 LLM/API 调用。
        # 递归上限由 workflow.run 按节点数显式设置，不存在触顶问题，此处仅提示成本。
        issues.append((
            "warning",
            f"review.max_retries={max_retries} 超过 4：每轮重试都会再次执行重写+配图+审核"
            f"（共 {max_retries} 轮额外 LLM/API 调用），请注意 LLM 成本与总耗时",
        ))

    return issues
