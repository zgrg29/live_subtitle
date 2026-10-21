# 架构

## 设计目标

| 优先级 | 目标 | 含义 |
|---|---|---|
| 1 | 不影响直播 | 不抢 GPU、不抢 CPU、崩了不能拖垮 OBS |
| 2 | 开箱即用 | 装上就能出字幕，不要求懂模型、不需要配 API |
| 3 | 低延迟 | 说完到上屏 p50 &lt; 1.2s |
| 4 | 够用的准确率 | 干净麦克风上的日常口语，不追求专业级 |

非目标：游戏内语音、唱歌、配音(TTS)、多人分离、多语种、云端 API。

## 数据流

```
                    ┌──────────── 20ms / 16kHz 单声道 float32 ────────────┐
 麦克风(PortAudio) ──► queue ──► EnergyVAD ──► Segmenter ──► Utterance ──► ASR
                                  (RMS+噪声底)   (开句/收句)    (整句PCM)    │
                                                                          ▼
                                                                     Composer
                                                                 (暂缓/合并/超时)
                                                                          │ 合并时可能再 ASR 一次
                                                                          ▼
                                                                         MT
                                                                          │
                                             ┌────────────────────────────┤
                                             ▼                            ▼
                                   logs/session-*.jsonl          Publisher(线程安全)
                                   logs/wav/*.wav (可选)                   │
                                                                 call_soon_threadsafe
                                                                           ▼
                                                                 asyncio.Queue × N
                                                                           ▼
                                                                 WebSocket /ws
                                                                           ▼
                                                                 overlay/index.html
                                                                 （OBS 浏览器源，纯 CSS）
```

## 线程模型

只有三类线程，刻意保持简单：

| 线程 | 职责 | 阻塞特性 |
|---|---|---|
| PortAudio 回调线程 | 把音频块塞进 `queue.Queue` | 绝不阻塞，队列满就丢块 |
| pipeline 线程 | VAD / 切句 / ASR / 合并 / MT / 落盘 | 允许阻塞（推理是同步的） |
| uvicorn 事件循环 | HTTP、WebSocket、静态页 | 绝不做推理 |

跨线程唯一的通道是 `Publisher`：worker 线程调 `publish()`，内部用
`loop.call_soon_threadsafe()` 投递到每个客户端的 `asyncio.Queue`。**推理慢不会卡住服务，服务慢不会卡住推理**。

为什么不用 asyncio 跑推理：ONNX 推理是 CPU 密集的同步调用，扔进事件循环会阻塞所有 WebSocket 推送；
扔进线程池又会和采集线程抢 GIL 调度。一个专职线程最直观，也最容易做优先级控制。

## 模块职责

| 模块 | 职责 | 不负责 |
|---|---|---|
| `audio/capture.py` | 麦克风/WAV/合成音源，统一 20ms 帧接口 | 不做降噪、不做重采样以外的处理 |
| `audio/vad.py` | 能量 VAD + 切句状态机 | 不做识别、不做合并判断 |
| `text/ja.py` | 日语句尾判断、语气词清理 | 不调模型、不改音频 |
| `composer.py` | 暂缓/合并/超时兜底，决定哪几段是一句 | 不做识别（通过回调再调 ASR）、不做翻译 |
| `asr/*` | 一段音频 → 文本 | 不做切句、不做翻译 |
| `translate/*` | 一句日语 → 一句中文 | 不做断句、不做字幕排版 |
| `pipeline.py` | 串起来、计时、广播、落盘 | 不做业务判断（合并策略在 composer） |
| `server.py` | HTTP/WS 暴露 | 不含业务逻辑 |
| `overlay/index.html` | 渲染 + 样式 | 不含任何识别/翻译逻辑 |

## 接口契约

### 音频帧

`np.float32`、单声道、16kHz、长度 = `sample_rate * block_ms / 1000`。所有音源都必须满足。

### Utterance

```python
@dataclass
class Utterance:
    index: int          # 声学片段序号，从 1 递增
    audio: np.ndarray   # 整段 PCM（含 pre-roll 和尾部静音）
    sample_rate: int
    start_ms: float     # 缓冲起点（含 pre-roll，可能早于开口）
    end_ms: float
    speech_start_ms: float  # 真正开口的位置（不是 start_ms）
    speech_end_ms: float    # 真正说完的位置 = end_ms - 尾部静音
    end_ts: float       # perf_counter：判定收句的时刻（含尾部静音等待）
    speech_end_ts: float# perf_counter：真正说完的时刻 = end_ts - 静音等待
```

`speech_end_ts` 是延迟统计的关键：用户感知的延迟必须从这个点开始算，而不是从"系统判定结束"开始，
否则 320ms 的静音等待会被漏掉，测出来的数字会骗人。

同理，合并窗口必须用 `speech_start_ms` / `speech_end_ms` 这条**音频时间轴**，不能用墙钟：
`selftest --wav` 是非实时回放，墙钟会让所有超时判断失真。

### ComposedLine

`composer.py` 最终交给 `pipeline._emit_line()` 的对象：

```python
@dataclass
class ComposedLine:
    index: int          # 用最后一段的序号，保证字幕 id 单调
    audio: np.ndarray   # 可能是多段拼接后的 PCM
    sample_rate: int
    ja: str             # 已经去掉句首语气词
    speech_end_ts: float
    speech_end_ms: float
    start_ms: float
    end_ms: float
    asr_ms: float       # 这句话花掉的全部识别时间（含被合并片段的识别）
    segments: int = 1   # 由几段音频组成
    merges: int = 0     # 合并了几次
```

合并策略只有三条：句尾像没说完就暂缓；**音频轴上** 800ms 内来了后续就合并；
等不到/超过上限就照常上屏。上一段 < 2.5s 时合并后重新识别，否则拼文本省一次 ASR。

### 上屏消息

```json
{
  "type": "line",
  "id": 12,
  "ja": "このゲーム、けっこう難しいんですよね。",
  "zh": "这个游戏挺难的啊。",
  "ts": "2026-10-21T11:40:02",
  "latency": { "end_to_line_ms": 947.3, "asr_ms": 431.2, "mt_ms": 96.1, "audio_ms": 1840.0 }
}
```

其它消息类型：`hello`（连接时下发配置 + 最近 30 条历史，供 OBS 刷新后恢复）、
`status`（运行状态）、`error`（错误提示）、`ping`（保活，前端忽略）。

### 配置

单个 `config.toml`，缺省值全在 `config.py` 里。未知键忽略，保证老配置能跑新版本。
`[segment]` 是新增段，老配置里没有也能跑（默认开启半句合并）；`[vad] end_silence_ms` 默认已从 420 降到 320。

## 延迟从哪来、怎么压

| 环节 | 现行值 | 可调 | 压它的代价 |
|---|---|---|---|
| 采集块大小 | 20ms | `audio.block_ms` | 太小 CPU 抖动变大 |
| 静音判句 | 320ms | `vad.end_silence_ms` | 句子被切碎，但 composer 会合并；太小会让片段变多、ASR 次数增加 |
| 合并等待 | 0–800ms | `segment.merge_wait_ms` | 只发生在"像没说完"的句子；调小会漏合并，调大会让这些句子出得晚 |
| ASR | **140–380ms**（2–4s 音频，实测）<br>合并句会多一次（短片段重识别） | 模型/线程数 | 更大模型更准更慢；线程数 >2 反而更慢 |
| 翻译 | **45–100ms**（opus-mt 75M int8） | 模型档位 | 更大模型更准，但延迟成倍增长 |
| 渲染 | 30–80ms | 不可控 | OBS 合成 |

结论：**延迟的大头仍是"等静音" + 长句 ASR**。ASR 的 RTF 约 0.11–0.14，2.4 秒话只花 0.25 秒算；
但 10 秒长句的 ASR 也要 1 秒左右。合并只在句尾不完整时多等最多 800ms，且通常只多识别一次短片段。
所以优化顺序是：先选低延迟模型，再考虑流式 ASR（见 ROADMAP M5）。

单句上限（`max_utterance_s`，默认 8s）会在**最近的低能量点**切分，并把剩余音频留给下一句，
而不是硬切——硬切会把词劈成两半（曾出现 `高額であ|課題となって`）。

## 失败与降级

| 故障 | 当前行为 | 目标行为 |
|---|---|---|
| 麦克风被占用/拔出 | 捕获异常 → 状态栏报错 → pipeline 退出，服务仍存活 | 自动重连 |
| 模型缺失 | 回退到演示引擎并明确提示 | 同左（M4 改为自动下载） |
| ASR 崩溃 | 该次返回空文本；若正在合并则退回文本拼接，已识别的半句不丢 | 连续 N 次失败则换引擎 |
| 合并识别为空 | 用两段文本拼起来（`、` 分隔），照常出字幕 | 同左 |
| 句子一直没说完 | 800ms 没后续、或总长超 12s/合并 2 次 → 到点照常出 | 同左 |
| 客户端卡住 | 队列满即丢消息，绝不反压推理 | 同左 |
| 端口占用 | 启动失败并提示 | 自动换端口 |

## 扩展点

| 要加的功能 | 改哪里 | 注意 |
|---|---|---|
| 真实日→中翻译 | 新增 `translate/xxx.py`，在 `factory.build_mt` 分支 | 必须自己保证 `mt.max_chars` 内 |
| 换识别模型 | `asr/sherpa_offline.py` | 目前只支持 transducer 结构 |
| 改"哪些句尾算没说完" | `text/ja.py` 的规则表 | 改完跑 `eval_pipeline.py` + 单元测试 |
| 调合并窗口/次数 | `composer.py` + `config.toml` `[segment]` | 必须用音频时间轴 |
| 加标点模型（可选） | `text/` 里加一层，ASR 文本 → 带标点文本 | 多一个模型；先做 A/B |
| 流式识别（降延迟） | `pipeline.py` 增加 partial 通道 + 前端"半行"渲染 | 不要翻译 partial，会闪烁；与 composer 的"暂缓"是两套机制，注意别打架 |
| 敏感词过滤 | `pipeline._emit_line` 里、翻译之前加一层 `filter` | 词库由用户提供，不内置 |
| OBS 场景联动/看门狗 | 新模块 `obs.py`，订阅 `GetStats` | 见 DECISIONS ADR-004 |
| SRT 导出 | `pipeline` 已记录每条时间戳，加一个 writer 即可 | 需要绝对时间轴，目前只有相对毫秒 |

## 刻意不做

- 不做前端框架：字幕页是一个 HTML，没有构建步骤，改一行 CSS 立刻生效；
- 不做数据库：会话日志就是 JSONL，出问题直接看；
- 不做插件系统：目前只有两个可替换点（ASR/MT），一个 `if` 就够；
- 不做多语言 UI：产品只面向日本主播，用户可见文案统一为日语。
  开发文档（README 以外的 `docs/`、HANDOFF、ROADMAP 等）保留中文，两者不混。
