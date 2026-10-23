# 0.5.2 验收记录

> 验收日期：2026-10-23 · 对象：`LiveSub 0.5.2`（开箱即用：自动通道 + 自动保存）
> 环境：Windows 11 26300 · Python 3.13.15 · 源码 + 绿色版

## 1. 自动化测试

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -q
```

```text
Ran 89 tests in 3.5s
OK
```

新增/更新：

| 测试 | 守什么 |
|---|---|
| `test_auto_selects_the_channel_with_speech` | 2ch 设备只有 ch2 有声时，自动锁定 ch2 |
| `test_auto_does_not_lock_without_speech` | 没有发话时不乱锁 |
| `test_auto_can_be_overridden_manually` | 自动锁定后仍能手动改回 ch1 |
| `test_auto_on_mono_device_is_mono` | 1ch 设备退化为单通道 |
| `test_empty_device_with_stale_index_uses_system_default` | 0.5.1 默认设备保存 bug 的迁移；`channel=0` 同时迁移到自动，显式 `channel=1` 保留，Sound Mapper 显式配置回到默认设备 |
| 设置页字符串检查 | 自动选项、顶部保存按钮、三个自动保存调度函数仍在 |

## 2. 源码 + 真实麦克风（1ch）

启动：

```powershell
$env:PYTHONPATH='D:\live_subtitle\src'
.\.venv\Scripts\python.exe -m livesub run --port 8798 --no-browser
```

| 检查 | 结果 |
|---|---|
| `/api/health` | `version=0.5.2`，`source=麦克风 (USBAudio1.0)`，`running=true` |
| `/api/audio` | `channel=-2`、`channel_mode=auto`、`auto_state=mono`、`active_channel=0` |
| `POST /api/audio {"channel":0}` | 200；`channel_mode=manual`、`active_channel=0`，配置写入 `channel = 0` |
| `POST /api/audio {"channel":-2}` | 200；回到 `auto/mono`，配置写入 `channel = -2` |

## 3. UI 静态检查

- `node` / headless Edge 在本机沙箱里不可用，未做真实浏览器渲染截图。
- 用 Python 检查了 `admin.html`：88 个 `id` 无重复；`$("...")` 引用的 76 个 id 全部存在。
- 关键字符串存在：`自動（推奨）`、`save-audio-top`、`自動保存`、`scheduleAudioSave`、`scheduleStyleSave`、`scheduleGlossarySave`、`rawIndex !== ""`。

## 4. 绿色版：旧配置迁移 + 真实麦克风

用一份模拟 0.5.1 保存 bug 的配置启动：

```toml
[audio]
device = ""
device_index = 0
channel = 0
gain = 1.0
```

```powershell
dist\LiveSub-0.5.2\LiveSub.exe --config checks\old-config.toml --no-browser
```

| 检查 | 结果 |
|---|---|
| `--version` | `LiveSub 0.5.2` |
| `/api/health` | `version=0.5.2`，`source=マイク (USBAudio1.0)`，`running=true` |
| 旧配置迁移 | `device=""`、`device_index=-1`、`channel=-2`（Sound Mapper → 实际默认麦克风，ch1 → 自动） |
| `/api/audio` | `channel_mode=auto`、`auto_state=mono`、`active_channel=0` |
| `POST /api/audio {"channel":0}` | 200；`channel_mode=manual`；配置写入 `channel=0` |
| `POST /api/audio {"channel":-2}` | 200；回到 auto/mono；配置写入 `channel=-2` |
| `/admin`（curl） | 含 `自動（推奨）`、`save-audio-top`、`変更は自動保存されます` |
| `/api/diag` | 含 `channel_mode=auto`、`auto_state=mono`、`active_channel=0` |

## 5. 未覆盖 / 已知风险

1. **没有真实 2ch 音频接口**：自动通道锁定路径由 fake sounddevice 单测覆盖（ch2 有声、ch1 无声），
   没有在 MOTU / AG03 上点检。
2. **浏览器实际渲染未点检**：headless Edge 在本机沙箱不可用；自动保存的防抖、焦点保持、
   并发保护做了代码审查 + 字符串/ID 静态检查，没有真实点击回放。
3. **自动判定的边界**：如果输入1是持续很响的游戏音、输入2才是麦克风，单靠能量会选错；
   这种情况需要手动选输入2。自动判定不是声源分离。
4. **zip 内的 `使い方.txt` 文件名编码**：沿用 0.5.1 的 tar/zip 方式（Windows 日文环境可读）；
   跨语言系统解压时可能显示乱码，但文件内容仍是 UTF-8。

## 6. 发布产物

`dist\LiveSub-0.5.2\`（602MB）+ `LiveSub-0.5.2-win64.zip`（373,370,295 bytes，
SHA256 `F761ECC1D4B922BD8BC0AFE5B0A6ADEE267A22389DFFE15FD85DA4CCC9DE1952`）。
