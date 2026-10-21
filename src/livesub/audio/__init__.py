"""音频采集：麦克风 / WAV 回放 / 合成信号（测试用）。"""

from .capture import AudioSource, MicSource, SyntheticSource, WavSource, list_input_devices

__all__ = ["AudioSource", "MicSource", "WavSource", "SyntheticSource", "list_input_devices"]
