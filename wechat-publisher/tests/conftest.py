"""pytest 集成：使端到端测试的跳过行为可感知。

当 tests/test_e2e_workflow.py 因 langgraph 等运行时依赖缺失而被整体跳过时，
在终端结果摘要中输出明确可见的警告（详见 tests/README.md）。
本文件只做报告增强，不引入网络或真实 LLM 调用。
"""

import os


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    skipped = terminalreporter.stats.get("skipped", [])
    e2e_skips = [
        rep for rep in skipped
        if os.path.basename(str(rep.fspath)) == "test_e2e_workflow.py"
    ]
    if not e2e_skips:
        return
    terminalreporter.write_sep("=", "WARNING: 端到端覆盖被跳过（依赖缺失）")
    terminalreporter.write_line(
        f"langgraph 等运行时依赖未安装，tests/test_e2e_workflow.py 的 "
        f"{len(e2e_skips)} 个端到端用例已全部跳过（skip）。"
    )
    terminalreporter.write_line(
        "如需恢复端到端覆盖，请安装完整依赖（requirements.txt）后重新运行套件。"
    )
