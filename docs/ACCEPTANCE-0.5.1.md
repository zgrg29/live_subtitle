# 0.5.1 验收记录

> 验收日期：2026-10-22 · 对象：`LiveSub 0.5.1`（诊断版）
> 环境：Windows 11 26300 · Python 3.13.15 · 绿色版 `dist\LiveSub-0.5.1\`
> 结论：**源码与绿色版均通过**；2ch 真实接口、OBS 实机渲染、浏览器实际渲染不在本轮范围。

## 1. 自动化测试

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -q
```

```text
Ran 84 tests in 4.0s
OK
```

0.5.1 新增 9 个回归：

| 测试 | 守什么 |
|---|---|
| `MicChannelSelectionTest::test_second_channel_is_used` | 2ch 设备选 `channel=1` 时，真的取第 2 路（本次故障的根因） |
| `MicChannelSelectionTest::test_gain_applies_to_selected_channel` | 增益作用在选中的通道上 |
| `MicChannelSelectionTest::test_mix_mode_averages_channels` | `channel=-1` 多路平均 |
| `MicChannelSelectionTest::test_mono_device_still_opens_one_channel` | 1ch 麦克风不被"想看电平"搞坏 |
| `AudioSettingConfigTest::test_channel_is_clamped` | `channel` 越界钳制（-1..7） |
| `PipelineDiagTest::test_every_stage_is_counted` | frames / speech / utt / asr / emitted 计数与最近音频缓冲 |
| `PipelineDiagTest::test_empty_recognition_is_visible` | ASR 返回空串时 `asr_empty` 必须可见（之前完全静默） |
| `DiagVerdictTest::test_verdict_follows_the_signal_chain` | 判定按信号链顺序给结论 |
| `OverlayAutoHideTest::test_admin_has_diagnostics_and_channel_picker` | 诊断 tab / 通道选择 / 支持包按钮不被误删 |

## 2. 绿色版：真实麦克风 + 0.5.0 旧配置

用 **0.5.0 的 `config.toml` 副本**启动（没有 `channel` 键），验证向前兼容：

```powershell
dist\LiveSub-0.5.1\LiveSub.exe --port 8805 --no-browser --config .tmp\acc\config-050.toml
```

| 检查 | 结果 |
|---|---|
| `/api/health` | `version=0.5.1`，`running=true`，识别 `sherpa:sherpa-ja`，翻译 `glossary:opus-onnx`，`last_error` 为空 |
| 旧配置兼容 | 0.5.0 的配置直接可用；`channel` 缺省 = 0 |
| `/api/diag` | 3 秒内 frames **422 → 575**，`last_frame_age_s=0.0`，`channel=0`、`channel_count=1`、10 个输入设备 |
| 门限显示 | `threshold_db=-41.9` 与配置 `0.008` 一致 |
| `POST /api/audio {"channel":-1}` | 200；切完 frames 继续增长，`channel=-1`，配置里写入 `channel = -1` |
| 切回 `channel=0` | 200，无需重启 |
| `/api/diag/recent`（4 秒） | 200；`level=-75.3 dB`、`peak=-59.7 dB`、`demo=false`、WAV 保存到 `logs/` |
| `/api/diag/bundle` | 200，90,908 bytes；zip 内含 `diag.json` / `summary.txt` / `session.jsonl` / `recent-10s.wav` / `config.toml` |
| `/admin` | 48,282 字符；含「診断」「入力チャンネル」「サポート用ファイルを保存」及 `panel-diag` / `diag-test` / `diag-bundle` |
| `/overlay` | 含 `scheduleClear`，0.5.0 的自动清屏未被破坏 |

## 3. 绿色版：真实模型端到端（WAV）

```powershell
dist\LiveSub-0.5.1\LiveSub.exe --wav models\test_wavs\3.wav --port 8807 --no-browser
```

| 检查 | 结果 |
|---|---|
| `/api/diag` | `asr=sherpa:sherpa-ja`、`demo=false`、`emitted=1`、`asr_empty=0` |
| `/api/metrics` | `count=1`、`errors=0`、`p50 end→line 1885 ms`（含模型加载后的首句） |
| 识别/翻译 | 「ヤンバルクイナとの出会いは十八歳の時だった」→「与Yambarquina的相遇是在18岁的时候。」 |
| 延迟拆分 | ASR 1425 ms / MT 692 ms（首句，模型已加载但冷缓存） |

## 4. 发布产物

| 文件 | 大小 | 说明 |
|---|---|---|
| `dist\LiveSub-0.5.1\` | 602 MB / 2,751 files | 绿色版目录，含两个模型 |
| `dist\LiveSub-0.5.1-win64.zip` | 373,362,486 bytes（356.1 MB） | 3,450 个条目；**不含** `logs/` / `recordings/` |
| `dist\LiveSub-0.5.1-win64.zip.sha256.txt` | 91 bytes | 校验和 |

SHA256：

```text
96C5BF3D53DC1E763A6A2C3A87693186E034873F4BBBD99FDBCF911AF09D2BED
```

`LiveSub.exe --version` → `LiveSub 0.5.1`；`使い方.txt` → `LiveSub ポータブル版 0.5.1`，
第 6 节为「字幕が出ないとき（診断タブ）」，第 2 节含输入通道说明。

## 5. 未覆盖 / 已知风险

1. **2ch 真实接口未点检**：本机只有 1ch USB 麦克风，`channel=1` 的路径由 fake sounddevice
   单测覆盖（取第 2 路、增益、混音、1ch 回退），没有在 MOTU 上实测。
2. **浏览器实渲染未点检**：诊断 tab 的 JS 通过 `node --check`，页面字符串与接口均已验证；
   headless Edge 被本机沙箱拦截，无法截图确认视觉效果与 2 秒轮询。
3. **OBS 实机渲染**：字幕页结构与 0.5.0 相同（只加诊断，不动 overlay），未在 OBS 里挂 10 分钟。
4. **真人麦克风 CER**：仍以 0.4.0 的播音腔数据为参考，偏乐观。
5. **支持包隐私**：zip 含配置与直近 10 秒音频，已在界面写明"不自动发送"；用户需自行决定是否提供。

## 6. 复现命令

```powershell
# 单元测试
.\.venv\Scripts\python.exe -m unittest discover -s tests -q

# 诊断相关单测
.\.venv\Scripts\python.exe -m unittest tests.test_audio_settings.MicChannelSelectionTest -v
.\.venv\Scripts\python.exe -m unittest tests.test_audio_settings.PipelineDiagTest -v

# 绿色版（真实麦克风）
dist\LiveSub-0.5.1\LiveSub.exe --port 8805 --no-browser
#   http://127.0.0.1:8805/admin?tab=diag

# 绿色版（真实模型 + WAV）
dist\LiveSub-0.5.1\LiveSub.exe --wav models\test_wavs\3.wav --port 8807 --no-browser
```
