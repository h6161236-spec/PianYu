# Windows 打包发布

本文档对应 `VCut Studio` 的 Windows 桌面发布流程。

## 1. 环境准备

建议在 Windows 11 x64 上执行，并准备：

- Python 3.11+
- PowerShell 5.1 或 PowerShell 7+
- `ffmpeg.exe` 与 `ffprobe.exe`
- 已安装项目依赖

推荐安装命令：

```powershell
pip install -e .[asr,release]
```

## 2. 一键构建

在仓库根目录运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\build_windows_release.ps1
```

脚本会自动完成这些动作：

- 检查 `PyInstaller` 是否可用
- 自动尝试从当前 `PATH` 定位 `ffmpeg.exe`
- 用 `packaging\VCutStudio.spec` 构建 Windows GUI 程序
- 输出 `dist\VCutStudio\`
- 额外复制发布说明与验收清单
- 生成压缩包 `release\VCutStudio-windows-v<version>.zip`

## 3. 指定 ffmpeg 目录

如果 `ffmpeg` 不在系统 `PATH` 中，可以手动指定：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\build_windows_release.ps1 -FfmpegDir "D:\tools\ffmpeg\bin"
```

目录下至少需要：

- `ffmpeg.exe`
- `ffprobe.exe` 可选但推荐

打包后可用的二进制会跟随程序一起进入发布目录，应用会自动优先使用程序目录中的版本。
如果只有 `ffmpeg.exe`，应用仍可工作，并在探测媒体信息时自动回退到 `ffmpeg` 模式。

## 4. 输出目录

默认输出：

- `dist\VCutStudio\VCutStudio.exe`
- `dist\VCutStudio\README.md`
- `dist\VCutStudio\docs\Windows打包发布.md`
- `dist\VCutStudio\docs\发布验收清单.md`
- `release\VCutStudio-windows-v<version>.zip`

## 5. 发布前建议

- 先跑一次测试：`python -m unittest discover -s tests -v`
- 用真实中文视频走完整流程：导入、分析、翻译、配音、导出
- 验证没有管理员权限时也能正常运行
- 确认目标机器存在 Windows 英文语音，或至少有一套系统英文 TTS
- 如果发布包没有附带 `ffmpeg.exe`，则需要目标机器自己安装 ffmpeg 并放到 `PATH` 中

## 6. 已知边界

- 当前不会自动把 Whisper 模型文件预下载到发布包中，首次实际使用 ASR 时仍可能触发模型下载
- 翻译依赖外部网络与外部模型服务
- API Key 仍存本地 JSON，尚未做系统级加密存储
