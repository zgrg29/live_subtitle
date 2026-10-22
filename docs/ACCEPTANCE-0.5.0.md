# 0.5.0 验收记录

> 验收日期：2026-10-22 · 对象：`LiveSub 0.5.0`
> 环境：Windows 11 26300 · Python 3.13.15 · `.venv`（sounddevice / sherpa-onnx / FastAPI / uvicorn）
> 结论：**源码版与绿色版均通过**；真人麦克风识别质量与 OBS 实机渲染不在本轮验收范围。

## 1. 自动化测试

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -q
```

```text
Ran 75 tests in 3.6s
OK
```

新增/覆盖的关键回归：

| 测试 | 守什么 |
|---|---|
| `test_audio_settings.py::RecorderTest` | WAV 写入、`lines.jsonl`、`meta.json`、60 分钟分段 |
| `RecorderTest::test_splits_long_recording` | 超过 `max_minutes` 分成 `audio-000/001`，音频不丢 |
| `RecordingPipelineTest` | 录制开启时，每条上屏字幕都能在 `lines.jsonl` 找到 |
| `PublisherLevelFilterTest` | `level` 只发给 `/ws?levels=1`，字幕页队列不被塞满 |
| `GlossaryLayoutTest` | 词典两个表默认折叠；追加按钮在表格上方 |
| `OpenAdminTest` | `/api/health` 返回 200 后才打开浏览器；只有 TCP 端口时不打开 |
| `ConfigTest` / `AudioSettingConfigTest` | 新配置段、范围钳制、旧配置兼容 |

## 2. 源码版端到端

启动：

```powershell
.\.venv\Scripts\python.exe -m livesub run --synthetic 300 --mock --port 8799 --no-browser
```

| 检查 | 结果 |
|---|---|
| `/api/health` | `version=0.5.0`，`running=true`，`last_error` 为空 |
| `/overlay` | 含 `clear_after_ms` / `scheduleClear`（静默自动清屏） |
| `/admin` | 含「音声」「記録」tab、电平条、折叠词典；`<details>` 默认无 `open` |
| `/api/audio` | 返回 10 个输入设备；`device_index`、`threshold_db=-41.9` 正常 |
| `/api/record` | 默认 `enabled=false`；开启 2.2 秒后得到 1 句、2.2 秒 WAV，关闭后文件可读 |
| `/ws` | 收到 `hello` / `line`，**没有** `level` |
| `/ws?levels=1` | 2 秒收到 19 条 `level`，同时收到 `line` |
| `tools/check_server.py` | HTTP + WebSocket 通过，字幕 #9 正常上屏 |
| `python -m livesub selftest --seconds 6` | 3 句，p50 320ms（仅切句等待，demo 引擎） |

## 3. 绿色版端到端

产物：`dist\LiveSub\`（602MB，含识别/翻译模型）

| 检查 | 结果 |
|---|---|
| `LiveSub.exe --version` | `LiveSub 0.5.0` |
| 启动 `--synthetic 300 --mock --port 8798 --no-browser` | `/api/health` 200 |
| `/admin` | 含音声/记录/折叠词典（39,469 字符） |
| `/api/audio` | 列出 10 个设备 |
| 录音开→2 秒→关 | 生成 `audio-000.wav`（32,320 frames），关闭后目录完整 |
| `/overlay` / 页面资源 | 打包内 `overlay/admin.html` 与源码 SHA256 一致 |
| 自动打开设置页 | 用只记录 URL 的假浏览器（`BROWSER` 环境变量）验证，收到 `http://127.0.0.1:8794/admin`；控制台打印「設定ページをブラウザで開きました」 |

## 4. 未覆盖 / 已知风险

1. **真人麦克风 CER 未复测**：本轮使用 demo 引擎和合成音频，只验证链路与 UI；真实识别质量仍以 0.4.0 的播音腔数据为参考，偏乐观。
2. **OBS 实机渲染未点检**：字幕页结构/JS 语法和 WebSocket 已验证，但没有在 OBS 里实际挂 10 分钟看丢帧。
3. **同名麦克风切换**：已用真实设备列表验证 `device_index` 能区分 3 个同名 `麦克风 (USBAudio1.0)`；其中 Realtek 麦克风在当前机器上被占用/不可用，切换失败时已验证会自动回退旧设备。
4. **自动打开浏览器**：验证的是"URL 被交给默认浏览器"；如果浏览器把新标签页开在后台，需要用户自己切过去。
5. **未签名**：首次运行仍会弹「不明な発行元」。

## 5. 发布产物

| 文件 | 大小 | 用途 |
|---|---|---|
| `dist\LiveSub-0.5.0\` | 602 MB（2,747 个文件） | 绿色版目录（解压即用） |
| `dist\LiveSub-0.5.0-win64.zip` | 373,004,176 bytes（355.7 MB） | GitHub Release 附件 |
| `dist\LiveSub-0.5.0-win64.zip.sha256.txt` | 91 bytes | 校验和 |

SHA256：

```text
611249BB80B52084C45D618A8178B0C4EF7EF4555BE32B408283B9D855EEBD9D
```

## 6. 复现命令

```powershell
# 单元测试
.\.venv\Scripts\python.exe -m unittest discover -s tests -q

# 源码端到端
.\.venv\Scripts\python.exe -m livesub run --synthetic 300 --mock --port 8799 --no-browser
.\.venv\Scripts\python.exe tools\check_server.py --port 8799 --lines 1
.\.venv\Scripts\python.exe -m livesub selftest --seconds 6

# 绿色版
dist\LiveSub\LiveSub.exe --version
dist\LiveSub\LiveSub.exe --synthetic 300 --mock --port 8798 --no-browser
```
