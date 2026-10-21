# Windows 上怎么启动（以及被系统拦截怎么办）

本机（Windows 11，智能应用控制开启）实测结论：

| 文件类型 | 结果 |
|---|---|
| `.exe`（未签名） | **可以运行**，只弹一次「未知发布者」提示，点「更多信息 → 仍要运行」即可 |
| `.bat` / `.cmd` | 被**智能应用控制**直接拒绝，没有绕过入口 |
| `.lnk` / `.ps1` | 被「危险文件类型」策略拒绝（Dangerous file extension from the web） |

所以本项目的入口定为 **exe**，不使用脚本启动器。

## 推荐：双击 exe

```
D:\live_subtitle\dist\LiveSub\LiveSub.exe
```

1. 第一次运行可能弹「未知发布者」→ 点「更多信息」→「仍要运行」
2. 会开一个黑色控制台窗口，**别关**（关掉 = 停止字幕）
3. 浏览器打开 <http://127.0.0.1:8765/overlay> 确认
4. OBS 里加「浏览器」源指向同一地址

绿色版整个文件夹可以拷走：`LiveSub.exe` + `_internal\` + `models\` + `config.toml`。

## 备用：命令行（exe 被拦时）

极少数机器的智能应用控制是强制模式，会连 exe 一起拒绝。这时用 `Win + R`：

```
D:\live_subtitle\.venv\Scripts\python.exe -m livesub run
```

（需要源码环境，见 README 的「方式 B」。）

## 为什么脚本会被拦

这不是杀毒误报，也和程序内容无关：Windows 对 `.bat` / `.lnk` / `.ps1` / `.vbs` 这几种
"可执行脚本或快捷方式"的默认策略就是不信任，且不提供"仍要运行"。而 exe 走的是另一条判定，
未签名时给一次提示，用户可以自己决定。

## 长期方案：签名

签名之后连「未知发布者」提示都不会出现，这才是面向主播的终态：

| 方案 | 成本 | 说明 |
|---|---|---|
| OV 代码签名证书 | 约 ¥1500–3000/年 | SmartScreen 与智能应用控制都放行，最直接 |
| Microsoft Store（MSIX） | 开发者账号 + 审核 | 由商店签名，但更新受制于商店 |
| 保持未签名 | 0 | 每次首次运行都要点一次提示；个别强制策略机器会直接拒绝 |

## 查本机状态

```powershell
# 智能应用控制：0=关闭 1=开启 2=评估模式
(Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\CI\Policy').VerifiedAndReputablePolicyState
```

被拦截的详细记录：事件查看器 → `应用程序和服务日志 → Microsoft → Windows → CodeIntegrity → Operational`。
