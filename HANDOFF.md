# 交接说明（下次从这里继续）

> 最后更新：2026-10-21 · 版本 **0.3.0**（M2.3 断句合并 + M2.2 术语表/补标点/评测体系）

## 一句话状态

**实时日语→中文字幕已经完整可用**：麦克风 → VAD 切句 → 日语识别 → 断句合并 → 中文翻译 → OBS 上屏。
绿色版 exe 在 `dist\LiveSub\`（602MB，含两个模型；**已重打为 0.2.1，包含 M2.3**）。

实测（Intel N97，4 个能效核）：ASR 140–380ms、MT **45–103ms**、短句端到端 **p50 0.8–1.2 秒**。
5 条长测试音频：最终字幕从 12 行降到 8 行，整句 CER **11.1% → 4.6%**
（合并 + 段首静音两项一起；只补静音是 9.1%，只合并不补静音是 6.1%）。

翻译模型是 **opus-mt-ja-zh（75M，ONNX int8）**，外面套了**术语表/固定表达表**（`glossary/`）。
60 句评测集上：裸模型 F1 **0.354**、明显错 24 句；套术语表后 **0.605**、明显错 **5 句**，
延迟只从 p50 71ms 变到 75ms。断句合并是规则式的（日语句尾 + 暂缓 + 合并），不是语义理解。

**质量路线已经定死**：不换模型、不做硬件分档。验收基线是这台 N97——跑不动的方案直接否掉，
质量只能靠零延迟的文本手段（词典 / 固定表达 / 后处理）往上堆。

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

# 5) 翻译质量 A/B：60 句评测集，几十秒跑完，报告写到 logs/eval/
.\.venv\Scripts\python.exe tools\eval_mt.py                    # 套术语表（当前默认）
.\.venv\Scripts\python.exe tools\eval_mt.py --no-glossary      # 裸模型对照
.\.venv\Scripts\python.exe tools\eval_mt.py --strip-punct      # 去掉标点，模拟 ASR 真实输入
.\.venv\Scripts\python.exe tools\eval_mt.py --strip-punct --no-glossary  # 真实输入下的裸模型
.\.venv\Scripts\python.exe tools\eval_mt.py --set eval\holdout_vtuber.jsonl   # 口语语域（冻结的 holdout）

# 6) 绿色版（0.2.1；改了 Python 要重新 build_exe，别忘了 glossary/ 也在 --add-data 里）
Start-Process 'D:\live_subtitle\dist\LiveSub\LiveSub.exe' -ArgumentList '--port','8767'
# /api/health 应显示 asr=sherpa:sherpa-ja、mt=opus-onnx:opus-mt-ja-zh-int8、demo=false
# /api/metrics 的 merged_segments > 0 说明合并逻辑已进包
```

## 质量现状（诚实版）

**用 60 句评测集（`eval/mt_eval.jsonl`，5 类各 12 句）量过，不是挑例子**：

| 指标 | 裸模型 | + 文本层 | 评价 |
|---|---|---|---|
| F1（60 句书写体） | 0.354 | **0.605** | 我写的、也是我调过的，**偏乐观** |
| 明显错（书写体） | 24 / 60 | **5 / 60** | |
| F1（60 句无标点，≈ASR 输出） | 0.273 | **0.504** | 线上真实输入形态 |
| 明显错（无标点） | 28 / 60 | **10 / 60** | 反问句、从句边界最容易崩 |
| **F1（37 句 VTuber holdout，范围内）** | 0.180 | **0.259** | **真实语域的诚实数字** |
| 明显错（VTuber holdout） | 24 / 37 | **17 / 37** | 口语语法 + 网络用语 |
| F1（24 句 VTuber dev，调词用） | 0.192 | **0.282** | 和 holdout 同量级，可复现 |
| 延迟 p50 | 71ms | 75ms | 文本层是 0ms；12 句走整句表 |

⚠️ **别再引用 0.605 作为"翻译质量"**。那是书写体 + 我自己的出题/调词，双重乐观偏差。
真实可用性看 VTuber 那一行：**一半句子能看懂，另一半要猜**。

**范围（2026-10-21 定）**：只做**雑談 + ゲーム実況**；**歌词不翻译**。
评测集里 `歌枠`/`歌` 的句子带 `out_of_scope: true`，`tools/eval_mt.py` 默认跳过，
要计入得加 `--all`。别为唱歌场景投任何工程成本。

`eval/holdout_vtuber.jsonl` 是冻结的 holdout——**禁止拿它调词典**，否则就失去意义。
要调词请用 `eval/dev_vtuber.jsonl`。**加词不再以提分为目标**，只保证 60 句回归不降。

分类（裸 → 套词典）：寒暄 0.365→**0.810**、直播 0.329→0.535、游戏 0.359→0.557、
名词 0.211→**0.489**、长句 0.506→0.633。

修好的主要是**词汇层**：

| 例子 | 过去 | 现在 |
|---|---|---|
| こんばんは | 晚安 | 晚上好 |
| おつかれさまでした | 我抓住了你 | 辛苦了 |
| マイクの音量 | 迈克的音量好吗 | 麦克风的音量好吗 |
| 配信を始めます | 开始发送 | 开始直播 |
| スパチャ | spacha | 超级留言 |
| 原神のイベント | 神迹活动 | 原神活动 |

**还会错的（5 句，全是语法/语气，词典救不了）**：

| 例子 | 输出 | 问题 |
|---|---|---|
| とりあえずセーブしておきます。 | 先来保存一下 | 意思对，"存个档"更地道 |
| 見に来てくれて、本当にありがとうございました。 | 你真的来找我，我真的是谢谢你 | `〜てくれて` 理解错 |
| えーと、今日はちょっと調子が悪いので… | 嗯，今天事情有点不对… | 长句语气整体走形 |
| 音が割れてるので、音量下げます | 噪音有点破，所以音量会降低 | 割れる / 主动被动 |
| 視聴者参加型でやります | 以后，观众可以参加 | 复合名词 |

结论：**日常口语 + 专有名词现在可读；长句的语气和逻辑连接仍是 75M 的天花板**。
食物名（`のり弁当`）还不稳，需要更多真实错例。

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
| 翻译质量盲评 | 有 60 句固定评测集（文本）；**真人麦克风的真实字幕还没进评测集**，对 60 句有过拟合风险 |
| 食物名 / 专有名词 | `のり弁当` 这类仍不稳（pre 注入会变成"我做了海参/海苔"）；需要更多真实错例 |
| 术语表 / 名词词典 | ✅ 第一版（48 条固定表达 + 28 条术语）；词条都是"实测翻错才加"的 |
| ASR 侧热词 / 替换 | 未做。`sherpa-onnx` 支持 hotwords，但要 `modified_beam_search`，得先量延迟 |
| 敏感词 / SRT 导出 | 未实现（M3） |
| 代码签名 | 未做；首次运行弹「未知发布者」 |
| 单实例锁 | 未做；重复双击会因端口占用退出（已有友好提示） |

## 下一步（按顺序）

**先说结论：词典这条线挖到底了。** 30 句 dev 集上把名词类词条加满，只从 0.235 到 0.275；
剩下 18 句错的全是语法（时制、限定、授受、语气），不是词。继续加词是浪费。

1. **产品级决策（最该做的，先想清楚再动手）**：口语语域就停在 0.27 左右，
   要不要把字幕改成"宁可少翻、也要看懂"——比如长句按现有句尾规则拆成两句分别翻、
   丢掉拿不准的修饰。这是权衡，不是技术问题，需要主播视角拍板。
2. **绿色版重打**：`glossary/` 已加进 `--add-data`，但 `dist\LiveSub` 还是旧的 0.2.1（无词典）。
   重打后要验证 `/api/metrics` 和字幕页正常。
3. **ASR 侧**（唯一还没动过的层）：`のり弁当`、`唐揚げ` 先看 `ja` 原文；`ja` 就错就试
   `sherpa-onnx` 的 hotwords（要 `modified_beam_search`，先量延迟）。
   **注意：ASR 的 CER 是在播音腔音频上测的，真人主播会更差**，这块可能是下一个真金矿。
4. **真人 20–50 句断句基线**：用主播语速录一段，跑 `selftest --wav` / `eval_pipeline.py`，
   看还有多少句被切碎、暂缓有没有超时。800ms 不够就调 `segment.merge_wait_ms`。
5. **OBS 联动 + 看门狗**（M3）：`GetStats` 丢帧 → 降级；场景联动；一键隐藏热键。
6. **签名与分发**（M4 收尾）：602MB 的绿色版对主播偏大，考虑模型按需下载的瘦身版。

**已经试过、确认无效的路**（别重复走）：
- 译前把口语缩约规范化成书面形（`しとく→しておく`、`ちゃった→てしまった`）：±0.00，见 DEVLOG；
- 换 ≥1B 的模型（Hy-MT2-1.8B / TranslateGemma 4B）：这台机器上单句 1.5s+，见 ADR-012。

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
| 加词 / 改词典 | `glossary/phrases.tsv`（整句）、`glossary/terms.tsv`（术语，pre/post 两模式） |
| 术语表怎么套上去 | `src/livesub/translate/glossary_mt.py` |
| 译文清洗（标点/重复/引号） | `src/livesub/text/postprocess.py` |
| 翻译质量评测 | `eval/mt_eval.jsonl` + `tools/eval_mt.py`（`--no-glossary` 跑裸模型） |
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
