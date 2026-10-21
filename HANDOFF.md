# 交接说明（下次从这里继续）

> 最后更新：2026-10-21 · 版本 0.2.1（M2.3：断句合并已接入）

## 一句话状态

**实时日语→中文字幕已经完整可用**：麦克风 → VAD 切句 → 日语识别 → 断句合并 → 中文翻译 → OBS 上屏。
绿色版 exe 在 `dist\LiveSub\`（602MB，含两个模型；**已重打为 0.2.1，包含 M2.3**）。

实测（Intel N97，4 个能效核）：ASR 140–380ms、MT **45–103ms**、短句端到端 **p50 0.8–1.2 秒**。
5 条长测试音频：最终字幕从 12 行降到 8 行，整句 CER **11.1% → 4.6%**
（合并 + 段首静音两项一起；只补静音是 9.1%，只合并不补静音是 6.1%）。

翻译模型是 **opus-mt-ja-zh（75M，ONNX int8）**，质量已经够用；再往上才是"换更大模型"的事。
断句合并是规则式的（日语句尾 + 暂缓 + 合并），不是语义理解，边界情况见下面的"尚未验证"。

## 已验证（可复现）

```powershell
$env:PYTHONPATH=''   # 已装 .pth，不需要 PYTHONPATH

# 1) 单元测试：28 个，全绿（含 BOM/坏配置/长句切分/合并/语气词的回归测试）
.\.venv\Scripts\python.exe -m unittest discover -s tests -v

# 2) 识别基准：RTF ≈ 0.14（补段首静音后）、平均 CER 4.5%
.\.venv\Scripts\python.exe tools\bench_asr.py --model-dir models/sherpa-ja --wav-dir models/test_wavs --threads 2

# 3) 整条流水线评估（断句行数 / 合并次数 / CER / 延迟）
.\.venv\Scripts\python.exe tools\eval_pipeline.py --wav-dir models/test_wavs
# 对照旧行为：
.\.venv\Scripts\python.exe tools\eval_pipeline.py --wav-dir models/test_wavs --no-merge --end-silence 420

# 4) 全链路（真实识别 + 真实翻译），会打印日语和中文；[合并 N 段] 表示发生了合并
.\.venv\Scripts\python.exe -m livesub selftest --wav models/test_wavs/3.wav --real

# 5) 绿色版（0.2.1，已包含 M2.3；改了 Python 要重新 build_exe）
Start-Process 'D:\live_subtitle\dist\LiveSub\LiveSub.exe' -ArgumentList '--port','8767'
# /api/health 应显示 asr=sherpa:sherpa-ja、mt=opus-onnx:opus-mt-ja-zh-int8、demo=false
# /api/metrics 的 merged_segments > 0 说明合并逻辑已进包
```

## 质量现状（诚实版）

opus-mt-ja-zh 75M 在字幕场景下：

| 例子 | 输出 | 评价 |
|---|---|---|
| このゲーム、けっこう難しいんですよね。 | 这个游戏,真是很难玩,不是吗? | ✅ |
| ちょっとトイレ行ってきます。 | 我得去一下厕所了 | ✅ |
| 今日のごはんは何ですか？ | 今天的晚餐是什么呢? | ✅ |
| 初めまして | 很高兴认识你 | ✅ |
| こんにちは、今日も配信を始めます。 | 你好,今天开始发送邮件了。 | ❌ 配信 翻错 |
| こんばんは | 晚安, | ⚠️ 应为"晚上好" |
| のり弁当（食物名） | 待测 | ⚠️ 先看 `ja` 原文是 `のり弁当` 还是听错 |
| （个别句子） | 我哋可以唔可以喺度讲讲呢啲故事 | ❌ 偶发粤语 |

结论：**日常口语可读，专有名词和极短寒暄还会错**。下一个对话做名词词典，但
**先分清是 ASR 听错、还是 MT 译错**，别一上来就往翻译后加词表。

断句合并（M2.3）的实际效果：

| 场景 | 过去 | 现在 |
|---|---|---|
| `このゲーム、` + `けっこう難しいんですよね` | 两行，第二行单独翻译生硬 | 一行「这个游戏,真是很难玩,不是吗?」 |
| `ヤンバルクイナとの出会いは` + `十八歳の時だった` | 两行 | 一行「与Yambarquina的相遇是在18岁的时候。」 |
| `気象庁は` + `雪や…影響` + `暴風雪や…` + `雪崩や…` | 4 行、`雪崩` 被识别成 `気崩れ` | 2 行、`雪崩` 正确 |
| `えーと` 单独触发 | 上屏「嗯—」 | 丢弃，不上屏 |

## 尚未验证 / 未实现

| 项 | 状态 |
|---|---|
| 真人麦克风 50 句 CER | 只有播音腔测试音频的数据，偏乐观 |
| 主播语速下的断句 A/B | 只在 5 条 WAV 上验证；`merge_wait_ms` / 句尾规则还没在真人麦克风前调过 |
| 名词结尾、动词中顿（如 `舞い`） | 仍会漏判成两行；需要更强的句尾模型或标点模型 |
| 翻译质量盲评 | 只有零散主观判断，没有 20 句 A/B 数据 |
| 食物名 / 专有名词 | 没有系统统计；先收集 20 条真实错例，分 ASR / MT |
| 术语表 / 名词词典 | 未实现（M2.2，下一个对话） |
| 敏感词 / SRT 导出 | 未实现（M3） |
| 代码签名 | 未做；首次运行弹「未知发布者」 |
| 单实例锁 | 未做；重复双击会因端口占用退出（已有友好提示） |

## 下一步（按顺序）

1. **名词词典（M2.2，下一个对话）**：先收集 20 条真实错例（`のり弁当`、`唐揚げ`、`卵焼き`、
   `味噌汁`、`おにぎり`、游戏名/角色名），逐条看 `ja`：
   - `ja` 就错 → ASR 层：同音/近音替换、热词，严重时换更大识别模型；
   - `ja` 对、`zh` 错 → MT 层：翻译后 `[glossary]` 替换；
   - 两段都对、组合后语义错 → 换模型或后编辑，词典救不了。
   设计见 ROADMAP M2.2，**先分类再加词，别凭感觉**。
2. **真人 20–50 句断句基线**：用主播语速录一段，跑 `selftest --wav` / `eval_pipeline.py`，
   看还有多少句被切碎、暂缓有没有超时。800ms 不够就调 `segment.merge_wait_ms`，不要先动句尾规则。
3. **绿色版已验证**（0.2.1）：exe → `3.wav` → 1 行、`merged_segments=1`、overlay 200。
   以后改 Python 仍需 `tools\build_exe.py` 重打；`config.example.toml` 和 overlay 由脚本自动同步。
4. **OBS 联动 + 看门狗**（M3）：`GetStats` 丢帧 → 降级；场景联动；一键隐藏热键。
5. **签名与分发**（M4 收尾）：602MB 的绿色版对主播偏大，考虑模型按需下载的瘦身版。

## 环境笔记（本机特有）

- Python 3.13.15；`.venv` 已装齐：sherpa-onnx 1.13.8、ctranslate2 4.8.3、sentencepiece、
  fastapi、uvicorn、sounddevice。
- **venv 的 ensurepip 在本机失败**，建环境用：
  `python -m venv --without-pip .venv` + `python -m pip --python .venv\Scripts\python.exe install -r ...`
- **pip 需要一次 `danger-full-access`**：沙箱禁止删除子项，pip 解包/打包都会失败。
- **HuggingFace 下载要设 `HF_HUB_DISABLE_XET=1`**，否则 xet 后端报 `I/O error: 拒绝访问`。
- **torch 只用于"把模型转成 CT2 格式"**，运行时不需要；打 exe 时必须 `--exclude-module torch`，
  否则绿色版会凭空多 365MB（已踩过）。
- 沙箱里 `tempfile.mkdtemp()` 建的目录无法写入（权限 0700），测试里统一用 `Path.mkdir()`。
- 控制台是 GBK，`Get-Content` 看中文会乱码，文件本身是 UTF-8。

## 模型

| 用途 | 路径 | 体积 | 许可 |
|---|---|---|---|
| 识别 | `models/sherpa-ja` | 153MB | Apache-2.0 |
| 翻译 | `models/opus-mt-ja-zh-int8` | 229MB | 仓库声明 Apache-2.0；上游 Helsinki CC-BY-4.0 |

翻译模型是从 `suirenka/opus-mt-ja-zh` 导出的（源文件保留在 `.tmp/opus2`）。**别用 NLLB**（CC-BY-NC）。
合并后的长句需要更多解码步数，`mt.max_new_tokens` 默认已从 32 提到 **48**；旧配置记得同步。

### 重新导出/量化翻译模型（改模型时才需要）

```powershell
# 1) 导出带 KV cache 的 ONNX（必须带 -with-past，否则慢 3~5 倍）
.\.venv\Scripts\optimum-cli.exe export onnx --model .tmp\opus2 --task text2text-generation-with-past models\opus-mt-ja-zh-past

# 2) int8 量化（约 900MB → 229MB，延迟 234ms → 79ms）
#    注意：quantize_dynamic 会用 tempfile.mkdtemp，在本机沙箱里无法写入，
#    需要先把 tempfile.mkdtemp 换成基于 Path.mkdir 的实现。
```

导出和量化都需要 torch（只在这一步用），运行时不依赖。

## 代码地图

| 想改什么 | 去哪个文件 |
|---|---|
| 切句太碎/太慢 | `src/livesub/audio/vad.py` + `config.toml` 的 `[vad]` |
| 哪些句尾算"没说完" | `src/livesub/text/ja.py` 的 `_CONTINUE_SUFFIXES` / `_COMPLETE_SUFFIXES` |
| 合并等多久、最多合几段 | `src/livesub/composer.py` + `config.toml` 的 `[segment]` |
| 合并后重识别还是拼文本 | `composer._merge()` 的 `re_asr_under_ms` 分支 |
| 长句被切的位置 | `Segmenter._close_at_pause()`（在低能量点切） |
| 麦克风断流检测 | `MicSource.frames()`（5 秒无数据就抛异常） |
| 识别引擎 | `src/livesub/asr/`，`factory.build_asr()` |
| 段首短词被吞 | `asr/sherpa_offline.py::transcribe` 的 `lead_pad_ms`（默认补 200ms 静音） |
| 翻译引擎 | `src/livesub/translate/opus_onnx.py`，`factory.build_mt()` |
| 上屏内容/时机 | `src/livesub/pipeline.py::_emit_line`（何时出由 `composer.py` 决定） |
| 字幕样式 | `overlay/index.html` 的 CSS 变量 + `config.py::OverlayConfig` |
| 打包 | `tools/build_exe.py` |

## 七个容易改错的约定

1. **延迟必须从 `Utterance.speech_end_ts` 起算**，不是 `end_ts`（后者多算了 320ms 静音等待）。
2. **不要翻译 partial**，也不要用"先出快译、合并后覆盖"的写法：字幕反复改写比慢更糟（ADR-003）。
3. **合并/超时判断用音频时间轴**（`Segmenter.position_ms` / `Utterance.speech_end_ms`），
   不要用墙钟；否则 `selftest --wav` 离线回放会全错。
4. **切句上限不是硬切**：到 `max_utterance_s` 时在低能量点切，剩余音频留给下一句；
   尾巴会继续走合并逻辑，别把 `active` 当成"这段彻底结束了"。
5. **`--k` 必须是无单位数**：`calc(100vw / 1920)` 再乘 `px` 是非法表达式，会让 `font-size` 整条失效。
6. **任何"条件依赖异步删除结果"的循环都是死循环**：按"未在淡出的行"计数，不要让
   `children.length` 当条件（曾把 OBS 的 CEF 崩掉 5 次）。
7. **语气词/空识别不能直接 return**：如果正有暂缓的句子，要把音频并进去（`_absorb`），
   否则合并链会被一个「えーと」打断。

另外：**字幕页是热更新的**——改 `overlay/index.html` 后复制到
`dist/LiveSub/_internal/overlay/index.html`，OBS 右键刷新即可；只有 Python 改动才要重打包。

## 已知的坑（都已修，但别改回去）

- 重连要带 generation 计数，否则 watchdog 和 onclose 会并存两条 WebSocket，字幕显示两遍；
- `Publisher` / `Metrics` 的 deque 必须加锁，否则 WS 握手时抛 `deque mutated during iteration`；
- 配置读取要容忍 BOM 和错误类型，否则双击 exe 会黑窗一闪而过；
- `Segmenter._seg_start_ms` 必须是"真正开口"的位置（`_pos_ms - 连续说话时长`），
  不能拿缓冲起点：缓冲含 pre-roll，会把合并窗口算短/算长；
- `segment.max_hold_ms` 别设太小：它比"续段时长"短的话，长句会被提前拆开（踩过，默认改到 12s）；
- ASR 的 `close()` 会把 ONNX session 置空，模型对象不能跨 `Pipeline.run()` 复用（评估脚本里每条重建）。

## 相关的其它文档

- [README.md](README.md) · [ARCHITECTURE.md](ARCHITECTURE.md) · [DECISIONS.md](DECISIONS.md)
- [ROADMAP.md](ROADMAP.md) · [docs/DEVLOG.md](docs/DEVLOG.md) · [docs/bench-asr.md](docs/bench-asr.md)
- [docs/windows-launch.md](docs/windows-launch.md) · [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)
