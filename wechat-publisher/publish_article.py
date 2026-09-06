"""发布现有 article.md 到公众号（草稿模式）。

解析 _compose_markdown 产出的 article.md -> 复用 FormatterAgent 的本地转换
（确定性、保留原文、剥离 [IMAGE:...] 占位符）生成公众号兼容 HTML ->
组装 ArticleState -> 走现有 PublisherAgent 发布（草稿）。

依赖：article.md 与 cover.png 与本脚本同目录；config 提供有效的微信登录会话
（storage/browser_session.json）。若会话过期，PublisherAgent 会尝试交互式登录
并失败，此时需先本地执行 `python main.py login` 扫码后重试。

用法：python publish_article.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from models.llm_router import LLMRouter, load_config
from graph.state import create_initial_state
from agents.formatter import FormatterAgent
from agents.publisher import PublisherAgent

ARTICLE_PATH = "article.md"
COVER_PATH = "cover.png"


def parse_article(path: str):
    """从 article.md 解析出 标题 / 摘要 / 正文。

    _compose_markdown 产出格式：
        # 标题
        > 摘要
        标签：...
        封面提示词：...
        # 标题        <-- 正文起始
        正文...
    """
    with open(path, encoding="utf-8") as f:
        lines = f.read().splitlines()

    title_indices = [i for i, l in enumerate(lines) if l.startswith("# ")]
    if not title_indices:
        raise ValueError("article.md 未找到标题（# 开头行）")
    title = lines[title_indices[0]][2:].strip()

    # 正文从第二个 # 标题之后开始
    body_start = (
        title_indices[1] + 1 if len(title_indices) >= 2 else title_indices[0] + 1
    )
    body = "\n".join(lines[body_start:]).strip()

    summary = ""
    for l in lines:
        if l.startswith("> "):
            summary = l[2:].strip()
            break

    return title, summary, body


def main():
    config = load_config()
    base_dir = os.path.dirname(os.path.abspath(__file__))
    article_path = os.path.join(base_dir, ARTICLE_PATH)
    cover_path = os.path.join(base_dir, COVER_PATH)

    title, summary, body = parse_article(article_path)
    print(f"[publish] 标题: {title}")
    print(f"[publish] 摘要: {summary[:50]}...")
    print(f"[publish] 正文长度: {len(body)} 字")
    print(f"[publish] 封面: {cover_path} (exists={os.path.exists(cover_path)})")

    # 复用 FormatterAgent 的本地转换：确定性、保留原文、剥离 [IMAGE:...] 占位符
    router = LLMRouter(config)
    formatter = FormatterAgent(router, config)
    formatted_html = formatter._format_locally(body, [])
    print(f"[publish] 转换后 HTML 长度: {len(formatted_html)}")

    state = create_initial_state(
        extra={
            "article_title": title,
            "formatted_html": formatted_html,
            "cover_image_path": cover_path,
            "summary": summary,
            "inline_images": [],
        }
    )

    publisher = PublisherAgent(router, config)
    result = publisher.run(state)

    pr = result.get("publish_result", {})
    print("\n[publish] 结果:")
    print(f"  success: {pr.get('success')}")
    print(f"  mode:    {pr.get('mode')}")
    print(f"  url:     {pr.get('url', '')}")
    if pr.get("error"):
        print(f"  error:   {pr['error']}")


if __name__ == "__main__":
    main()
