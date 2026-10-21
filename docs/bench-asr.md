# 日语识别基准（M1）

记录模型选型、速度和质量数据。**换模型/改线程数/改 VAD 参数后请重跑并更新本文件。**

## 被测模型

| 项 | 值 |
|---|---|
| 仓库 | `csukuangfj/reazonspeech-k2-v2`（ReazonSpeech k2 v2 的 ONNX 转换） |
| 结构 | transducer（encoder + decoder + joiner） |
| 量化 | int8 |
| 体积 | encoder 147.5MB + decoder 2.8MB + joiner 2.6MB + tokens 46KB ≈ **153MB** |
| 许可 | **Apache-2.0**（仓库已声明） |
| 训练数据 | ReazonSpeech 日语语料（电视字幕） |
| 安装 | `python -m livesub download --repo csukuangfj/reazonspeech-k2-v2 --to models/sherpa-ja` |

选它的理由：日語专用、体积极小、CPU 实时、许可干净、自带 5 条带标准转写的测试音频。

## 复现

```powershell
python -m livesub download --repo csukuangfj/reazonspeech-k2-v2 --test-wavs models/test_wavs
python tools/bench_asr.py --model-dir models/sherpa-ja --wav-dir models/test_wavs --threads 2
python -m livesub selftest --wav models/test_wavs/4.wav --real   # 整条链路（含 VAD）
```

## 速度（5 条音频，共 50.5 秒）

| 线程数 | 总推理耗时 | RTF | 结论 |
|---|---|---|---|
| 1 | 8018ms | 0.159 | 能用，但慢 |
| **2** | **5694ms** | **0.113** | **默认值，最优** |
| 3 | 8552ms | 0.169 | 变慢（超订） |
| 4 | 10311ms | 0.204 | 明显变慢 |

> RTF = 推理耗时 / 音频时长。0.11 意味着 1 秒音频只要 0.11 秒算力。
> 3~4 线程反而更慢，说明这台机器上线程超订严重；`config.asr.num_threads` 保持 2。
>
> M2.3 在 ASR 段首补了 200ms 静音（`asr.lead_pad_ms`）以修复首词被吞，复测 RTF 约 0.14；
> 线程数结论不变，仍然是 2 线程最优。

换算成实际延迟（端到端实测）：

| 单句音频长度 | ASR 耗时 | 含切句等待的总延迟（翻译 0ms） |
|---|---|---|
| 2.4s | 196–270ms | 616–692ms |
| 3.1s | 214ms | 640ms |
| 7.1s | 801ms | 801ms（该句是被上限切出来的，无静音等待） |

**结论：ASR 不是延迟瓶颈，"等静音"才是。** 7 秒长句才会让 ASR 升到 800ms 量级。

> M2.3 之后静音等待降到 320ms；句尾像没说完时会再等最多 800ms 用于合并。
> 合并句还会多识别一次短片段，实际"说完→上屏"见 [ROADMAP.md](ROADMAP.md) M2.3。

## 质量（2 线程）

平均 **CER 4.5%**（字级，比对前已去掉标点和空格）。

| 文件 | 时长 | CER | 错误情况 |
|---|---|---|---|
| 1.wav | 13.43s | 0.0% | 无 |
| 2.wav | 6.62s | 0.0% | 无（全是假名，没有汉字歧义） |
| 3.wav | 6.23s | 9.5% | `１８歳` → `十八歳`（数字规范化差异，不算识别错误） |
| 4.wav | 10.26s | 13.0% | `Ｈ２Ａ` → `H二A`、`１００億` → `百億`（同上） |
| 5.wav | 13.96s | 0.0% | 无 |

**去掉"阿拉伯数字 vs 汉字数字"这类规范差异后，真实识别错误接近 0。**
这符合预期：测试音频是干净的播音腔，且与训练语料同分布（电视字幕）。

## 已知问题

1. **段首字容易丢/听错**。切句后段首没有上下文，出现过 `ヤンバルクイナ` → `バルクイナ`、
   `雪崩` → `気崩れ`。这是离线 transducer 的固有弱点；M2.3 已用"句尾判断 + 暂缓合并 +
   短片段合并后重识别"解决（见 ROADMAP M2.3），不再靠调大 `vad.end_silence_ms`。
2. **长句被上限切分**。原实现 8 秒硬切会把词切开（`高額であ|課題と…`）。
   已改为在最近的低能量点切（`Segmenter._close_at_pause`），实测切点落在自然停顿处。
3. **测试音频不代表真实麦克风**。主播的语速、口音、房间混响都会让 CER 上升，需要用真人录音复测。

## 下一步

- 用真人麦克风录 50 句（游戏解说风格，带停顿和口头语）做真实基线；
- 在真人语料上对比 `segment.merge_wait_ms` = 600 / 800 / 1000 对"行数 / CER / 延迟"的影响；
- 用 `python tools/eval_pipeline.py --wav-dir <目录>` 跑整条流水线，别只看单段 ASR；
- 若真实场景 CER 超过 15%，考虑换更大的日语模型（代价是延迟和内存）。
