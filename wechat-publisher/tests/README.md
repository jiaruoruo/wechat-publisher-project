# 测试说明

运行方式：

```bash
python -m unittest discover -s tests -v
```

大部分用例是纯标准库（无需安装运行时依赖）；`test_e2e_workflow.py`
需要 langgraph 等依赖，未安装时自动跳过（skip）。

跳过行为可感知：依赖缺失时，模块导入阶段会在 stderr 打印 `WARNING:`
开头的警告；用 pytest 运行时，结果摘要末尾还会输出独立的
「WARNING: 端到端覆盖被跳过（依赖缺失）」分隔区块（由 `tests/conftest.py` 提供），
明确告知哪些端到端用例未被执行。

## 覆盖范围

| 模块 | 用例 | 说明 |
| --- | --- | --- |
| `tools/json_utils.py` | 7 | JSON 提取：纯 JSON、代码块、JSON 前后附带说明文字、降级默认值 |
| `graph/conditions.py` | 8 | `review_condition` 三分支 + 缺失 metadata 兜底；`increment_retry` 重试计数 |
| `graph/state.py` | 5 | `create_initial_state` 默认值、metadata 合并、extra 覆盖 |
| `models/image_sizes.py` | 6 | `closest_size` 尺寸映射（DALL-E / DashScope、非法输入兜底） |
| `config/structured_logging.py` | 2 | 结构化日志 JSON 行格式 |
| `tools/notifier.py` | 4 | 桥接回调注册/注销、异常隔离 |
| `test_e2e_workflow.py` | 6 | **端到端**（需运行时依赖）：桩 LLM/网络/浏览器跑完整 LangGraph 工作流，验证 retry_counter 重试循环与 max_retries 强制通过、json_mode 只发给 JSON 契约 agent、stream/invoke 两种路径最终状态完整 |

## 说明

- 运行工作流依赖 langgraph/langchain 等第三方库，本机未安装时仅能验证纯函数与
  无依赖模块；改动后建议在安装完整依赖的环境中执行一次端到端 `python main.py run`。
- 测试均为确定性用例（无网络、无 LLM 调用）。
