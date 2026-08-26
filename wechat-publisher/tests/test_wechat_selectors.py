"""tests for browser/wechat_selectors.py — YAML/JSON 外置加载与运行时覆盖

覆盖：
- 内置默认值完整性 + 模块常量与生效配置一致
- YAML / JSON 覆盖：分组级替换、未覆盖分组保持默认、后者覆盖前者
- 缺失 / 损坏文件回退默认
- 运行时覆盖：环境变量 WECHAT_SELECTORS_FILE、reload_selectors 热加载
"""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from browser import wechat_selectors as ws

# publish_actions 实际引用的全部分组
REQUIRED_GROUPS = [
    "nav_new_article",
    "nav_editor_load",
    "title_inputs",
    "content_editors",
    "content_verify_editor",
    "cover_area",
    "cover_select_from_body",
    "cover_body_first_image",
    "cover_preview",
    "cover_file_inputs",
    "cover_upload_btn",
    "summary_inputs",
    "draft_buttons",
    "publish_buttons",
    "confirm_button",
    "toast",
]


class DefaultSelectorsTest(unittest.TestCase):
    def test_defaults_cover_all_groups(self):
        for key in REQUIRED_GROUPS:
            self.assertIn(key, ws.DEFAULT_SELECTORS, key)

    def test_defaults_lists_are_non_empty(self):
        for key, value in ws.DEFAULT_SELECTORS.items():
            self.assertTrue(value, f"{key} 为空")

    def test_module_constants_match_loaded_config(self):
        self.assertEqual(ws.NAV_NEW_ARTICLE, ws._SELECTORS["nav_new_article"])
        self.assertEqual(ws.TITLE_INPUTS, ws._SELECTORS["title_inputs"])
        self.assertEqual(ws.TOAST, ws._SELECTORS["toast"])
        self.assertEqual(ws.COVER_SELECT_FROM_BODY, ws._SELECTORS["cover_select_from_body"])


class LoadSelectorsTest(unittest.TestCase):
    def test_missing_file_falls_back_to_defaults(self):
        merged = ws.load_selectors(["/nonexistent/selectors.yaml"])
        self.assertEqual(merged, ws.DEFAULT_SELECTORS)

    def test_yaml_override_replaces_group_only(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "override.yaml")
            with open(path, "w", encoding="utf-8") as f:
                f.write('title_inputs:\n  - "#my-title"\n  - ".custom"\n')
            merged = ws.load_selectors([path])
            self.assertEqual(merged["title_inputs"], ["#my-title", ".custom"])
            # 未覆盖的分组保持默认
            self.assertEqual(merged["nav_new_article"], ws.DEFAULT_SELECTORS["nav_new_article"])
            self.assertEqual(merged["toast"], ws.DEFAULT_SELECTORS["toast"])

    def test_json_override(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "override.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"toast": ".custom-toast"}, f)
            merged = ws.load_selectors([path])
            self.assertEqual(merged["toast"], ".custom-toast")

    def test_string_group_override(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "override.yaml")
            with open(path, "w", encoding="utf-8") as f:
                f.write("toast: '.single-toast'\n")
            merged = ws.load_selectors([path])
            self.assertEqual(merged["toast"], ".single-toast")

    def test_later_paths_win(self):
        with tempfile.TemporaryDirectory() as d:
            p1 = os.path.join(d, "a.yaml")
            p2 = os.path.join(d, "b.yaml")
            with open(p1, "w", encoding="utf-8") as f:
                f.write("toast: '.a'\n")
            with open(p2, "w", encoding="utf-8") as f:
                f.write("toast: '.b'\n")
            merged = ws.load_selectors([p1, p2])
            self.assertEqual(merged["toast"], ".b")

    def test_invalid_yaml_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "bad.yaml")
            with open(path, "w", encoding="utf-8") as f:
                f.write("a: [1, 2, 3\n  b: unclosed")  # 未闭合 flow 序列，非法 YAML
            merged = ws.load_selectors([path])
            self.assertEqual(merged, ws.DEFAULT_SELECTORS)

    def test_non_dict_yaml_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "list.yaml")
            with open(path, "w", encoding="utf-8") as f:
                f.write("- a\n- b\n")
            merged = ws.load_selectors([path])
            self.assertEqual(merged, ws.DEFAULT_SELECTORS)

    def test_arbitrary_new_group_allowed(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "x.yaml")
            with open(path, "w", encoding="utf-8") as f:
                f.write("my_custom_group:\n  - '.a'\n")
            merged = ws.load_selectors([path])
            self.assertIn("my_custom_group", merged)


class RuntimeOverrideTest(unittest.TestCase):
    def test_env_var_override_applied(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "env.yaml")
            with open(path, "w", encoding="utf-8") as f:
                f.write("toast: '.env-toast'\n")
            with mock.patch.dict(os.environ, {"WECHAT_SELECTORS_FILE": path}):
                selectors = ws.load_selectors(ws._custom_paths())
                self.assertEqual(selectors["toast"], ".env-toast")

    def test_reload_selectors_updates_constants(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "reload.yaml")
            with open(path, "w", encoding="utf-8") as f:
                f.write("toast: '.reloaded'\n")
            with mock.patch.dict(os.environ, {"WECHAT_SELECTORS_FILE": path}):
                ws.reload_selectors()
                try:
                    self.assertEqual(ws.TOAST, ".reloaded")
                    self.assertEqual(ws.get_selectors()["toast"], ".reloaded")
                finally:
                    pass
            # env 已恢复 → reload 回默认
            ws.reload_selectors()
            self.assertEqual(ws.TOAST, ws.DEFAULT_SELECTORS["toast"])

    def test_reload_restores_other_constants(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "reload.yaml")
            with open(path, "w", encoding="utf-8") as f:
                f.write("title_inputs:\n  - '#custom-title'\n")
            with mock.patch.dict(os.environ, {"WECHAT_SELECTORS_FILE": path}):
                ws.reload_selectors()
                try:
                    self.assertEqual(ws.TITLE_INPUTS, ["#custom-title"])
                    # 未覆盖分组不受影响
                    self.assertEqual(ws.TOAST, ws.DEFAULT_SELECTORS["toast"])
                finally:
                    pass
            ws.reload_selectors()


class ValidateSelectorsConfigTest(unittest.TestCase):
    """validate_selectors_config：缺失分组 / 空列表 / 损坏 YAML 等告警"""

    def _write(self, content, suffix=".yaml"):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "selectors" + suffix)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return path

    def test_valid_full_file_no_issues(self):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "config", "selectors.yaml")
        self.assertTrue(os.path.exists(path), "config/selectors.yaml 应存在")
        self.assertEqual(ws.validate_selectors_config(path), [])

    def test_missing_file_warns(self):
        issues = ws.validate_selectors_config("/nonexistent/selectors.yaml")
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0][0], "warning")
        self.assertIn("不存在", issues[0][1])

    def test_corrupted_yaml_warns(self):
        path = self._write("a: [1, 2, 3\n  b: unclosed")
        issues = ws.validate_selectors_config(path)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0][0], "warning")
        self.assertIn("解析失败", issues[0][1])

    def test_non_mapping_warns(self):
        path = self._write("- a\n- b\n")
        issues = ws.validate_selectors_config(path)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0][0], "warning")
        self.assertIn("格式错误", issues[0][1])

    def test_missing_group_warns(self):
        path = self._write("title_inputs:\n  - '#title'\n")
        issues = ws.validate_selectors_config(path)
        kinds = [s for s, _ in issues]
        self.assertIn("warning", kinds)
        self.assertTrue(
            any("缺少分组" in m and "title_inputs" not in m for _, m in issues),
            issues,
        )

    def test_empty_list_is_error(self):
        path = self._write("title_inputs: []\n")
        issues = ws.validate_selectors_config(path)
        self.assertTrue(any(s == "error" and "空列表" in m for s, m in issues), issues)

    def test_empty_string_group_is_error(self):
        path = self._write("toast: ''\n")
        issues = ws.validate_selectors_config(path)
        self.assertTrue(any(s == "error" and "空字符串" in m for s, m in issues), issues)

    def test_type_error_warns(self):
        path = self._write("toast: 123\n")
        issues = ws.validate_selectors_config(path)
        self.assertTrue(any(s == "warning" and "类型异常" in m for s, m in issues), issues)

    def test_null_group_warns(self):
        path = self._write("toast: null\n")
        issues = ws.validate_selectors_config(path)
        self.assertTrue(any(s == "warning" and "为 null" in m for s, m in issues), issues)

    def test_empty_entry_in_list_warns(self):
        path = self._write("title_inputs:\n  - '#title'\n  - ''\n")
        issues = ws.validate_selectors_config(path)
        self.assertTrue(any(s == "warning" and "含空/非法条目" in m for s, m in issues), issues)


if __name__ == "__main__":
    unittest.main()
