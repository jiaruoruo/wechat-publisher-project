"""定时调度器 - 基于 APScheduler"""

import os
import logging
from datetime import datetime

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from models.llm_router import load_config
from graph.workflow import ArticleWorkflow
from tools.content_db import ContentDB
from config.paths import PAUSE_FLAG_PATH as PAUSE_FLAG_FILE
from config.validation import validate_config

logger = logging.getLogger(__name__)


def run_workflow():
    """执行一次完整的工作流"""
    # 检查暂停标志
    if os.path.exists(PAUSE_FLAG_FILE):
        logger.info(f"[{datetime.now()}] 调度已暂停，跳过本次执行（删除 {PAUSE_FLAG_FILE} 或发送 /resume 恢复）")
        return

    logger.info(f"[{datetime.now()}] 定时任务触发，开始执行工作流...")
    try:
        config = load_config()

        # 发布前检查是否有待发布任务（短连接，用完即关）
        with ContentDB(config.get("storage", {}).get("db_path")) as db:
            recent_count = db.get_article_count(days=1)
            max_daily = config.get("schedule", {}).get("max_daily_articles", 3)
            if recent_count >= max_daily:
                logger.warning(
                    f"今日已发布 {recent_count} 篇文章，"
                    f"达到每日上限 {max_daily}，跳过本次发布"
                )
                return

        workflow = ArticleWorkflow(config)
        result = workflow.run()

        publish_result = result.get("publish_result", {})
        if publish_result.get("success"):
            logger.info("定时任务执行成功！")
        else:
            logger.warning(f"定时任务执行完成但发布未成功: {publish_result.get('error', '')}")

    except Exception as e:
        logger.error(f"定时任务执行失败: {e}", exc_info=True)


def start_scheduler():
    """启动定时调度器"""
    config = load_config()

    # 启动前输出配置校验结果（不影响运行）
    for severity, message in validate_config(config):
        log = logger.warning if severity == "error" else logger.info
        log(f"配置校验[{severity}]: {message}")

    schedule_config = config.get("schedule", {})

    cron_expr = schedule_config.get("cron", "0 8 * * *")
    timezone = schedule_config.get("timezone", "Asia/Shanghai")

    # 用 APScheduler 自带解析器（支持 6 字段含秒、@daily 等命名表达式），
    # 替换手写 5 字段 split 映射，避免解析不一致/不支持扩展语法。
    try:
        trigger = CronTrigger.from_crontab(cron_expr, timezone=timezone)
    except (ValueError, TypeError) as e:
        logger.warning(f"cron 表达式解析失败（{cron_expr!r}）：{e}，回退到默认 0 8 * * *")
        trigger = CronTrigger.from_crontab("0 8 * * *", timezone=timezone)

    scheduler = BlockingScheduler(timezone=timezone)
    scheduler.add_job(
        run_workflow,
        trigger=trigger,
        id="wechat_publish",
        name="微信公众号自动发布",
        misfire_grace_time=3600,  # 允许 1 小时的误差
    )

    logger.info(f"定时调度器已启动，cron: {cron_expr}, 时区: {timezone}")
    logger.info("按 Ctrl+C 停止调度器")

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("调度器已停止")