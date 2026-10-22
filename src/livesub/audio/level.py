"""音量（RMS）与 dBFS 的换算。

设置页的音量条和 VAD 的阈值都用 dBFS 显示：
    0 dBFS = 数字满刻度，-60 dBFS 以下基本听不见。
人说话大约在 -30 ~ -12 dBFS；小声/远场麦克风可能只有 -45 dBFS。
"""

from __future__ import annotations

import math

# 低于这个值直接当成静音，避免 log10(0) 变成 -inf
FLOOR_DB = -90.0


def rms_to_db(rms: float) -> float:
    """RMS（0~1）→ dBFS。"""
    value = float(rms)
    if value <= 1e-9:
        return FLOOR_DB
    return max(FLOOR_DB, 20.0 * math.log10(value))


def db_to_rms(db: float) -> float:
    """dBFS → RMS。"""
    return 10.0 ** (max(FLOOR_DB, float(db)) / 20.0)


def level_percent(db: float, floor_db: float = -60.0) -> float:
    """把 dBFS 映射成 0~100 的条宽，方便前端画电平。"""
    span = max(1.0, -floor_db)
    return max(0.0, min(100.0, (float(db) - floor_db) / span * 100.0))
