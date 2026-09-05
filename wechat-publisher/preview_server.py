"""预览入口 - 为无前端的公众号自动化项目提供一个实时状态页。

复用项目自带的 Hermes Webhook 服务器（hermes/webhook_server.py）：
- 保留 /health 健康检查和飞书 webhook POST 处理逻辑不变
- 新增 / 返回一个自包含的 HTML 状态面板（实时轮询 /health）

仅使用 Python 标准库，因此无需安装 langgraph/langchain/playwright
等完整依赖栈即可预览。
"""

import os
import re

from hermes.webhook_server import DaemonThreadingHTTPServer, FeishuWebhookHandler
from config.paths import BASE_DIR

HOST = "127.0.0.1"
PORT = 9000

_DASHBOARD_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WeChat Multi-Agent System · 预览</title>
<style>
  :root { --bg:#0f1420; --card:#1a2233; --fg:#e6e9f0; --muted:#8b93a7; --accent:#4f8cff; --ok:#2ecc71; --err:#ff5f56; }
  * { box-sizing: border-box; }
  body { margin:0; font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif; background:var(--bg); color:var(--fg); line-height:1.6; }
  .wrap { max-width: 860px; margin: 0 auto; padding: 40px 20px 60px; }
  header h1 { font-size:1.6rem; margin:0 0 4px; }
  header .sub { color:var(--muted); font-size:.9rem; margin-bottom:24px; }
  .card { background:var(--card); border:1px solid #26304a; border-radius:12px; padding:20px 22px; margin-bottom:18px; }
  .card h2 { margin:0 0 14px; font-size:1.05rem; }
  .badge { display:inline-flex; align-items:center; gap:8px; padding:4px 12px; border-radius:999px; font-size:.85rem; font-weight:600; }
  .badge .dot { width:9px; height:9px; border-radius:50%; background:var(--err); }
  .badge.ok { background:rgba(46,204,113,.12); color:var(--ok); }
  .badge.ok .dot { background:var(--ok); box-shadow:0 0 8px var(--ok); }
  .badge.err { background:rgba(255,95,86,.12); color:var(--err); }
  table { width:100%; border-collapse: collapse; font-size:.92rem; }
  td { padding:7px 0; border-bottom:1px solid #232c42; }
  td:first-child { color:var(--muted); width:200px; }
  code { background:#0c1220; border:1px solid #26304a; padding:1px 7px; border-radius:6px; font-size:.85rem; }
  .cmds { display:grid; grid-template-columns: 1fr 1fr; gap:8px 24px; font-size:.9rem; }
  .cmds div { padding:6px 0; border-bottom:1px solid #232c42; }
  .cmds b { color:#7fb2ff; font-family: ui-monospace, Menlo, monospace; }
  .stages { display:flex; flex-wrap:wrap; gap:8px; }
  .stage { background:#0c1220; border:1px solid #26304a; border-radius:8px; padding:6px 12px; font-size:.85rem; }
  .stage::before { content:"→ "; color:var(--accent); }
  .foot { color:var(--muted); font-size:.8rem; margin-top:24px; text-align:center; }
  .json { background:#0c1220; border:1px solid #26304a; border-radius:8px; padding:10px 14px; font-family: ui-monospace, Menlo, monospace; font-size:.85rem; overflow-x:auto; white-space:pre; }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>🤖 WeChat Multi-Agent System</h1>
    <div class="sub">微信公众号内容自动生产与发布 · Hermes Agent 预览</div>
  </header>

  <div class="card">
    <h2>服务状态</h2>
    <span class="badge err" id="badge"><span class="dot"></span><span id="statusText">检测中…</span></span>
    <div style="color:var(--muted); font-size:.85rem; margin-top:10px;">最后检查：<span id="checkedAt">—</span></div>
    <div class="json" id="healthJson" style="margin-top:12px;">…</div>
  </div>

  <div class="card">
    <h2>配置摘要 <span style="color:var(--muted);font-weight:400;font-size:.8rem;">(config/settings.yaml)</span></h2>
    <table>
      <tr><td>内容领域</td><td>__DOMAIN__</td></tr>
      <tr><td>排版模板</td><td>__TEMPLATE__</td></tr>
      <tr><td>发布模式</td><td>__PUBLISH_MODE__</td></tr>
      <tr><td>Webhook 监听</td><td>__WEBHOOK_HOST__:__WEBHOOK_PORT__</td></tr>
      <tr><td>健康检查</td><td><a href="/health" style="color:var(--accent)">/health</a></td></tr>
    </table>
  </div>

  <div class="card">
    <h2>工作流（6 个 Agent）</h2>
    <div class="stages">
      <span class="stage">选题策划</span>
      <span class="stage">内容创作</span>
      <span class="stage">配图生成</span>
      <span class="stage">审核校对</span>
      <span class="stage">排版美化</span>
      <span class="stage">发布执行</span>
    </div>
  </div>

  <div class="card">
    <h2>飞书命令参考</h2>
    <div class="cmds">
      <div><b>/start [主题]</b> 触发工作流</div>
      <div><b>/status</b> 查看任务状态</div>
      <div><b>/history</b> 最近 30 天文章</div>
      <div><b>/config</b> 配置摘要</div>
      <div><b>/login</b> 提醒重新登录</div>
      <div><b>/pause</b> 暂停定时调度</div>
      <div><b>/resume</b> 恢复定时调度</div>
      <div><b>/help</b> 显示帮助</div>
    </div>
  </div>

  <div class="foot">预览由项目自带的 Hermes Webhook 服务器驱动（hermes/webhook_server.py） · 端口 __WEBHOOK_PORT__</div>
</div>
<script>
  const badge = document.getElementById('badge');
  const statusText = document.getElementById('statusText');
  const checkedAt = document.getElementById('checkedAt');
  const healthJson = document.getElementById('healthJson');
  async function check() {
    try {
      const r = await fetch('/health', { cache: 'no-store' });
      const data = await r.json();
      const ok = r.ok && data.status === 'ok';
      badge.className = 'badge ' + (ok ? 'ok' : 'err');
      statusText.textContent = ok ? '运行中 · ' + data.service : '异常';
      healthJson.textContent = JSON.stringify(data, null, 2);
    } catch (e) {
      badge.className = 'badge err';
      statusText.textContent = '离线';
      healthJson.textContent = '无法连接 /health：' + e;
    }
    checkedAt.textContent = new Date().toLocaleTimeString('zh-CN');
  }
  check();
  setInterval(check, 3000);
</script>
</body>
</html>
"""


def _read_settings() -> str:
    """读取 config/settings.yaml 原文（仅用于展示，不解析密钥）。"""
    path = os.path.join(BASE_DIR, "config", "settings.yaml")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def _yaml_value(text: str, dotted: str) -> str:
    """从简单的两级 YAML 中提取 `section.key` 的值（够用即可，不引入 pyyaml）。"""
    section = None
    for line in text.splitlines():
        stripped = line.rstrip()
        if not stripped.strip() or stripped.lstrip().startswith("#"):
            continue
        if not stripped.startswith((" ", "\t")):
            section = stripped.split(":", 1)[0].strip()
            continue
        m = re.match(r"^\s+([\w_]+):\s*(.*)$", stripped)
        if m and section:
            key = m.group(1)
            val = re.sub(r"\s+#.*$", "", m.group(2)).strip().strip("\"'")
            if f"{section}.{key}" == dotted:
                return val
    return ""


def build_dashboard() -> str:
    text = _read_settings()
    domain = _yaml_value(text, "content.domain") or "未设置"
    template = _yaml_value(text, "content.template") or "simple"
    publish_mode = _yaml_value(text, "wechat.publish_mode") or "draft"
    webhook_port = _yaml_value(text, "feishu.webhook_port") or str(PORT)
    webhook_host = _yaml_value(text, "feishu.webhook_host") or HOST

    return (
        _DASHBOARD_TEMPLATE
        .replace("__DOMAIN__", domain)
        .replace("__TEMPLATE__", template)
        .replace("__PUBLISH_MODE__", publish_mode)
        .replace("__WEBHOOK_HOST__", webhook_host)
        .replace("__WEBHOOK_PORT__", webhook_port)
    )


class PreviewHandler(FeishuWebhookHandler):
    """继承项目的 Webhook Handler，仅在其基础上增加 / 的 HTML 面板。"""

    on_event = None
    verification_token = ""
    encrypt_key = ""

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            body = build_dashboard().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            super().do_GET()


def main():
    # 用 ThreadingHTTPServer：慢 POST（飞书 webhook）不再阻塞 /health 轮询；
    # daemon_threads=True 确保 Ctrl+C 退出时请求处理线程不阻塞。
    server = DaemonThreadingHTTPServer((HOST, PORT), PreviewHandler)
    print(f"Preview dashboard: http://{HOST}:{PORT}/", flush=True)
    print(f"Health check:      http://{HOST}:{PORT}/health", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
