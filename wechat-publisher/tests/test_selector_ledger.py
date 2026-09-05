"""tests for browser/selector_ledger.py — JSONL 台账、聚合、精简建议

覆盖：
- serialize / parse_line 往返、损坏行容错
- append 按日期写文件、追加多行、失败不抛异常
- load 读取同一天多行
- aggregate：跨多次发布的命中数与命中率
- suggest_trimming：0 命中删除、兜底命中提前、样本不足跳过
- analyze / print_report 完整流水线
"""

import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from browser import selector_ledger as ledger
from browser import wechat_selectors as ws


def _report(hits_by_group, never_hit=None, all_missed=None):
    """构造一份 editor.selector_stats 报告"""
    return {
        "groups_attempted": len(hits_by_group),
        "groups_hit": len(hits_by_group),
        "total_hits": sum(sum(v.values()) for v in hits_by_group.values()),
        "hits_by_group": hits_by_group,
        "never_hit": never_hit or {},
        "all_missed": all_missed or [],
    }


class SerializeParseTest(unittest.TestCase):
    def test_serialize_parse_roundtrip(self):
        report = _report({"标题输入": {"#title": 1}})
        line = ledger.serialize(report, now=mock.Mock(isoformat=mock.Mock(return_value="2026-08-21T10:00:00")))
        parsed = ledger.parse_line(line)
        self.assertEqual(parsed["hits_by_group"], {"标题输入": {"#title": 1}})
        self.assertEqual(parsed["ts"], "2026-08-21T10:00:00")

    def test_parse_line_handles_bad_lines(self):
        self.assertIsNone(ledger.parse_line(""))
        self.assertIsNone(ledger.parse_line("not json {"))
        self.assertIsNone(ledger.parse_line("\n"))

    def test_parse_line_rejects_non_dict(self):
        # 数组行会炸掉 aggregate 的 .get()，必须拒绝
        self.assertIsNone(ledger.parse_line("[1, 2, 3]"))
        self.assertIsNone(ledger.parse_line('"just a string"'))


class AppendLoadTest(unittest.TestCase):
    def test_append_writes_dated_jsonl(self):
        with tempfile.TemporaryDirectory() as d:
            path = ledger.append_selector_stats(_report({"标题输入": {"#title": 1}}), ledger_dir=d)
            self.assertTrue(path.endswith(".jsonl"))
            self.assertEqual(os.path.dirname(path), d)
            day = __import__("datetime").datetime.now().strftime("%Y%m%d")
            self.assertEqual(os.path.basename(path), f"{day}.jsonl")
            with open(path, "r", encoding="utf-8") as f:
                lines = f.read().strip().splitlines()
            self.assertEqual(len(lines), 1)

    def test_append_multiple_lines(self):
        with tempfile.TemporaryDirectory() as d:
            ledger.append_selector_stats(_report({"标题输入": {"#title": 1}}), ledger_dir=d)
            ledger.append_selector_stats(_report({"标题输入": {"#title": 2}}), ledger_dir=d)
            reports = ledger.load_selector_stats(days=1, ledger_dir=d)
            self.assertEqual(len(reports), 2)
            self.assertEqual(reports[0]["hits_by_group"]["标题输入"]["#title"], 1)
            self.assertEqual(reports[1]["hits_by_group"]["标题输入"]["#title"], 2)

    def test_append_failure_does_not_raise(self):
        path = ledger.append_selector_stats(_report({}), ledger_dir="Z:/invalid/ledger")
        self.assertEqual(path, "")

    def test_load_empty_dir(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(ledger.load_selector_stats(days=7, ledger_dir=d), [])

    def test_load_skips_corrupt_lines(self):
        with tempfile.TemporaryDirectory() as d:
            ledger.append_selector_stats(_report({"标题输入": {"#title": 1}}), ledger_dir=d)
            day = __import__("datetime").datetime.now().strftime("%Y%m%d")
            with open(os.path.join(d, f"{day}.jsonl"), "a", encoding="utf-8") as f:
                f.write("broken line\n")
            reports = ledger.load_selector_stats(days=1, ledger_dir=d)
            self.assertEqual(len(reports), 1)


class AggregateTest(unittest.TestCase):
    def test_aggregate_counts_hits_and_rate(self):
        r1 = _report({"标题输入": {"#title": 1}})
        r2 = _report({"标题输入": {"#title": 1, 'input[placeholder*="标题"]': 1}})
        agg = ledger.aggregate([r1, r2])
        self.assertEqual(agg["publishes"], 2)
        g = agg["groups"]["标题输入"]
        self.assertEqual(g["publishes"], 2)
        self.assertEqual(g["selectors"]["#title"]["hits"], 2)
        self.assertEqual(g["selectors"]["#title"]["rate"], 1.0)
        self.assertEqual(g["selectors"]['input[placeholder*="标题"]']["hits"], 1)
        self.assertEqual(g["selectors"]['input[placeholder*="标题"]']["rate"], 0.5)

    def test_aggregate_includes_never_hit_selectors(self):
        r1 = _report(
            {"标题输入": {"#title": 1}},
            never_hit={"标题输入": ['input[placeholder*="标题"]', "textarea"]},
        )
        agg = ledger.aggregate([r1])
        g = agg["groups"]["标题输入"]
        self.assertIn("textarea", g["selectors"])
        self.assertEqual(g["selectors"]["textarea"]["hits"], 0)
        self.assertEqual(g["publishes"], 1)

    def test_aggregate_label_in_both_hits_and_never_hit_counts_once(self):
        r1 = _report(
            {"标题输入": {"#title": 1}},
            never_hit={"标题输入": ["textarea"]},
        )
        agg = ledger.aggregate([r1, r1])
        self.assertEqual(agg["groups"]["标题输入"]["publishes"], 2)

    def test_aggregate_all_missed_group(self):
        r1 = _report({}, never_hit={"保存草稿": ["#js_send", "a"]}, all_missed=["保存草稿"])
        agg = ledger.aggregate([r1])
        g = agg["groups"]["保存草稿"]
        self.assertEqual(g["publishes"], 1)
        self.assertEqual(g["selectors"]["#js_send"]["hits"], 0)


class SuggestTrimTest(unittest.TestCase):
    def _agg(self, hits_by_group, publishes):
        """构造 agg：同一份报告重复 publishes 次"""
        reports = [
            _report(hits_by_group, never_hit={})
            for _ in range(publishes)
        ]
        return ledger.aggregate(reports)

    def test_zero_hit_selector_suggests_remove(self):
        agg = self._agg({"标题输入": {"#title": 1}}, publishes=3)
        # 手工补一个 0 命中的当前配置选择器（真实报告来自 never_hit）
        agg["groups"]["标题输入"]["selectors"]['input[name="title"]'] = {"hits": 0, "rate": 0.0}
        result = ledger.suggest_trimming(agg, min_publishes=2)
        sugs = result["suggestions"][0]["suggestions"]
        self.assertTrue(
            any(s["kind"] == "remove" and s["selector"] == 'input[name="title"]' for s in sugs)
        )

    def test_fallback_hit_suggests_promote(self):
        # 主选择器 #title 0 命中，兜底 input[placeholder*="标题"] 全命中（位置 2）
        agg = self._agg({"标题输入": {}}, publishes=3)
        agg["groups"]["标题输入"]["selectors"] = {
            "#title": {"hits": 0, "rate": 0.0},
            'input[placeholder*="标题"]': {"hits": 3, "rate": 1.0},
        }
        result = ledger.suggest_trimming(agg, min_publishes=2)
        sugs = result["suggestions"][0]["suggestions"]
        kinds = {s["kind"] for s in sugs}
        self.assertIn("promote", kinds)
        promoted = next(s for s in sugs if s["kind"] == "promote")
        self.assertEqual(promoted["selector"], 'input[placeholder*="标题"]')
        self.assertEqual(promoted["position"], 2)

    def test_insufficient_samples_skipped(self):
        agg = self._agg({"标题输入": {"#title": 1}}, publishes=1)
        result = ledger.suggest_trimming(agg, min_publishes=2)
        self.assertEqual(result["suggestions"], [])

    def test_reordered_by_hits_keeps_full_current_list(self):
        agg = self._agg({"标题输入": {"#title": 2}}, publishes=3)
        agg["groups"]["标题输入"]["selectors"] = {
            "#title": {"hits": 3, "rate": 1.0},
            'input[placeholder*="标题"]': {"hits": 1, "rate": 1 / 3},
            'input[name="title"]': {"hits": 0, "rate": 0.0},
        }
        result = ledger.suggest_trimming(agg, min_publishes=2)
        ordered = result["reordered"]["标题输入"]
        # 当前配置的完整清单都被保留（无数据的选择器按原位置排），只按命中次数重排
        self.assertEqual(
            ordered,
            [
                "#title",
                'input[placeholder*="标题"]',
                'textarea[placeholder*="标题"]',
                ".title_editor .weui-desktop-form__input input",
                'input[name="title"]',
            ],
        )
        # 无台账数据的选择器不产生 remove 建议
        self.assertEqual(
            [s["selector"] for s in result["suggestions"][0]["suggestions"]],
            ['input[name="title"]'],
        )


class AnalyzeReportTest(unittest.TestCase):
    def test_analyze_pipeline_with_temp_dir(self):
        with tempfile.TemporaryDirectory() as d:
            ledger.append_selector_stats(
                _report(
                    {"标题输入": {"#title": 1}},
                    never_hit={"标题输入": ['input[name="title"]']},
                ),
                ledger_dir=d,
            )
            result = ledger.analyze(days=1, min_publishes=1, ledger_dir=d)
            self.assertEqual(result["aggregated"]["publishes"], 1)
            self.assertTrue(result["suggestions"])
            self.assertIn("标题输入", result["reordered"])

    def test_print_report_runs_with_records(self):
        with tempfile.TemporaryDirectory() as d:
            ledger.append_selector_stats(
                _report(
                    {"标题输入": {"#title": 1}},
                    never_hit={"标题输入": ['input[name="title"]']},
                ),
                ledger_dir=d,
            )
            buf = io.StringIO()
            with redirect_stdout(buf):
                ledger.print_report(days=1, min_publishes=1, ledger_dir=d)
            out = buf.getvalue()
            self.assertIn("选择器命中率台账", out)
            self.assertIn("[标题输入]", out)
            self.assertIn("精简建议", out)

    def test_print_report_empty(self):
        with tempfile.TemporaryDirectory() as d:
            buf = io.StringIO()
            with redirect_stdout(buf):
                ledger.print_report(days=1, min_publishes=1, ledger_dir=d)
            self.assertIn("暂无记录", buf.getvalue())

    def test_group_keys_match_default_selectors(self):
        # 标签映射里的每个分组键都必须在 DEFAULT_SELECTORS 中存在
        for key in ws.SELECTOR_GROUP_KEYS.values():
            self.assertIn(key, ws.DEFAULT_SELECTORS, key)


if __name__ == "__main__":
    unittest.main()
