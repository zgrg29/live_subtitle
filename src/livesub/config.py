"""配置系统：一个 TOML 文件，全部缺省值写在代码里。

原则：用户永远不需要配置就能跑。config.toml 只在需要改（设备、字体、颜色）时才存在，
首次运行会自动生成一份带注释的默认文件。
"""

from __future__ import annotations

import sys
import tomllib
from dataclasses import dataclass, fields, is_dataclass
from pathlib import Path
from typing import Any

CONFIG_FILENAME = "config.toml"


def project_root() -> Path:
    """源码根目录（src 的上一级）。与当前工作目录无关。"""
    return Path(__file__).resolve().parents[2]


def is_frozen() -> bool:
    """是否在 PyInstaller 打出来的 exe 里跑。"""
    return bool(getattr(sys, "frozen", False))


def data_dir() -> Path:
    """可写数据目录：config、models、logs 都放这里。

    打包后 = exe 所在目录（绿色版，整个文件夹可搬运）；
    源码运行 = 项目根目录。
    """
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return project_root()


def bundle_dir() -> Path:
    """只读资源目录：overlay 等打包进 exe 的静态文件。

    PyInstaller 6 的 onedir 会把数据放进 `_internal`，运行时由 sys._MEIPASS 指向。
    """
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", data_dir()))
    return project_root()


def resolve_config_path(path: str | Path = CONFIG_FILENAME) -> Path:
    """把配置路径解析成绝对路径。

    优先级：显式绝对路径 > 当前目录已存在的 > 数据目录（exe 旁 / 项目根）的。
    这样从任何目录（Win+R、快捷方式、别的程序）启动都能找到同一份配置。
    """
    p = Path(path)
    if p.is_absolute():
        return p
    if p.exists():
        return p.resolve()
    return data_dir() / p


@dataclass
class AudioConfig:
    device: str = ""  # 空 = 系统默认麦克风；也可填设备名的一部分，如 "Microphone"
    # PortAudio 的设备索引。同名麦克风（两个同型号 USB 麦）只靠名字会选到第一个，
    # 所以设置页会同时保存索引；重启后索引变了会退回按名字匹配。
    device_index: int = -1
    # 0 起始的输入通道。-1 = 所有通道取平均。
    # 2ch 音频接口（MOTU / UR22 / AG03…）常把麦克风插在输入 2，只取 0 号通道
    # 会得到"有电平但没人声"的音频：电平条会动，识别却一直是空字符串。
    channel: int = 0
    sample_rate: int = 16000  # ASR 模型通常是 16k，最好不要改
    block_ms: int = 20  # 每次读取的音频块长度，20ms 是延迟/开销的平衡点
    gain: float = 1.0


@dataclass
class VADConfig:
    threshold: float = 0.008  # 绝对 RMS 门限（静音环境下 0.008 足够灵敏）
    noise_ratio: float = 2.5  # 同时要求高于噪声底 N 倍，避免风扇声被当成说话
    min_speech_ms: int = 160  # 短于此不认为是一句话（挡掉咳嗽、碰麦）
    end_silence_ms: int = 320  # 声学收句阈值；切碎了由 [segment] 的合并兜底
    max_utterance_s: float = 8.0  # 单句上限，防止不停顿导致延迟堆积（会在低能量处切）
    min_utterance_ms: int = 250  # 太短的片段直接丢掉
    pre_roll_ms: int = 120  # 起始前保留一点音频，避免吃掉第一个音节


@dataclass
class SegmentConfig:
    """半句合并：声学切句之后，再按日语句尾决定"要不要等一下"。"""

    merge: bool = True  # 总开关；关掉就退回"静音 420ms 就出"的老行为
    merge_wait_ms: int = 800  # 句子看起来没说完时，等后续语音的最大停顿
    max_merge: int = 2  # 最多合并几段（3 段音频封顶），防止无限等
    max_total_s: float = 12.0  # 合并后音频总长上限
    max_hold_ms: int = 12000  # 暂缓的硬上限（护栏，正常不会触发）
    # 上一段短于这个时长时，合并后重新识别一遍（补上下文，更准但多花一次 ASR）；
    # 长片段本身上下文已经够，直接拼文本，避免长音频再识别一次导致延迟翻倍。
    re_asr_under_ms: int = 2500


@dataclass
class ASRConfig:
    engine: str = "auto"  # auto | sherpa | mock
    model_dir: str = "models/sherpa-ja"  # 相对项目根目录
    num_threads: int = 2  # 纯 CPU 下 2 线程足够，多了会抢 OBS/x264
    lead_pad_ms: int = 200  # 段首补静音，避免离线模型吞掉第一个短词
    fallback_to_mock: bool = True  # 模型缺失时用 demo 引擎，保证产品能启动


@dataclass
class MTConfig:
    engine: str = "opus"  # opus（默认，75M ONNX）| qwen（实验，需要 llama.cpp）| m2m100 | none | echo
    model_dir: str = "models/opus-mt-ja-zh-int8"
    max_new_tokens: int = 48  # 合并后的长句需要更多解码步数
    # 实验项：Qwen 走 llama.cpp（这台机器上太慢，默认不启用）
    model_path: str = "models/qwen2.5-1.5b/qwen2.5-1.5b-instruct-q4_k_m.gguf"
    llama_server: str = "tools/llama/llama-server.exe"
    context: int = 512
    num_threads: int = 2
    target_lang: str = "zh"
    max_chars: int = 40  # 中文单行硬上限
    punctuate: bool = True  # 译前给 ASR 的无标点文本补句末标点（实测 +0.05 F1）
    fallback_to_none: bool = True  # 模型缺失时只显示日语，不阻止启动


@dataclass
class GlossaryConfig:
    enabled: bool = True  # 关掉就是"裸模型"，用于 A/B 对比
    dir: str = "glossary"  # 相对 exe 目录 / 项目根；用户可放同名文件覆盖内置条目
    max_phrase_len: int = 24  # 超过这个长度就不做整句查表，避免长句被套模板


@dataclass
class OverlayConfig:
    bilingual: bool = True  # 是否同时显示日语原文
    font_family: str = '"Microsoft YaHei", "Noto Sans SC", "PingFang SC", sans-serif'
    zh_size: int = 56
    ja_size: int = 30
    zh_color: str = "#FFFFFF"
    ja_color: str = "#B9C6D4"
    outline_color: str = "#000000"
    outline_width: int = 5  # 描边宽度(px)，0 为不描边
    background: str = "transparent"  # 例如 rgba(0,0,0,.35)
    position: str = "bottom"  # bottom | top
    margin_percent: int = 8  # 距上下边缘的百分比
    max_lines: int = 2  # 屏幕上最多保留几行，多了滚动消失
    line_gap: int = 10  # 行间距(px)
    fade_ms: int = 180  # 淡入时长
    # 最后一句字幕显示多久后自动消失（毫秒）；0 = 一直留着。
    # 5 秒是「一句话说完后还有足够时间读完」和「沉默时别一直挂在屏幕上」的折中；
    # 前端还会按字数给长句自动延长一点（见 overlay/index.html）。
    clear_after_ms: int = 5000


@dataclass
class UiConfig:
    open_admin: bool = True  # 启动时自动用浏览器打开设置页


@dataclass
class RecordConfig:
    """可选：把麦克风音频 + 识别/翻译结果存到本地，用来持续改进。

    默认关闭。开启后数据只写本机，不发任何网络请求。
    """

    enabled: bool = False
    dir: str = "recordings"  # 相对数据目录（源码根 / exe 所在目录）
    max_minutes: int = 60  # 单个 WAV 文件最长多少分钟，超过就分段；0 = 不分段


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"  # 只监听本机；不要改成 0.0.0.0
    port: int = 8765


@dataclass
class DebugConfig:
    save_wav: bool = False  # 保存每句话的音频，用于调 ASR
    log_utf8: bool = True
    show_latency_hud: bool = False  # 默认不显示；OBS 里加 ?debug=1 即可


@dataclass
class Config:
    audio: AudioConfig = None  # type: ignore[assignment]
    vad: VADConfig = None  # type: ignore[assignment]
    segment: SegmentConfig = None  # type: ignore[assignment]
    asr: ASRConfig = None  # type: ignore[assignment]
    mt: MTConfig = None  # type: ignore[assignment]
    glossary: GlossaryConfig = None  # type: ignore[assignment]
    overlay: OverlayConfig = None  # type: ignore[assignment]
    ui: UiConfig = None  # type: ignore[assignment]
    record: RecordConfig = None  # type: ignore[assignment]
    server: ServerConfig = None  # type: ignore[assignment]
    debug: DebugConfig = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        for name, cls in _SECTIONS.items():
            if getattr(self, name) is None:
                setattr(self, name, cls())


_SECTIONS = {
    "audio": AudioConfig,
    "vad": VADConfig,
    "segment": SegmentConfig,
    "asr": ASRConfig,
    "mt": MTConfig,
    "glossary": GlossaryConfig,
    "overlay": OverlayConfig,
    "ui": UiConfig,
    "record": RecordConfig,
    "server": ServerConfig,
    "debug": DebugConfig,
}


def _apply(obj: Any, data: dict[str, Any]) -> None:
    """把 TOML 里的值覆盖到已有默认值上；未知键忽略（向前兼容）。"""
    for f in fields(obj):
        if f.name not in data:
            continue
        cur = getattr(obj, f.name)
        value = data[f.name]
        if is_dataclass(cur):
            if isinstance(value, dict):
                _apply(cur, value)
        else:
            setattr(obj, f.name, value)


def asdict_overlay(cfg: Config) -> dict[str, Any]:
    """把 overlay 段转成可以 JSON 化的 dict（给设置面板用）。"""
    return {f.name: getattr(cfg.overlay, f.name) for f in fields(cfg.overlay)}


def asdict_audio(cfg: Config) -> dict[str, Any]:
    """麦克风设置（给设置面板用）。threshold 是 RMS，UI 会换算成 dB。"""
    return {
        "device": cfg.audio.device,
        "device_index": cfg.audio.device_index,
        "channel": cfg.audio.channel,
        "gain": cfg.audio.gain,
        "threshold": cfg.vad.threshold,
        "noise_ratio": cfg.vad.noise_ratio,
        "sample_rate": cfg.audio.sample_rate,
    }


def apply_overlay(cfg: Config, values: dict[str, Any]) -> dict[str, Any]:
    """把面板提交的值应用到内存里的配置，并做一遍类型/范围校验。"""
    for f in fields(cfg.overlay):
        if f.name in values:
            setattr(cfg.overlay, f.name, values[f.name])
    _validate(cfg)
    return asdict_overlay(cfg)


def save_section(path: Path | str, section: str, values: dict[str, Any]) -> Path:
    """把一批值写回 config.toml 的某个 section，**保留原有注释和顺序**。

    为什么不用 `to_toml()` 整份重写：用户手写的注释和排版会被抹掉，
    而这些注释正是这个项目的主要文档。所以这里只替换/追加键值行。
    """
    p = resolve_config_path(path)
    if not p.exists():
        cfg = Config()
        target = getattr(cfg, section, None)
        if target is not None:
            _apply(target, values)
        p.write_text(to_toml(cfg), encoding="utf-8")
        return p

    lines = p.read_text(encoding="utf-8-sig").splitlines()
    header = f"[{section}]"
    start: int | None = None
    end = len(lines)
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if start is None and stripped == header:
                start = i
            elif start is not None:
                end = i
                break

    rendered = {k: f"{k} = {_toml_value(v)}" for k, v in values.items()}
    if start is None:
        lines += ["", header, *rendered.values()]
    else:
        body = lines[start + 1 : end]
        pending = dict(rendered)
        for i, line in enumerate(body):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            key = stripped.split("=", 1)[0].strip()
            if key in pending:
                body[i] = pending.pop(key)
        if pending:
            # 追加在 section 末尾的最后一个非空行之后，避免插到空行中间
            insert_at = len(body)
            while insert_at > 0 and not body[insert_at - 1].strip():
                insert_at -= 1
            body[insert_at:insert_at] = list(pending.values())
        lines[start + 1 : end] = body

    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value) if isinstance(value, float) else str(value)
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def load(path: Path | str = CONFIG_FILENAME) -> Config:
    """读取配置；文件不存在时生成默认配置并返回。

    任何解析失败都退回默认值并打印原因——双击 exe 时如果直接抛异常，
    用户只会看到黑窗一闪而过，无从排查。
    """
    p = resolve_config_path(path)
    cfg = Config()
    if not p.exists():
        try:
            p.write_text(to_toml(cfg), encoding="utf-8")
        except OSError:
            pass  # 只读目录下也允许运行
        return cfg

    try:
        # utf-8-sig：记事本另存为 UTF-8 会带 BOM，tomllib 遇到 BOM 会直接报错
        text = p.read_bytes().decode("utf-8-sig")
        raw = tomllib.loads(text)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        print(f"[設定] {p} の読み込みに失敗しました（{exc}）。今回は既定の設定を使用します。", file=sys.stderr)
        return cfg

    _apply(cfg, raw)
    _validate(cfg)
    return cfg


def _num(value: Any, default: float, cast=float):  # noqa: ANN001
    try:
        return cast(value)
    except (TypeError, ValueError):
        return default


def _bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on", "是", "はい", "オン")
    return default


def _validate(cfg: Config) -> None:
    """逐项做类型转换和范围钳制。TOML 里写错类型不应该让程序起不来。"""
    a = cfg.audio
    a.device = str(a.device or "")
    a.device_index = max(-1, _num(a.device_index, -1, int))
    a.channel = max(-1, min(7, _num(a.channel, 0, int)))
    a.sample_rate = max(8000, min(48000, _num(a.sample_rate, 16000, int)))
    a.block_ms = max(10, min(100, _num(a.block_ms, 20, int)))
    a.gain = max(0.1, min(8.0, _num(a.gain, 1.0, float)))

    v = cfg.vad
    # 允许把门限降到 -66dBFS 左右（0.0005），够安静的小麦克风用；
    # 上限 0.2（-14dBFS）避免误设成「几乎永远不说话」。
    v.threshold = max(0.0005, min(0.2, _num(v.threshold, 0.008, float)))
    v.noise_ratio = max(1.0, min(8.0, _num(v.noise_ratio, 2.5, float)))
    v.min_speech_ms = max(50, _num(v.min_speech_ms, 160, int))
    v.end_silence_ms = max(150, _num(v.end_silence_ms, 320, int))
    v.max_utterance_s = max(2.0, _num(v.max_utterance_s, 8.0, float))
    v.min_utterance_ms = max(100, _num(v.min_utterance_ms, 250, int))
    v.pre_roll_ms = max(0, _num(v.pre_roll_ms, 120, int))

    g = cfg.segment
    g.merge = _bool(g.merge, True)
    g.merge_wait_ms = max(0, min(3000, _num(g.merge_wait_ms, 800, int)))
    g.max_merge = max(0, min(6, _num(g.max_merge, 2, int)))
    g.max_total_s = max(2.0, min(30.0, _num(g.max_total_s, 12.0, float)))
    g.max_hold_ms = max(500, min(30000, _num(g.max_hold_ms, 12000, int)))
    g.re_asr_under_ms = max(0, min(10000, _num(g.re_asr_under_ms, 2500, int)))

    s = cfg.asr
    s.engine = str(s.engine).lower()
    s.num_threads = max(1, min(16, _num(s.num_threads, 2, int)))
    s.lead_pad_ms = max(0, min(1000, _num(s.lead_pad_ms, 200, int)))
    s.fallback_to_mock = _bool(s.fallback_to_mock, True)

    cfg.mt.engine = str(cfg.mt.engine).lower()
    cfg.mt.max_chars = max(8, _num(cfg.mt.max_chars, 40, int))
    cfg.mt.max_new_tokens = max(8, min(128, _num(cfg.mt.max_new_tokens, 48, int)))
    cfg.mt.num_threads = max(1, min(16, _num(cfg.mt.num_threads, 2, int)))
    cfg.mt.context = max(128, min(4096, _num(cfg.mt.context, 512, int)))
    cfg.mt.fallback_to_none = _bool(cfg.mt.fallback_to_none, True)
    cfg.mt.punctuate = _bool(cfg.mt.punctuate, True)

    gl = cfg.glossary
    gl.enabled = _bool(gl.enabled, True)
    gl.dir = str(gl.dir if gl.dir is not None else "glossary")
    gl.max_phrase_len = max(4, min(64, _num(gl.max_phrase_len, 24, int)))

    o = cfg.overlay
    o.bilingual = _bool(o.bilingual, True)
    o.zh_size = max(12, min(200, _num(o.zh_size, 56, int)))
    o.ja_size = max(10, min(200, _num(o.ja_size, 30, int)))
    o.outline_width = max(0, min(12, _num(o.outline_width, 5, int)))
    o.max_lines = max(1, min(6, _num(o.max_lines, 2, int)))
    o.margin_percent = max(0, min(45, _num(o.margin_percent, 8, int)))
    o.line_gap = max(0, _num(o.line_gap, 10, int))
    o.fade_ms = max(0, _num(o.fade_ms, 180, int))
    o.clear_after_ms = max(0, min(60000, _num(o.clear_after_ms, 5000, int)))
    o.position = "top" if str(o.position).lower() == "top" else "bottom"

    cfg.ui.open_admin = _bool(cfg.ui.open_admin, True)

    r = cfg.record
    r.enabled = _bool(r.enabled, False)
    r.dir = str(r.dir if r.dir is not None else "recordings")
    r.max_minutes = max(0, min(1440, _num(r.max_minutes, 60, int)))

    cfg.server.host = str(cfg.server.host or "127.0.0.1")
    cfg.server.port = max(1, min(65535, _num(cfg.server.port, 8765, int)))

    cfg.debug.save_wav = _bool(cfg.debug.save_wav, False)
    cfg.debug.show_latency_hud = _bool(cfg.debug.show_latency_hud, False)


def to_toml(cfg: Config) -> str:
    """把配置序列化成带日语注释的 TOML（首次运行时写给用户看）。"""
    out: list[str] = [
        "# LiveSub の設定ファイルです。変更したらプログラムを再起動してください。",
        "# すべて既定値のままで動きます。マイクや見た目を変えたいときだけ編集してください。",
        "",
    ]
    comments = _COMMENTS
    for f in fields(cfg):
        section = getattr(cfg, f.name)
        out.append(f"[{f.name}]")
        for g in fields(section):
            if g.name in comments.get(f.name, {}):
                out.append(f"# {comments[f.name][g.name]}")
            value = getattr(section, g.name)
            if isinstance(value, bool):
                text = "true" if value else "false"
            elif isinstance(value, (int, float)):
                text = repr(value) if isinstance(value, float) else str(value)
            else:
                text = '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'
            out.append(f"{g.name} = {text}")
        out.append("")
    return "\n".join(out)


_COMMENTS: dict[str, dict[str, str]] = {
    "audio": {
        "device": "空欄＝システム既定のマイク。名前の一部を書けば一致するマイクを使います",
        "device_index": "同じ名前のマイクが複数あるときの識別用。通常は設定ページから選べば自動で入ります",
        "channel": "入力チャンネル（0始まり）。2chインターフェースでマイクが入力2なら 1、全部まとめるなら -1",
        "gain": "入力ゲイン。マイクが小さいときは 1.5〜2.0 に上げます",
    },
    "vad": {
        "threshold": "声とみなす最小音量（RMS）。小さい声を拾いたいときは小さくします（設定ページの「音声」タブからも変更可）",
        "end_silence_ms": "無音が何 ms 続いたら発話終了とみなすか。[segment] の統合があるので、区切りのために大きくする必要はありません",
        "min_speech_ms": "大きくするほど咳・マイク叩きなどの雑音を弾きます",
    },
    "segment": {
        "merge": "半文の統合スイッチ。オフにすると「無音が来たらすぐ表示」に戻ります",
        "merge_wait_ms": "言い終わっていないように見えるとき、続きを最大何 ms 待つか",
        "max_merge": "最大いくつまで統合するか（1 文が無限に待つのを防ぐ）",
        "max_hold_ms": "保留の上限。超えたら先に表示して画面が空くのを防ぎます",
        "re_asr_under_ms": "前の断片がこの長さ未満なら再認識。長い断片はテキストを連結して ASR を 1 回節約",
    },
    "asr": {
        "model_dir": "モデル置き場。python -m livesub download で取得できます",
        "num_threads": "CPU スレッド数。x264 のソフトウェアエンコード配信では 1〜2 を推奨",
        "lead_pad_ms": "先頭に少し無音を足して、最初の短い語が飲まれるのを防ぎます。通常は変更不要",
    },
    "mt": {"engine": "opus=既定（75M、速くて実用的）| qwen=より正確だが非常に遅い | none=日本語だけ表示"},
    "glossary": {
        "enabled": "辞書（定型フレーズ／用語）のスイッチ。オフにするとモデル出力そのまま",
        "dir": "辞書ディレクトリ（phrases.tsv + terms.tsv）。同じ名前のファイルを exe の隣に置くと内蔵項目を上書き",
        "max_phrase_len": "これより長い文は定型フレーズの照合をしません",
    },
    "overlay": {
        "bilingual": "日本語の原文も表示するか",
        "zh_size": "中国語字幕の文字サイズ(px)。OBS キャンバス 1920x1080 基準",
        "outline_width": "縁取りの幅(px)。0 で縁取りなし",
        "clear_after_ms": "最後の字幕を表示してから自動で消すまでの時間(ms)。0 で消しません（長い文は自動で少し延長）",
    },
    "ui": {
        "open_admin": "起動したときに設定ページをブラウザで自動的に開くか",
    },
    "record": {
        "enabled": "改善用の記録。オンにするとマイク音声と認識/翻訳結果をこの PC に保存します（既定オフ・外部送信なし）",
        "dir": "保存先フォルダ（データフォルダからの相対パス）",
        "max_minutes": "1 ファイルの最大長（分）。超えたら audio-001.wav のように分割します。0 で分割なし",
    },
    "server": {"port": "OBS のブラウザソースに入力するポート"},
}
