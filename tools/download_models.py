"""模型下载脚本（等价于 `python -m livesub download`）。

    python tools/download_models.py --search "zipformer ja"
    python tools/download_models.py --repo owner/name --to models/sherpa-ja
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from livesub import models  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(models.main(sys.argv[1:]))
