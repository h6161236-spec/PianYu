# Windows 打包发布

本文档对应 PianYu (片语) 的 Windows 桌面发布流程。

## 环境准备

建议在 Windows 10/11 x64 上构建，并准备：

- Python 3.11+
- PowerShell 5.1 或 PowerShell 7+
- PyInstaller
- `ffmpeg.exe`，推荐同时提供 `ffprobe.exe`
- Microsoft PowerPoint，用于真实 PPT 页面预览和 `.ppt` 转换
- 可选：LibreOffice，用于 `.ppt` 转 `.pptx` 的备用路径

安装构建依赖：

```powershell
pip install -e .[asr,release]
```

## 一键构建

在仓库根目录运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\build_windows_release.ps1
```

脚本会完成：

- 检查 PyInstaller 是否可用
- 从 `PATH` 自动定位 `ffmpeg.exe`
- 使用 `packaging\PianYu.spec` 构建 GUI 程序
- 复制 README、docs、图标、可选 ffmpeg 文件
- 输出 `dist\片语\`
- 生成 `release\片语-windows-v<version>.zip`

## 指定 ffmpeg 目录

如果 `ffmpeg` 不在系统 `PATH`，可以手动指定：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\build_windows_release.ps1 -FfmpegDir "D:\tools\ffmpeg\bin"
```

目录中至少需要：

- `ffmpeg.exe`
- `ffprobe.exe`，可选但推荐

打包后，应用会优先使用程序目录里的 ffmpeg。

## 本地模型资产

仓库不会提交大型模型文件。发布包如需离线 TTS/ASR，需要在本机提前准备 `models/` 目录，再由打包脚本按现有逻辑复制可用资产。

常见路径：

- `models/tts/kokoro-*`
- `models/tts/vits-melo-tts-zh_en`
- `models/tts/sherpa-onnx*` 或等效可执行文件目录

## 输出目录

默认输出：

- `dist\片语\片语.exe`
- `dist\片语\README.md`
- `dist\片语\docs\使用指南.md`
- `dist\片语\docs\Windows打包发布.md`
- `dist\片语\docs\发布验收清单.md`
- `release\片语-windows-v<version>.zip`

## 发布前检查

- 运行测试：`python -m pytest`
- 在干净目录解压 release zip
- 双击启动 `片语.exe`
- 导入真实 PPT/PPTX
- 生成中文稿和英文稿
- 使用至少一个中文或英文音色导出视频
- 检查字幕烧录、sidecar `.srt`、输出视频播放

## 已知边界

- 脚本生成和翻译依赖外部 OpenAI-compatible 服务。
- API Key 当前保存在本地设置 JSON 中，未接入系统凭据管理。
- 没有 PowerPoint 时，PPT 页面预览可能退化为文本预览图。
- `.ppt` 文件需要 PowerPoint 或 LibreOffice 先转换为 `.pptx`。
- 大型离线模型不进入 Git 仓库，需要由发布机本地准备。
