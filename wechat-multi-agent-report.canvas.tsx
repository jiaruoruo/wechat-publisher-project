import { Divider, Grid, H1, H2, H3, Stack, Stat, Table, Text } from 'qoder/canvas';

export default function WeChatMultiAgentReport() {
  return (
    <Stack gap={20}>
      <H1>WeChat Multi-Agent System - Design & Completion Report</H1>
      <Text tone="secondary">
        Multi-agent collaboration system for WeChat Official Account content auto-production and auto-publishing, with Hermes Agent as Feishu bot interface.
      </Text>

      <Divider />

      <H2>Accomplishment Overview</H2>
      <Grid columns={4} gap={12}>
        <Stat value="38" label="Total Files" />
        <Stat value="6+1" label="AI Agents (Core+Hermes)" />
        <Stat value="9" label="Modules" />
        <Stat value="30" label="Python Files Verified" tone="success" />
      </Grid>

      <Divider />

      <H2>System Architecture</H2>
      <Grid columns={2} gap={16}>
        <Stack gap={8}>
          <H3>Core Agent Pipeline</H3>
          <Table
            headers={['Agent', 'Responsibility', 'Model Temp']}
            rows={[
              ['TopicPlanner', 'Analyze trends, generate topics', '0.9'],
              ['ContentWriter', 'Write articles, handle rewrites', '0.7'],
              ['ImageGenerator', 'Generate cover + inline images', '0.3'],
              ['Reviewer', 'Quality review, compliance check', '0.2'],
              ['Formatter', 'Markdown to WeChat HTML', '0.3'],
              ['Publisher', 'Browser automation publish', 'N/A'],
            ]}
          />
        </Stack>
        <Stack gap={8}>
          <H3>Hermes Agent (Feishu Interface)</H3>
          <Table
            headers={['Component', 'Responsibility']}
            rows={[
              ['HermesAgent', 'Command dispatch, workflow orchestration'],
              ['FeishuClient', 'Token mgmt, card messages, API calls'],
              ['FeishuWebhookServer', 'Event callback receiver (v1/v2)'],
              ['CommandParser', 'Text command parsing (/start, etc.)'],
            ]}
          />
        </Stack>
      </Grid>

      <Divider />

      <H2>Tech Stack</H2>
      <Table
        headers={['Layer', 'Technology']}
        rows={[
          ['Orchestration', 'LangGraph StateGraph'],
          ['LLM Routing', 'OpenAI / DeepSeek / DashScope'],
          ['Image Gen', 'DALL-E 3 / Tongyi Wanxiang'],
          ['Browser', 'Playwright (Chromium)'],
          ['Scheduler', 'APScheduler (cron)'],
          ['Storage', 'SQLite + local filesystem'],
          ['Notification', 'Log + Email + WeChat Work + Feishu Card'],
          ['Human Interface', 'Feishu Bot (Hermes Agent)'],
        ]}
      />

      <Divider />

      <H2>Module Breakdown</H2>
      <Table
        headers={['Module', 'Files', 'Key Components']}
        rows={[
          ['agents/', '8', 'BaseAgent + 6 specialized agents (TopicPlanner, ContentWriter, ImageGenerator, Reviewer, Formatter, Publisher)'],
          ['models/', '3', 'LLMRouter (multi-model), ImageModel'],
          ['graph/', '4', 'ArticleState, ArticleWorkflow, Conditions'],
          ['tools/', '5', 'WebSearch, ContentDB, Notifier, WechatAPI'],
          ['browser/', '3', 'WechatSession (Cookie), PublishActions (3 inject strategies)'],
          ['hermes/', '5', 'HermesAgent, FeishuClient, WebhookServer, CommandParser'],
          ['config/', '7', 'settings.yaml + 6 prompt templates'],
          ['root', '3', 'main.py (6 CLI commands), scheduler.py, requirements.txt'],
        ]}
      />

      <Divider />

      <H2>Key Features</H2>
      <Grid columns={3} gap={12}>
        <Stack gap={4}>
          <H3>Core Workflow</H3>
          <Table
            headers={['Feature', 'Status']}
            rows={[
              ['LangGraph StateGraph orchestration', 'Done'],
              ['Conditional review retry (max 2)', 'Done'],
              ['Multi-model configurable routing', 'Done'],
              ['Image marker parsing [IMAGE: ...]', 'Done'],
              ['Cover image 900x383 (2.35:1)', 'Done'],
              ['Inline images 1024x768', 'Done'],
              ['3 HTML templates (simple/business/lively)', 'Done'],
              ['Base64 image embedding', 'Done'],
            ]}
          />
        </Stack>
        <Stack gap={4}>
          <H3>Safety & Operations</H3>
          <Table
            headers={['Feature', 'Status']}
            rows={[
              ['Pre-publish screenshot preview', 'Done'],
              ['Cookie persistence (storage_state)', 'Done'],
              ['Cookie expiry notification', 'Done'],
              ['Daily publish limit check', 'Done'],
              ['Review logs stored in SQLite', 'Done'],
              ['Draft-only mode (configurable)', 'Done'],
              ['APScheduler cron scheduling', 'Done'],
            ]}
          />
        </Stack>
        <Stack gap={4}>
          <H3>Hermes Agent (Feishu)</H3>
          <Table
            headers={['Feature', 'Status']}
            rows={[
              ['Feishu bot event callback (v1/v2)', 'Done'],
              ['Command parsing (/start, /status...)', 'Done'],
              ['Workflow trigger from Feishu', 'Done'],
              ['Real-time progress card push', 'Done'],
              ['Result card with publish details', 'Done'],
              ['Notifier -> Feishu bridge', 'Done'],
              ['URL verification challenge', 'Done'],
              ['Card interaction callback', 'Done'],
            ]}
          />
        </Stack>
      </Grid>

      <Divider />

      <H2>Hermes Agent - Feishu Command Reference</H2>
      <Table
        headers={['Command', 'Action', 'Response']}
        rows={[
          ['/start', 'Trigger workflow (auto topic)', 'Ack card + progress cards + result card'],
          ['/start <topic>', 'Trigger workflow (specific topic)', 'Ack card + progress cards + result card'],
          ['/status', 'View current task status', 'Workflow progress card or idle status'],
          ['/history', 'Recent article list (30 days)', 'Text message with titles'],
          ['/config', 'View config summary', 'Text with content/publish/review/schedule'],
          ['/login', 'Remind re-login', 'Text with CLI command'],
          ['/pause', 'Pause scheduler', 'Confirmation text'],
          ['/resume', 'Resume scheduler', 'Confirmation text'],
          ['/help', 'Show help', 'Command list with examples'],
        ]}
      />

      <Divider />

      <H2>Feishu Card Message Types</H2>
      <Table
        headers={['Card Type', 'Trigger', 'Content']}
        rows={[
          ['Status Card', 'Startup, errors, idle status', 'Title + status + details + color header'],
          ['Workflow Card', 'During workflow execution', 'Article title + stage + 6-agent progress'],
          ['Result Card', 'After workflow completes', 'Title + success/fail + publish details'],
          ['Notification Bridge', 'Cookie expiry, review failure', 'Auto-forwarded from Notifier'],
        ]}
      />

      <Divider />

      <H2>Verification Evidence</H2>
      <Table
        headers={['Check', 'Method', 'Result']}
        rows={[
          ['Syntax validation', 'py_compile (all 30 .py files)', 'PASS'],
          ['Project structure', 'File listing vs spec (38 files)', 'MATCH'],
          ['Spec requirements', 'Line-by-line audit', 'ALL MET'],
          ['Hermes module', '5 files (agent, client, webhook, commands, init)', 'COMPLETE'],
          ['Import dependencies', 'requirements.txt (13 packages)', 'COMPLETE'],
        ]}
      />

      <Divider />

      <H2>CLI Usage</H2>
      <Table
        headers={['Command', 'Description']}
        rows={[
          ['python main.py run', 'Execute full workflow once'],
          ['python main.py --run-now', 'Quick execute (same as run)'],
          ['python main.py run --topic "AI"', 'Execute with specific topic'],
          ['python main.py login', 'Interactive WeChat login'],
          ['python main.py schedule', 'Start APScheduler daemon'],
          ['python main.py hermes', 'Start Hermes Agent (Feishu bot)'],
          ['python main.py check', 'Verify config and session status'],
        ]}
      />

      <Divider />

      <Text tone="secondary" size="small">
        Project: d:\AI\qoder-workspace\wechat-publisher | Generated 2026-08-13
      </Text>
    </Stack>
  );
}
