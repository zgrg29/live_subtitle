"""打包入口（PyInstaller 用）。

源码运行时也可以直接 `python launcher.py`，行为等同于 `python -m livesub`。
"""

from __future__ import annotations

import sys
from pathlib import Path

if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from livesub.__main__ import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
