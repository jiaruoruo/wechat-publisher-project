"""微信公众号内容自动生产与发布 - 主入口"""

import os
import sys
import argparse
import logging

# 确保项目根目录在 Python 路径中
from config.paths import BASE_DIR
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from models.llm_router import load_config, LLMRouter
from graph.workflow import ArticleWorkflow
from agents.topic_planner import TopicPlannerAgent, resolve_planning_config
from browser.wechat_session import WechatSession
from tools import competitor_feed, metrics_feed
from browser.screenshot_utils import cleanup_old_screenshots
from browser.wechat_selectors import validate_selectors_config
from browser.selector_ledger import cleanup_old_stats
from config.paths import SELECTOR_STATS_DIR
from tools.content_db import ContentDB
from scheduler import start_scheduler, run_workflow
from config.validation import validate_config


def setup_logging(verbose: bool = False):
    """配置日志"""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def cmd_run(args):
    """立即执行一次工作流"""
    config = load_config()
    workflow = ArticleWorkflow(config)

    # 支持通过命令行参数覆盖选题
    initial_state = {}
    if args.topic:
        initial_state["topic"] = args.topic
    if args.title:
        initial_state["article_title"] = args.title

    result = workflow.run(initial_state if initial_state else None)

    # 输出结果
    publish_result = result.get("publish_result", {})
    if publish_result.get("success"):
        print(f"\n发布成功！模式: {publish_result.get('mode', 'draft')}")
    else:
        print(f"\n发布失败: {publish_result.get('error', '未知错误')}")


def cmd_topics(args):
    """产出候选选题清单供人工审核（只读：不写库、不发布、不生成正文）"""
    config = load_config()
    router = LLMRouter(config)
    agent = TopicPlannerAgent(router, config)

    count = getattr(args, "count", 12) or 12
    topics = agent.suggest_topics(count=count)

    if not topics:
        print("\n候选选题产出为空（可能因搜索服务或 LLM 异常）。可稍后重试。")
        return

    print(f"\n=== 候选选题（共 {len(topics)} 个，覆盖 AI/机器人/芯片，国内/国际混合）===")
    for i, t in enumerate(topics, 1):
        direction = t.get("direction", "")
        region = t.get("region", "")
        confidence = t.get("confidence", "medium")
        tag = f"[{direction}/{region}] 可信度:{confidence}"
        print(f"\n{i}. {t.get('title', '')}  {tag}")
        angle = t.get("angle", "")
        if angle:
            print(f"   角度: {angle}")
        why = t.get("why", "")
        if why:
            print(f"   为什么写: {why}")
        evidence = t.get("evidence", "")
        if evidence:
            print(f"   依据: {evidence}")
    print("\n请回复编号选择，或给出你自己的选题（用 run --topic 执行，当前为 draft 模式不群发）。")


def cmd_research(args):
    """采集竞品爆款标题 + 自有表现，提炼模式库，构建数据化选题库（research）"""
    config = load_config()
    db_path = config.get("storage", {}).get("db_path")
    content = config.get("content", {})
    planning = resolve_planning_config(content)
    keywords = planning.get("competitor_keywords") or content.get("keywords", [])

    if not keywords:
        print("[WARNING] 未配置竞品采集关键词（content.keywords 与 topic_planning.competitor_keywords 均为空），跳过竞品采集。")
        return

    with ContentDB(db_path) as db:
        # 1) 竞品/热门标题 + 模式库
        titles, patterns = competitor_feed.build_competitor_bank(
            db,
            keywords,
            max_keywords=planning.get("competitor_max_keywords", 3),
            results_per_keyword=planning.get("competitor_results_per_keyword", 5),
        )
        print(f"\n[采集] 竞品/热门标题 {len(titles)} 条，提炼标题模式 {len(patterns)} 类")
        for p in patterns[:12]:
            print(f"  - [{p.get('pattern_type')}] {p.get('pattern')} (命中 {p.get('hit_count')})")

        # 2) 自有文章表现（需登录会话；失败则尝试 CSV 补录）
        metrics = []
        csv_path = getattr(args, "metrics_csv", None)
        if csv_path:
            metrics = metrics_feed.import_metrics_from_csv(csv_path)
        else:
            try:
                session = WechatSession(config)
                session.start()
                if session.check_login():
                    metrics = metrics_feed.collect_metrics(session, config)
                else:
                    print("[WARNING] 公众号未登录或会话失效，跳过自有表现采集（可用 --metrics-csv 手动补录）")
                session.close()
            except Exception as e:  # noqa: BLE001
                print(f"[WARNING] 表现采集失败（已降级）: {e}")

        if metrics:
            db.save_article_metrics(metrics)

        # 3) 爆款归因（即时预览）
        report = metrics_feed.attribute_performance(metrics, patterns)
        print(f"\n[归因] 自有表现样本 {len(metrics)} 条")
        if not report:
            print("  样本不足或暂无数据，暂无法归因（多运行并积累表现数据后可复盘）。")
        else:
            for r in report[:10]:
                print(f"  - {r['segment']}: 均阅读 {r['avg_read']} / 均分享 {r['avg_share']} / 均点赞 {r['avg_like']} (n={r['count']})")

    print("\n数据已沉淀到选题库与模式库（DB）。运行 `python main.py topics` 即可获得带评分的候选选题。")


def cmd_report(args):
    """输出爆款归因报告（基于已沉淀的 DB 数据，report）"""
    config = load_config()
    db_path = config.get("storage", {}).get("db_path")

    with ContentDB(db_path) as db:
        metrics = db.get_article_metrics()
        patterns = db.get_title_patterns()
        titles = db.get_competitor_titles(limit=10)

    print("\n=== 爆款归因报告 ===")
    print(f"自有表现样本: {len(metrics)} 条 | 竞品模式库: {len(patterns)} 类 | 竞品标题池: {len(titles)} 条")

    if not metrics:
        print("暂无自有表现数据：请先运行 `python main.py research`（或 research --metrics-csv 补录）采集阅读/分享/点赞。")
        return

    report = metrics_feed.attribute_performance(metrics, patterns)
    if not report:
        print("样本不足，无法归因。")
        return

    print("\n按平均阅读降序的归因：")
    for r in report:
        print(f"  - {r['segment']}: 均阅读 {r['avg_read']} | 均分享 {r['avg_share']} | 均点赞 {r['avg_like']} | n={r['count']}")

    if len(metrics) < 3:
        print("\n[提示] 样本量偏小（<3），归因结论仅供参考；随发布积累可逐步提权 history 维度。")


def cmd_login(args):
    """交互式登录公众号"""
    config = load_config()
    session = WechatSession(config)

    try:
        session.start()
        print("请在浏览器中扫码登录公众号...")
        session.login_interactive(timeout=args.timeout)
        print("登录成功！会话已保存。")
    except Exception as e:
        print(f"登录失败: {e}")
    finally:
        session.close()


def cmd_schedule(args):
    """启动定时调度"""
    print("启动定时调度器...")
    start_scheduler()


def cmd_check(args):
    """检查配置和登录状态"""
    print("检查配置...")
    try:
        config = load_config()
        print("  配置文件加载成功")

        # 统一配置校验（API Key / 模型 / 飞书 / 通知）
        issues = validate_config(config)
        if not issues:
            print("  ✅ 配置校验通过")
        for severity, message in issues:
            # 避免 emoji（❌/⚠️）在 GBK 控制台下编码崩溃，使用文本标记
            tag = "[错误]" if severity == "error" else "[警告]"
            print(f"  {tag} {message}")

        # 选择器配置校验（config/selectors.yaml，对比内置默认值）
        selector_issues = validate_selectors_config()
        if selector_issues:
            for severity, message in selector_issues:
                tag = "[错误]" if severity == "error" else "[警告]"
                print(f"  {tag} {message}")
        else:
            print("  选择器配置校验通过（config/selectors.yaml）")

        # 展示配置信息
        models = config.get("models", {})
        print(f"  已配置 {len(models)} 个 Agent 模型")

        # 检查内容配置
        content = config.get("content", {})
        print(f"  内容领域: {content.get('domain', '未设置')}")
        print(f"  排版模板: {content.get('template', 'simple')}")
        print(f"  发布模式: {config.get('wechat', {}).get('publish_mode', 'draft')}")
        # 发布锁等待超时（与 workflow._publish_lock_timeout 一致的兜底解析）
        raw_lock_timeout = config.get("wechat", {}).get("publish_lock_timeout", 0)
        try:
            lock_timeout = max(0.0, float(raw_lock_timeout))
            print(f"  发布锁等待超时: {lock_timeout:.0f} 秒（0 = 立即跳过）")
        except (TypeError, ValueError):
            print(f"  发布锁等待超时: 配置非法（{raw_lock_timeout!r}），按 0 秒（立即跳过）处理")
        # 发布锁票据队列清扫（清理长时间空置的 .queue 目录）
        try:
            from tools.publish_lock import cleanup_publish_lock_queue
            stale, dir_removed = cleanup_publish_lock_queue(idle_hours=24.0)
            if dir_removed:
                print(f"  发布锁队列: 已清理长时间空置的票据队列目录（回收 {stale} 张过期票据）")
            elif stale:
                print(f"  发布锁队列: 回收 {stale} 张过期票据（活跃等待者仍在排队）")
            else:
                print("  发布锁队列: 无需清理")
        except Exception as e:
            print(f"  发布锁队列清扫失败: {e}")
        retention = config.get("browser", {}).get("screenshot_retention_days", 30)
        print(f"  截图保留策略: {retention} 天（0 = 不清理）")

        # 截图保留清理（防止时间戳化后截图无限堆积；与发布流程共用同一策略）
        try:
            screenshot_dir = config.get("browser", {}).get("screenshot_dir", "storage/screenshots")
            if not os.path.isabs(screenshot_dir):
                screenshot_dir = os.path.join(BASE_DIR, screenshot_dir)
            removed = cleanup_old_screenshots(screenshot_dir, retention)
            if removed:
                print(f"  截图保留清理: 删除 {removed} 个过期日期目录/文件（保留 {retention} 天）")
            else:
                print("  截图保留清理: 无需清理")
        except Exception as e:
            print(f"  截图保留清理失败: {e}")

        # 台账保留清理（selector_stats 按日期归档，长期运行会按天堆积）
        try:
            removed_stats = cleanup_old_stats(SELECTOR_STATS_DIR, retention)
            if removed_stats:
                print(f"  台账保留清理: 删除 {removed_stats} 个过期日期文件（保留 {retention} 天）")
            else:
                print("  台账保留清理: 无需清理")
        except Exception as e:
            print(f"  台账保留清理失败: {e}")

        # 孤儿草稿清理（发布失败/中断残留的 draft 行，干扰统计与选题去重）
        try:
            with ContentDB(config.get("storage", {}).get("db_path")) as _db:
                removed_drafts = _db.delete_orphan_drafts(days=7)
            if removed_drafts:
                print(f"  孤儿草稿清理: 删除 7 天前仍未发表的草稿 {removed_drafts} 条")
            else:
                print("  孤儿草稿清理: 无需清理")
        except Exception as e:
            print(f"  孤儿草稿清理失败: {e}")

        # 检查飞书配置
        feishu = config.get("feishu", {})
        feishu_status = "已配置" if feishu.get("app_id") else "未配置"
        print(f"  飞书机器人: {feishu_status}")

    except Exception as e:
        print(f"  配置检查失败: {e}")

    print("\n检查浏览器会话...")
    from config.paths import SESSION_PATH
    session_file = config.get("browser", {}).get("session_file", SESSION_PATH)
    if not os.path.isabs(session_file):
        session_file = os.path.join(BASE_DIR, session_file)
    if os.path.exists(session_file):
        print(f"  会话文件存在: {session_file}")
    else:
        print(f"  会话文件不存在，请先运行 `python main.py login` 登录")


def cmd_selectors(args):
    """输出选择器命中率台账与精简建议"""
    from browser.selector_ledger import print_report
    print_report(days=args.days, min_publishes=args.min_publishes)


def cmd_hermes(args):
    """启动 Hermes Agent（飞书机器人接口）"""
    from hermes.agent import HermesAgent

    config = load_config()

    # 覆盖端口配置（如果命令行指定了）
    if args.port:
        config.setdefault("feishu", {})["webhook_port"] = args.port

    # 检查飞书配置
    feishu = config.get("feishu", {})
    if not feishu.get("app_id"):
        print("错误：未配置飞书机器人！")
        print("请在 config/settings.yaml 中填写 feishu.app_id 和 feishu.app_secret")
        return

    print(f"启动 Hermes Agent...")
    print(f"  Webhook 端口: {feishu.get('webhook_port', 9000)}")
    print(f"  默认通知群: {feishu.get('default_chat_id', '未设置')}")
    print(f"  飞书 App ID: {feishu.get('app_id', '')[:8]}...")
    print()
    print("Hermes Agent 已就绪，等待飞书消息...")
    print("按 Ctrl+C 停止")

    agent = HermesAgent(config)
    agent.run_forever()


def main():
    parser = argparse.ArgumentParser(
        description="微信公众号内容自动生产与发布系统",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用示例:
  python main.py run                    # 立即执行一次完整工作流
  python main.py run --topic "AI趋势"   # 指定选题执行
  python main.py --run-now              # 立即执行（快捷方式）
  python main.py login                  # 交互式登录公众号
  python main.py schedule               # 启动定时调度
  python main.py hermes                 # 启动 Hermes Agent（飞书接口）
  python main.py check                  # 检查配置状态
  python main.py selectors              # 输出选择器命中率台账与精简建议
        """,
    )

    parser.add_argument("-v", "--verbose", action="store_true", help="详细日志输出")
    parser.add_argument("--run-now", action="store_true", help="立即执行一次工作流（等同于 run 命令）")
    # 为 --run-now 快捷方式提供与 run 子命令一致的默认值（cmd_run 依赖 args.topic/title 存在）
    parser.set_defaults(topic=None, title=None)

    subparsers = parser.add_subparsers(dest="command", help="子命令")

    # run 命令
    run_parser = subparsers.add_parser("run", help="立即执行一次工作流")
    run_parser.add_argument("--topic", type=str, help="指定选题（可选）")
    run_parser.add_argument("--title", type=str, help="指定标题（可选）")

    # topics 命令（候选选题清单，只读不发布）
    topics_parser = subparsers.add_parser("topics", help="产出候选选题清单供审核（不写库、不发布）")
    topics_parser.add_argument("--count", type=int, default=12, help="候选数量（默认 12，实际 10~15）")

    # research 命令（运营增长：竞品模式 + 表现采集，构建数据化选题库）
    research_parser = subparsers.add_parser(
        "research", help="采集竞品爆款标题与自有表现，提炼模式库、构建选题库"
    )
    research_parser.add_argument(
        "--metrics-csv", type=str, default=None,
        help="手动补录自有表现数据的 CSV（列：title,publish_url,read_count,share_count,like_count），"
             "用于后台选择器失效时兜底，不触发浏览器抓取",
    )

    # report 命令（爆款归因报告）
    subparsers.add_parser("report", help="输出爆款归因报告（基于已沉淀的 DB 数据）")

    # login 命令
    login_parser = subparsers.add_parser("login", help="交互式登录公众号")
    login_parser.add_argument("--timeout", type=int, default=120, help="登录超时时间（秒）")

    # schedule 命令
    subparsers.add_parser("schedule", help="启动定时调度器")

    # check 命令
    subparsers.add_parser("check", help="检查配置和登录状态")

    # selectors 命令（选择器命中率台账）
    selectors_parser = subparsers.add_parser("selectors", help="输出选择器命中率台账与精简建议")
    selectors_parser.add_argument("--days", type=int, default=30, help="统计最近 N 天（默认 30）")
    selectors_parser.add_argument("--min-publishes", type=int, default=2, help="参与建议的最少发布次数（默认 2）")

    # hermes 命令
    hermes_parser = subparsers.add_parser("hermes", help="启动 Hermes Agent（飞书机器人接口）")
    hermes_parser.add_argument("--port", type=int, help="Webhook 端口（覆盖配置文件）")

    args = parser.parse_args()

    # 配置日志
    setup_logging(args.verbose)

    # 处理 --run-now 快捷方式（复用 run 子命令的默认值 topic/title）
    if args.run_now:
        args.command = "run"

    # 分发命令
    commands = {
        "run": cmd_run,
        "topics": cmd_topics,
        "research": cmd_research,
        "report": cmd_report,
        "login": cmd_login,
        "schedule": cmd_schedule,
        "check": cmd_check,
        "selectors": cmd_selectors,
        "hermes": cmd_hermes,
    }

    if args.command in commands:
        commands[args.command](args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()