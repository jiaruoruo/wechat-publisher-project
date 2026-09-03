"""条件分支逻辑 - 审核不通过时回退重写"""

from graph.state import ArticleState


def review_condition(state: ArticleState) -> str:
    """
    审核结果条件判断

    Returns:
        "approved"   - 审核通过，进入排版阶段
        "rejected"   - 审核不通过且未达最大重试次数，回退重写
        "max_retries" - 达到最大重试次数，强制进入排版（标记需人工审核）
    """
    review_result = state.get("review_result", {})
    retry_count = state.get("retry_count", 0)
    # 统一从 metadata 读取 max_retries，与 workflow.run()/reviewer 不再各自读配置
    max_retries = state.get("metadata", {}).get("max_retries", 2)

    if review_result.get("passed", True):
        return "approved"
    if retry_count >= max_retries:
        return "max_retries"
    return "rejected"


def topic_condition(state: ArticleState) -> str:
    """
    选题阶段闸门：选题降级时短路，跳过后续所有节点

    选题是流水线的第一环，一旦降级（LLM 调用连续失败或输出缺少必需字段），
    后续节点只会在占位内容上白跑一遍生成/配图/审核/发布，还会占用每日发布配额。
    这里让 topic_planner 始终正常返回、只把 topic_degraded 写进 metadata，
    由本函数统一判定，保证 CLI / scheduler / Hermes 三个入口行为一致。

    Returns:
        "continue" - 选题正常，进入内容创作
        "aborted"  - 选题降级，直连 END
    """
    if (state.get("metadata") or {}).get("topic_degraded"):
        return "aborted"
    return "continue"


def increment_retry(state: ArticleState) -> dict:
    """重试计数节点：每次审核 rejected 后自增 retry_count

    重试计数集中在 workflow 层维护，Reviewer 只产出 review_result，
    避免计数逻辑分散在 Agent 内部导致状态被覆盖/重复累加。
    """
    return {"retry_count": state.get("retry_count", 0) + 1}
