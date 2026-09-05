# WeChat Multi-Agent System - 技术规格说明书

> 微信公众号内容自动生产与自动发布多 Agent 协作系统

## 1. 系统概述

本系统实现了一套完整的微信公众号内容自动化生产与发布流程，采用多 Agent 协作架构，通过 LangGraph StateGraph 编排 6 个专业化 AI Agent 的工作流，结合 Playwright 浏览器自动化实现公众号后台操作。系统新增 Hermes Agent 模块，通过飞书机器人提供人机交互接口，支持远程任务触发、状态查询和通知推送。

### 1.1 核心能力

- **自动选题**：基于热点搜索和历史内容分析，自动生成选题方案
- **内容创作**：AI 驱动的文章撰写，支持审核反馈重写
- **配图生成**：自动解析文内图片标记，生成封面图和插图
- **质量审核**：5 维度自动审核（合规性/准确性/文字质量/标题吸引力/可读性）
- **排版美化**：Markdown 转公众号兼容 HTML，内联 CSS + 3 套预设模板
- **自动发布**：Playwright 浏览器自动化，支持草稿/群发模式
- **飞书管控**：通过 Hermes Agent 在飞书群中远程管理和监控系统

### 1.2 技术栈

| 层级 | 技术选型 |
|------|----------|
| 工作流编排 | LangGraph StateGraph |
| LLM 路由 | OpenAI / DeepSeek / DashScope（可配置） |
| 图片生成 | DALL-E 3 / 通义万相 wanx-v1 |
| 浏览器自动化 | Playwright (Chromium) |
| 定时调度 | APScheduler (cron) |
| 数据存储 | SQLite + 本地文件系统 |
| 通知渠道 | 日志文件 + Email (SMTP) + 企业微信 Webhook + 飞书卡片 |
| 人机交互 | 飞书机器人 (Hermes Agent) |

---

## 2. 项目结构

```
wechat-publisher/
├── main.py                          # CLI 主入口（6 个子命令）
├── scheduler.py                     # APScheduler 定时调度器
├── requirements.txt                 # Python 依赖（13 个包）
│
├── agents/                          # 6 个专业化 Agent
│   ├── __init__.py
│   ├── base.py                      # BaseAgent 抽象基类
│   ├── topic_planner.py             # 选题策划 Agent
│   ├── content_writer.py            # 内容创作 Agent
│   ├── image_generator.py           # 配图生成 Agent
│   ├── reviewer.py                  # 审核校对 Agent
│   ├── formatter.py                 # 排版美化 Agent
│   └── publisher.py                 # 发布执行 Agent
│
├── models/                          # 模型层
│   ├── __init__.py
│   ├── llm_router.py                # 多模型路由器
│   └── image_model.py               # 图片生成模型封装
│
├── graph/                           # 工作流编排
│   ├── __init__.py
│   ├── state.py                     # ArticleState TypedDict
│   ├── workflow.py                  # ArticleWorkflow 主工作流
│   └── conditions.py                # 审核条件分支逻辑
│
├── browser/                         # 浏览器自动化
│   ├── __init__.py
│   ├── wechat_session.py            # Playwright 会话管理（Cookie 持久化）
│   └── publish_actions.py           # 公众号后台操作封装
│
├── hermes/                          # Hermes Agent（飞书接口层）
│   ├── __init__.py
│   ├── agent.py                     # HermesAgent 核心逻辑
│   ├── feishu_client.py             # 飞书 API 客户端
│   ├── webhook_server.py            # 飞书事件回调 Webhook 服务器
│   └── commands.py                  # 飞书命令解析器
│
├── tools/                           # 工具集
│   ├── __init__.py
│   ├── web_search.py                # DuckDuckGo 网络搜索
│   ├── content_db.py                # SQLite 内容存储
│   ├── notifier.py                  # 多通道通知系统
│   └── wechat_api.py                # 公众号 API（备用）
│
├── config/                          # 配置文件
│   ├── settings.yaml                # 全局配置
│   └── prompts/                     # Agent Prompt 模板
│       ├── topic_planner.yaml
│       ├── content_writer.yaml
│       ├── image_generator.yaml
│       ├── reviewer.yaml
│       ├── formatter.yaml
│       └── publisher.yaml
│
└── storage/                         # 运行时存储（自动创建）
    ├── browser_session.json         # 浏览器 Cookie
    ├── articles.db                  # SQLite 数据库
    ├── screenshots/                 # 发布前截图
    └── images/                      # 生成的图片
```

**文件统计**：38 个文件（30 个 Python + 7 个 YAML + 1 个 TXT）

---

## 3. 核心 Agent 设计

### 3.1 ArticleState（共享状态）

所有 Agent 通过 `ArticleState` TypedDict 共享数据：

```python
class ArticleState(TypedDict):
    topic: str                    # 选题标题
    outline: str                  # 文章大纲
    target_audience: str          # 目标受众描述
    content: str                  # Markdown 格式正文
    article_title: str            # 文章标题（最终版）
    summary: str                  # 文章摘要
    cover_prompt: str             # 封面图生成提示词
    cover_image_path: str         # 封面图本地路径
    inline_images: list[str]      # 文内插图路径列表
    review_result: dict[str, Any] # {"passed": bool, "feedback": str, "score": int}
    retry_count: int              # 当前重写次数
    formatted_html: str           # 公众号兼容的富文本 HTML
    publish_result: dict[str, Any]  # {"success": bool, "url": str, "error": str}
    metadata: dict[str, Any]      # 额外信息
```

### 3.2 Agent 模型配置

| Agent | Provider | Model | Temperature | 设计考量 |
|-------|----------|-------|-------------|----------|
| TopicPlanner | openai | gpt-4o | 0.9 | 高温度激发创意 |
| ContentWriter | openai | gpt-4o | 0.7 | 兼顾创造力与准确性 |
| ImageGenerator | openai | gpt-4o | 0.3 | 提示词翻译需精确 |
| Reviewer | openai | gpt-4o | 0.2 | 严谨客观避免误判 |
| Formatter | openai | gpt-4o | 0.3 | 格式转换需确定性 |
| Publisher | openai | gpt-4o | 0.3 | 发布清单需确定性 |

图片生成模型：通义万相 `wanx-v1`（DashScope），可切换 DALL-E 3（OpenAI）

### 3.3 Agent 详细说明

#### TopicPlannerAgent（选题策划）
- 集成 `web_search`（DuckDuckGo）获取热点话题
- 集成 `content_db` 查询历史文章避免重复
- 输出 JSON 格式：`{topic, outline, target_audience}`
- 带降级处理（JSON 解析失败时使用默认值）

#### ContentWriterAgent（内容创作）
- 基于选题和大纲创作 Markdown 格式文章
- 支持基于审核反馈的重写模式（接收 `review_result.feedback`）
- 自动提取 Markdown 内容（去除代码块标记）
- 字数范围可配置（默认 1500-3000 字）

#### ImageGeneratorAgent（配图生成）
- 解析文内 `[IMAGE: ...]` 标记
- 使用 LLM 增强中文提示词为英文
- 封面图尺寸 900×383（2.35:1），插图尺寸 1024×768
- 图片保存为 PNG；文内图片以 {{IMAGE_PATH_N}} 占位符保留，发布时上传微信 CDN 外链（失败回退 base64）

#### ReviewerAgent（审核校对）
- 5 维度审核：合规性、准确性、文字质量、标题吸引力、可读性
- 输出 JSON 审核结果：`{passed, feedback, score}`
- 管理 `retry_count` 计数器
- 审核不通过时提供具体修改建议

#### FormatterAgent（排版美化）
- LLM 驱动排版 + 本地降级方案
- 3 套预设模板：`simple`（简洁）、`business`（商务）、`lively`（活泼）
- 内联 CSS 样式（公众号兼容）
- 图片占位符 {{IMAGE_PATH_N}} 原样保留（发布阶段解析为微信 CDN 外链，见 browser/image_uploader.py）

#### PublisherAgent（发布执行）
- Playwright 浏览器自动化操作公众号后台
- 发布前截图预览（`preview_before_publish`）
- Cookie 过期时发送通知
- 发布结果通知（含截图路径）
- 支持 `draft`（草稿）和 `publish`（群发）模式

---

## 4. 工作流编排

### 4.1 LangGraph StateGraph

```
START → topic_planner → content_writer → image_generator → reviewer
                                                                │
                                                    ┌───────────┼───────────┐
                                                    │           │           │
                                                approved    rejected    max_retries
                                                    │           │           │
                                                formatter   content_writer  formatter
                                                    │                       │
                                                publisher               publisher
                                                    │                       │
                                                   END                     END
```

### 4.2 条件分支逻辑

```python
def review_condition(state) -> str:
    if passed:           return "approved"     # → formatter → publisher
    if retry >= max:     return "max_retries"  # → formatter → publisher（强制）
    else:                return "rejected"     # → content_writer（重写）
```

- 最大重试次数可配置（默认 2 次）
- 达到上限后强制进入排版阶段，同时发送通知

### 4.3 工作流完成后

1. 文章数据保存到 SQLite（articles 表 + review_logs 表）
2. 检查审核是否达到最大重试次数，发送通知
3. 输出执行摘要日志

---

## 5. 浏览器自动化

### 5.1 WechatSession（会话管理）

- 基于 Playwright Chromium 浏览器
- `storage_state` 持久化 Cookie 到 JSON 文件
- `check_login()` 检测登录状态（通过 URL 判断）
- `login_interactive()` 扫码登录（等待用户操作，超时可配置）

### 5.2 PublishActions（后台操作）

- **多种选择器回退策略**：适配公众号后台 DOM 变更；选择器清单外置在 `config/selectors.yaml`（改版只需改配置），支持 `WECHAT_SELECTORS_FILE` 环境变量或 `wechat.selectors_file` 运行时覆盖，长驻进程可调 `wechat_selectors.reload_selectors()` 热加载
- **3 种内容注入方式**：
  1. 编辑器 API（`window.editor`）
  2. 剪贴板粘贴（`navigator.clipboard`）
  3. innerHTML 直接注入
- **发布前截图**：`preview_before_publish()` 保存预览截图（按日期归档 + 时间戳后缀）
- **截图保留策略**：`browser.screenshot_retention_days`（默认 30 天，0 = 禁用）每次发布前清理过期日期目录，防止截图无限堆积
- **草稿/发布双模式**：可配置仅保存草稿或直接群发

---

## 6. Hermes Agent（飞书接口层）

### 6.1 架构

```
飞书用户 ──(消息)──> 飞书服务器 ──(webhook)──> Hermes Agent
                                                  │
     ┌────────────────────────────────────────────┘
     │  解析命令 → 触发工作流 / 查询状态 / 推送通知
     │
     ├── /start [主题]  → 后台线程启动 ArticleWorkflow
     ├── /status        → 查看工作流进度（卡片消息）
     ├── /history       → 查询最近 30 天文章
     ├── /config        → 查看配置摘要
     ├── /login         → 提醒重新登录公众号
     ├── /pause         → 暂停定时调度
     ├── /resume        → 恢复定时调度
     ├── /help          → 帮助信息
     │
     └── 通知桥接：Cookie过期/发布结果/审核失败 → 飞书卡片推送
```

### 6.2 组件说明

#### HermesAgent（`hermes/agent.py`）
- 核心调度器，管理 Webhook 服务器和通知桥接
- 工作流状态跟踪（`_workflow_status` dict + `_status_lock`）
- 后台线程执行工作流，避免阻塞 Webhook
- 命令路由分发（`_dispatch_command` → handler map）
- Notifier → 飞书通知桥接（monkey-patch `_send_notification`）

#### FeishuClient（`hermes/feishu_client.py`）
- `tenant_access_token` 自动获取与刷新（提前 5 分钟）
- 发送文本消息 / 卡片消息 / 回复消息
- 3 种卡片消息构造方法：
  - `build_status_card()` — 状态卡片（标题+状态+详情+颜色）
  - `build_workflow_card()` — 进度卡片（6 Agent 进度条）
  - `build_result_card()` — 结果卡片（成功/失败+详情）

#### FeishuWebhookServer（`hermes/webhook_server.py`）
- 基于 `http.server` 的轻量 HTTP 服务器
- 后台守护线程运行
- 支持飞书事件 v1.0 和 v2.0 格式
- URL 验证挑战（`url_verification`）
- `verification_token` 安全验证
- 卡片交互回调处理（`card_action`）
- `/health` 健康检查端点

#### CommandParser（`hermes/commands.py`）
- 正则匹配解析 9 种命令
- 输出 `ParsedCommand` 数据类（action + params + 来源信息）
- 提供 `get_help_text()` 帮助文档

### 6.3 飞书命令参考

| 命令 | 动作 | 响应类型 |
|------|------|----------|
| `/start` | 触发工作流（自动选题） | 确认卡片 → 进度卡片 → 结果卡片 |
| `/start <主题>` | 触发工作流（指定主题） | 确认卡片 → 进度卡片 → 结果卡片 |
| `/status` | 查看当前任务状态 | 进度卡片 / 结果卡片 / 空闲状态卡片 |
| `/history` | 最近 30 天文章列表 | 文本消息 |
| `/config` | 查看配置摘要 | 文本消息 |
| `/login` | 提醒重新登录 | 文本消息（含 CLI 命令） |
| `/pause` | 暂停定时调度 | 确认文本 |
| `/resume` | 恢复定时调度 | 确认文本 |
| `/help` | 显示帮助 | 命令列表文本 |

### 6.4 飞书卡片消息类型

| 卡片类型 | 触发时机 | 内容 |
|----------|----------|------|
| Status Card | 启动/错误/空闲 | 标题 + 状态 + 详情 + 颜色头 |
| Workflow Card | 工作流执行中 | 文章标题 + 当前阶段 + 6 Agent 进度 |
| Result Card | 工作流完成 | 标题 + 成功/失败 + 发布详情 |
| Notification Bridge | Cookie 过期/审核失败 | 从 Notifier 自动转发 |

### 6.5 通知桥接机制

Hermes Agent 通过 monkey-patch 将 Notifier 的 `_send_notification` 方法桥接到飞书：

```python
def _setup_notification_bridge(self):
    original_send = self.notifier._send_notification
    def bridge_send(title, message, level="info"):
        original_send(title, message, level)  # 原有逻辑（日志/邮件/企微）
        card = FeishuClient.build_status_card(...)
        self.feishu.send_card_to_default(card)  # 额外发送到飞书
    self.notifier._send_notification = bridge_send
```

---

## 7. 工具集

### 7.1 WebSearch（网络搜索）
- 基于 DuckDuckGo，无需 API Key
- 用于选题策划阶段获取热点话题

### 7.2 ContentDB（内容存储）
- SQLite 数据库，包含 `articles` 表和 `review_logs` 表
- `save_article()` — 保存文章记录
- `update_article_status()` — 更新发布状态
- `save_review_log()` — 保存审核日志
- `get_recent_titles()` — 查询最近文章标题
- `get_article_count()` — 统计文章数量（用于每日上限检查）

### 7.3 Notifier（通知系统）
- 3 通道通知：日志文件 + Email (SMTP_SSL) + 企业微信 Webhook
- 专用方法：
  - `notify_cookie_expired()` — Cookie 过期通知
  - `notify_publish_result()` — 发布结果通知
  - `notify_review_failed()` — 审核失败通知

### 7.4 LLMRouter（模型路由）
- 根据 Agent 名称从 YAML 配置加载对应 LLM 实例
- 支持 OpenAI / DeepSeek / DashScope 三种 Provider
- API Key 优先从环境变量读取，回退到配置文件
- LLM 实例缓存，避免重复创建

---

## 8. 配置说明

### 8.1 全局配置（config/settings.yaml）

| 配置段 | 说明 |
|--------|------|
| `wechat` | 公众号名称、发布模式（draft/publish）、图片外链、发布锁等待超时 `publish_lock_timeout`（FIFO 公平排队） |
| `content` | 内容领域、关键词、字数范围、风格、模板 |
| `models` | 6 个 Agent 的 provider/model/temperature |
| `image_model` | 图片生成模型配置 |
| `api_keys` | API 密钥（建议用环境变量） |
| `review` | 审核配置（max_retries, min_score） |
| `schedule` | 定时调度（cron 表达式、时区、每日上限） |
| `browser` | 浏览器配置（headless、session 文件路径） |
| `storage` | 存储路径（数据库、图片目录） |
| `notification` | 通知配置（邮件、企业微信） |
| `feishu` | 飞书机器人配置（Hermes Agent） |

### 8.2 飞书配置（feishu 段）

| 字段 | 说明 |
|------|------|
| `app_id` | 飞书开放平台 App ID |
| `app_secret` | 飞书开放平台 App Secret |
| `verification_token` | 事件订阅 Verification Token |
| `encrypt_key` | 事件订阅 Encrypt Key（可选） |
| `default_chat_id` | 默认通知群聊 ID |
| `webhook_host` | Webhook 监听地址（默认 127.0.0.1，建议经反向代理对外） |
| `webhook_port` | Webhook 监听端口（默认 9000） |
| `webhook_allow_from` | IP 白名单（IP 或 CIDR，空 = 不限制；可用环境变量 FEISHU_WEBHOOK_ALLOW_FROM 覆盖） |
| `require_encrypt` | 强制事件加密（true 时未加密请求一律拒绝；可用环境变量 HERMES_REQUIRE_ENCRYPT=1 开启） |

---

## 9. CLI 命令

| 命令 | 说明 | 关键参数 |
|------|------|----------|
| `python main.py run` | 立即执行一次完整工作流 | `--topic`, `--title` |
| `python main.py --run-now` | 快捷执行（等同于 run） | — |
| `python main.py login` | 交互式扫码登录公众号 | `--timeout` |
| `python main.py schedule` | 启动 APScheduler 定时调度 | — |
| `python main.py hermes` | 启动 Hermes Agent（飞书接口） | `--port` |
| `python main.py check` | 检查配置和登录状态 | — |

---

## 10. 数据流

### 10.1 文章生产流程

```
1. TopicPlanner
   输入: (配置中的 domain/keywords)
   工具: web_search, content_db
   输出: topic, outline, target_audience

2. ContentWriter
   输入: topic, outline, target_audience
   输出: content (Markdown), article_title, summary

3. ImageGenerator
   输入: content (解析 [IMAGE: ...] 标记)
   工具: image_model (DashScope/OpenAI)
   输出: cover_image_path, inline_images[]

4. Reviewer
   输入: content, article_title
   输出: review_result {passed, feedback, score}
   分支: approved → Formatter / rejected → ContentWriter / max_retries → Formatter

5. Formatter
   输入: content, cover_image_path, inline_images
   输出: formatted_html (内联 CSS + {{IMAGE_PATH_N}} 占位符)

6. Publisher
   输入: formatted_html, article_title, cover_image_path, inline_images
   工具: WechatSession, PublishActions, image_uploader
   说明: 注入前把 {{IMAGE_PATH_N}} 解析为微信 CDN 外链（wechat.external_images=true；
        逐张独立兜底——上传失败回退 base64，已成功的 CDN URL 保留）
        封面图走同一 CDN 链路：先上传拿 URL → 作为正文首图注入 → 「从正文选择」设为封面，
        失败回退旧的文件上传流程；
        wechat.cover_in_body=false 时：封面仍走 CDN 上传拿 URL，但不注入正文、不用「从正文选择」，
        设置封面直接走旧的文件上传
   输出: publish_result {success, url, error}
   通知: Notifier (Cookie 过期/发布结果)
```

### 10.2 Hermes Agent 交互流程

```
用户在飞书发送 /start AI趋势
  → 飞书服务器 webhook 回调
    → WebhookServer 接收事件
      → CommandParser 解析为 ParsedCommand(action="start", topic="AI趋势")
        → HermesAgent._cmd_start()
          → 回复确认卡片
          → 启动后台线程执行 ArticleWorkflow
            → 工作流各阶段推送进度卡片
            → 完成后推送结果卡片
```

---

## 11. 依赖清单

```
langgraph>=1.0          # 工作流编排（已在 1.2.11 验证）
langchain-core>=1.0     # LLM 框架核心（LangChain 1.x）
langchain-openai>=1.0   # OpenAI 集成
playwright>=1.40        # 浏览器自动化
dashscope>=1.14         # 通义千问/万相
openai>=1.0             # OpenAI API
apscheduler>=3.10       # 定时调度
markdown>=3.5           # Markdown 解析
beautifulsoup4>=4.12    # HTML 解析
pyyaml>=6.0             # YAML 配置
pycryptodome>=3.19      # 飞书事件订阅加密（encrypt_key）解密
Pillow>=10.0            # 图片处理
requests>=2.28          # HTTP 请求（飞书 API）
```

> 注：`langchain` 主包与 `langchain-community` 已从依赖中移除（代码仅使用
> `langchain_core` / `langchain_openai`）；`sqlite-utils` 未使用（内置 `sqlite3`）。

---

## 12. 安全与容错

| 机制 | 说明 |
|------|------|
| Cookie 持久化 | `storage_state` 保存登录态，避免重复扫码 |
| Cookie 过期通知 | 检测到登录失效时多渠道通知 |
| 审核回退 | 最多 2 次重写，达到上限强制发布 + 通知 |
| 每日发布上限 | 调度器检查当日文章数，超限跳过 |
| 发布前预览 | 截图保存，便于事后检查 |
| 草稿模式 | 默认仅保存草稿，不直接群发 |
| 飞书 Token 自动刷新 | 提前 5 分钟刷新 tenant_access_token |
| Webhook 安全验证 | verification_token 校验请求来源 |
| 工作流互斥 | Hermes Agent 通过 `_status_lock` 防止并发执行 |

---

*文档版本：v2.0 | 更新日期：2026-08-13 | 项目路径：d:\AI\qoder-workspace\wechat-publisher*
