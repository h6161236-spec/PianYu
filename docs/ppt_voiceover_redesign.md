# PPT Voiceover Redesign

## 目标

把当前项目从“视频转字幕/翻译/配音工具”改造成“PPT 导入 -> 生成整套口播稿 -> 分页编辑中英文稿 -> 自动配音 -> 生成双语字幕视频”的桌面工具。

对应本轮需求，目标流程应当是：

1. 导入 `ppt/pptx`
2. 提取整套 PPT 的结构、每页文本、备注、缩略图
3. 调用大模型先生成整套中文口播稿，并拆分到每页
4. 页面中同时显示“当前页 PPT 预览 + 当前页中文稿 + 当前页英文稿”
5. 中文稿可修改，英文稿可由中文稿翻译生成后再修改
6. 按页生成配音与字幕，自动拼成视频
7. 视频中 PPT 随配音时长自动翻页，字幕为中英双语

## 现有项目盘点

当前仓库不是空白项目，而是一个已经跑通了以下链路的 PySide6 Windows 桌面程序：

- 本地项目保存/加载
- 后台 Worker + 进度条 + 可暂停/取消任务
- OpenAI 兼容大模型批量调用
- 英文 TTS 批量生成
- SRT/VTT 读写
- `ffmpeg` 视频、音频、字幕封装
- 双语字幕烧录

### 可以直接复用

| 模块 | 现状 | 结论 |
| --- | --- | --- |
| `src/vcut_studio/providers/translation.py` | 已支持 OpenAI 兼容接口、批量 JSON 输出、超时重试 | 直接复用，改 prompt 和数据结构 |
| `src/vcut_studio/providers/tts.py` | 已支持英文 TTS、音色管理、批量生成 | 直接复用到英文口播；中文口播需新增中文 TTS Provider |
| `src/vcut_studio/subtitles.py` | 已支持字幕读写 | 直接复用 |
| `src/vcut_studio/exporting.py` | 已支持字幕烧录、音轨合成、ffmpeg 调用 | 部分复用，新增“PPT 幻灯片视频导出”分支 |
| `src/vcut_studio/ui/workers.py` | 后台任务结构完整 | 直接复用，新增 PPT 解析/脚本生成/渲染 Worker |
| `src/vcut_studio/settings.py` | 已有模型、TTS、导出配置体系 | 直接复用并扩展 |
| `src/vcut_studio/project_store.py` | 已支持项目序列化 | 直接复用并扩展新的项目模型 |

### 只能部分复用

| 模块 | 原用途 | 处理建议 |
| --- | --- | --- |
| `src/vcut_studio/ui/pages.py` | 面向视频时间轴、翻译表格、配音表格 | UI 结构可借鉴，但需要新增 PPT 专用工作台 |
| `src/vcut_studio/ui/main_window.py` | 当前主流程围绕“导入视频” | 主流程要重构为“导入 PPT”主入口 |
| `src/vcut_studio/models.py` | 当前核心对象是 `Project -> Segment` | 需要新增 `SlidePage`/`PresentationProject` 一类模型 |
| `src/vcut_studio/media.py` | 面向视频探测/预览代理 | 只有 ffmpeg 路径解析可继续用，PPT 渲染需新模块 |

### 基本不复用

| 模块 | 原用途 | 结论 |
| --- | --- | --- |
| `src/vcut_studio/asr.py` | 视频转写 | 新方案里不是主链路 |
| `src/vcut_studio/cutting.py` | 删除口头禅/停顿 | 新方案里不是主链路 |

## 参考 PPT 观察

对仓库内 `COGT_TR_User Training_TR03_Payment Management_参考.PPTX` 的快速检查结果：

- 总页数：`47`
- `notesSlides` 数量：`31`
- 抽样结果显示，备注页结构存在，但采样里没有看到可直接复用的完整讲稿文本，更多像空备注页或只含页码
- 幻灯片正文以英文培训内容为主，因此“中文口播稿”应视为基于英文课件内容自动生成，而不是简单抽取现有中文备注

这意味着：

- 不能把“有 notesSlides”直接等同于“已有讲稿”
- 讲稿生成必须以“页内文本 + 页标题 + 目录结构 + 可选备注”为主

## 建议的新技术路线

### 1. PPT 解析

建议新增 `src/vcut_studio/ppt_io.py`，职责如下：

- 读取 `pptx`
- 提取演示文稿元信息
- 提取每页标题、正文、表格文本、备注文本
- 提取页序和章节结构
- 生成每页的内部数据对象

实现建议：

- 首选 `python-pptx` 读取结构化内容和 notes
- 不建议手写 XML 解析作为主方案，只保留为极端兼容兜底

原因：

- `python-pptx` 对 slide / notes 的对象模型更稳
- 方便后续支持表格、文本框、备注
- 可避免手工拼装 OOXML 文本时出现的碎片化问题

### 2. 幻灯片渲染

`python-pptx` 负责“读内容”，不负责“高保真渲染图片”。这里建议双通道：

- 首选：Windows + 已安装 PowerPoint 时，走 PowerPoint COM 导出 PNG
- 兜底：未安装 PowerPoint 时，走 LibreOffice headless 转 PDF，再转 PNG

建议新增 `src/vcut_studio/ppt_render.py`：

- `render_with_powerpoint()`
- `render_with_libreoffice()`
- `render_slide_images()`

### 3. 讲稿生成

建议新增 `src/vcut_studio/providers/script_generation.py` 或 `src/vcut_studio/script_generation.py`。

不要只做“单页 prompt 逐页生成”，而要做两阶段：

1. 全局理解阶段
   - 输入整套 PPT 的标题页、目录页、每页摘要
   - 产出课程风格、目标受众、整体讲解策略
2. 分页生成阶段
   - 按页生成中文口播稿
   - 结合全局风格，保证前后页语气一致、衔接自然

建议输出 JSON：

```json
{
  "deck_summary": "整套课程讲解定位",
  "slides": [
    {
      "slide_index": 1,
      "zh_script": "本页中文口播稿",
      "speaker_notes_summary": "抽取到的备注摘要",
      "estimated_duration_sec": 18
    }
  ]
}
```

说明：

- 中文稿是主稿
- 英文稿从中文稿翻译生成，不直接从幻灯片英文正文硬翻
- 这样后期改中文，再重译英文，逻辑更顺

### 4. 页面编辑工作台

建议新增一个 PPT 专用页面，而不是继续挤在当前的翻译表格中。

建议 UI 结构：

- 左侧：页缩略图列表
- 中间：当前页大图预览
- 右侧上半：中文口播稿编辑区
- 右侧下半：英文口播稿编辑区
- 底部：当前页参数
  - 预计时长
  - 实际 TTS 时长
  - 音色
  - 状态

这样可以直接满足“页面能同时看到这一页 PPT 和对应稿子”。

### 5. 翻译策略

现有 `translation.py` 可以继续用，但 prompt 要改。

当前 prompt 偏“字幕翻译”，新 prompt 要偏“培训课件口播翻译”：

- 保持讲师口吻
- 语句自然可读
- 允许适度扩写连接词
- 保持术语一致
- 返回每页英文稿

### 6. 配音策略

建议第一阶段先做：

- 中文稿为编辑主稿
- 英文稿为输出口播稿
- 英文 TTS 生成最终配音
- 中英双语字幕同时烧录

原因：

- 现有项目已经具备英文 TTS 生产链路
- 可以最快落地

如果需要“中文配音 + 英文字幕”或“中英文双语配音”，第二阶段再补中文 TTS Provider。

### 7. 视频拼接策略

建议新增 `src/vcut_studio/ppt_exporting.py`，不要强行塞进现有“视频剪辑导出”路径。

推荐流程：

1. 为每页生成静态 PNG
2. 为每页英文稿生成单独音频
3. 读取每页音频真实时长
4. 以“页音频时长”作为该页视频时长
5. 用 ffmpeg 将每页 `图片 + 音频` 拼成片段
6. 再 concat 成整片
7. 从分页稿件派生中英双语 SRT
8. 最后烧录字幕或同时输出 sidecar SRT

这样天然满足“视频中的 PPT 随着稿子翻页”。

## 推荐的数据模型

建议扩展模型，不建议继续用 `Segment` 强行表示幻灯片页。

```text
PresentationProject
  - project_id
  - name
  - source_ppt_path
  - rendered_slide_dir
  - deck_summary
  - slides: list[SlidePage]
  - exports

SlidePage
  - slide_id
  - slide_index
  - title
  - source_text
  - notes_text
  - image_path
  - zh_script
  - en_script
  - estimated_duration_ms
  - actual_tts_duration_ms
  - voice_id
  - tts_audio_path
  - translation_status
  - tts_status
  - review_status
```

另外建议增加“字幕派生层”，不要把字幕切分结果直接存成主编辑稿：

```text
SubtitleChunk
  - slide_index
  - start_ms
  - end_ms
  - zh_text
  - en_text
```

原因：

- 用户编辑的是“页级讲稿”
- 导出时再把页级讲稿按句号、分号、长度阈值拆成字幕块

## 建议的模块拆分

建议新增或重构如下文件：

```text
src/vcut_studio/
  ppt_io.py
  ppt_render.py
  ppt_exporting.py
  script_generation.py
  presentation_models.py
  ui/
    ppt_workspace.py
    ppt_workers.py
```

现有文件建议改造方式：

- `models.py`
  - 保留旧视频项目模型
  - 新增 PPT 项目模型，避免破坏旧功能
- `project_store.py`
  - 支持多项目类型序列化
- `settings.py`
  - 增加 PPT 渲染器配置、脚本生成配置、中文 TTS 预留配置
- `ui/main_window.py`
  - 菜单入口从“导入视频”扩展为“导入 PPT”
- `exporting.py`
  - 保留视频导出逻辑
  - 与 `ppt_exporting.py` 做职责分离

## 为什么不建议直接照搬开源项目

我看过几类开源方向，它们有参考价值，但不适合作为当前项目的直接主干：

- `ppt_presenter`
  - 证明了“PPT -> 备注 -> TTS -> 视频”链路可行
  - 但它是脚本式工具，没有桌面编辑工作台
- `pptx-to-video`
  - 证明了“notes 或 slide text 都可作为讲稿输入”
  - 但没有中英双稿编辑、没有本地项目管理、没有双语字幕工作流
- `ppt2video`
  - 证明了 Windows + PowerPoint + ffmpeg 方案可行
  - 但它更像一次性转换脚本，不适合作为长期可维护的桌面产品

所以建议是：

- 借鉴它们的处理链路
- 不直接搬它们的产品形态

## 推荐实施顺序

### Phase 1：先跑通 MVP

目标：

- 导入 PPT
- 渲染每页图片
- 生成分页中文稿
- 翻译分页英文稿
- 页面同时预览 PPT + 中英文稿
- 英文 TTS
- 生成英文口播 + 中英双语字幕视频

这是最接近当前项目可复用能力的路线。

### Phase 2：增强编辑体验

增加：

- 页缩略图导航
- 一键全量生成 / 单页重生成
- 页级时长预估
- 字幕切分预览
- 批量术语表
- 中文 TTS

### Phase 3：高级功能

增加：

- 动画/高亮区域同步
- 页内步骤讲解节奏控制
- 多音色模板
- AI 审稿/术语一致性检查
- 多语言导出

## 我建议的第一版产品定义

为了尽快可交付，我建议第一版先明确为：

- 输入：`pptx`
- 编辑语言：中文主稿 + 英文译稿
- 输出语音：英文
- 输出字幕：中英双语
- 翻页逻辑：按每页最终配音时长自动翻页
- 备注使用方式：有备注就作为生成先验，无备注就从页内文本生成

这是当前项目复用率最高、风险最低、最快能出成果的方案。

## 外部参考

- python-pptx Notes 文档：
  - https://python-pptx.readthedocs.io/en/latest/user/notes.html
- python-pptx Notes Slide 分析：
  - https://python-pptx.readthedocs.io/en/latest/dev/analysis/sld-notes-slide.html
- PowerPoint `Presentation.Export` 官方文档：
  - https://learn.microsoft.com/zh-cn/office/vba/api/powerpoint.presentation.export
- 开源参考 `ppt_presenter`：
  - https://github.com/chaonan99/ppt_presenter
- 开源参考 `pptx-to-video`：
  - https://github.com/drrobotk/pptx-to-video
- 开源参考 `ppt2video`：
  - https://github.com/DH-Center-Tuebingen/ppt2video

