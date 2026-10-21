# 第三方组件与许可

本仓库的**代码**采用 [MIT](LICENSE)。但"能跑起来"还依赖两类外部资产，它们**不适用 MIT**，
分发前必须逐项核对。这份文件是核对清单，不是法律意见。

## 1. 运行依赖（Python 包，随安装由用户拉取，不进本仓库）

| 包 | 许可 | 备注 |
|---|---|---|
| numpy | BSD-3-Clause | |
| sounddevice | MIT | 内含 PortAudio（MIT） |
| fastapi | MIT | |
| starlette | BSD-3-Clause | FastAPI 依赖 |
| uvicorn | BSD-3-Clause | |
| websockets | BSD-3-Clause | |
| pydantic / pydantic-core | MIT | |
| sherpa-onnx | Apache-2.0 | 可选依赖，真实识别引擎 |
| onnxruntime | MIT | sherpa-onnx 依赖 |
| ctranslate2 | MIT | 旧翻译引擎（已弃用，仅保留代码） |
| sentencepiece | Apache-2.0 | 翻文分词器 |
| transformers | Apache-2.0 | 仅用于加载 Marian 分词器（运行时不加载 torch） |

这些都以源码/源码依赖方式存在，用户 `pip install` 时由包管理器处理，
本项目不重新分发它们的二进制，因此不需要额外声明——**但如果 M4 用 Nuitka 打包，二进制会被打进发行包**，
届时必须把上述许可原文一并放进安装目录，并在关于页面列出。

## 2. 模型（当前不随仓库分发）

原则（见 `DECISIONS.md` ADR-005）：

1. `models/` 在 `.gitignore` 里，模型由用户按需下载；
2. 模型的许可与代码许可**分开**声明；
3. 任何模型进入安装包前，必须在此处登记：来源 URL、许可名称、许可原文位置、
   是否要求署名、是否传染（share-alike）。

### 已采用的模型

| 用途 | 模型 | 许可 | 体积 | 分发方式 |
|---|---|---|---|---|
| 日语 ASR | [`csukuangfj/reazonspeech-k2-v2`](https://huggingface.co/csukuangfj/reazonspeech-k2-v2)（ReazonSpeech k2 v2 的 ONNX 转换，int8） | **Apache-2.0**（仓库 cardData 已声明） | 153MB | 运行时下载，不进仓库 |
| 日→中翻译 | [`suirenka/opus-mt-ja-zh`](https://huggingface.co/suirenka/opus-mt-ja-zh)（75M Marian，ONNX int8） | **Apache-2.0**（仓库声明）；上游 Helsinki-NLP opus-mt 系为 CC-BY-4.0 | 229MB | 同上 |

> 分发翻译模型时需同时署名 Helsinki-NLP / Marian，并在安装目录附许可原文。

> 明确排除：`facebook/nllb-200-*` 系列是 **CC-BY-NC-4.0**（禁止商用），与本项目 MIT 定位冲突，不得使用/分发。

核对记录（2026-10-21）：

- 上游 `reazon-research/reazonspeech-k2-v2` 同样声明 Apache-2.0；
- 本项目只使用 int8 版本（encoder/decoder/joiner + tokens.txt），不下载 565MB 的 fp32；
- 分发时如需内置，须在安装目录附 Apache-2.0 原文，并注明模型来自 ReazonSpeech。

### 候选清单（未采用）

| 用途 | 候选 | 许可（**采用时需重新核实**） | 备注 |
|---|---|---|---|
| 日语 ASR（备选） | Whisper / kotoba-whisper | Whisper MIT；kotoba-whisper Apache-2.0 | 准确率高，但 CPU 延迟不达标 |
| 日语 ASR（备选） | sherpa-onnx 流式多语种 zipformer（含 ja） | 需核对 | 可做流式，但多语种模型日语精度通常低于专用模型 |
| 日→中翻译（备选 A） | Marian / Opus-MT 系 | 常见为 CC-BY-4.0 | CC-BY 要求署名，可再分发 |
| 日→中翻译（备选 B） | Qwen 系（0.6B/1.7B） | Apache-2.0 | 量化权重同样受基础模型许可约束 |

> ⚠️ 表的"许可"栏必须在真正采用那个模型的那一天重新核实并写死版本号/commit。
> 模型作者修改许可是常有的事，凭记忆写会被打脸。

## 3. 如果 M4 内置 lite 模型

需要同时做到：

- 安装目录下附 `licenses/` 子目录，放所有第三方许可原文；
- 关于页面/README 列出每个模型的名称、作者、来源 URL、许可；
- 若模型许可要求署名，必须在用户可见处署名，而不是只放在 GitHub 上；
- 若模型许可带 share-alike，**不能**把模型和 MIT 代码混成一个"整体作品"分发。

## 4. 字体

字幕页只使用系统已安装字体（默认微软雅黑），不内嵌字体文件，因此不涉及字体再分发。
如果以后要内嵌思源黑体/Noto Sans SC，需遵守 SIL OFL 1.1（可再分发，但须保留版权声明）。
