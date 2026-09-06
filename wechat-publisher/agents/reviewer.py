"""审核校对 Agent - 内容质量审核、合规检查"""

import json
import logging
from datetime import datetime

from agents.base import BaseAgent
from graph.state import ArticleState
from tools.json_utils import extract_json_from_response

logger = logging.getLogger(__name__)

# 微信公众平台硬性约束（程序化护栏参考值）
_TITLE_MAX = 64            # 标题上限（字符）
_SUMMARY_MAX = 120         # 摘要（digest）上限（字符）
_CONTENT_SHORT_FLOOR = 200  # 正文过短阈值（仅作提示，不强制否决）

# 敏感词分类 → 中文标签（用于 feedback 与日志，便于定位问题类别）
_SENSITIVE_CATEGORY_LABELS = {
    "political": "政治敏感",
    "false_advertising": "虚假宣传",
    "absolute_terms": "绝对化用语",
    "infringement": "侵权/违规转载",
}

# compliance 文本的否定语境防护：避免「未违规 / 不存在侵权」被误判为违规
_NEGATION_WINDOW = 5                        # 回溯窗口（字符）
_NEGATION_CHARS = ("未", "无", "不", "非")   # 紧邻关键词的前一个字
_NEGATION_PHRASES = (                       # 窗口内出现的否定短语
    "不存在", "未发现", "未出现", "未见", "没有", "不涉及", "未涉及",
)


class ReviewerAgent(BaseAgent):
    """审核校对 Agent：对文章进行全面质量审核"""

    agent_name = "reviewer"
    prompt_file = "reviewer.yaml"

    def __init__(self, llm_router, config: dict):
        super().__init__(llm_router, config)
        self.review_config = config.get("review", {})
        self.min_score = self.review_config.get("min_score", 7)
        # 合规一票否决词表（命中即判定不通过，由 _deterministic_checks 程序化预检）
        self.banned_keywords = self.review_config.get("banned_keywords") or []
        # 分类敏感词表（政治 / 虚假宣传 / 绝对化用语 / 侵权），命中即阻断
        raw_sensitive = self.review_config.get("sensitive_words") or {}
        self.sensitive_words = raw_sensitive if isinstance(raw_sensitive, dict) else {}
        # 维度分门槛：0 = 关闭该项校验（缺失/非数值时跳过，不误杀）
        self.min_title_score = self.review_config.get("min_title_score", 0)
        self.min_readability_score = self.review_config.get("min_readability_score", 0)
        # 自然度/去AI味维度门槛：0 = 关闭校验；缺失/非数值时跳过（不误杀）
        self.min_naturalness_score = self.review_config.get("min_naturalness_score", 0)
        # details.compliance 文本的违规判定词（命中即一票否决）
        self.compliance_negative_keywords = (
            self.review_config.get("compliance_negative_keywords") or []
        )
        # 把阈值注入 system prompt：reviewer.yaml 含 {min_score} 占位符，此前
        # _load_prompt 仅做 yaml.safe_load 未格式化，模型收到的门槛是字面量
        # "{min_score}"，导致 min_score 形同虚设。用 replace 避免 json 示例里的
        # { } 被 str.format 误当占位符而抛错。
        self.system_prompt = self.system_prompt.replace(
            "{min_score}", str(self.min_score)
        )

    def run(self, state: ArticleState) -> dict:
        """执行内容审核"""
        article_title = state.get("article_title", "")
        content = state.get("content", "")
        target_audience = state.get("target_audience", "")
        summary = state.get("summary", "")
        retry_count = state.get("retry_count", 0)
        # 上一轮审核结果：重试时用来核对"上次指出的问题是否已修复"
        prev_review = state.get("review_result", {}) or {}

        # 1) 确定性预检（先于 LLM 的程序化护栏）
        checks = self._deterministic_checks(article_title, content, summary)

        # 2) 构建审核 prompt（重试时带上历史反馈，聚焦修复核验）
        prompt = self._build_prompt(
            article_title, content, target_audience, summary, retry_count, prev_review
        )

        # 3) 调用 LLM（json_mode 强制 JSON 输出，降低解析失败率）
        response = self.invoke(prompt, json_mode=True)

        # 4) 解析审核结果。解析失败 = fail-safe 不通过，进入重试/人工复核，
        #    而非像旧实现那样默认 passed=True 带病放行。
        try:
            parsed = self._parse_review_result(response)
            parse_ok = True
        except Exception as e:
            logger.error(f"解析审核结果失败: {e}")
            parsed = {
                "passed": False,
                "score": 0,
                "feedback": "审核结果解析失败，已标记需复核（fail-safe）",
                "details": {},
            }
            parse_ok = False

        # 5) 维度门控：details.compliance 文本 + title/readability 维度分
        details = parsed.get("details", {}) or {}
        gates = self._enforce_details_gates(details)

        # 6) 确定性裁决：LLM 结论 + 分数门槛 + 合规预检 + 维度门控
        #    - 分数低于 min_score 时即便 LLM 说通过也判不通过，门槛真正生效；
        #    - 命中敏感词/违禁词、compliance 判定为违规、或维度分不达标 → 否决。
        llm_passed = bool(parsed.get("passed", True))
        score = int(parsed.get("score", 0))
        passed = (
            llm_passed
            and (score >= self.min_score)
            and not checks["blocked"]
            and not gates["blocked"]
        )

        feedback = str(parsed.get("feedback", ""))
        # 预检/门控的阻断原因并入 feedback，驱动下一轮针对性重写并便于人工复核
        all_issues = [*checks["issues"], *gates["issues"]]
        if all_issues:
            feedback = (feedback + "｜程序化预检：" + "；".join(all_issues)).strip("｜")
        if checks["report"] or gates["report"]:
            details = {
                **details,
                "deterministic_checks": checks["report"],
                "deterministic_gates": gates["report"],
            }
        if checks["blocked"] or gates["blocked"]:
            # 只记录命中的词与类别（report），绝不打印正文片段，避免日志污染/内容泄露
            logger.warning(
                f"审核门控拦截: checks={checks['report']}, gates={gates['report']}"
            )

        review_result = {
            "passed": passed,
            "score": score,
            "feedback": feedback,
            "details": details,
            "parse_ok": parse_ok,
        }

        logger.info(
            f"审核结果: passed={passed}, score={score}, "
            f"min_score={self.min_score}, retry={retry_count}, parse_ok={parse_ok}"
        )

        # 重试计数由 workflow 层 increment_retry 节点维护，这里不返回 retry_count。
        # review_history 为 schema 级 Annotated[list, append_review_history]，
        # 只需返回本次审核记录，LangGraph 自动追加到历史（含各维度明细，便于审计）。
        return {
            "review_result": review_result,
            "review_history": [
                {
                    "retry": retry_count,
                    "passed": passed,
                    "score": score,
                    "feedback": feedback,
                    "details": details,
                },
            ],
            "metadata": {
                "reviewed_at": datetime.now().isoformat(),
            },
        }

    # ── prompt 构建 ──────────────────────────────────────

    def _build_prompt(
        self,
        title: str,
        content: str,
        audience: str,
        summary: str,
        retry_count: int,
        prev_review: dict,
    ) -> str:
        """构建审核 human prompt；重试时附带上一轮审核意见以核验修复"""
        prior = ""
        if retry_count and prev_review:
            prior_details = prev_review.get("details", {}) or {}
            prior = (
                "\n## 上一轮审核意见（请重点核验以下问题是否已修复）\n"
                f"- 结论：{'通过' if prev_review.get('passed') else '不通过'}"
                f"（评分 {prev_review.get('score', 'N/A')}）\n"
                f"- 反馈：{prev_review.get('feedback', '')}\n"
            )
            if prior_details:
                prior += (
                    "- 各维度评估："
                    + json.dumps(prior_details, ensure_ascii=False)
                    + "\n"
                )

        summary_block = f"- 摘要：{summary}\n" if summary else ""

        return f"""请对以下微信公众号文章进行全面审核。

## 文章信息
- 标题：{title}
- 目标受众：{audience}
- 最低通过分数：{self.min_score}/10
{summary_block}
## 文章内容
{content}
{prior}
请严格按照 JSON 格式输出审核结果。"""

    # ── 确定性预检 ──────────────────────────────────────

    def _deterministic_checks(self, title: str, content: str, summary: str) -> dict:
        """程序化护栏：不依赖 LLM 的硬约束与提示项

        - 命中分类敏感词或平铺违禁词（合规一票否决）→ blocked=True（强制不通过）
        - 标题/摘要超长、正文过短、无插图标记 → 仅提示（advisory），不否决

        扫描范围为「标题 + 正文」：标题同样可能含绝对化用语等违规表述。
        """
        issues: list[str] = []
        report: dict = {}
        blocked = False
        # 扫描范围：标题 + 正文（标题同样可能含绝对化用语等违规表述）
        scan_text = f"{title}\n{content}" if title else content

        # 合规一票否决一：分类敏感词（政治 / 虚假宣传 / 绝对化用语 / 侵权）
        if self.sensitive_words and scan_text:
            cat_hits: dict[str, list[str]] = {}
            for category, words in self.sensitive_words.items():
                if not isinstance(words, (list, tuple)) or not words:
                    continue
                cat_words = [w for w in words if w and w in scan_text]
                if cat_words:
                    cat_hits[category] = cat_words
            if cat_hits:
                blocked = True
                report["sensitive_words"] = cat_hits
                for category, cat_words in cat_hits.items():
                    label = _SENSITIVE_CATEGORY_LABELS.get(category, category)
                    issues.append(f"命中【{label}】敏感词：{', '.join(cat_words)}")

        # 合规一票否决二：平铺违禁词（向后兼容，与分类词表合并生效）
        if self.banned_keywords and scan_text:
            banned_hits = [kw for kw in self.banned_keywords if kw and kw in scan_text]
            if banned_hits:
                blocked = True
                issues.append(f"命中违禁词：{', '.join(banned_hits)}")
                report["banned_keywords"] = banned_hits

        # 标题长度（微信上限 64 字符）
        if title and len(title) > _TITLE_MAX:
            issues.append(f"标题过长（{len(title)}>{_TITLE_MAX} 字符），可能被平台截断")
            report["title_len"] = len(title)

        # 摘要长度（digest 上限 120 字符）
        if summary and len(summary) > _SUMMARY_MAX:
            issues.append(f"摘要过长（{len(summary)}>{_SUMMARY_MAX} 字符）")
            report["summary_len"] = len(summary)

        # 正文过短（提示，不否决）
        if len(content) < _CONTENT_SHORT_FLOOR:
            issues.append(
                f"正文过短（{len(content)}<{_CONTENT_SHORT_FLOOR} 字），内容可能不完整"
            )
            report["content_len"] = len(content)

        # 缺少插图标记（提示）
        if "[IMAGE:" not in content:
            issues.append("正文未包含任何 [IMAGE: ...] 插图标记")
            report["has_inline_image"] = False

        return {"blocked": blocked, "issues": issues, "report": report}

    # ── 维度门控 ──────────────────────────────────────

    def _enforce_details_gates(self, details: dict) -> dict:
        """维度门控：解析 details.compliance 文本与 title/readability 维度分

        把「一票否决」从提示词层落到代码层，避免 LLM 虽在 compliance 里写了
        「存在违规」却仍把 passed 置为 true 时被放行。

        约定：
        - 字段缺失或非数值 → **跳过该门控**（视为通过）。这与「解析失败
          fail-safe 判不通过」方向相反但场景不同：解析失败是完全没拿到审核
          结论（无信息，应失败）；维度字段缺失是拿到了结论只是维度不全
          （有信息，不该误杀）。
        - compliance 命中否定词时需排除「未/无/不/非」等否定语境，避免
          「未发现违规」「不存在侵权」被误判为违规。
        """
        issues: list[str] = []
        report: dict = {}
        blocked = False
        details = details or {}

        # 1) compliance 文本门控（一票否决）
        compliance = str(details.get("compliance") or "")
        if compliance and self.compliance_negative_keywords:
            violations = [
                kw
                for kw in self.compliance_negative_keywords
                if kw and kw in compliance and not self._is_negated(compliance, kw)
            ]
            if violations:
                blocked = True
                issues.append(f"合规性判定为违规（{', '.join(violations)}），一票否决")
                report["compliance_violations"] = violations

        # 2) 维度分门槛（缺失/非数值跳过；阈值 0 = 关闭该项校验）
        for field, threshold, label in (
            ("title_score", self.min_title_score, "标题"),
            ("readability_score", self.min_readability_score, "可读性"),
            ("naturalness_score", self.min_naturalness_score, "自然度/去AI味"),  # 新增：去AI味双保险
        ):
            if not threshold:
                continue
            if field not in details:
                continue  # 字段缺失 → 跳过，不误杀
            value = self._safe_int(details.get(field))
            if value is None:
                continue  # 非数值 → 跳过
            report[field] = value
            if value < threshold:
                blocked = True
                issues.append(f"{label}维度评分 {value} 低于门槛 {threshold}")

        return {"blocked": blocked, "issues": issues, "report": report}

    def _is_negated(self, text: str, keyword: str) -> bool:
        """判断 keyword 在 text 中是否处于否定语境（如「未发现违规」「不存在侵权」）

        仅回溯关键词前 _NEGATION_WINDOW 个字符：
        - 紧邻的前一个字是「未/无/不/非」→ 否定；
        - 窗口内出现否定短语（未发现 / 不存在 / 没有 /…）→ 否定。
        """
        idx = text.find(keyword)
        if idx == -1:
            return False
        prefix = text[max(0, idx - _NEGATION_WINDOW) : idx]
        if prefix and prefix[-1] in _NEGATION_CHARS:
            return True
        return any(phrase in prefix for phrase in _NEGATION_PHRASES)

    @staticmethod
    def _safe_int(value) -> int | None:
        """安全转 int：LLM 可能返回 "8" / null / 非数值；失败返回 None（跳过门控）"""
        if value is None or isinstance(value, bool):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _parse_review_result(self, response: str) -> dict:
        """解析 LLM 返回的审核结果"""
        data = extract_json_from_response(response)
        return {
            "passed": bool(data.get("passed", True)),
            # 缺省计 0 分：缺失分数时按"不通过"处理（fail-safe），而非旧实现的默认 7
            "score": int(data.get("score", 0)),
            "feedback": str(data.get("feedback", "")),
            "details": data.get("details", {}),
        }
