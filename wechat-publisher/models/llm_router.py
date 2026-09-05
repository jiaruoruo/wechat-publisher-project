"""多模型路由 - 根据 Agent 名称从配置中加载对应的 LLM 实例"""

import os
import logging
from typing import Any

import yaml
from langchain_openai import ChatOpenAI
from langchain_core.language_models import BaseChatModel


from config.paths import SETTINGS_PATH as CONFIG_PATH

logger = logging.getLogger(__name__)

# 模块级配置缓存：避免 main / hermes / scheduler / workflow 各自重复解析
# 配置文件（每次 open + yaml.safe_load）。单进程内只解析一次。
_config_cache: dict | None = None


def load_config(force_reload: bool = False) -> dict:
    """加载全局配置（带模块级缓存）

    Args:
        force_reload: 为 True 时忽略缓存、重新从磁盘读取（测试或热重载用）。
    """
    global _config_cache
    if _config_cache is not None and not force_reload:
        return _config_cache
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        _config_cache = yaml.safe_load(f)
    return _config_cache


def clear_config_cache() -> None:
    """清空配置缓存（测试用，避免跨用例污染模块级状态）"""
    global _config_cache
    _config_cache = None


# Provider -> 环境变量名映射
PROVIDER_ENV_MAP = {
    "openai": "OPENAI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "dashscope": "DASHSCOPE_API_KEY",
}

# Provider -> API Base URL 映射
PROVIDER_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "dashscope": "https://dashscope.aliyuncs.com/compatible-mode/v1",
}


class LLMRouter:
    """多模型路由器，根据 Agent 名称返回对应的 LLM 实例"""

    def __init__(self, config: dict | None = None):
        self.config = config or load_config()
        self._cache: dict[str, BaseChatModel] = {}

    def get_llm(self, agent_name: str) -> BaseChatModel:
        """根据 Agent 名称获取对应的 LLM 实例（带缓存）"""
        if agent_name in self._cache:
            return self._cache[agent_name]

        models_config = self.config.get("models", {})
        agent_config = models_config.get(agent_name)
        if not agent_config:
            raise ValueError(
                f"未找到 Agent '{agent_name}' 的模型配置，"
                f"可用: {list(models_config.keys())}"
            )

        llm = self._create_llm(agent_config)
        self._cache[agent_name] = llm
        return llm

    def _create_llm(self, agent_config: dict) -> BaseChatModel:
        """根据配置创建 LLM 实例"""
        provider = agent_config["provider"]
        model = agent_config["model"]
        temperature = agent_config.get("temperature", 0.7)

        # 获取 API Key：优先环境变量，其次配置文件
        api_key = self._get_api_key(provider)
        base_url = PROVIDER_BASE_URLS.get(provider)

        return ChatOpenAI(
            model=model,
            temperature=temperature,
            api_key=api_key,
            base_url=base_url,
            request_timeout=120,  # 2 分钟超时，防止 API 挂起
            max_retries=2,        # SDK 内置重试（限流/网络错误自动重试）
        )

    def _get_api_key(self, provider: str) -> str:
        """获取 API Key"""
        # 优先从环境变量获取
        env_var = PROVIDER_ENV_MAP.get(provider)
        if env_var:
            key = os.environ.get(env_var)
            if key:
                return key

        # 其次从配置文件获取（明文密钥，需告警）
        api_keys = self.config.get("api_keys", {})
        key = api_keys.get(provider, "")
        if key:
            logger.warning(
                f"provider '{provider}' 正在从 settings.yaml 读取明文 API Key，"
                f"建议改用环境变量 {env_var} 并在 .gitignore 中忽略 config/settings.yaml"
            )
            return key

        raise ValueError(
            f"未找到 provider '{provider}' 的 API Key，"
            f"请设置环境变量 {env_var} 或在 config/settings.yaml 中配置"
        )

    def get_image_model_config(self) -> dict:
        """获取图片生成模型配置"""
        return self.config.get("image_model", {
            "provider": "dashscope",
            "model": "wanx-v1",
        })

    def list_available_agents(self) -> list[str]:
        """列出所有已配置的 Agent 名称"""
        return list(self.config.get("models", {}).keys())
