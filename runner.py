"""根启动器：保证 `python -I runner.py ...` 也能找到工程包（不依赖 cwd/PYTHONPATH）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from adjudicator.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
