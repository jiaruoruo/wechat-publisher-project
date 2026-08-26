# langgraph 0.2 → 1.2 适配整改清单

> 基于实际安装环境逐项实测（`pip install -r requirements.txt` 后的版本）：
> langgraph **1.2.11** / langchain **1.3.16** / langchain-core **1.6.0** / langchain-openai **1.6.0** / pydantic **2.13.4** / openai **3.3.1**（Python 3.14.6）
>
> 验证方式：`python -m unittest tests.test_e2e_workflow -v`（6 个端到端用例，桩 LLM/网络/浏览器跑真实 LangGraph 图）
> + 对已安装包做 `inspect.signature` / `model_fields` / 源码检索。

## 一、已实测确认兼容（无需改动）

| 用法 | 结论 |
| --- | --- |
| `StateGraph(ArticleState)` + `TypedDict(total=False)` | ✅ 1.x 继续支持；缺失字段由 `.get()` 读取 |
| `add_node / add_edge / add_conditional_edges / compile` | ✅ e2e 通过 |
| `app.invoke(state)` | ✅ 返回合并后的完整状态 dict |
| `app.stream(state)`（无参） | ✅ 默认 `stream_mode=None` → 运行时解析为 **"updates"**，事件为 `{"节点名": 该节点更新}`；`Pregel.stream` 签名已确认 |
| `START / END` 导入路径 | ✅ `from langgraph.graph import StateGraph, START, END` 不变 |
| `ChatOpenAI(model=, api_key=, base_url=, temperature=, request_timeout=, max_retries=)` | ✅ 全部合法：`model/api_key/base_url` 是 pydantic 别名（映射到 `model_name/openai_api_key/openai_api_base`），`request_timeout`/`max_retries` **未弃用**（`model_fields` 实测） |
| `SystemMessage / HumanMessage / BaseChatModel / response.content` | ✅ 不变 |
| 弃用警告 | ✅ e2e 全链路无任何 LangChain/langgraph 弃用警告（唯一 DeprecationWarning 来自 langsmith 内部 `asyncio.iscoroutinefunction`，第三方，与项目无关） |

## 二、需修改（真实问题）

### P0 — 行为 / 正确性

1. **`BaseAgent.invoke()` 的 `config={"timeout": timeout}` 是死参数**
   - 证据：langchain-core 1.6 的 `RunnableConfig` 字段实测为 `callbacks, configurable, max_concurrency, metadata, recursion_limit, run_id, run_name, tags` —— **没有 `timeout`**，该配置被静默忽略。
   - 真正生效的超时是 `llm_router._create_llm` 构造时的 `request_timeout=120`。
   - 改法：删除 `invoke()` 的 `timeout` 参数与 `config={"timeout": ...}`（或保留签名但文档化为 no-op）；如需每次调用单独超时，需按调用构造不同超时的模型实例（成本高，不建议）。

2. **ContentDB 连接从不关闭（`ResourceWarning: unclosed database`）**
   - 证据：`python -W default` 跑 e2e 时出现多条 unclosed database 警告。
   - 现状：`ArticleWorkflow.__init__`、`TopicPlannerAgent.__init__`、`HermesAgent.__init__` 各建一个 thread-local 连接，均无人 `close()`；长驻的 Hermes 进程会累积连接。
   - 改法（择一）：
     a. `ArticleWorkflow` 加 `close()`（关 `self.db`），`run()` 结束后调用；
     b. `TopicPlannerAgent` 复用 workflow 的 `db` 实例而非自己新建；
     c. `ContentDB` 用 `weakref.finalize` 兜底关闭。

3. **requirements.txt / SPEC.md 版本语义漂移**
   - 证据：`requirements.txt` 与 `SPEC.md` 写的是 `langgraph>=0.2 / langchain>=0.3 / langchain-openai>=0.2`，实际装机为 1.2.11 / 1.3.16 / 1.6.0。`>=0.2` 允许装回 0.2，而 0.2 与 1.x 的 stream 事件语义有差异，会造成同一代码两端行为不一致。
   - 改法：`langgraph>=1.0`、`langchain>=1.0`、`langchain-openai>=1.0`，并同步更新 SPEC.md（或至少在 README 标注"已在 1.2.11 / 1.3.16 / 1.6.0 验证"）。

4. **`langchain-community` 与 `langchain` 在代码中完全未使用**
   - 证据：全库检索无任何 `import langchain_community` / `import langchain`（仅用 `langchain_core` 与 `langchain_openai`）。
   - 影响：`langchain-community` 在 1.x 下会拖入 `langchain-classic`，是纯冗余。
   - 改法：从 requirements 删除；确认无隐式依赖后（跑一遍完整测试即可）。

### P1 — 健壮性（可选强化）

5. **TypedDict 全字段为 LastValue 语义，`metadata` 合并靠手写** ✅ 已整改
   - 现状：每个节点手动 `{**state.get("metadata", {}), ...}` 合并；新节点漏写即静默覆盖（`review_history` 同理）。
   - 已实现：`graph/state.py` 新增 `Annotated` 归并器
     `metadata: Annotated[dict, merge_metadata]`、`review_history: Annotated[list, append_review_history]`；
     `review_history` 提升为顶层通道，`create_initial_state` 初始化 `[]`；
     6 个 Agent 的 metadata 返回改为**部分更新**，由 schema 归并；
     `workflow.run()` 的 stream 累积复用同一组 `CHANNEL_REDUCERS`（单一来源，不漂移）。
   - 验证：e2e 断言 metadata 完整（max_retries + 各节点字段共存）、
     review_history 在 invoke 与 stream 两种路径下都正确追加；40 个测试全绿。

6. **`recursion_limit` 与重试循环的耦合** ✅ 已整改（结论已修正）
   - **勘误**：初版误以为默认 recursion_limit=25（langchain-core 常量）。实测 **langgraph 1.2 默认是 10007**
     （`langgraph/_internal/_config.py: DEFAULT_RECURSION_LIMIT = int(getenv("LANGGRAPH_DEFAULT_RECURSION_LIMIT", "10007"))`），
     常规 max_retries 根本不会触顶（无限循环图在默认配置下跑完 1000 次验证过）。
   - 已实现（作为加固而非修 bug）：`workflow.run()` 按节点数显式传入
     `config={"recursion_limit": max(25, 2*(6+4*max_retries))}`，理由：
     ① 资源上限确定化，不依赖环境变量/版本默认值；② 失控循环 bug 时快速失败，
     避免烧掉上万次真实 LLM 调用（e2e 用例 `test_recursion_limit_guards_runaway_loop` 验证）。
   - `config/validation.py`：`max_retries` 非负整数校验（error）；超过 4 时告警改为**成本提示**
     （每轮重试 = 重写+配图+审核一轮 LLM/API 调用），不再引用递归上限（避免误导）。

7. **stream 事件的 None 更新守卫（已就位，保持）**
   - langgraph 1.x 对"返回了 schema 未声明键"的节点会产生 `{"节点": None}` 事件；`graph/workflow.py` 已加 `if node_state:` 守卫。后续新增节点返回未声明键时不会崩。

### P2 — 可选现代化（非必须）

8. 迁移 langgraph 1.x 新特性：
   - `langgraph.types` / `Annotation` 状态注解（配合第 5 项）；
   - `checkpointer` 持久化（当前每次运行都是新编译图，无跨运行状态，短期不需要）；
   - `stream_mode` 多模式组合（`["updates", "messages"]`）做更细粒度进度上报。

## 三、验证步骤

```bash
pip install -r requirements.txt          # 已在 Python 3.14.6 安装成功
python -m unittest discover -s tests -v   # 39 个用例（33 个纯 stdlib + 6 个端到端）
python -W default -m unittest tests.test_e2e_workflow -v   # 检查弃用/资源警告
python main.py check                      # 配置校验输出（注意 Windows GBK 控制台勿用 emoji）
```

## 四、建议执行顺序

1. P0-1（删死参数）✅ → 2. P0-2（关连接）✅ → 3. P0-3/4（版本与依赖清理）✅ → 4. P1-5（metadata 归并）✅ → 5. P1-6（递归限制显式化）✅ → 6. P2-8（按需）。
