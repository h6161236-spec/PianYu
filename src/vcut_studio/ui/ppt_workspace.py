from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ..models import Project, SlidePage


def _inline_row(*widgets: QWidget) -> QWidget:
    container = QWidget()
    layout = QHBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(6)
    for widget in widgets:
        layout.addWidget(widget)
    layout.addStretch(1)
    return container


def _microphone_icon() -> QIcon:
    pixmap = QPixmap(18, 18)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    pen = QPen(QColor("#0f766e"))
    pen.setWidth(2)
    painter.setPen(pen)
    painter.drawRoundedRect(6, 2, 6, 9, 3, 3)
    painter.drawLine(9, 11, 9, 15)
    painter.drawArc(3, 7, 12, 10, 0, -180 * 16)
    painter.drawLine(6, 15, 12, 15)
    painter.end()
    return QIcon(pixmap)


def _format_duration_label(duration_ms: int) -> str:
    total_seconds = max(0, int(round(max(0, duration_ms) / 1000.0)))
    minutes, seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours > 0:
        return f"{hours} 小时 {minutes:02d} 分 {seconds:02d} 秒"
    if minutes > 0:
        return f"{minutes} 分 {seconds:02d} 秒"
    return f"{seconds} 秒"


def _project_estimated_duration_ms(project: Project) -> int:
    return sum(max(0, int(slide.estimated_duration_ms or 0)) for slide in project.slides)


def _status_label(value: str, *, kind: str) -> str:
    normalized = str(value or "").strip().lower()
    if kind == "translation":
        mapping = {
            "pending": "待翻译",
            "translated": "已翻译",
            "needs_review": "待校对",
            "manual": "手动填写",
        }
    else:
        mapping = {
            "pending": "未生成",
            "completed": "已完成",
        }
    return mapping.get(normalized, value or "未开始")


class PptWorkspacePage(QWidget):
    slide_selection_changed = Signal(int)
    chinese_script_edited = Signal(int, str)
    english_script_edited = Signal(int, str)
    chinese_voice_input_pressed = Signal()
    chinese_voice_input_released = Signal()
    generate_all_requested = Signal()
    generate_current_requested = Signal()
    translate_all_requested = Signal()
    translate_current_requested = Signal()
    export_requested = Signal()
    quick_export_requested = Signal()
    save_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._loaded_project: Project | None = None
        self._loading = False
        self._current_slide_index = 0
        self._preview_pixmap = QPixmap()
        self._running = False
        self._voice_input_recording = False
        self._voice_input_transcribing = False

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(12)

        self.summary_label = QLabel("导入 PPT 后，这里会显示页数、预计口播时长和整套内容摘要。")
        self.summary_label.setWordWrap(True)
        root.addWidget(self.summary_label)

        action_row = QHBoxLayout()
        self.generate_all_button = QPushButton("整套生成中文稿")
        self.generate_current_button = QPushButton("生成当前页中文稿")
        self.translate_all_button = QPushButton("整套翻译英文稿")
        self.translate_current_button = QPushButton("翻译当前页英文稿")
        self.export_button = QPushButton("导出视频")
        self.quick_export_button = QPushButton("一键导出")
        self.save_button = QPushButton("保存项目")
        action_row.addWidget(self.generate_all_button)
        action_row.addWidget(self.generate_current_button)
        action_row.addWidget(self.translate_all_button)
        action_row.addWidget(self.translate_current_button)
        action_row.addWidget(self.export_button)
        action_row.addWidget(self.quick_export_button)
        action_row.addStretch(1)
        action_row.addWidget(self.save_button)
        root.addLayout(action_row)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setFormat("")
        self.progress_bar.setVisible(False)
        root.addWidget(self.progress_bar)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)

        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(8)
        left_layout.addWidget(QLabel("幻灯片"))
        self.slide_list = QListWidget()
        self.slide_list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self.slide_list.setAlternatingRowColors(True)
        self.slide_list.currentRowChanged.connect(self._on_slide_changed)
        left_layout.addWidget(self.slide_list, stretch=1)
        splitter.addWidget(left_panel)

        center_panel = QWidget()
        center_layout = QVBoxLayout(center_panel)
        center_layout.setContentsMargins(0, 0, 0, 0)
        center_layout.setSpacing(10)

        self.slide_meta_label = QLabel("请选择左侧幻灯片，查看当前 PPT 页面内容。")
        self.slide_meta_label.setWordWrap(True)
        center_layout.addWidget(self.slide_meta_label)

        center_splitter = QSplitter(Qt.Orientation.Vertical)
        center_splitter.setChildrenCollapsible(False)

        preview_group = QGroupBox("页面预览")
        preview_layout = QVBoxLayout(preview_group)
        preview_layout.setContentsMargins(12, 12, 12, 12)
        self.preview_label = QLabel("当前页面还没有预览图。")
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setObjectName("pptPreviewLabel")
        self.preview_label.setMinimumSize(640, 360)
        preview_scroll = QScrollArea()
        preview_scroll.setWidgetResizable(True)
        preview_scroll.setWidget(self.preview_label)
        preview_layout.addWidget(preview_scroll)
        center_splitter.addWidget(preview_group)

        content_group = QGroupBox("页面内容")
        content_layout = QFormLayout(content_group)
        self.title_label = QLabel("-")
        self.title_label.setWordWrap(True)
        self.source_text_edit = QPlainTextEdit()
        self.source_text_edit.setReadOnly(True)
        self.source_text_edit.setMinimumHeight(140)
        self.notes_text_edit = QPlainTextEdit()
        self.notes_text_edit.setReadOnly(True)
        self.notes_text_edit.setMinimumHeight(120)
        content_layout.addRow("标题", self.title_label)
        content_layout.addRow("页面文本", self.source_text_edit)
        content_layout.addRow("备注", self.notes_text_edit)
        center_splitter.addWidget(content_group)
        center_splitter.setStretchFactor(0, 4)
        center_splitter.setStretchFactor(1, 2)
        center_splitter.setSizes([560, 280])
        center_layout.addWidget(center_splitter, stretch=1)
        splitter.addWidget(center_panel)

        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(10)

        script_meta_group = QGroupBox("口播状态")
        script_meta_layout = QFormLayout(script_meta_group)
        self.total_duration_label = QLabel("-")
        self.duration_label = QLabel("-")
        self.translation_status_label = QLabel("-")
        self.tts_status_label = QLabel("-")
        script_meta_layout.addRow("整套预计时长", self.total_duration_label)
        script_meta_layout.addRow("当前页时长", self.duration_label)
        script_meta_layout.addRow("翻译状态", self.translation_status_label)
        script_meta_layout.addRow("配音状态", self.tts_status_label)
        right_layout.addWidget(script_meta_group)

        chinese_group = QGroupBox("中文口播稿")
        chinese_layout = QVBoxLayout(chinese_group)
        self.zh_script_edit = QPlainTextEdit()
        self.zh_script_edit.textChanged.connect(self._emit_chinese_script_changed)
        chinese_layout.addWidget(self.zh_script_edit)
        self.zh_voice_input_button = QPushButton("按住录入语音")
        microphone_icon = QIcon.fromTheme("audio-input-microphone")
        if microphone_icon.isNull():
            microphone_icon = _microphone_icon()
        self.zh_voice_input_button.setIcon(microphone_icon)
        self.zh_voice_input_button.setAutoDefault(False)
        self.zh_voice_input_button.setStyleSheet(
            "QPushButton { padding: 8px 14px; border-radius: 8px; font-weight: 600; }"
        )
        self.zh_voice_input_status_label = QLabel("按住按钮录音，松开后自动转成中文稿。")
        self.zh_voice_input_status_label.setWordWrap(True)
        self.zh_voice_input_row = _inline_row(self.zh_voice_input_button, self.zh_voice_input_status_label)
        self.zh_voice_input_row.setVisible(False)
        chinese_layout.addWidget(self.zh_voice_input_row)
        right_layout.addWidget(chinese_group, stretch=1)

        english_group = QGroupBox("英文口播稿")
        english_layout = QVBoxLayout(english_group)
        self.en_script_edit = QPlainTextEdit()
        self.en_script_edit.textChanged.connect(self._emit_english_script_changed)
        english_layout.addWidget(self.en_script_edit)
        right_layout.addWidget(english_group, stretch=1)
        splitter.addWidget(right_panel)

        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 4)
        splitter.setStretchFactor(2, 3)
        root.addWidget(splitter, stretch=1)

        self.generate_all_button.clicked.connect(self.generate_all_requested.emit)
        self.generate_current_button.clicked.connect(self.generate_current_requested.emit)
        self.translate_all_button.clicked.connect(self.translate_all_requested.emit)
        self.translate_current_button.clicked.connect(self.translate_current_requested.emit)
        self.export_button.clicked.connect(self.export_requested.emit)
        self.quick_export_button.clicked.connect(self.quick_export_requested.emit)
        self.save_button.clicked.connect(self.save_requested.emit)
        self.zh_voice_input_button.pressed.connect(self.chinese_voice_input_pressed.emit)
        self.zh_voice_input_button.released.connect(self.chinese_voice_input_released.emit)
        self._refresh_voice_input_controls()

    def resizeEvent(self, event: object) -> None:
        super().resizeEvent(event)
        self._refresh_preview_label()

    def _update_project_summary_labels(self, project: Project | None) -> None:
        if project is None:
            self.summary_label.setText("导入 PPT 后，这里会显示页数、预计口播时长和整套内容摘要。")
            self.total_duration_label.setText("-")
            return

        total_estimated_duration_ms = _project_estimated_duration_ms(project)
        total_duration_text = (
            _format_duration_label(total_estimated_duration_ms)
            if total_estimated_duration_ms > 0
            else "尚未估算"
        )
        self.summary_label.setText(
            f"共 {len(project.slides)} 页"
            f" | 源文件：{project.source_ppt_path or '未导入'}"
            f" | 预计时长：{total_duration_text}"
            f" | 摘要：{project.deck_summary or '暂无摘要'}"
        )
        self.total_duration_label.setText(total_duration_text)

    def load_project(self, project: Project) -> None:
        self._loaded_project = project
        self._loading = True
        try:
            selected_slide_index = self.selected_slide_index() or 1
            self._update_project_summary_labels(project)
            self.slide_list.clear()
            for slide in project.slides:
                title = slide.title or "未命名页面"
                item = QListWidgetItem(f"{slide.slide_index:02d}. {title}")
                item.setData(Qt.ItemDataRole.UserRole, slide.slide_index)
                self.slide_list.addItem(item)

            target_row = 0
            for row_index in range(self.slide_list.count()):
                item = self.slide_list.item(row_index)
                if int(item.data(Qt.ItemDataRole.UserRole) or 0) == selected_slide_index:
                    target_row = row_index
                    break
            if self.slide_list.count() > 0:
                self.slide_list.setCurrentRow(target_row)
            else:
                self._load_slide(None)
        finally:
            self._loading = False

    def selected_slide_index(self) -> int:
        item = self.slide_list.currentItem()
        if item is None:
            return 0
        return int(item.data(Qt.ItemDataRole.UserRole) or 0)

    def current_slide(self) -> SlidePage | None:
        if self._loaded_project is None:
            return None
        slide_index = self.selected_slide_index()
        for slide in self._loaded_project.slides:
            if slide.slide_index == slide_index:
                return slide
        return None

    def set_running(self, running: bool) -> None:
        self._running = running
        self.generate_all_button.setEnabled(not running)
        self.generate_current_button.setEnabled(not running and self.current_slide() is not None)
        self.translate_all_button.setEnabled(not running)
        self.translate_current_button.setEnabled(not running and self.current_slide() is not None)
        self.export_button.setEnabled(not running)
        self.quick_export_button.setEnabled(not running)
        self.save_button.setEnabled(not running)
        self.progress_bar.setVisible(running)
        if not running:
            self.progress_bar.setValue(0)
            self.progress_bar.setFormat("")
        self._refresh_voice_input_controls()

    def set_progress(self, progress: int, message: str) -> None:
        clamped = max(0, min(int(progress), 100))
        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(clamped)
        self.progress_bar.setFormat(str(message or "").strip())

    def update_slide_scripts(
        self,
        slide_index: int,
        *,
        zh_script: str | None = None,
        en_script: str | None = None,
        estimated_duration_ms: int | None = None,
        translation_status: str | None = None,
        refresh_summary: bool = True,
    ) -> None:
        if self._loaded_project is None:
            return
        updated_slide: SlidePage | None = None
        for slide in self._loaded_project.slides:
            if slide.slide_index != slide_index:
                continue
            if zh_script is not None:
                slide.zh_script = zh_script
            if en_script is not None:
                slide.en_script = en_script
            if estimated_duration_ms is not None:
                slide.estimated_duration_ms = estimated_duration_ms
            if translation_status is not None:
                slide.translation_status = translation_status
            updated_slide = slide
            break
        if updated_slide is None:
            return
        if refresh_summary:
            self._update_project_summary_labels(self._loaded_project)
        if self.selected_slide_index() != slide_index:
            return
        self._loading = True
        try:
            if zh_script is not None:
                self.zh_script_edit.setPlainText(updated_slide.zh_script)
            if en_script is not None:
                self.en_script_edit.setPlainText(updated_slide.en_script)
            if estimated_duration_ms is not None:
                self.duration_label.setText(_format_duration_label(updated_slide.estimated_duration_ms))
            if translation_status is not None:
                self.translation_status_label.setText(
                    _status_label(updated_slide.translation_status, kind="translation")
                )
        finally:
            self._loading = False

    def insert_chinese_script_text(self, text: str) -> None:
        normalized = text.strip()
        if not normalized:
            return
        cursor = self.zh_script_edit.textCursor()
        existing = self.zh_script_edit.toPlainText()
        insert_text = normalized
        if existing:
            position = cursor.position()
            if 0 < position <= len(existing) and not existing[position - 1].isspace():
                insert_text = "\n" + insert_text
            if position < len(existing) and not existing[position].isspace():
                insert_text = insert_text + "\n"
        self.zh_script_edit.setFocus()
        cursor.insertText(insert_text)
        self.zh_script_edit.setTextCursor(cursor)

    def set_chinese_voice_input_state(
        self,
        *,
        recording: bool = False,
        transcribing: bool = False,
        message: str = "",
    ) -> None:
        self._voice_input_recording = recording
        self._voice_input_transcribing = transcribing
        if message:
            self.zh_voice_input_status_label.setText(message)
        elif transcribing:
            self.zh_voice_input_status_label.setText("正在识别语音，请稍候。")
        elif recording:
            self.zh_voice_input_status_label.setText("正在录音，松开按钮后开始识别。")
        elif self.current_slide() is None:
            self.zh_voice_input_status_label.setText("请先选择一个 PPT 页面，再开始语音输入。")
        else:
            self.zh_voice_input_status_label.setText("按住按钮录音，松开后自动转成中文稿。")
        self._refresh_voice_input_controls()

    def _on_slide_changed(self, row: int) -> None:
        if row < 0:
            self._load_slide(None)
            return
        slide = self.current_slide()
        self._load_slide(slide)
        if not self._loading and slide is not None:
            self.slide_selection_changed.emit(slide.slide_index)

    def _load_slide(self, slide: SlidePage | None) -> None:
        self._loading = True
        try:
            if slide is None:
                self._current_slide_index = 0
                self._update_project_summary_labels(self._loaded_project)
                self.slide_meta_label.setText("当前没有可显示的 PPT 页面。")
                self.source_text_edit.setPlainText("")
                self.notes_text_edit.setPlainText("")
                self.zh_script_edit.setPlainText("")
                self.en_script_edit.setPlainText("")
                self.total_duration_label.setText(
                    _format_duration_label(_project_estimated_duration_ms(self._loaded_project))
                    if self._loaded_project is not None and _project_estimated_duration_ms(self._loaded_project) > 0
                    else "-"
                )
                self.duration_label.setText("-")
                self.translation_status_label.setText("-")
                self.tts_status_label.setText("-")
                self._preview_pixmap = QPixmap()
                self.preview_label.setText("当前页面还没有预览图。")
                self.preview_label.setPixmap(QPixmap())
                self._refresh_voice_input_controls()
                return

            self._current_slide_index = slide.slide_index
            self._update_project_summary_labels(self._loaded_project)
            self.slide_meta_label.setText(f"第 {slide.slide_index} 页 | 标题：{slide.title or '未命名'}")
            self.title_label.setText(slide.title or "-")
            self.source_text_edit.setPlainText(slide.source_text)
            self.notes_text_edit.setPlainText(slide.notes_text)
            self.zh_script_edit.setPlainText(slide.zh_script)
            self.en_script_edit.setPlainText(slide.en_script)
            self.total_duration_label.setText(
                _format_duration_label(_project_estimated_duration_ms(self._loaded_project))
                if self._loaded_project is not None and _project_estimated_duration_ms(self._loaded_project) > 0
                else "-"
            )
            self.duration_label.setText(_format_duration_label(slide.estimated_duration_ms))
            self.translation_status_label.setText(
                _status_label(slide.translation_status, kind="translation")
            )
            self.tts_status_label.setText(_status_label(slide.tts_status, kind="tts"))

            preview_path = Path(slide.preview_image_path) if slide.preview_image_path else None
            if preview_path is not None and preview_path.exists():
                self._preview_pixmap = QPixmap(str(preview_path))
            else:
                self._preview_pixmap = QPixmap()
            self._refresh_preview_label()
            self._refresh_voice_input_controls()
        finally:
            self._loading = False

    def _refresh_preview_label(self) -> None:
        if self._preview_pixmap.isNull():
            self.preview_label.setPixmap(QPixmap())
            self.preview_label.setText("当前页面还没有预览图。")
            return
        viewport_width = max(320, self.preview_label.width() - 24)
        viewport_height = max(180, self.preview_label.height() - 24)
        scaled = self._preview_pixmap.scaled(
            viewport_width,
            viewport_height,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.preview_label.setText("")
        self.preview_label.setPixmap(scaled)

    def _emit_chinese_script_changed(self) -> None:
        if self._loading or self._current_slide_index <= 0:
            return
        self.chinese_script_edited.emit(self._current_slide_index, self.zh_script_edit.toPlainText())

    def _emit_english_script_changed(self) -> None:
        if self._loading or self._current_slide_index <= 0:
            return
        self.english_script_edited.emit(self._current_slide_index, self.en_script_edit.toPlainText())

    def _refresh_voice_input_controls(self) -> None:
        has_slide = self.current_slide() is not None
        button_enabled = has_slide and not self._running and not self._voice_input_transcribing
        if self._voice_input_recording:
            self.zh_voice_input_button.setEnabled(True)
            self.zh_voice_input_button.setText("松开结束录音")
            self.zh_voice_input_button.setDown(True)
            return
        if self._voice_input_transcribing:
            self.zh_voice_input_button.setEnabled(False)
            self.zh_voice_input_button.setText("识别中...")
            self.zh_voice_input_button.setDown(False)
            return
        self.zh_voice_input_button.setEnabled(button_enabled)
        self.zh_voice_input_button.setText("按住说话")
        self.zh_voice_input_button.setDown(False)
