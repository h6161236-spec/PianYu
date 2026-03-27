# VCut Studio

`VCut Studio` 是一个 Windows 桌面端视频剪辑软件，当前版本聚焦两条主线：

- 自动识别并删除语气词、无效停顿
- 把中文视频翻译成英文字幕，并生成英文配音

## 当前 V1 能力

- 导入本地视频并探测媒体信息
- 使用 `faster-whisper` 进行中文转写
- 自动生成“语气词 / 长停顿”剪辑建议，并由用户手动确认
- 通过外接 OpenAI 兼容大模型翻译英文字幕
- 支持人工改写英文文本
- 使用 Windows 内置英文语音生成分段配音
- 导出三种成片：
  - 纯净中文版视频
  - 英文字幕视频
  - 英文配音视频
- 全中文界面

## 本地运行

```powershell
pip install -e .[asr]
python -m vcut_studio
```

如果你要打 Windows 发布包，建议安装：

```powershell
pip install -e .[asr,release]
```

## 外部依赖

- `ffmpeg` / `ffprobe`
- `faster-whisper`
- Windows 可用英文语音
- 可访问的 OpenAI 兼容翻译接口

说明：

- 发布包支持自动识别程序目录中的 `ffmpeg.exe`，如果同时存在 `ffprobe.exe` 会优先使用
- 翻译 API Key 当前保存在本地设置 JSON 中，尚未接入 Windows Credential Manager / DPAPI

## 打包发布

Windows 打包脚本：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\build_windows_release.ps1
```

构建说明见：

- [Windows 打包发布](docs/Windows打包发布.md)
- [发布验收清单](docs/发布验收清单.md)
- [后续优化记录](docs/后续优化记录.md)

## GitHub 提交建议

- 仓库建议只提交源码、脚本、文档和测试，不提交 `build/`、`dist/`、`release/` 等生成产物。
- `models/` 下的离线 TTS / ASR 资源体积很大，其中部分文件超过 GitHub 单文件限制，建议保留在本地并通过 `.gitignore` 排除。
- 如果需要恢复离线 TTS，把 `kokoro` 模型目录和 `sherpa-onnx` 运行时文件放回 `models/tts/` 即可。

## 项目结构

```text
src/vcut_studio/
  app.py
  asr.py
  context.py
  exporting.py
  media.py
  models.py
  project_store.py
  settings.py
  providers/
  ui/
tests/
scripts/
packaging/
docs/
```
