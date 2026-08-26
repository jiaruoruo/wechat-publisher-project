"""公众号编辑器选择器清单 — 集中维护，支持 YAML/JSON 外置与运行时覆盖

选择器默认值内置在本文件（代码兜底），实际生效值按优先级从低到高合并：

    1. 内置默认值 DEFAULT_SELECTORS
    2. config/selectors.yaml（推荐维护位置：编辑器改版时只需改此文件）
    3. settings.yaml 的 wechat.selectors_file 指向的自定义文件
    4. 环境变量 WECHAT_SELECTORS_FILE 指向的自定义文件（优先级最高）

同级分组的 YAML 值整体替换内置默认（列表 = 多选择器按顺序回退，
字符串 = 单一选择器）。文件格式按扩展名识别：.yaml/.yml 用 PyYAML，
.json 用标准库 json。

长驻进程（Hermes）编辑 YAML 后无需重启：调用 reload_selectors() 热加载。
publish_actions 通过模块引用（sel.NAV_NEW_ARTICLE）读取常量，
因此 reload 后立即生效。
"""

import json
import logging
import os

from config.paths import CONFIG_DIR, SETTINGS_PATH

logger = logging.getLogger(__name__)

# 交互标签 → 配置分组键（publish_actions 的命中率统计、selector_ledger 的
# 台账分析共用；新增交互时两边自动生效）
SELECTOR_GROUP_KEYS = {
    "新建图文入口": "nav_new_article",
    "标题输入": "title_inputs",
    "正文编辑器": "content_editors",
    "封面文件上传": "cover_file_inputs",
    "封面区域": "cover_area",
    "从正文选择": "cover_select_from_body",
    "正文首图": "cover_body_first_image",
    "摘要输入": "summary_inputs",
    "保存草稿": "draft_buttons",
    "发布": "publish_buttons",
    "登录态特征": "login_indicators",
}

try:
    import yaml
except ImportError:  # pragma: no cover - yaml 是项目硬依赖
    yaml = None

# ── 内置默认值（config/selectors.yaml 缺失/损坏时的兜底）────────────────
DEFAULT_SELECTORS: dict = {
    # 导航
    "nav_new_article": [
        'a:has-text("新的创作")',
        'a:has-text("图文消息")',
        ".new-creation-accordion",
        ".weui-desktop-btn_wrp .weui-desktop-btn",
        'a[href*="appmsg"]',
    ],
    "nav_editor_load": "#title, .edui-body-container, [contenteditable='true']",
    # 标题
    "title_inputs": [
        "#title",
        'input[placeholder*="标题"]',
        'textarea[placeholder*="标题"]',
        ".title_editor .weui-desktop-form__input input",
        'input[name="title"]',
    ],
    # 正文编辑器
    "content_editors": [
        ".edui-body-container",
        "#ueditor_0",
        ".ProseMirror",
        '[contenteditable="true"]',
    ],
    "content_verify_editor": '[contenteditable="true"], .edui-body-container, .ProseMirror',
    # 封面区域
    "cover_area": [
        ".js_cover_area",
        ".cover_area",
        ".cover-panel",
        ".js_cover",
        ".cover-upload-area",
        'a[class*="cover"]',
        "text=封面",
    ],
    "cover_select_from_body": [
        "text=从正文选择",
        'button:has-text("从正文选择")',
        'a:has-text("从正文选择")',
        'li:has-text("从正文选择")',
        'label:has-text("从正文选择")',
        "text=从正文中选择",
        'div:has-text("从正文")',
        '[class*="choose"]:has-text("正文")',
        "text=正文",  # 最后兜底
    ],
    "cover_body_first_image": [
        ".cover-body-img",
        ".js_cover_from_body img",
        '[class*="from-body"] img',
        '[class*="from_body"] img',
        ".weui-desktop-radio-group img",
    ],
    "cover_preview": ".cover_preview, .js_cover_preview",
    # 封面上传（文件方式）
    "cover_file_inputs": [
        'input[type="file"][accept*="image"]',
        '.cover-upload input[type="file"]',
        '.weui-desktop-form__upload input[type="file"]',
    ],
    "cover_upload_btn": 'text="上传"',
    # 摘要
    "summary_inputs": [
        "#js_description",
        'textarea[placeholder*="摘要"]',
        'textarea[name="digest"]',
        ".weui-desktop-form__textarea",
    ],
    # 保存草稿
    "draft_buttons": [
        'button:has-text("保存草稿")',
        'a:has-text("保存草稿")',
        "#js_send",
        '.weui-desktop-btn:has-text("保存")',
    ],
    # 发布（群发）
    "publish_buttons": [
        'button:has-text("群发")',
        'a:has-text("群发")',
        "#js_send",
        '.weui-desktop-btn:has-text("发表")',
        'button:has-text("发表")',
    ],
    # 通用
    "confirm_button": 'button:has-text("确定")',
    "toast": ".weui-desktop-toast, .weui-toast",
    # 已登录后台的特征选择器（check_login 用；编辑器改版时一处维护）
    "login_indicators": [
        ".weui-desktop-panel",
    ],
}


def _load_file(path: str) -> dict | None:
    """按扩展名加载 YAML/JSON 选择器文件；路径无效或解析失败返回 None"""
    if not path or not os.path.exists(path):
        return None
    try:
        ext = os.path.splitext(path)[1].lower()
        with open(path, "r", encoding="utf-8") as f:
            if ext == ".json":
                data = json.load(f)
            else:
                if yaml is None:
                    logger.warning(f"PyYAML 未安装，无法解析选择器文件: {path}")
                    return None
                data = yaml.safe_load(f)
        return data if isinstance(data, dict) else None
    except Exception as e:
        logger.warning(f"选择器文件解析失败（忽略）: {path}: {e}")
        return None


def _merge(base: dict, override: dict) -> dict:
    """分组级合并：override 中的每个键整体替换 base 同名键，新增键保留"""
    merged = dict(base)
    merged.update(override)
    return merged


def load_selectors(custom_paths=()) -> dict:
    """按优先级合并选择器：内置默认 → config/selectors.yaml → 自定义路径（后者覆盖前者）

    Args:
        custom_paths: 额外选择器文件路径（按顺序，后者覆盖前者）
    """
    selectors = dict(DEFAULT_SELECTORS)
    paths = [os.path.join(CONFIG_DIR, "selectors.yaml"), *list(custom_paths)]
    for path in paths:
        data = _load_file(path)
        if data:
            selectors = _merge(selectors, data)
    return selectors


def _custom_paths() -> list[str]:
    """收集运行时覆盖路径：settings.yaml 的 wechat.selectors_file + 环境变量（后者优先）"""
    paths = []
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            cfg = (yaml.safe_load(f) if yaml else {}) or {}
        p = (cfg.get("wechat") or {}).get("selectors_file")
        if p:
            paths.append(os.path.abspath(p))
    except Exception:
        pass
    env = os.environ.get("WECHAT_SELECTORS_FILE")
    if env:
        paths.append(os.path.abspath(env))
    return paths


def get_selectors() -> dict:
    """当前生效的选择器（含默认 + 所有覆盖）"""
    return _SELECTORS


def get_selector_list(key: str) -> list[str] | None:
    """读取某分组选择器并规范为列表；分组不存在返回 None

    列表/字符串统一返回 list[str]；便于调用方（如 check_login）做多选择器回退。
    """
    value = _SELECTORS.get(key)
    if value is None:
        return None
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str)]
    return None


def validate_selectors_config(selectors_file: str | None = None) -> list[tuple[str, str]]:
    """校验 selectors 配置文件（默认 config/selectors.yaml），对比内置默认值

    Returns:
        [(severity, message), ...]，severity ∈ {"warning", "error"}；
        无问题返回空列表。

    检查项：
    - 文件缺失 / YAML 解析失败 / 非映射 → warning（回退内置默认值）
    - 缺失分组（对比 DEFAULT_SELECTORS 键）→ warning（该分组回退默认）
    - 空列表 / 空字符串分组 → error（该交互将无法执行）
    - 分组类型异常（非列表/字符串）或列表内空条目 → warning
    """
    issues: list[tuple[str, str]] = []
    path = selectors_file or os.path.join(CONFIG_DIR, "selectors.yaml")
    if not os.path.exists(path):
        issues.append(("warning", f"选择器配置文件不存在: {path}（使用内置默认值）"))
        return issues
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) if yaml else None
    except Exception as e:
        issues.append(("warning", f"选择器配置文件解析失败: {path}: {e}（使用内置默认值）"))
        return issues
    if not isinstance(data, dict):
        issues.append(("warning", f"选择器配置文件格式错误（应为键值对映射）: {path}（使用内置默认值）"))
        return issues

    for key in DEFAULT_SELECTORS:
        if key not in data:
            issues.append(("warning", f"选择器配置缺少分组 {key}（该分组回退内置默认值）"))

    # SELECTOR_GROUP_KEYS（交互标签→分组键）与 DEFAULT_SELECTORS 键的一致性：
    # 命中率统计（publish_actions）与台账分析（selector_ledger）都依赖它反查分组，
    # 键名不一致会导致对应交互的统计缺失。
    for label, key in SELECTOR_GROUP_KEYS.items():
        if key not in DEFAULT_SELECTORS:
            issues.append((
                "warning",
                f"SELECTOR_GROUP_KEYS 引用了不存在的分组键 {key}（标签 {label}），"
                f"该交互的命中率统计将缺失",
            ))

    for key, value in data.items():
        if isinstance(value, list):
            if not value:
                issues.append(("error", f"选择器分组 {key} 为空列表（该交互将无法执行）"))
            for entry in value:
                if not isinstance(entry, str) or not entry.strip():
                    issues.append(("warning", f"选择器分组 {key} 含空/非法条目: {entry!r}"))
        elif isinstance(value, str):
            if not value.strip():
                issues.append(("error", f"选择器分组 {key} 为空字符串（该交互将无法执行）"))
        elif value is None:
            issues.append(("warning", f"选择器分组 {key} 为 null（该交互将无法执行）"))
        else:
            issues.append(("warning", f"选择器分组 {key} 类型异常: {type(value).__name__}（应为列表或字符串）"))
    return issues


def reload_selectors() -> dict:
    """热加载选择器：重新读取 YAML/JSON 并更新模块级常量，无需重启进程"""
    global _SELECTORS
    _SELECTORS = load_selectors(_custom_paths())
    logger.info(f"选择器已重新加载（{len(_SELECTORS)} 个分组）")
    return _SELECTORS


# 模块加载时计算一次（含 config/selectors.yaml 与运行时覆盖）；
# 长驻进程可调用 reload_selectors() 刷新
_SELECTORS = load_selectors(_custom_paths())


def __getattr__(name: str):
    """支持 sel.NAV_NEW_ARTICLE 形式的访问（键名转大写）

    替代原先的 globals()[KEY.upper()] = value 动态注入：
    IDE 可静态分析、重命名安全，且刷新 _SELECTORS 后自动生效，无需重注入。
    """
    key = name.lower()
    if key in _SELECTORS:
        return _SELECTORS[key]
    raise AttributeError(f"module 'wechat_selectors' has no attribute {name!r}")
