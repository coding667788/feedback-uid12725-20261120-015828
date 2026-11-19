"""测试包自举：在 python -I（隔离模式）下也能导入工程包。

-I 会剥离 cwd 与 PYTHONPATH，而本测试以源码目录形式运行、未安装为 site-package，
因此在测试包初始化时把工程根目录显式加入 sys.path。
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
