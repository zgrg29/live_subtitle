# LiveSub

给 OBS 用的**本地实时日语→中文字幕**。只采集你的麦克风，不碰游戏声音，不上传任何音频，全程离线。

> **当前状态：M2.3（识别 + 翻译 + 断句合并）。** 识别 RTF 0.11–0.14 / CER 4.5%；
> 翻译用本地 opus-mt-ja-zh（75M，ONNX int8），单句 **45–100ms**。
> 半句会按日语句尾判断先暂缓、等后续语音合并后再翻译，不再把
> 「このゲーム、」「けっこう難しいんですよね」拆成两行。
> 5 条长测试音频的整句 CER 从 **11.1% → 4.6%**（合并 + 段首静音两项一起；只补静音是 9.1%）。数据和复现命令见
> [ROADMAP.md](ROADMAP.md) M2.3、[docs/DEVLOG.md](docs/DEVLOG.md)；交接见 [HANDOFF.md](HANDOFF.md)。

---

## 项目进度

| 阶段 | 状态 | 产出 |
|---|---|---|
| **M0 骨架** | ✅ 2026-10-21 | 采集 → 切句 → 识别 → 翻译 → OBS 全链路、逐句延迟统计、TOML 配置、样式系统 |
| **M1 日语识别** | ✅ 2026-10-21 | sherpa-onnx + ReazonSpeech（153MB），RTF 0.113、CER 4.5% |
| **M2 翻译** | ✅ 2026-10-21 | opus-mt-ja-zh 75M + ONNX int8（229MB），45–103ms/句 |
| **M2.3 断句合并** | ✅ 2026-10-21 | 日语句尾判断 + 暂缓合并 + 语气词过滤 + 段首静音；测试集 CER 11.1% → 4.6% |
| **M2.2 术语表 / 名词词典** | ⬜ 下一个对话 | 食物名、游戏名、直播用语；先分清是 ASR 听错还是 MT 译错 |
| **M3 直播工程化** | ⬜ | 场景联动、丢帧看门狗、一键隐藏热键、SRT 导出 |
| **M4 分发** | 🚧 部分 | 绿色版 exe 已重打（0.2.1，602MB，未签名，含 M2.3 合并逻辑） |

端到端现状：**短句（2–4 秒）说完到上屏 p50 约 0.8–1.2 秒**；被判定为"没说完"的句子会额外
等待后续语音（最多 800ms），换来一句完整字幕。5 条 6–14 秒的长测试音频实测 p50 1.2–1.9 秒。
开发过程与踩坑记录见 [docs/DEVLOG.md](docs/DEVLOG.md)。

> **下一步（下一个对话）**：M2.2 术语表 / 名词词典。先拿真实错例分清"日语原文就错了（ASR）"
> 还是"日语对了但中文没翻对（MT）"——食物名大概率是前者，翻译词典救不了。设计见
> [ROADMAP.md](ROADMAP.md) M2.2。

---

## 为什么只做麦克风

这是本项目最重要的一个取舍：**只抓麦克风，不抓系统混音**。

- 声源干净：没有游戏音效、BGM、队友语音叠在一起，同样的小模型识别率明显更高；
- 算力便宜：不需要人声分离，不需要大模型兜底，4 核 CPU 就能跑；
- 主播也省事：不用装虚拟声卡、不用配音频路由，选一个麦克风就完事。

代价是：**听不懂游戏里的语音**（比如日服游戏队友说话）。这个场景我们明确不做，
它和"低成本、开箱即用"是冲突的。

## 特性

| 能力 | 状态 |
|---|---|
| 麦克风采集（可选设备、增益） | ✅ 已实现 |
| 能量 VAD 自动切句（挡咳嗽/碰麦/静音） | ✅ 已实现 |
| 半句合并（日语句尾判断 + 暂缓 + 合并后整句翻译） | ✅ 已实现 |
| 语气词过滤（「えーと」不上屏） | ✅ 已实现 |
| 延迟统计（逐句 ASR/MT/说完→上屏） | ✅ 已实现 |
| OBS 浏览器源字幕页（透明、纯 CSS） | ✅ 已实现 |
| 样式自定义（字体/字号/颜色/描边/双语/位置/行数） | ✅ 已实现 |
| 预排版模式（不推流也能调样式） | ✅ 已实现 |
| 日语识别（sherpa-onnx，纯 CPU） | ✅ 已接入，实测 RTF 0.11–0.14 / CER 4.5%（[数据](docs/bench-asr.md)） |
| 日→中翻译（opus-mt-ja-zh 75M，ONNX int8） | ✅ 已接入，单句 **45–100ms**，日常口语可读 |
| 敏感词过滤 / 术语表 | ⬜ 计划中（M2） |
| OBS 场景联动 / 一键隐藏 / 丢帧看门狗 | ⬜ 计划中（M3） |
| 一键安装包（免 Python） | ⬜ 计划中（M4） |

## 快速开始

### 方式 A：双击 exe（推荐，不用装 Python）

源码目录里已经打好一份绿色版：

```
D:\live_subtitle\dist\LiveSub\LiveSub.exe
```

双击即可。第一次可能弹「未知发布者」→ 点「更多信息」→「仍要运行」。

> ✅ 这份 exe 是 **0.2.1**，已包含 M2.3 的断句合并（启动后 `/api/metrics` 里能看到
> `merged_segments`）。如果改了源码，执行 `python tools\build_exe.py` 重打。

- 会开一个黑色控制台窗口，**别关**，关掉就是停止字幕
- 想改字体/颜色/麦克风：编辑**同目录**下的 `config.toml`，改完重启 exe
- 整个 `dist\LiveSub` 文件夹可以压缩发给别人，拿到就能用（含 153MB 模型）
- 换机器无需装 Python、无需联网

### 方式 B：从源码运行（开发用）

### 0. 环境
- Windows 10/11
- Python 3.10 ~ 3.13（推荐 3.11/3.12，第三方轮子最全）

### 1. 安装

```powershell
git clone https://github.com/zgrg29/live_subtitle.git
cd live_subtitle
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
```

### 2. 下载日语识别模型（约 153MB，只需一次）

```powershell
python -m livesub download --repo csukuangfj/reazonspeech-k2-v2 --to models/sherpa-ja
```

跳过这一步也能启动，但会退化成"预设台词"的演示模式（启动时会明确提示）。

### 3. 先自测（不用麦克风）

```powershell
python -m livesub selftest                          # 演示模式，验证链路
python -m livesub selftest --wav your.wav --real    # 用真实模型跑一段音频
```

会看到类似输出，证明整条链路和测量都在工作：

```
idx    audio s   asr ms   mt ms   end->line ms  text
────────────────────────────────────────────────────────────────
2         5.41   1637.9   418.5       1736.1  ヤンバルクイナとの出会いは十八歳の時だった  [合并 2 段]
                                              → 与Yambarquina的相遇是在18岁的时候。
```

`[合并 2 段]` 表示这句话被停顿切开后又合了回来；`idx` 用最后一段的序号，所以不一定连续。
表中的 ASR/MT 都是真实耗时（不含 OBS 渲染）。

### 4. 启动

最可靠的方式（不依赖 `.bat`，已实测）：

1. 按 `Win + R`
2. 粘贴并回车：

```
D:\live_subtitle\.venv\Scripts\python.exe -m livesub run
```

3. 出现控制台窗口即启动成功；**不要关这个窗口**，关掉就是停止字幕。

浏览器打开 <http://127.0.0.1:8765/overlay?preview=1> 可以先看样式（`?debug=1` 看连接和延迟）。

> Windows 11 的「智能应用控制」会拦截 `.bat` / `.lnk` 这类文件，双击 `启动.bat` 可能被拒。
> 原因和处理办法见 [docs/windows-launch.md](docs/windows-launch.md)。

### 5. 接进 OBS

1. 来源 → `+` → **浏览器**
2. URL 填 `http://127.0.0.1:8765/overlay`
3. 宽 `1920`，高 `1080`，位置 `0, 0`
4. **不要**勾选"不可见时关闭源"（关掉会导致重新加载、字幕闪烁）
5. 背景天生透明，不需要额外设置

详细步骤和踩坑见 [docs/obs-setup.md](docs/obs-setup.md)。

## 常用命令

```powershell
python -m livesub run                 # 启动（默认命令，等价于不带参数）
python -m livesub run --port 9000     # 换端口
python -m livesub selftest            # 无硬件自测
python -m livesub devices             # 列出麦克风
python -m livesub doctor              # 环境自检
python -m livesub download --search "zipformer ja"   # 找识别模型
python -m livesub download --repo owner/name --to models/sherpa-ja
python -m livesub run --wav test.wav  # 用 WAV 当输入（调试，实时回放）
python tools/eval_pipeline.py --wav-dir models/test_wavs   # 断句/合并/CER 评估
```

调试时 `--mock` 强制演示模式（不加载任何模型）。

## 样式自定义

改 `config.toml`（首次运行自动生成，带中文注释；也可以照抄 [config.example.toml](config.example.toml)），改完**重启**生效。

| 配置项 | 默认 | 说明 |
|---|---|---|
| `overlay.bilingual` | `true` | 是否显示日语原文；关掉只留中文 |
| `overlay.font_family` | 微软雅黑 | 任何已安装字体，如 `"思源黑体"` |
| `overlay.zh_size` / `ja_size` | `48` / `26` | 按 1920 宽画布计算，其他尺寸自动等比缩放 |
| `overlay.zh_color` / `ja_color` | `#FFFFFF` / `#B9C6D4` | 文字颜色 |
| `overlay.outline_color` / `outline_width` | 黑 / `4` | 描边，`0` 为不描边 |
| `overlay.background` | `transparent` | 可填 `rgba(0,0,0,.35)` 加半透明底 |
| `overlay.position` | `bottom` | `bottom` / `top` |
| `overlay.margin_percent` | `8` | 距上/下边缘的百分比 |
| `overlay.max_lines` | `2` | 屏幕上最多同时几行，超出从上方滚走 |
| `audio.device` | 空 | 留空=默认麦克风；也可填设备名的一部分 |
| `vad.end_silence_ms` | `320` | 声学收句阈值；切碎了由 `[segment]` 的合并兜底 |
| `segment.merge` | `true` | 半句合并总开关；关掉退回"静音到点就出" |
| `segment.merge_wait_ms` | `800` | 句子看起来没说完时，最多再等多久的后续语音 |
| `segment.max_merge` | `2` | 最多合并几段，防止一句话无限等 |

调样式不用推流：`http://127.0.0.1:8765/overlay?preview=1`，改完重启刷新即可。

## 延迟与资源

延迟全部来自真实时间戳，由 `python -m livesub selftest --real` 和 `/api/metrics` 输出。

| 阶段 | 实测 | 说明 |
|---|---|---|
| 说完 → 判定收句（静音等待） | **320ms** | 可配置；这是延迟的大头之一 |
| 半句合并等待 | **0–800ms** | 只有"看起来没说完"的句子才等；说完了立刻出 |
| ASR（整句识别，2 线程 int8） | **140–380ms** | 2.4s 音频约 200ms；合并句会多识别一次 |
| 翻译（opus-mt-ja-zh 75M，ONNX int8） | **45–100ms** | 带 KV cache 的贪心解码 |
| 上屏 → 显示器 | 30–80ms | OBS 渲染，不计入程序 |

**短句实测端到端 p50 约 0.8–1.2 秒**（4 逻辑核心的机器）。长句的 ASR 时间会线性变长：
5 条 6–14 秒的测试音频 p50 1.2–1.9 秒。合并等待只发生在句尾像助词/接续词的片段上，
是"晚一点但不再切碎"的交换。

**翻译质量**：日常口语可读，例如
`このゲーム、けっこう難しいんですよね。` → 「这个游戏,真是很难玩,不是吗?」，
`ちょっとトイレ行ってきます。` → 「我得去一下厕所了」。
仍会出错的地方：游戏专有名词（`配信` 可能翻成"传送/发送"）、极短的口头语、以及训练语料里混杂的粤语表达
（偶发，出现时通常是整句）。

> 曾经用过 M2M100-418M，质量不可接受（`こんばんは` → "晚餐好"）已弃用；
> 也试过 Qwen LLM，但这台 4 核 N97 跑不动（1.5B 单句 1.7 秒，预填充就要 1 秒）。取舍记录见
> [DECISIONS.md](DECISIONS.md) ADR-010。

资源占用（纯 CPU，无独显）：

- 常驻内存 ~150MB；模型加载后 +约 200MB
- CPU：识别 2 线程（**不要调高，3~4 线程反而更慢**，见[基准](docs/bench-asr.md)），翻译按档位
- GPU：**默认 0**。GPU 只会作为可选加速，且带"直播优先"的自动降级（见 [DECISIONS.md](DECISIONS.md) ADR-004）

## 工作原理

```
麦克风 ──20ms帧──► 能量VAD ──► 切句器 ──► 整句识别 ──► 合并判断 ──► 翻译 ──► WebSocket ──► OBS浏览器源
                                                      │（没说完就暂缓，后续语音来了重识别）
                                                      └─ 每句记录：音频长度 / 识别耗时 / 翻译耗时 / 说完→上屏
```

为什么是"切句 + 整句识别"而不是"流式识别"：麦克风场景下两种方案体感接近，
但整句识别的准确率更高、代码少一半、模型更好选。详见 [ARCHITECTURE.md](ARCHITECTURE.md)。

## 目录结构

```
src/livesub/
  audio/     采集（麦克风/WAV/合成）、VAD、切句
  text/      日语文本后处理（句尾判断、语气词）
  asr/       识别引擎（sherpa-onnx 适配器 + 演示引擎）
  translate/ 翻译引擎（接口 + 演示实现）
  composer.py  半句合并状态机（暂缓/合并/超时兜底）
  pipeline.py  主流水线、事件广播、会话日志
  server.py    本地 HTTP + WebSocket
  models.py    模型下载
  __main__.py  命令行
overlay/index.html   OBS 里显示的字幕页（单文件，纯 CSS）
tools/       模型下载、识别基准、整条流水线评估、服务检查
tests/       单元测试（不需要麦克风和模型）
```

## 开发

```powershell
pip install -e .
python -m unittest discover -s tests -v      # 28 个测试，不需要硬件
python tools/check_server.py --port 8765     # 端到端检查 HTTP + WebSocket
python tools/eval_pipeline.py --wav-dir models/test_wavs   # 切句/CER/延迟评估
```

贡献前请先看 [ARCHITECTURE.md](ARCHITECTURE.md) 的接口约定，以及 [DECISIONS.md](DECISIONS.md)
里已经拍板的取舍——有些"看起来该加的功能"是刻意不做。

## 已知局限

这些是**目前确实做不到**的，不是 bug：

1. **断句合并是启发式，不是理解语义**。它靠日语句尾（助词、接续词、`です/ます`、长度）判断
   "像不像说完了"：以名词结尾的长片段、`舞い` 这种动词中顿、或停顿超过 800ms 的换气，仍可能
   被切成两行。反过来，两句本来独立的话如果间隔不到 800ms，也可能被并成一行。可调
   `segment.merge_wait_ms` 与 `segment.merge` 权衡；机理见 [ROADMAP.md](ROADMAP.md) M2.3。
2. **专有名词 / 食物名不稳**。`配信` 可能翻成"发送/传送"，游戏名、角色名、`のり弁当` 这类食物名也一样。
   原因分两种：**ASR 听错**（日语原文就不对）或 **MT 译错**（原文对、中文错）。下一个对话先拿真实错例
   分类，再决定词典放在 ASR 侧还是 MT 侧，见 [ROADMAP.md](ROADMAP.md) M2.2。
3. **偶发粤语**。极少数句子会输出「我哋…」这类粤语表达，是翻译模型训练语料混杂导致的。
4. **只认一个人说话**。不区分说话人，多人抢话时会把声音混在一起识别。
5. **听不懂游戏里的语音**。只采集麦克风（ADR-001），日服队友语音不在范围内。
6. **低配机器的延迟**。4 核能效核（如 N97）短句实测 0.8–1.2 秒；合并句会多等最多 800ms、
   长句 ASR 也更久。更弱的机器会更慢，可以用 `vad.end_silence_ms`、`segment.merge_wait_ms`，
   或直接 `segment.merge = false` 换速度。

## 常见问题

**字幕页一片空白？**
先开 `?preview=1`。如果预排版能显示，说明是数据没进来，开 `?debug=1` 看 WebSocket 状态。

**识别到"（日语音频 x.x 秒，未加载识别模型）"？**
说明在演示模式：模型没装。按 `python -m livesub doctor` 的提示下载模型。

**模型下载不动？**
国内直连 huggingface.co 经常不通，先设镜像：

```powershell
$env:LIVESUB_HF_MIRROR="https://hf-mirror.com"
python -m livesub download --search "zipformer ja"
```

**端口被占用？**
`python -m livesub run --port 9000`，然后记得把 OBS 的 URL 一起改。

**食物名 / 专有名词识别或翻译不对？**
先看字幕页的日语原文（`overlay.bilingual = true`），分三种情况：

- **日语原文就错了**（`のり弁当` → `のりべん` / `乗り弁当`）：这是**识别层**，词典要加在 ASR 输出上做同音/近音替换，或者用热词、换更大的识别模型；
- **日语原文对、中文错**（`のり弁当` → "海苔盒饭" / 没翻）：这是**翻译层**，翻译后做术语表替换最便宜；
- **两段都对、组合后语义错**：翻译模型的能力问题，词典救不了，需要换模型做 A/B。

下一个对话的 M2.2 就是先拿真实错例把这三类分开，再决定词典放哪。见 [ROADMAP.md](ROADMAP.md) M2.2。

**杀毒软件报毒？**
从源码运行不会被报。以后出 exe 时要处理误报（M4 的事），这也是我们优先做绿色版而不是安装器的原因之一。

**双击 `启动.bat` 提示"智能应用控制已阻止"？**
Windows 11 的 SAC / 危险文件类型策略会拦截 `.bat`、`.lnk` 等脚本类文件，与程序本身无关。
用 `Win + R` 跑下面这行即可，详见 [docs/windows-launch.md](docs/windows-launch.md)：

```
D:\live_subtitle\.venv\Scripts\python.exe -m livesub run
```

## 许可

- 代码：[MIT](LICENSE)
- 模型：**不随代码分发**，各自遵循上游许可，见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)

MIT 只覆盖本仓库的代码。把模型打进安装包之前，必须先读那份 notices。
