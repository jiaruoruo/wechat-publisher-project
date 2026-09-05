# AGENTS.md

本仓库是微信公众号自动发布项目（多智能体 + LangGraph 工作流 + 浏览器自动化）。
全部应用代码位于子目录 `wechat-publisher/`。

## 权威规格

- 架构、状态契约与模块边界的权威说明：`wechat-publisher/SPEC.md`
- LangGraph 1.x 适配决策记录：`wechat-publisher/LANGGRAPH_1X_ADAPTATION.md`

## 验证命令（变更后必跑）

任何代码变更后的唯一验证命令：

```bash
cd wechat-publisher && python -m pytest
```

要求：必须在 `wechat-publisher/` 目录下运行，才能使 `wechat-publisher/pytest.ini`（testpaths=tests）生效；从仓库根直接运行时该配置不会被加载（rootdir 落在仓库根，仅靠默认递归发现测试）。
PowerShell（不支持 `&&`）等价写法：

```powershell
cd wechat-publisher; python -m pytest
```

测试均为确定性测试（无网络、无真实 LLM 调用）。
注意：`tests/test_e2e_workflow.py` 在 langgraph 等依赖未安装时会自动跳过，详见 `wechat-publisher/tests/README.md`。

## 敏感文件（禁止提交、禁止读取内容写入报告）

- `wechat-publisher/.env` — API Key 等机密（已被 .gitignore 排除）
- `wechat-publisher/config/settings.yaml` — 可能含明文配置（已被 .gitignore 排除）
- 参考模板：`wechat-publisher/.env.example`（当前同样被 .gitignore 的 `.env.*` 规则排除，未入库）

## 发布入口

- `wechat-publisher/publish.ps1`（PowerShell）/ `wechat-publisher/publish.bat`
- 流程：配置体检（`python main.py check`）→ 登录检查（`wechat-publisher/storage/browser_session.json`）→ 发布（`python main.py run`）
- 首次使用需先登录：`python main.py login`
