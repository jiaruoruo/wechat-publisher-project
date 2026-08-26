"""统一测试入口：一行命令跑全部测试

用法：
    python tests/run_all.py            # 默认 discover 当前目录 test_*.py
    python tests/run_all.py -v         # verbose
    python -m pytest                   # 若已安装 pytest（见 pytest.ini）

等价于 `python -m unittest discover -s tests -p "test_*.py"`，
但集中在一处，便于 CI / 本地一键执行。
"""
import sys
import unittest


def main():
    loader = unittest.TestLoader()
    suite = loader.discover("tests", pattern="test_*.py")
    runner = unittest.TextTestRunner(verbosity=2 if "-v" in sys.argv else 1)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    main()
