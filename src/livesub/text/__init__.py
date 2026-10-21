"""文本后处理（目前只有日语）。"""

from .ja import Ending, EndingInfo, analyze, is_filler_only, strip_fillers

__all__ = ["Ending", "EndingInfo", "analyze", "is_filler_only", "strip_fillers"]
