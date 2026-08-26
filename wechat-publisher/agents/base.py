"""BaseAgent 抽象基类 - 所有 Agent 的公共接口"""

import os
import logging
from abc import ABC, abstractmethod
from typing import Any

import yaml
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import SystemMessage, HumanMessage

from models.llm_router import LLMRouter
from graph.state import ArticleState
from config.structured_logging import api_event
from config.paths import BASE_DIR

logger = logging.getLogger(__name__)

# 懒绑定哨兵：区分"尚未尝试 bind"与"已确认不支持 json_mode（None）"
_UNSET = object()


class BaseAgent(ABC):
    """Agent 抽象基类"""

    # 子类必须设置
    agent_name: str  # 对应 settings.yaml 中的模型配置键名
    prompt_file: str  # 对应 config/prompts/ 下的 prompt 文件名

    def __init__(self, llm_router: LLMRouter, config: dict):
        self.llm_router = llm_router
        self.config = config
        self.llm: BaseChatModel = llm_router.get_llm(self.agent_name)
        self.system_prompt = self._load_prompt()

        # json_mode（response_format）懒绑定并缓存：首次需要时才 bind 一次，
        # 之后复用，避免每次 invoke 都新建 Runnable（如 reviewer 等 JSON 契约 Agent）。
        self._llm_json: BaseChatModel | None | object = _UNSET

    def _load_prompt(self) -> str:
        """从 YAML 文件加载 prompt 模板"""
        prompt_path = os.path.join(
            BASE_DIR, "config", "prompts", self.prompt_file
        )
        with open(prompt_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return data.get("system_prompt", "")

    @abstractmethod
    def run(self, state: ArticleState) -> dict:
        """
        执行 Agent 逻辑，返回需要更新的状态字段

        Args:
            state: 当前全局状态

        Returns:
            dict: 需要合并到全局状态的字段
        """
        ...

    def invoke(self, prompt: str, json_mode: bool = False) -> str:
        """调用 LLM 并返回文本结果

        Args:
            prompt: 用户消息
            json_mode: 强制模型输出 JSON（response_format），降低 JSON 解析失败率

        注意：请求超时由 LLM 实例构造时的 request_timeout 控制
        （见 models/llm_router.py，默认 120s）。langchain-core 的
        RunnableConfig 没有 timeout 字段，per-call 超时参数是无效的，
        故不再提供。
        """
        messages = [
            SystemMessage(content=self.system_prompt),
            HumanMessage(content=prompt),
        ]
        llm = self.llm
        if json_mode:
            # 懒绑定并缓存：首次需要 json 时才 bind 一次，之后复用。
            if self._llm_json is _UNSET:
                try:
                    self._llm_json = self.llm.bind(
                        response_format={"type": "json_object"}
                    )
                except Exception as e:
                    logger.debug(f"json_mode 不可用，降级为文本输出: {e}")
                    self._llm_json = None
            # 非 JSON 契约 Agent（如 content_writer）从不进入此分支，bind 永不被调用
            llm = self._llm_json or self.llm

        import time as _time
        _start = _time.time()
        response = llm.invoke(messages)
        _elapsed = (_time.time() - _start) * 1000
        api_event(
            provider=self.llm_router.config.get("models", {}).get(self.agent_name, {}).get("provider", "unknown"),
            call=self.agent_name,
            duration_ms=round(_elapsed, 1),
            status="ok",
        )
        return response.content

    def __call__(self, state: ArticleState) -> dict:
        """LangGraph 节点调用入口"""
        logger.info(f"[{self.agent_name}] 开始执行...")
        try:
            result = self.run(state)
            logger.info(f"[{self.agent_name}] 执行完成")
            return result
        except Exception as e:
            logger.error(f"[{self.agent_name}] 执行失败: {e}")
            raise