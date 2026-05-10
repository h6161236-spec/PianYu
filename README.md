# 片语 PianYu

片语是一款 Windows 桌面端 PPT 口播视频制作工具。它可以导入 PPT/PPTX 课件，提取幻灯片文字和演讲备注，生成中英文口播稿，合成配音，并导出带字幕的课件讲解视频。

## 主要功能

- 导入 `.pptx` 和旧版 `.ppt` 文件
- 提取幻灯片标题、正文、表格文字、组合文本和演讲备注
- 有 Microsoft PowerPoint 时渲染真实幻灯片预览
- 没有 PowerPoint 时生成文本预览图作为备用
- 调用 OpenAI-compatible 模型生成中文逐页口播稿
- 将中文口播稿翻译为自然的英文讲解稿
- 在桌面工作台中逐页编辑中文稿和英文稿
- 支持按住录音，把中文口述内容转写到当前页
- 导出中文或英文配音视频
- 支持 Kokoro、Melo 等本地 TTS 资产布局
- 支持中英双语、仅中文、仅英文字幕
- 支持字幕烧录、sidecar `.srt`、字幕字号/颜色/位置配置

## 工作流程

1. 导入 PPT/PPTX。
2. 检查每页预览、原始文字和备注。
3. 整套生成中文口播稿，或只生成当前页。
4. 需要英文输出时，再整套翻译英文稿或翻译当前页。
5. 手动调整每页中英文稿。
6. 选择配音语言、音色、字幕模式、输出目录和格式。
7. 导出讲解视频。

“一键导出”会在缺少稿件时自动补齐中文稿/英文稿，然后继续合成配音并导出视频。

## 环境要求

- Windows 10/11
- Python 3.11+
- PowerShell
- `ffmpeg.exe`
- Microsoft PowerPoint，推荐，用于真实预览和 `.ppt` 转换
- LibreOffice，可选，用于旧版 `.ppt` 转换备用
- OpenAI-compatible 聊天模型接口，用于生成和翻译口播稿

## 本地运行

安装开发依赖：

```powershell
pip install -e .[asr,dev]
```

启动：

```powershell
python -m vcut_studio
```

当前源码包名仍为 `vcut_studio`，这是为了保留已有代码结构；软件界面和发布包名称为“片语”。

## 打包发布

安装发布依赖：

```powershell
pip install -e .[asr,release]
```

构建 Windows 发布包：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\build_windows_release.ps1
```

指定 ffmpeg 目录：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\build_windows_release.ps1 -FfmpegDir "D:\tools\ffmpeg\bin"
```

默认输出：

- `dist\片语\片语.exe`
- `release\片语-windows-v<version>.zip`

## 文档

- [使用指南](docs/使用指南.md)
- [Windows 打包发布](docs/Windows打包发布.md)
- [发布验收清单](docs/发布验收清单.md)

## 仓库说明

仓库只提交源码、测试、脚本、打包配置、图标和文档。以下内容不进入 Git：

- `build/`、`dist/`、`release/`
- `.test-artifacts/`、临时目录、`.codex/`
- 本地 `.pptx`、`.mp4`、`.srt`、`.vcutproj`
- `models/` 和语音候选素材

## 项目结构

```text
src/vcut_studio/
  app.py                  # 桌面应用入口
  ppt_io.py               # PPT 导入、提取和预览渲染
  ppt_scripts.py          # 口播稿生成和翻译提示词
  ppt_exporting.py        # PPT 配音视频导出
  providers/              # 翻译和 TTS provider
  ui/                     # PySide6 桌面界面
tests/
scripts/
packaging/
docs/
```
