from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QItemSelectionModel, QRectF, QSize, QTime, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QStackedWidget,
    QSplitter,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from ..models import Project
from ..providers.tts import (
    all_tts_voices,
    available_tts_voices,
    installed_windows_voices,
    kokoro_local_ready,
    tts_provider_choices,
    tts_provider_display_name,
)
from ..settings import AppSettings
from .dubbing_logic import (
    build_dubbing_summary_text,
    dub_review_status,
    dub_review_status_label,
    segment_target_voice_id,
)

SEGMENT_REMOVED_COLOR = QColor("#2f2527")
SEGMENT_TRIMMED_COLOR = QColor("#3a3423")
SUGGESTION_ACCEPTED_COLOR = QColor("#1d3a36")
DUB_REUSABLE_COLOR = QColor("#1d3a36")
DUB_STALE_COLOR = QColor("#3a3423")
DUB_MISSING_COLOR = QColor("#242b35")
DUB_GENERATING_COLOR = QColor("#1d3142")


def format_timestamp(ms: int) -> str:
    total_ms = max(0, ms)
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{milliseconds:03d}"


def timestamp_to_qtime(ms: int) -> QTime:
    total_ms = max(0, int(ms))
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1_000)
    return QTime(hours % 24, minutes, seconds, milliseconds)


def qtime_to_timestamp(value: QTime) -> int:
    return max(0, value.msecsSinceStartOfDay())


def set_combo_value(combo: QComboBox, value: str) -> None:
    index = combo.findData(value)
    if index < 0:
        index = combo.findText(value)
    if index >= 0:
        combo.setCurrentIndex(index)
    elif combo.isEditable():
        combo.setEditText(value)


def configure_resizable_columns(table: QTableWidget, widths: list[int]) -> None:
    header = table.horizontalHeader()
    header.setStretchLastSection(False)
    header.setMinimumSectionSize(72)
    header.setCascadingSectionResizes(False)
    for column_index in range(table.columnCount()):
        header.setSectionResizeMode(column_index, QHeaderView.ResizeMode.Interactive)
        if column_index < len(widths):
            table.setColumnWidth(column_index, widths[column_index])


def build_inline_row(*widgets: QWidget) -> QWidget:
    container = QWidget()
    layout = QHBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(6)
    for widget in widgets:
        layout.addWidget(widget)
    return container


def format_suggestion_reason(reason: str, details: str) -> str:
    if reason == "filler_word":
        return f"语气词：{details}" if details else "语气词"
    if reason == "long_pause":
        return f"长停顿：{details}" if details else "长停顿"
    if reason == "silent_range":
        return f"无声/无人声：{details}" if details else "无声/无人声"
    return details or reason


def format_suggestion_type(reason: str) -> str:
    if reason == "filler_word":
        return "口癖"
    if reason == "long_pause":
        return "停顿"
    if reason == "silent_range":
        return "无声/无人声"
    return "其他"


def suggestion_filter_matches(reason: str, filter_value: str) -> bool:
    normalized = str(filter_value or "").strip().lower()
    if normalized in {"", "all"}:
        return True
    return reason == normalized


def format_internal_status(status: str) -> str:
    status_map = {
        "pending": "待处理",
        "translated": "已翻译",
        "edited": "已编辑",
        "completed": "已完成",
        "failed": "失败",
        "generating": "生成中",
    }
    return status_map.get(status, status)


def format_voice_label(voice_id: str | None) -> str:
    normalized = (voice_id or "").strip()
    if not normalized:
        return "未指定"
    for voice in all_tts_voices():
        if voice.voice_id == normalized:
            return voice.label
    if normalized.startswith("system:"):
        return normalized.split(":", 1)[1].strip() or normalized
    return normalized


def _segment_overlap_ms(start_ms: int, end_ms: int, suggestions: list[object]) -> int:
    accepted_ranges = [
        (max(0, int(getattr(suggestion, "start_ms", 0))), max(0, int(getattr(suggestion, "end_ms", 0))))
        for suggestion in suggestions
        if bool(getattr(suggestion, "accepted", False))
    ]
    accepted_ranges = [(start, end) for start, end in accepted_ranges if end > start]
    if not accepted_ranges:
        return 0

    accepted_ranges.sort(key=lambda item: (item[0], item[1]))
    merged: list[tuple[int, int]] = []
    for range_start, range_end in accepted_ranges:
        if not merged or range_start > merged[-1][1]:
            merged.append((range_start, range_end))
        else:
            previous_start, previous_end = merged[-1]
            merged[-1] = (previous_start, max(previous_end, range_end))

    overlap_ms = 0
    for range_start, range_end in merged:
        overlap_start = max(start_ms, range_start)
        overlap_end = min(end_ms, range_end)
        if overlap_end > overlap_start:
            overlap_ms += overlap_end - overlap_start
    return overlap_ms


class AudioTrackWidget(QWidget):
    seek_requested = Signal(int)

    def __init__(self) -> None:
        super().__init__()
        self._duration_ms = 0
        self._position_ms = 0
        self._waveform_points: list[float] = []
        self._focus_range: tuple[int, int] | None = None
        self._suggestions: list[tuple[int, int, bool]] = []
        self.setMinimumHeight(110)

    def sizeHint(self) -> QSize:
        return QSize(640, 110)

    def clear(self) -> None:
        self._duration_ms = 0
        self._position_ms = 0
        self._waveform_points = []
        self._focus_range = None
        self._suggestions = []
        self.update()

    def set_waveform_data(self, duration_ms: int, waveform_points: list[float]) -> None:
        self._duration_ms = max(0, int(duration_ms))
        self._waveform_points = list(waveform_points)
        self.update()

    def set_playhead(self, position_ms: int) -> None:
        self._position_ms = max(0, int(position_ms))
        self.update()

    def set_focus_range(self, start_ms: int | None, end_ms: int | None) -> None:
        if start_ms is None or end_ms is None or end_ms <= start_ms:
            self._focus_range = None
        else:
            self._focus_range = (max(0, int(start_ms)), max(0, int(end_ms)))
        self.update()

    def set_suggestions(self, suggestions: list[object]) -> None:
        self._suggestions = [
            (
                max(0, int(getattr(item, "start_ms", 0))),
                max(0, int(getattr(item, "end_ms", 0))),
                bool(getattr(item, "accepted", False)),
            )
            for item in suggestions
            if int(getattr(item, "end_ms", 0)) > int(getattr(item, "start_ms", 0))
        ]
        self.update()

    def mousePressEvent(self, event: object) -> None:
        if self._duration_ms <= 0:
            return
        x_pos = getattr(event, "position", lambda: None)()
        if x_pos is None:
            return
        ratio = min(1.0, max(0.0, float(x_pos.x()) / max(1.0, float(self.width()))))
        self.seek_requested.emit(int(self._duration_ms * ratio))

    def paintEvent(self, _event: object) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.fillRect(self.rect(), QColor("#0f141a"))

        if self._duration_ms <= 0:
            painter.setPen(QColor("#8b9bb0"))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "导入视频后，这里会显示音轨和时间轴。")
            return

        bounds = self.rect().adjusted(12, 10, -12, -10)
        if bounds.width() <= 0 or bounds.height() <= 0:
            return

        def _time_to_x(value_ms: int) -> float:
            ratio = min(1.0, max(0.0, value_ms / max(1, self._duration_ms)))
            return bounds.left() + bounds.width() * ratio

        painter.setPen(Qt.PenStyle.NoPen)
        for start_ms, end_ms, accepted in self._suggestions:
            rect = QRectF(
                _time_to_x(start_ms),
                bounds.top(),
                max(2.0, _time_to_x(end_ms) - _time_to_x(start_ms)),
                bounds.height(),
            )
            painter.fillRect(rect, QColor(190, 65, 72, 88 if accepted else 40))

        if self._focus_range is not None:
            start_ms, end_ms = self._focus_range
            focus_rect = QRectF(
                _time_to_x(start_ms),
                bounds.top(),
                max(2.0, _time_to_x(end_ms) - _time_to_x(start_ms)),
                bounds.height(),
            )
            painter.fillRect(focus_rect, QColor(45, 110, 184, 84))
            painter.setPen(QPen(QColor("#60a5fa"), 1.5))
            painter.drawRect(focus_rect)

        mid_y = bounds.center().y()
        painter.setPen(QPen(QColor("#334155"), 1))
        painter.drawLine(bounds.left(), mid_y, bounds.right(), mid_y)

        points = self._waveform_points or [0.0]
        painter.setPen(QPen(QColor("#20c997"), 1))
        point_count = max(1, len(points))
        for point_index, point in enumerate(points):
            x_pos = bounds.left() + (bounds.width() * point_index / max(1, point_count - 1))
            amplitude = max(2.0, point * (bounds.height() / 2.0))
            painter.drawLine(
                x_pos,
                mid_y - amplitude,
                x_pos,
                mid_y + amplitude,
            )

        playhead_x = _time_to_x(self._position_ms)
        painter.setPen(QPen(QColor("#7dd3fc"), 2))
        painter.drawLine(playhead_x, bounds.top(), playhead_x, bounds.bottom())


class HomePage(QWidget):
    def __init__(self) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setSpacing(16)

        title = QLabel("VCut Studio")
        title.setStyleSheet("font-size: 28px; font-weight: 700;")
        subtitle = QLabel("Windows 桌面端视频工具，支持两条独立任务：删除停顿/语气词，以及中文视频翻译/英文配音。")
        subtitle.setWordWrap(True)

        self.project_summary = QLabel()
        self.project_summary.setWordWrap(True)
        self.workflow_summary = QLabel(
            "建议流程：先识别字幕/分段，再根据需要单独走“删除建议剪辑”或“翻译/配音”路线。"
        )
        self.workflow_summary.setWordWrap(True)

        button_row = QHBoxLayout()
        self.new_project_button = QPushButton("新建项目")
        self.import_video_button = QPushButton("导入视频")
        self.open_project_button = QPushButton("打开项目")
        self.save_project_button = QPushButton("项目另存为")
        self.scan_dependencies_button = QPushButton("扫描依赖")
        button_row.addWidget(self.new_project_button)
        button_row.addWidget(self.import_video_button)
        button_row.addWidget(self.open_project_button)
        button_row.addWidget(self.save_project_button)
        button_row.addStretch(1)
        button_row.addWidget(self.scan_dependencies_button)

        dependency_group = QGroupBox("依赖状态")
        dependency_layout = QVBoxLayout(dependency_group)
        self.dependency_table = QTableWidget(0, 3)
        self.dependency_table.setHorizontalHeaderLabels(["组件", "状态", "详情"])
        self.dependency_table.verticalHeader().setVisible(False)
        self.dependency_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.dependency_table.setSelectionMode(QAbstractItemView.NoSelection)
        configure_resizable_columns(self.dependency_table, [160, 120, 520])
        dependency_layout.addWidget(self.dependency_table)

        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addLayout(button_row)
        layout.addWidget(self.project_summary)
        layout.addWidget(self.workflow_summary)
        layout.addWidget(dependency_group, stretch=1)

    def set_project_info(self, project: Project, project_path: Path | None) -> None:
        location = str(project_path) if project_path else "尚未保存"
        summary = [
            f"项目：{project.name}",
            f"保存位置：{location}",
            f"源视频：{project.video_path or '无'}",
            f"媒体信息：{project.media_info.width}x{project.media_info.height} | {project.media_info.fps:.2f} fps | 音频{'有' if project.media_info.has_audio else '无'}",
            f"分段数：{len(project.segments)}",
            f"建议剪辑数：{len(project.cut_suggestions)}",
            f"导出记录：{len(project.exports)}",
        ]
        self.project_summary.setText("\n".join(summary))

    def set_dependency_rows(self, rows: list[tuple[str, str, str]]) -> None:
        self.dependency_table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            for column_index, value in enumerate(row):
                self.dependency_table.setItem(row_index, column_index, QTableWidgetItem(value))


class TimelinePage(QWidget):
    suggestion_acceptance_changed = Signal(int, bool)
    suggestion_selection_changed = Signal(list)
    suggestion_range_apply_requested = Signal(int, int, int)
    segment_selection_changed = Signal(list)
    seek_requested = Signal(int)
    play_requested = Signal()
    pause_requested = Signal()
    stop_requested = Signal()
    preview_volume_changed = Signal(int)
    preview_mute_toggled = Signal(bool)
    generate_suggestions_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._loading_suggestions = False
        self._loading_segments = False
        self._preview_available = False
        self._translation_panel_attached = False
        self._dubbing_panel_attached = False
        self._tab_index_by_name: dict[str, int] = {}
        self._current_preview_ms = 0
        self._selected_suggestion_row: int | None = None
        self._visible_suggestion_rows: list[int] = []
        self._loaded_project: Project | None = None
        self._preview_muted = False

        root = QVBoxLayout(self)
        root.setSpacing(12)

        self.info_label = QLabel("识别字幕用于翻译；删除建议用于剪辑，这两条任务可以单独使用。")
        self.info_label.setWordWrap(True)
        root.addWidget(self.info_label)

        self.analysis_progress_bar = QProgressBar()
        self.analysis_progress_bar.setRange(0, 100)
        self.analysis_progress_bar.setValue(0)
        self.analysis_progress_bar.setFormat("等待开始")
        self.analysis_progress_bar.setVisible(False)
        root.addWidget(self.analysis_progress_bar)

        preview_container = QGroupBox("视频监看与音轨")
        preview_layout = QVBoxLayout(preview_container)
        self.video_widget = QVideoWidget()
        self.video_widget.setMinimumHeight(360)
        preview_layout.addWidget(self.video_widget, stretch=1)

        preview_buttons = QHBoxLayout()
        self.play_button = QPushButton("播放")
        self.pause_button = QPushButton("暂停画面")
        self.stop_preview_button = QPushButton("停止")
        self.mute_preview_button = QPushButton("静音")
        self.mute_preview_button.setCheckable(True)
        self.preview_volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.preview_volume_slider.setRange(0, 100)
        self.preview_volume_slider.setValue(100)
        self.preview_volume_slider.setFixedWidth(150)
        self.preview_volume_value_label = QLabel("100%")
        preview_buttons.addWidget(self.play_button)
        preview_buttons.addWidget(self.pause_button)
        preview_buttons.addWidget(self.stop_preview_button)
        preview_buttons.addSpacing(8)
        preview_buttons.addWidget(QLabel("音量"))
        preview_buttons.addWidget(self.preview_volume_slider)
        preview_buttons.addWidget(self.preview_volume_value_label)
        preview_buttons.addWidget(self.mute_preview_button)
        preview_buttons.addStretch(1)
        preview_layout.addLayout(preview_buttons)

        timeline_row = QHBoxLayout()
        self.current_time_label = QLabel("00:00:00.000")
        self.position_slider = QSlider(Qt.Orientation.Horizontal)
        self.position_slider.setRange(0, 0)
        self.total_time_label = QLabel("00:00:00.000")
        timeline_row.addWidget(self.current_time_label)
        timeline_row.addWidget(self.position_slider, stretch=1)
        timeline_row.addWidget(self.total_time_label)
        preview_layout.addLayout(timeline_row)

        preview_layout.addWidget(QLabel("音轨 / 时间轴"))
        self.audio_track_widget = AudioTrackWidget()
        preview_layout.addWidget(self.audio_track_widget)
        root.addWidget(preview_container, stretch=3)

        self.play_button.clicked.connect(self.play_requested.emit)
        self.pause_button.clicked.connect(self.pause_requested.emit)
        self.stop_preview_button.clicked.connect(self.stop_requested.emit)
        self.preview_volume_slider.valueChanged.connect(self.preview_volume_changed.emit)
        self.mute_preview_button.toggled.connect(self.preview_mute_toggled.emit)
        self.position_slider.sliderMoved.connect(self.seek_requested.emit)
        self.audio_track_widget.seek_requested.connect(self.seek_requested.emit)

        recognition_panel = QWidget()
        recognition_layout = QVBoxLayout(recognition_panel)
        recognition_layout.setContentsMargins(0, 0, 0, 0)
        button_row = QHBoxLayout()
        self.analyze_button = QPushButton("识别字幕 / 分段")
        button_row.addWidget(self.analyze_button)
        button_row.addStretch(1)

        segments_group = QGroupBox("识别分段（翻译路线）")
        segments_layout = QVBoxLayout(segments_group)
        self.segments_table = QTableWidget(0, 6)
        self.segments_table.setHorizontalHeaderLabels(
            ["状态", "开始", "结束", "中文", "英文", "配音状态"]
        )
        self.segments_table.verticalHeader().setVisible(False)
        self.segments_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.segments_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.segments_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        configure_resizable_columns(self.segments_table, [100, 120, 120, 420, 420, 140])
        self.segments_table.itemSelectionChanged.connect(self._on_segment_selection_changed)
        segments_layout.addWidget(self.segments_table)

        recognition_layout.addLayout(button_row)
        recognition_layout.addWidget(segments_group, stretch=1)

        suggestion_panel = QWidget()
        suggestion_layout = QVBoxLayout(suggestion_panel)
        suggestion_layout.setContentsMargins(0, 0, 0, 0)
        suggestion_top_row = QHBoxLayout()
        self.generate_suggestions_button = QPushButton("生成删除建议")
        suggestion_top_row.addWidget(self.generate_suggestions_button)
        suggestion_top_row.addWidget(QLabel("类型筛选"))
        self.suggestion_type_filter_combo = QComboBox()
        self.suggestion_type_filter_combo.addItem("全部类型", "all")
        self.suggestion_type_filter_combo.addItem("仅停顿", "long_pause")
        self.suggestion_type_filter_combo.addItem("仅无声/无人声", "silent_range")
        self.suggestion_type_filter_combo.addItem("仅口癖", "filler_word")
        suggestion_top_row.addWidget(self.suggestion_type_filter_combo)
        suggestion_top_row.addStretch(1)

        suggestions_group = QGroupBox("建议删除（剪辑路线）")
        suggestions_layout = QVBoxLayout(suggestions_group)
        suggestion_action_row = QHBoxLayout()
        self.accept_selected_button = QPushButton("勾选所选建议")
        self.clear_selected_button = QPushButton("取消所选勾选")
        self.accept_all_button = QPushButton("全部勾选")
        self.reject_all_button = QPushButton("全部取消")
        suggestion_action_row.addWidget(self.accept_selected_button)
        suggestion_action_row.addWidget(self.clear_selected_button)
        suggestion_action_row.addWidget(self.accept_all_button)
        suggestion_action_row.addWidget(self.reject_all_button)
        suggestion_action_row.addWidget(QLabel("范围时间"))
        self.suggestion_start_time_edit = QTimeEdit()
        self.suggestion_start_time_edit.setDisplayFormat("HH:mm:ss.zzz")
        self.suggestion_end_time_edit = QTimeEdit()
        self.suggestion_end_time_edit.setDisplayFormat("HH:mm:ss.zzz")
        self.use_current_as_start_button = QPushButton("当前时间设为开始")
        self.use_current_as_end_button = QPushButton("当前时间设为结束")
        self.apply_suggestion_range_button = QPushButton("应用范围")
        suggestion_action_row.addWidget(self.suggestion_start_time_edit)
        suggestion_action_row.addWidget(self.suggestion_end_time_edit)
        suggestion_action_row.addWidget(self.use_current_as_start_button)
        suggestion_action_row.addWidget(self.use_current_as_end_button)
        suggestion_action_row.addWidget(self.apply_suggestion_range_button)
        suggestion_action_row.addStretch(1)
        suggestions_layout.addLayout(suggestion_action_row)

        self.suggestions_table = QTableWidget(0, 5)
        self.suggestions_table.setHorizontalHeaderLabels(["勾选", "类型", "范围", "说明", "评分"])
        self.suggestions_table.verticalHeader().setVisible(False)
        self.suggestions_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.suggestions_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.suggestions_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        configure_resizable_columns(self.suggestions_table, [80, 130, 260, 360, 100])
        self.suggestions_table.itemChanged.connect(self._on_suggestion_item_changed)
        self.suggestions_table.itemSelectionChanged.connect(self._on_suggestion_selection_changed)
        suggestions_layout.addWidget(self.suggestions_table)

        self.generate_suggestions_button.clicked.connect(self.generate_suggestions_requested.emit)
        self.suggestion_type_filter_combo.currentIndexChanged.connect(self._reload_current_project)
        self.use_current_as_start_button.clicked.connect(self._use_current_preview_as_suggestion_start)
        self.use_current_as_end_button.clicked.connect(self._use_current_preview_as_suggestion_end)
        self.apply_suggestion_range_button.clicked.connect(self._emit_suggestion_range_apply_requested)

        suggestion_layout.addLayout(suggestion_top_row)
        suggestion_layout.addWidget(suggestions_group, stretch=1)
        self.workspace_tabs = QTabWidget()
        self.workspace_tabs.setDocumentMode(True)
        self.workspace_tabs.setTabPosition(QTabWidget.TabPosition.North)
        self.workspace_tabs.addTab(recognition_panel, "识别")
        self._tab_index_by_name["识别"] = 0
        self.workspace_tabs.addTab(suggestion_panel, "建议删除")
        self._tab_index_by_name["建议删除"] = 1
        root.addWidget(self.workspace_tabs, stretch=4)

    def attach_workspace_panels(
        self,
        translation_page: QWidget,
        dubbing_page: QWidget,
    ) -> None:
        if not self._translation_panel_attached:
            tab_index = self.workspace_tabs.addTab(translation_page, "翻译")
            self._tab_index_by_name["翻译"] = tab_index
            self._translation_panel_attached = True
        if not self._dubbing_panel_attached:
            tab_index = self.workspace_tabs.addTab(dubbing_page, "配音")
            self._tab_index_by_name["配音"] = tab_index
            self._dubbing_panel_attached = True

    def show_workspace_tab(self, title: str) -> None:
        tab_index = self._tab_index_by_name.get(title)
        if tab_index is not None:
            self.workspace_tabs.setCurrentIndex(tab_index)

    def load_project(self, project: Project) -> None:
        self._loaded_project = project
        if project.video_path:
            if project.segments:
                self.info_label.setText(
                    f"项目“{project.name}”已完成字幕识别，共 {len(project.segments)} 个分段。"
                    f" 你可以直接去翻译，也可以单独生成 {len(project.cut_suggestions)} 条删除建议。"
                )
            elif project.analysis_completed:
                self.info_label.setText(
                    f"项目“{project.name}”已完成识别，但没有检测到可用语音分段。"
                    " 现在可以直接生成无声/无人声的删除建议。"
                )
            else:
                self.info_label.setText(
                    f"项目“{project.name}”已导入。先做“识别字幕 / 分段”，之后可单独走翻译或剪辑路线。"
                )
        else:
            self.info_label.setText("请先导入源视频，再开始识别字幕、翻译或生成删除建议。")

        selected_segment_rows = set(self.selected_segment_rows())
        self._loading_segments = True
        self.segments_table.setRowCount(len(project.segments))
        for row_index, segment in enumerate(project.segments):
            overlap_ms = _segment_overlap_ms(segment.start_ms, segment.end_ms, project.cut_suggestions)
            if overlap_ms >= segment.duration_ms and segment.duration_ms > 0:
                state = "已删除"
            elif overlap_ms > 0:
                state = f"已裁剪 {overlap_ms}ms"
            else:
                state = "保留"
            values = [
                state,
                format_timestamp(segment.start_ms),
                format_timestamp(segment.end_ms),
                segment.zh_text,
                segment.en_text,
                format_internal_status(segment.tts_status),
            ]
            for column_index, value in enumerate(values):
                item = QTableWidgetItem(value)
                if overlap_ms >= segment.duration_ms and segment.duration_ms > 0:
                    item.setBackground(SEGMENT_REMOVED_COLOR)
                elif overlap_ms > 0:
                    item.setBackground(SEGMENT_TRIMMED_COLOR)
                self.segments_table.setItem(row_index, column_index, item)
        self._restore_row_selection(self.segments_table, selected_segment_rows)
        self._loading_segments = False

        selected_suggestion_rows = set(self.selected_suggestion_rows())
        self._loading_suggestions = True
        self._visible_suggestion_rows = [
            row_index
            for row_index, suggestion in enumerate(project.cut_suggestions)
            if suggestion_filter_matches(
                suggestion.reason,
                str(self.suggestion_type_filter_combo.currentData() or "all"),
            )
        ]
        self.suggestions_table.setRowCount(len(self._visible_suggestion_rows))
        for table_row, suggestion_index in enumerate(self._visible_suggestion_rows):
            suggestion = project.cut_suggestions[suggestion_index]
            check_item = QTableWidgetItem()
            check_item.setFlags(
                Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsSelectable
                | Qt.ItemFlag.ItemIsUserCheckable
            )
            check_item.setCheckState(
                Qt.CheckState.Checked if suggestion.accepted else Qt.CheckState.Unchecked
            )
            self.suggestions_table.setItem(table_row, 0, check_item)

            values = [
                format_suggestion_type(suggestion.reason),
                f"{format_timestamp(suggestion.start_ms)} -> {format_timestamp(suggestion.end_ms)}",
                format_suggestion_reason(suggestion.reason, suggestion.details),
                f"{suggestion.score:.2f}",
            ]
            for column_index, value in enumerate(values, start=1):
                item = QTableWidgetItem(value)
                if suggestion.accepted:
                    item.setBackground(SUGGESTION_ACCEPTED_COLOR)
                self.suggestions_table.setItem(table_row, column_index, item)
        visible_row_lookup = {
            suggestion_index: table_row for table_row, suggestion_index in enumerate(self._visible_suggestion_rows)
        }
        self._restore_row_selection(
            self.suggestions_table,
            {
                visible_row_lookup[suggestion_index]
                for suggestion_index in selected_suggestion_rows
                if suggestion_index in visible_row_lookup
            },
        )
        self._loading_suggestions = False
        visible_selected_rows = self.selected_suggestion_rows()
        if visible_selected_rows:
            selected_row = min(visible_selected_rows)
            if 0 <= selected_row < len(project.cut_suggestions):
                suggestion = project.cut_suggestions[selected_row]
                self.set_selected_suggestion_range(
                    selected_row,
                    suggestion.start_ms,
                    suggestion.end_ms,
                )
        else:
            self.clear_selected_suggestion_range()

        self.audio_track_widget.set_suggestions(project.cut_suggestions)

    def _restore_row_selection(self, table: QTableWidget, selected_rows: set[int]) -> None:
        selection_model = table.selectionModel()
        if selection_model is None:
            return
        selection_model.clearSelection()
        flags = (
            QItemSelectionModel.SelectionFlag.Select
            | QItemSelectionModel.SelectionFlag.Rows
        )
        for row_index in sorted(selected_rows):
            if 0 <= row_index < table.rowCount():
                selection_model.select(table.model().index(row_index, 0), flags)

    def selected_segment_rows(self) -> list[int]:
        selection_model = self.segments_table.selectionModel()
        if selection_model is None:
            return []
        return sorted(index.row() for index in selection_model.selectedRows())

    def selected_suggestion_rows(self) -> list[int]:
        selection_model = self.suggestions_table.selectionModel()
        if selection_model is None:
            return []
        rows: list[int] = []
        for index in selection_model.selectedRows():
            table_row = index.row()
            if 0 <= table_row < len(self._visible_suggestion_rows):
                rows.append(self._visible_suggestion_rows[table_row])
        return sorted(rows)

    def set_analysis_running(self, running: bool) -> None:
        self.analyze_button.setEnabled(not running)
        self.generate_suggestions_button.setEnabled(not running)
        self.suggestion_type_filter_combo.setEnabled(not running)
        self.accept_selected_button.setEnabled(not running)
        self.clear_selected_button.setEnabled(not running)
        self.accept_all_button.setEnabled(not running)
        self.reject_all_button.setEnabled(not running)
        self.suggestion_start_time_edit.setEnabled(not running and self._selected_suggestion_row is not None)
        self.suggestion_end_time_edit.setEnabled(not running and self._selected_suggestion_row is not None)
        self.use_current_as_start_button.setEnabled(not running and self._selected_suggestion_row is not None)
        self.use_current_as_end_button.setEnabled(not running and self._selected_suggestion_row is not None)
        self.apply_suggestion_range_button.setEnabled(not running and self._selected_suggestion_row is not None)
        self.analyze_button.setText("识别中..." if running else "识别字幕 / 分段")
        self.analysis_progress_bar.setVisible(running)
        if running:
            self.analysis_progress_bar.setValue(max(0, self.analysis_progress_bar.value()))
        else:
            self.analysis_progress_bar.setValue(0)
            self.analysis_progress_bar.setFormat("等待开始")

    def set_analysis_progress(self, progress: int, message: str) -> None:
        clamped = max(0, min(int(progress), 100))
        self.analysis_progress_bar.setVisible(True)
        self.analysis_progress_bar.setValue(clamped)
        self.analysis_progress_bar.setFormat(f"{clamped}% | {message}")

    def set_preview_enabled(self, enabled: bool) -> None:
        self._preview_available = enabled
        self.play_button.setEnabled(enabled)
        self.pause_button.setEnabled(enabled)
        self.stop_preview_button.setEnabled(enabled)
        self.mute_preview_button.setEnabled(enabled)
        self.preview_volume_slider.setEnabled(enabled)
        self.position_slider.setEnabled(enabled)
        self.audio_track_widget.setEnabled(enabled)

    def set_preview_position(self, position_ms: int, duration_ms: int) -> None:
        self._current_preview_ms = max(0, int(position_ms))
        self.position_slider.blockSignals(True)
        self.position_slider.setRange(0, max(0, int(duration_ms)))
        self.position_slider.setValue(max(0, int(position_ms)))
        self.position_slider.blockSignals(False)
        self.current_time_label.setText(format_timestamp(position_ms))
        self.total_time_label.setText(format_timestamp(duration_ms))
        self.audio_track_widget.set_playhead(position_ms)

    def set_preview_playing(self, playing: bool) -> None:
        self.play_button.setEnabled(self._preview_available and not playing)
        self.pause_button.setEnabled(self._preview_available)
        self.pause_button.setText("暂停画面" if playing else "继续预览")

    def set_preview_audio_state(self, volume_percent: int, muted: bool) -> None:
        clamped = max(0, min(int(volume_percent), 100))
        self._preview_muted = bool(muted)
        self.preview_volume_slider.blockSignals(True)
        self.preview_volume_slider.setValue(clamped)
        self.preview_volume_slider.blockSignals(False)
        self.preview_volume_value_label.setText(f"{clamped}%")
        self.mute_preview_button.blockSignals(True)
        self.mute_preview_button.setChecked(self._preview_muted)
        self.mute_preview_button.setText("取消静音" if self._preview_muted else "静音")
        self.mute_preview_button.blockSignals(False)

    def set_waveform_data(self, duration_ms: int, waveform_points: list[float]) -> None:
        self.audio_track_widget.set_waveform_data(duration_ms, waveform_points)

    def set_focus_range(self, start_ms: int | None, end_ms: int | None) -> None:
        self.audio_track_widget.set_focus_range(start_ms, end_ms)

    def set_selected_suggestion_range(self, row: int, start_ms: int, end_ms: int) -> None:
        self._selected_suggestion_row = row
        self.suggestion_start_time_edit.setTime(timestamp_to_qtime(start_ms))
        self.suggestion_end_time_edit.setTime(timestamp_to_qtime(end_ms))
        self.suggestion_start_time_edit.setEnabled(True)
        self.suggestion_end_time_edit.setEnabled(True)
        self.use_current_as_start_button.setEnabled(True)
        self.use_current_as_end_button.setEnabled(True)
        self.apply_suggestion_range_button.setEnabled(True)

    def clear_selected_suggestion_range(self) -> None:
        self._selected_suggestion_row = None
        self.suggestion_start_time_edit.setTime(timestamp_to_qtime(0))
        self.suggestion_end_time_edit.setTime(timestamp_to_qtime(0))
        self.suggestion_start_time_edit.setEnabled(False)
        self.suggestion_end_time_edit.setEnabled(False)
        self.use_current_as_start_button.setEnabled(False)
        self.use_current_as_end_button.setEnabled(False)
        self.apply_suggestion_range_button.setEnabled(False)

    def _on_suggestion_item_changed(self, item: QTableWidgetItem) -> None:
        if self._loading_suggestions or item.column() != 0:
            return
        table_row = item.row()
        if not (0 <= table_row < len(self._visible_suggestion_rows)):
            return
        suggestion_row = self._visible_suggestion_rows[table_row]
        self.suggestion_acceptance_changed.emit(
            suggestion_row,
            item.checkState() == Qt.CheckState.Checked,
        )

    def _on_suggestion_selection_changed(self) -> None:
        if self._loading_suggestions:
            return
        if not self.selected_suggestion_rows():
            self.clear_selected_suggestion_range()
        self.suggestion_selection_changed.emit(self.selected_suggestion_rows())

    def _on_segment_selection_changed(self) -> None:
        if not self._loading_segments:
            self.segment_selection_changed.emit(self.selected_segment_rows())

    def _use_current_preview_as_suggestion_start(self) -> None:
        self.suggestion_start_time_edit.setTime(timestamp_to_qtime(self._current_preview_ms))

    def _use_current_preview_as_suggestion_end(self) -> None:
        self.suggestion_end_time_edit.setTime(timestamp_to_qtime(self._current_preview_ms))

    def _emit_suggestion_range_apply_requested(self) -> None:
        if self._selected_suggestion_row is None:
            return
        self.suggestion_range_apply_requested.emit(
            self._selected_suggestion_row,
            qtime_to_timestamp(self.suggestion_start_time_edit.time()),
            qtime_to_timestamp(self.suggestion_end_time_edit.time()),
        )

    def _reload_current_project(self) -> None:
        if self._loaded_project is None:
            return
        self.load_project(self._loaded_project)


class TranslationPage(QWidget):
    chinese_text_edited = Signal(int, str)
    english_text_edited = Signal(int, str)
    selection_changed = Signal()
    import_source_subtitle_requested = Signal()
    import_translation_subtitle_requested = Signal()
    export_subtitle_requested = Signal()
    save_requested = Signal()
    proofread_source_requested = Signal()
    undo_proofread_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(0, 0, 0, 0)
        self._loading_table = False
        self._proofread_undo_available = False

        self.provider_summary = QLabel()
        self.provider_summary.setWordWrap(True)
        layout.addWidget(self.provider_summary)

        button_row = QHBoxLayout()
        self.translate_all_button = QPushButton("翻译全部片段")
        self.translate_selected_button = QPushButton("重译所选片段")
        self.import_source_subtitle_button = QPushButton("导入中文字幕")
        self.import_translation_subtitle_button = QPushButton("导入英文字幕")
        self.export_subtitle_button = QPushButton("导出当前字幕")
        self.save_button = QPushButton("保存翻译结果")
        self.proofread_source_button = QPushButton("校正中文字幕")
        self.undo_proofread_button = QPushButton("恢复校正前")
        self.test_translation_button = QPushButton("测试翻译配置")
        button_row.addWidget(self.translate_all_button)
        button_row.addWidget(self.translate_selected_button)
        button_row.addWidget(self.proofread_source_button)
        button_row.addWidget(self.undo_proofread_button)
        button_row.addWidget(self.import_source_subtitle_button)
        button_row.addWidget(self.import_translation_subtitle_button)
        button_row.addWidget(self.export_subtitle_button)
        button_row.addWidget(self.save_button)
        button_row.addStretch(1)
        button_row.addWidget(self.test_translation_button)
        layout.addLayout(button_row)

        self.translation_progress_bar = QProgressBar()
        self.translation_progress_bar.setRange(0, 100)
        self.translation_progress_bar.setValue(0)
        self.translation_progress_bar.setFormat("等待开始")
        self.translation_progress_bar.setVisible(False)
        layout.addWidget(self.translation_progress_bar)

        self.translation_table = QTableWidget(0, 5)
        self.translation_table.setHorizontalHeaderLabels(["ID", "开始", "结束", "中文", "英文"])
        self.translation_table.verticalHeader().setVisible(False)
        self.translation_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.translation_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.translation_table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
            | QAbstractItemView.EditTrigger.SelectedClicked
        )
        configure_resizable_columns(self.translation_table, [140, 120, 120, 420, 420])
        self.translation_table.cellChanged.connect(self._on_cell_changed)
        self.translation_table.itemSelectionChanged.connect(self.selection_changed.emit)
        layout.addWidget(self.translation_table, stretch=1)

        self.import_source_subtitle_button.clicked.connect(self.import_source_subtitle_requested.emit)
        self.import_translation_subtitle_button.clicked.connect(self.import_translation_subtitle_requested.emit)
        self.export_subtitle_button.clicked.connect(self.export_subtitle_requested.emit)
        self.save_button.clicked.connect(self.save_requested.emit)
        self.proofread_source_button.clicked.connect(self.proofread_source_requested.emit)
        self.undo_proofread_button.clicked.connect(self.undo_proofread_requested.emit)

        self.status_label = QLabel(
            "这里可以直接编辑中文和英文列；支持导入 SRT/VTT 字幕。修改中文后会清空旧英文，方便重新翻译。"
            " 点击“翻译全部片段”时会自动跳过已有英文结果，并把已完成结果及时保存到项目。"
            " “校正中文字幕”在未选中行时会处理全部片段，选中行时只处理所选片段。"
        )
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

    def load_project(self, project: Project, settings: AppSettings) -> None:
        masked_key = "已配置" if settings.translation.api_key else "缺少 API Key"
        proxy_text = "已启用" if settings.translation.proxy_enabled else "未启用"
        self.provider_summary.setText(
            "提供方："
            f"{settings.translation.provider_type} | "
            f"模型：{settings.translation.model} | "
            f"Base URL: {settings.translation.base_url} | "
            f"密钥：{masked_key} | "
            f"代理：{proxy_text}"
        )
        self._loading_table = True
        self.translation_table.setRowCount(len(project.segments))
        for row_index, segment in enumerate(project.segments):
            values = [
                segment.segment_id[:8],
                format_timestamp(segment.start_ms),
                format_timestamp(segment.end_ms),
                segment.zh_text,
                segment.en_text,
            ]
            for column_index, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column_index not in {3, 4}:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.translation_table.setItem(row_index, column_index, item)
        self._loading_table = False

    def _on_cell_changed(self, row: int, column: int) -> None:
        if self._loading_table or column not in {3, 4}:
            return
        item = self.translation_table.item(row, column)
        if column == 3:
            self.chinese_text_edited.emit(row, item.text() if item else "")
        else:
            self.english_text_edited.emit(row, item.text() if item else "")

    def selected_rows(self) -> list[int]:
        selection_model = self.translation_table.selectionModel()
        if selection_model is None:
            return []
        return sorted(index.row() for index in selection_model.selectedRows())

    def set_translation_running(self, running: bool) -> None:
        self.translate_all_button.setEnabled(not running)
        self.translate_selected_button.setEnabled(not running)
        self.import_source_subtitle_button.setEnabled(not running)
        self.import_translation_subtitle_button.setEnabled(not running)
        self.export_subtitle_button.setEnabled(not running)
        self.save_button.setEnabled(not running)
        self.proofread_source_button.setEnabled(not running)
        self.undo_proofread_button.setEnabled(not running and self._proofread_undo_available)
        self.test_translation_button.setEnabled(not running)
        self.translate_all_button.setText("翻译中..." if running else "翻译全部片段")
        self.translation_progress_bar.setVisible(running)
        if not running:
            self.translation_progress_bar.setValue(0)
            self.translation_progress_bar.setFormat("等待开始")

    def clear_english_text(self, row: int) -> None:
        if not (0 <= row < self.translation_table.rowCount()):
            return
        self._loading_table = True
        item = self.translation_table.item(row, 4)
        if item is not None:
            item.setText("")
        self._loading_table = False

    def set_translation_progress(self, progress: int, message: str) -> None:
        clamped = max(0, min(int(progress), 100))
        self.translation_progress_bar.setVisible(True)
        self.translation_progress_bar.setValue(clamped)
        self.translation_progress_bar.setFormat(f"{clamped}% | {message}")

    def set_proofread_undo_available(self, available: bool) -> None:
        self._proofread_undo_available = bool(available)
        self.undo_proofread_button.setEnabled(self._proofread_undo_available)

    def update_row_texts(
        self,
        row: int,
        *,
        zh_text: str | None = None,
        en_text: str | None = None,
    ) -> None:
        if not (0 <= row < self.translation_table.rowCount()):
            return
        self._loading_table = True
        try:
            if zh_text is not None:
                zh_item = self.translation_table.item(row, 3)
                if zh_item is None:
                    zh_item = QTableWidgetItem(zh_text)
                    self.translation_table.setItem(row, 3, zh_item)
                else:
                    zh_item.setText(zh_text)
            if en_text is not None:
                en_item = self.translation_table.item(row, 4)
                if en_item is None:
                    en_item = QTableWidgetItem(en_text)
                    self.translation_table.setItem(row, 4, en_item)
                else:
                    en_item.setText(en_text)
        finally:
            self._loading_table = False


class DubbingPage(QWidget):
    selection_changed = Signal()
    check_state_changed = Signal(int, bool)
    play_generated_requested = Signal(int)

    def __init__(self) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(0, 0, 0, 0)
        self._loading_table = False

        top_row = QHBoxLayout()
        top_row.addWidget(QLabel("默认音色"))
        self.voice_combo = QComboBox()
        self._reload_voice_options("kokoro_local")
        top_row.addWidget(self.voice_combo)
        top_row.addWidget(QLabel("语速"))
        self.rate_spin = QDoubleSpinBox()
        self.rate_spin.setRange(0.5, 2.0)
        self.rate_spin.setSingleStep(0.05)
        top_row.addWidget(self.rate_spin)
        top_row.addStretch(1)
        self.apply_voice_button = QPushButton("应用音色到勾选/所选片段")
        self.generate_button = QPushButton("生成勾选/所选配音")
        self.preview_button = QPushButton("试听音色")
        self.stop_button = QPushButton("停止播放")
        top_row.addWidget(self.apply_voice_button)
        top_row.addWidget(self.generate_button)
        top_row.addWidget(self.preview_button)
        top_row.addWidget(self.stop_button)
        layout.addLayout(top_row)

        selection_row = QHBoxLayout()
        self.check_selected_button = QPushButton("勾选所选")
        self.uncheck_selected_button = QPushButton("取消所选勾选")
        self.check_all_button = QPushButton("全部勾选")
        self.uncheck_all_button = QPushButton("全部取消")
        selection_row.addWidget(self.check_selected_button)
        selection_row.addWidget(self.uncheck_selected_button)
        selection_row.addWidget(self.check_all_button)
        selection_row.addWidget(self.uncheck_all_button)
        selection_row.addStretch(1)
        layout.addLayout(selection_row)

        self.dubbing_progress_bar = QProgressBar()
        self.dubbing_progress_bar.setRange(0, 100)
        self.dubbing_progress_bar.setValue(0)
        self.dubbing_progress_bar.setFormat("等待开始")
        self.dubbing_progress_bar.setVisible(False)
        layout.addWidget(self.dubbing_progress_bar)

        self.summary_label = QLabel()
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)

        self.dubbing_table = QTableWidget(0, 7)
        self.dubbing_table.setHorizontalHeaderLabels(["勾选", "ID", "音色", "时长", "缓存状态", "英文", "配音音频"])
        self.dubbing_table.verticalHeader().setVisible(False)
        self.dubbing_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.dubbing_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.dubbing_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        configure_resizable_columns(self.dubbing_table, [80, 140, 200, 100, 140, 420, 360])
        self.dubbing_table.itemChanged.connect(self._on_item_changed)
        self.dubbing_table.itemSelectionChanged.connect(self._on_selection_changed)
        self.dubbing_table.cellDoubleClicked.connect(self._on_cell_double_clicked)
        layout.addWidget(self.dubbing_table, stretch=1)

    def load_project(self, project: Project, settings: AppSettings) -> None:
        selected_rows = set(self.selected_rows())
        self._reload_voice_options(settings.tts.provider_type, settings.tts.default_voice)
        self.rate_spin.setValue(settings.tts.rate)
        if str(settings.tts.provider_type or "").strip() == "kokoro_local":
            if kokoro_local_ready():
                voice_summary = "当前正在使用离线 Kokoro 英文音色，已内置 11 个音色，推荐温柔女声 Bella。"
            else:
                voice_summary = "已选择离线 Kokoro，但暂未检测到本地模型，请检查程序目录下的 models/tts。"
        else:
            english_voice_names = [
                voice.name for voice in installed_windows_voices() if voice.culture.lower().startswith("en")
            ]
            if len(english_voice_names) <= 1:
                voice_summary = (
                    "当前系统仅检测到 1 个英文系统音色，预设音色主要是语速/风格预设，实际音色会比较接近。"
                )
            else:
                voice_summary = f"当前系统检测到 {len(english_voice_names)} 个英文系统音色。"
            if kokoro_local_ready():
                voice_summary += " 已检测到离线 Kokoro 音色，可在设置里切换。"
        self.summary_label.setText(
            f"提供方：{tts_provider_display_name(settings.tts.provider_type)} | 采样率：{settings.tts.sample_rate} Hz | 重试次数：{settings.tts.retry_count} | "
            f"{build_dubbing_summary_text(project, settings)} | {voice_summary}"
        )
        self._loading_table = True
        self.dubbing_table.setRowCount(len(project.segments))
        for row_index, segment in enumerate(project.segments):
            check_item = QTableWidgetItem()
            check_item.setFlags(
                Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsSelectable
                | Qt.ItemFlag.ItemIsUserCheckable
            )
            check_item.setCheckState(
                Qt.CheckState.Checked if segment.dub_selected else Qt.CheckState.Unchecked
            )
            self.dubbing_table.setItem(row_index, 0, check_item)
            review_status = dub_review_status(segment, settings)
            values = [
                segment.segment_id[:8],
                format_voice_label(segment_target_voice_id(segment, settings)),
                f"{segment.duration_ms / 1000:.2f}s",
                dub_review_status_label(review_status),
                segment.en_text,
                segment.dub_audio_path or "",
            ]
            for column_index, value in enumerate(values, start=1):
                item = QTableWidgetItem(value)
                if column_index == 4:
                    if review_status == "reusable":
                        item.setBackground(DUB_REUSABLE_COLOR)
                    elif review_status == "stale":
                        item.setBackground(DUB_STALE_COLOR)
                    elif review_status == "missing_text":
                        item.setBackground(DUB_MISSING_COLOR)
                    elif review_status == "generating":
                        item.setBackground(DUB_GENERATING_COLOR)
                self.dubbing_table.setItem(row_index, column_index, item)

        selection_model = self.dubbing_table.selectionModel()
        if selection_model is not None:
            selection_model.clearSelection()
            flags = (
                QItemSelectionModel.SelectionFlag.Select
                | QItemSelectionModel.SelectionFlag.Rows
            )
            for row_index in sorted(selected_rows):
                if 0 <= row_index < len(project.segments):
                    selection_model.select(self.dubbing_table.model().index(row_index, 0), flags)
        self._loading_table = False
        self.selection_changed.emit()

    def selected_rows(self) -> list[int]:
        selection_model = self.dubbing_table.selectionModel()
        if selection_model is None:
            return []
        return sorted(index.row() for index in selection_model.selectedRows())

    def checked_rows(self) -> list[int]:
        checked_rows: list[int] = []
        for row_index in range(self.dubbing_table.rowCount()):
            item = self.dubbing_table.item(row_index, 0)
            if item is not None and item.checkState() == Qt.CheckState.Checked:
                checked_rows.append(row_index)
        return checked_rows

    def current_voice_id(self) -> str:
        return str(self.voice_combo.currentData() or "").strip()

    def _reload_voice_options(self, provider_type: str, selected_voice: str | None = None) -> None:
        options = available_tts_voices(provider_type)
        preferred_voice = (selected_voice or str(self.voice_combo.currentData() or "")).strip()
        self.voice_combo.blockSignals(True)
        self.voice_combo.clear()
        for voice in options:
            self.voice_combo.addItem(f"{voice.label} ({voice.accent} / {voice.style})", voice.voice_id)
        self.voice_combo.blockSignals(False)
        if preferred_voice:
            set_combo_value(self.voice_combo, preferred_voice)

    def set_dubbing_running(self, running: bool) -> None:
        self.voice_combo.setEnabled(not running)
        self.rate_spin.setEnabled(not running)
        self.apply_voice_button.setEnabled(not running)
        self.generate_button.setEnabled(not running)
        self.preview_button.setEnabled(not running)
        self.check_selected_button.setEnabled(not running)
        self.uncheck_selected_button.setEnabled(not running)
        self.check_all_button.setEnabled(not running)
        self.uncheck_all_button.setEnabled(not running)
        self.generate_button.setText("生成中..." if running else "生成勾选/所选配音")
        self.dubbing_progress_bar.setVisible(running)
        if not running:
            self.dubbing_progress_bar.setValue(0)
            self.dubbing_progress_bar.setFormat("等待开始")

    def _on_cell_double_clicked(self, row: int, _column: int) -> None:
        self.play_generated_requested.emit(row)

    def _on_selection_changed(self) -> None:
        if not self._loading_table:
            self.selection_changed.emit()

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if self._loading_table or item.column() != 0:
            return
        self.check_state_changed.emit(item.row(), item.checkState() == Qt.CheckState.Checked)

    def set_dubbing_progress(self, progress: int, message: str) -> None:
        clamped = max(0, min(int(progress), 100))
        self.dubbing_progress_bar.setVisible(True)
        self.dubbing_progress_bar.setValue(clamped)
        self.dubbing_progress_bar.setFormat(f"{clamped}% | {message}")


class ExportPage(QWidget):
    output_dir_browse_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(0, 0, 0, 0)

        options_group = QGroupBox("导出选项")
        options_form = QFormLayout(options_group)
        self.export_type_combo = QComboBox()
        self.export_type_combo.addItem("纯净中文版视频（按建议删除）", "clean_zh")
        self.export_type_combo.addItem("原视频 + 字幕（英文/双语）", "en_subtitle")
        self.export_type_combo.addItem("英文配音视频", "en_dub")
        self.export_type_combo.addItem("英文配音 + 字幕视频", "en_dub_subtitle")
        self.output_dir_edit = QLineEdit()
        self.output_dir_button = QPushButton("选择...")
        self.output_container_combo = QComboBox()
        self.output_container_combo.addItems(["mp4", "mov", "mkv"])
        self.subtitle_mode_combo = QComboBox()
        self.subtitle_mode_combo.addItem("英文字幕", "en")
        self.subtitle_mode_combo.addItem("中英双语", "bilingual")
        self.burn_subtitles_checkbox = QCheckBox("将字幕烧录进视频")
        self.sidecar_srt_checkbox = QCheckBox("同时导出 SRT 文件")
        options_form.addRow("输出类型", self.export_type_combo)
        options_form.addRow("输出目录", build_inline_row(self.output_dir_edit, self.output_dir_button))
        options_form.addRow("容器格式", self.output_container_combo)
        options_form.addRow("字幕模式", self.subtitle_mode_combo)
        options_form.addRow(self.burn_subtitles_checkbox)
        options_form.addRow(self.sidecar_srt_checkbox)

        self.export_progress_bar = QProgressBar()
        self.export_progress_bar.setRange(0, 100)
        self.export_progress_bar.setValue(0)
        self.export_progress_bar.setFormat("等待开始")
        self.export_progress_bar.setVisible(False)

        self.summary_label = QLabel()
        self.summary_label.setWordWrap(True)
        self.export_button = QPushButton("开始导出")

        layout.addWidget(options_group)
        layout.addWidget(self.export_progress_bar)
        layout.addWidget(self.summary_label)
        layout.addWidget(self.export_button, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addStretch(1)

        self.output_dir_button.clicked.connect(self.output_dir_browse_requested.emit)
        self.export_type_combo.currentIndexChanged.connect(self._update_subtitle_option_state)

    def load_project(self, project: Project, settings: AppSettings) -> None:
        set_combo_value(self.export_type_combo, settings.export.default_export_type)
        self.output_dir_edit.setText(settings.media.default_output_dir)
        set_combo_value(self.output_container_combo, settings.export.output_container)
        set_combo_value(self.subtitle_mode_combo, getattr(settings.export, "subtitle_mode", "en"))
        self.burn_subtitles_checkbox.setChecked(settings.export.burn_subtitles)
        self.sidecar_srt_checkbox.setChecked(settings.export.export_sidecar_srt)
        accepted_count = sum(1 for suggestion in project.cut_suggestions if suggestion.accepted)
        accepted_duration_ms = sum(
            max(0, suggestion.end_ms - suggestion.start_ms)
            for suggestion in project.cut_suggestions
            if suggestion.accepted
        )
        kept_duration_ms = max(0, project.source_duration_ms - accepted_duration_ms)
        self.summary_label.setText(
            f"项目“{project.name}”已准备好导出。"
            f" 源视频时长：{project.source_duration_ms / 1000:.2f}s。"
            f" 已接受的删除建议：{accepted_count} 条，预计删除 {accepted_duration_ms / 1000:.2f}s，保留 {kept_duration_ms / 1000:.2f}s。"
        )
        self._update_subtitle_option_state()

    def set_export_running(self, running: bool) -> None:
        self.export_type_combo.setEnabled(not running)
        self.output_dir_edit.setEnabled(not running)
        self.output_dir_button.setEnabled(not running)
        self.output_container_combo.setEnabled(not running)
        self.export_button.setEnabled(not running)
        self.export_button.setText("导出中..." if running else "开始导出")
        self.export_progress_bar.setVisible(running)
        if not running:
            self.export_progress_bar.setValue(0)
            self.export_progress_bar.setFormat("等待开始")
            self._update_subtitle_option_state()
        else:
            self.subtitle_mode_combo.setEnabled(False)
            self.burn_subtitles_checkbox.setEnabled(False)
            self.sidecar_srt_checkbox.setEnabled(False)

    def set_export_progress(self, progress: int, message: str) -> None:
        clamped = max(0, min(int(progress), 100))
        self.export_progress_bar.setVisible(True)
        self.export_progress_bar.setValue(clamped)
        self.export_progress_bar.setFormat(f"{clamped}% | {message}")

    def _subtitle_options_enabled(self) -> bool:
        export_type = str(self.export_type_combo.currentData() or "")
        return export_type in {"en_subtitle", "en_dub_subtitle"}

    def _update_subtitle_option_state(self) -> None:
        enabled = self._subtitle_options_enabled()
        self.subtitle_mode_combo.setEnabled(enabled)
        self.burn_subtitles_checkbox.setEnabled(enabled)
        self.sidecar_srt_checkbox.setEnabled(enabled)


class SettingsPage(QWidget):
    asr_runtime_check_requested = Signal()
    asr_cache_dir_browse_requested = Signal()
    asr_local_model_dir_browse_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(18, 18, 18, 18)
        outer_layout.setSpacing(14)

        header_card = QFrame()
        header_card.setObjectName("settingsHeaderCard")
        header_layout = QHBoxLayout(header_card)
        header_layout.setContentsMargins(20, 18, 20, 18)
        header_layout.setSpacing(14)
        title_layout = QVBoxLayout()
        title_layout.setSpacing(4)
        header_title = QLabel("设置")
        header_title.setObjectName("settingsHeaderTitle")
        header_text = QLabel("按分类整理工作区、转写、翻译、配音和导出参数，避免所有选项堆在同一页。")
        header_text.setObjectName("settingsHeaderText")
        header_text.setWordWrap(True)
        title_layout.addWidget(header_title)
        title_layout.addWidget(header_text)
        self.top_save_button = QPushButton("保存设置")
        self.top_reset_button = QPushButton("恢复默认")
        header_layout.addLayout(title_layout, stretch=1)
        header_layout.addWidget(self.top_reset_button)
        header_layout.addWidget(self.top_save_button)
        outer_layout.addWidget(header_card)

        body_layout = QHBoxLayout()
        body_layout.setSpacing(14)
        outer_layout.addLayout(body_layout, stretch=1)

        sidebar_frame = QFrame()
        sidebar_frame.setObjectName("settingsSidebar")
        sidebar_frame.setFixedWidth(220)
        sidebar_layout = QVBoxLayout(sidebar_frame)
        sidebar_layout.setContentsMargins(14, 16, 14, 16)
        sidebar_layout.setSpacing(10)
        nav_title = QLabel("分类")
        nav_title.setObjectName("settingsNavTitle")
        nav_hint = QLabel("像 VS Code 一样按主题浏览设置。")
        nav_hint.setObjectName("settingsNavHint")
        nav_hint.setWordWrap(True)
        # Keep navigation separate from the actual form widgets so we can
        # switch sections without duplicating the load/save wiring.
        self.settings_nav = QListWidget()
        self.settings_nav.setObjectName("settingsNav")
        self.settings_nav.setFrameShape(QFrame.Shape.NoFrame)
        self.settings_nav.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.settings_nav.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.settings_nav.setSpacing(4)
        self.settings_nav.setUniformItemSizes(True)
        sidebar_layout.addWidget(nav_title)
        sidebar_layout.addWidget(nav_hint)
        sidebar_layout.addWidget(self.settings_nav, stretch=1)
        body_layout.addWidget(sidebar_frame)

        content_frame = QFrame()
        content_frame.setObjectName("settingsContent")
        content_layout = QVBoxLayout(content_frame)
        content_layout.setContentsMargins(20, 20, 20, 20)
        content_layout.setSpacing(0)
        self.settings_stack = QStackedWidget()
        self.settings_stack.setObjectName("settingsStack")
        content_layout.addWidget(self.settings_stack)
        body_layout.addWidget(content_frame, stretch=1)

        self.workspace_dir_edit = QLineEdit()
        self.temp_dir_edit = QLineEdit()
        self.auto_save_minutes_spin = QSpinBox()
        self.auto_save_minutes_spin.setRange(1, 60)
        self.recent_projects_limit_spin = QSpinBox()
        self.recent_projects_limit_spin.setRange(1, 50)
        self.log_level_combo = QComboBox()
        self.log_level_combo.addItem("调试", "DEBUG")
        self.log_level_combo.addItem("信息", "INFO")
        self.log_level_combo.addItem("警告", "WARNING")
        self.log_level_combo.addItem("错误", "ERROR")
        workspace_group = self._build_form_group(
            "工作区",
            [
                ("工作区目录", self.workspace_dir_edit),
                ("临时目录", self.temp_dir_edit),
                ("自动保存间隔（分钟）", self.auto_save_minutes_spin),
                ("最近项目数", self.recent_projects_limit_spin),
                ("日志级别", self.log_level_combo),
            ],
        )

        self.ffmpeg_path_edit = QLineEdit()
        self.default_output_dir_edit = QLineEdit()
        self.overwrite_existing_checkbox = QCheckBox("覆盖已存在的输出文件")
        self.video_codec_edit = QLineEdit()
        self.audio_codec_edit = QLineEdit()
        self.quality_preset_combo = QComboBox()
        self.quality_preset_combo.addItem("更快", "faster")
        self.quality_preset_combo.addItem("中等", "medium")
        self.quality_preset_combo.addItem("更慢", "slower")
        media_group = self._build_form_group(
            "媒体",
            [
                ("ffmpeg 路径", self.ffmpeg_path_edit),
                ("默认输出目录", self.default_output_dir_edit),
                ("视频编码器", self.video_codec_edit),
                ("音频编码器", self.audio_codec_edit),
                ("质量预设", self.quality_preset_combo),
                ("覆盖输出", self.overwrite_existing_checkbox),
            ],
        )

        self.asr_model_button_group = QButtonGroup(self)
        self.asr_model_button_group.setExclusive(True)
        self.asr_small_radio = QRadioButton("small（轻量，推荐）")
        self.asr_medium_radio = QRadioButton("medium（更准，更慢）")
        self.asr_large_radio = QRadioButton("large-v3（更准，更重）")
        self.asr_custom_radio = QRadioButton("自定义")
        self.asr_custom_model_edit = QLineEdit()
        self.asr_custom_model_edit.setPlaceholderText("输入自定义模型名，例如 base / tiny / turbo")
        self.asr_model_button_group.addButton(self.asr_small_radio)
        self.asr_model_button_group.addButton(self.asr_medium_radio)
        self.asr_model_button_group.addButton(self.asr_large_radio)
        self.asr_model_button_group.addButton(self.asr_custom_radio)
        self._asr_model_radios = {
            "small": self.asr_small_radio,
            "medium": self.asr_medium_radio,
            "large-v3": self.asr_large_radio,
        }
        self.asr_model_note = QLabel(
            "说明：small 适合日常调试和 CPU；medium 更稳一些但更慢；large-v3 精度更高，但加载更慢、占用更大。"
        )
        self.asr_model_note.setWordWrap(True)
        self.asr_model_selector = QWidget()
        asr_model_layout = QVBoxLayout(self.asr_model_selector)
        asr_model_layout.setContentsMargins(0, 0, 0, 0)
        asr_model_layout.setSpacing(6)
        top_model_row = QHBoxLayout()
        top_model_row.setContentsMargins(0, 0, 0, 0)
        top_model_row.setSpacing(12)
        top_model_row.addWidget(self.asr_small_radio)
        top_model_row.addWidget(self.asr_medium_radio)
        top_model_row.addWidget(self.asr_large_radio)
        top_model_row.addStretch(1)
        custom_model_row = QHBoxLayout()
        custom_model_row.setContentsMargins(0, 0, 0, 0)
        custom_model_row.setSpacing(8)
        custom_model_row.addWidget(self.asr_custom_radio)
        custom_model_row.addWidget(self.asr_custom_model_edit, stretch=1)
        asr_model_layout.addLayout(top_model_row)
        asr_model_layout.addLayout(custom_model_row)
        asr_model_layout.addWidget(self.asr_model_note)
        self.asr_small_radio.setChecked(True)
        self.asr_small_radio.toggled.connect(self._sync_asr_model_editor_state)
        self.asr_medium_radio.toggled.connect(self._sync_asr_model_editor_state)
        self.asr_large_radio.toggled.connect(self._sync_asr_model_editor_state)
        self.asr_custom_radio.toggled.connect(self._sync_asr_model_editor_state)
        self._sync_asr_model_editor_state()
        self.asr_device_combo = QComboBox()
        self.asr_device_combo.addItem("CPU", "cpu")
        self.asr_device_combo.addItem("CUDA", "cuda")
        self.asr_device_combo.addItem("自动", "auto")
        self.vad_enabled_checkbox = QCheckBox("转写前启用 VAD")
        self.segment_max_seconds_spin = QSpinBox()
        self.segment_max_seconds_spin.setRange(5, 120)
        self.min_confidence_spin = QDoubleSpinBox()
        self.min_confidence_spin.setRange(0.0, 1.0)
        self.min_confidence_spin.setSingleStep(0.05)
        self.asr_cache_dir_edit = QLineEdit()
        self.asr_cache_dir_edit.setPlaceholderText("模型缓存目录；留空则使用默认 Hugging Face 缓存")
        self.asr_cache_dir_button = QPushButton("浏览...")
        self.asr_cache_dir_button.clicked.connect(self.asr_cache_dir_browse_requested.emit)
        self.asr_local_model_dir_edit = QLineEdit()
        self.asr_local_model_dir_edit.setPlaceholderText("本地模型目录；填了以后优先从这里加载")
        self.asr_local_model_dir_button = QPushButton("浏览...")
        self.asr_local_model_dir_button.clicked.connect(self.asr_local_model_dir_browse_requested.emit)
        self.asr_local_files_only_checkbox = QCheckBox("仅使用本地缓存/本地模型，不自动下载")
        self.asr_runtime_check_button = QPushButton("检测 GPU 环境")
        self.asr_runtime_check_button.clicked.connect(self.asr_runtime_check_requested.emit)
        asr_group = self._build_form_group(
            "ASR",
            [
                ("识别模型", self.asr_model_selector),
                ("运行设备", self.asr_device_combo),
                ("模型缓存目录", build_inline_row(self.asr_cache_dir_edit, self.asr_cache_dir_button)),
                ("本地模型目录", build_inline_row(self.asr_local_model_dir_edit, self.asr_local_model_dir_button)),
                ("仅本地加载", self.asr_local_files_only_checkbox),
                ("环境检测", self.asr_runtime_check_button),
                ("分段最长秒数", self.segment_max_seconds_spin),
                ("最低置信度", self.min_confidence_spin),
                ("VAD", self.vad_enabled_checkbox),
            ],
        )

        self.filler_words_edit = QPlainTextEdit()
        self.filler_words_edit.setPlaceholderText("每行一个，或使用逗号分隔")
        self.pause_threshold_spin = QSpinBox()
        self.pause_threshold_spin.setRange(100, 5000)
        self.cut_padding_spin = QSpinBox()
        self.cut_padding_spin.setRange(0, 1000)
        self.merge_gap_spin = QSpinBox()
        self.merge_gap_spin.setRange(0, 1000)
        self.cutting_mode_combo = QComboBox()
        self.cutting_mode_combo.addItem("保守", "conservative")
        self.cutting_mode_combo.addItem("标准", "standard")
        self.cutting_mode_combo.addItem("激进", "aggressive")
        self.preserve_intro_pause_checkbox = QCheckBox("保留句首停顿")
        cutting_group = self._build_form_group(
            "剪辑规则",
            [
                ("语气词列表", self.filler_words_edit),
                ("停顿阈值（ms）", self.pause_threshold_spin),
                ("剪辑留白（ms）", self.cut_padding_spin),
                ("合并间隔（ms）", self.merge_gap_spin),
                ("模式", self.cutting_mode_combo),
                ("句首停顿", self.preserve_intro_pause_checkbox),
            ],
        )

        self.translation_provider_combo = QComboBox()
        self.translation_provider_combo.addItem("OpenAI 兼容接口", "openai_compatible")
        self.translation_base_url_edit = QLineEdit()
        self.translation_api_key_edit = QLineEdit()
        self.translation_api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.translation_model_edit = QLineEdit()
        self.translation_system_prompt_edit = QPlainTextEdit()
        self.translation_timeout_spin = QSpinBox()
        self.translation_timeout_spin.setRange(5, 600)
        self.translation_retries_spin = QSpinBox()
        self.translation_retries_spin.setRange(0, 10)
        self.translation_concurrency_spin = QSpinBox()
        self.translation_concurrency_spin.setRange(1, 16)
        self.translation_batch_size_spin = QSpinBox()
        self.translation_batch_size_spin.setRange(1, 64)
        self.translation_proxy_enabled_checkbox = QCheckBox("翻译请求使用代理")
        self.translation_http_proxy_edit = QLineEdit()
        self.translation_http_proxy_edit.setPlaceholderText("例如：http://127.0.0.1:7890")
        self.translation_https_proxy_edit = QLineEdit()
        self.translation_https_proxy_edit.setPlaceholderText("例如：http://127.0.0.1:7890")
        self.translation_no_proxy_edit = QLineEdit()
        self.translation_no_proxy_edit.setPlaceholderText("多个地址用英文逗号分隔，例如：localhost,.corp.local")
        translation_group = self._build_form_group(
            "翻译模型",
            [
                ("提供方", self.translation_provider_combo),
                ("Base URL", self.translation_base_url_edit),
                ("API Key", self.translation_api_key_edit),
                ("模型", self.translation_model_edit),
                ("系统提示词", self.translation_system_prompt_edit),
                ("超时（秒）", self.translation_timeout_spin),
                ("重试次数", self.translation_retries_spin),
                ("并发数", self.translation_concurrency_spin),
                ("批大小", self.translation_batch_size_spin),
                ("启用代理", self.translation_proxy_enabled_checkbox),
                ("HTTP 代理", self.translation_http_proxy_edit),
                ("HTTPS 代理", self.translation_https_proxy_edit),
                ("不走代理", self.translation_no_proxy_edit),
            ],
        )

        self.tts_provider_combo = QComboBox()
        for label, provider_type in tts_provider_choices():
            self.tts_provider_combo.addItem(label, provider_type)
        self.tts_default_voice_combo = QComboBox()
        self.tts_rate_spin = QDoubleSpinBox()
        self.tts_rate_spin.setRange(0.5, 2.0)
        self.tts_rate_spin.setSingleStep(0.05)
        self.tts_volume_spin = QDoubleSpinBox()
        self.tts_volume_spin.setRange(0.0, 2.0)
        self.tts_volume_spin.setSingleStep(0.1)
        self.tts_sample_rate_spin = QSpinBox()
        self.tts_sample_rate_spin.setRange(8000, 48000)
        self.tts_retry_spin = QSpinBox()
        self.tts_retry_spin.setRange(0, 10)
        self.tts_preview_text_edit = QPlainTextEdit()
        self.tts_hint_label = QLabel()
        self.tts_hint_label.setWordWrap(True)
        self.tts_provider_combo.currentIndexChanged.connect(self._sync_tts_provider_state)
        tts_group = self._build_form_group(
            "TTS",
            [
                ("提供方", self.tts_provider_combo),
                ("默认音色", self.tts_default_voice_combo),
                ("说明", self.tts_hint_label),
                ("语速", self.tts_rate_spin),
                ("音量", self.tts_volume_spin),
                ("采样率", self.tts_sample_rate_spin),
                ("重试次数", self.tts_retry_spin),
                ("试听文本", self.tts_preview_text_edit),
            ],
        )

        self.export_default_type_combo = QComboBox()
        self.export_default_type_combo.addItem("纯净中文版视频", "clean_zh")
        self.export_default_type_combo.addItem("原视频 + 字幕（英文/双语）", "en_subtitle")
        self.export_default_type_combo.addItem("英文配音视频", "en_dub")
        self.export_default_type_combo.addItem("英文配音 + 字幕视频", "en_dub_subtitle")
        self.export_subtitle_mode_combo = QComboBox()
        self.export_subtitle_mode_combo.addItem("英文字幕", "en")
        self.export_subtitle_mode_combo.addItem("中英双语", "bilingual")
        self.export_burn_subtitles_checkbox = QCheckBox("默认烧录字幕")
        self.export_sidecar_srt_checkbox = QCheckBox("默认导出 SRT 文件")
        self.audio_lufs_target_spin = QSpinBox()
        self.audio_lufs_target_spin.setRange(-30, 0)
        self.export_subtitle_font_size_spin = QSpinBox()
        self.export_subtitle_font_size_spin.setRange(8, 72)
        self.export_subtitle_font_size_spin.setSingleStep(2)
        self.export_subtitle_font_size_spin.setSuffix(" px")
        self.export_subtitle_font_size_spin.setToolTip("字幕大小，数字越大，烧录到视频里的字幕会越大。")
        self.output_container_combo = QComboBox()
        self.output_container_combo.addItems(["mp4", "mov", "mkv"])
        export_group = self._build_form_group(
            "导出",
            [
                ("默认导出类型", self.export_default_type_combo),
                ("默认字幕模式", self.export_subtitle_mode_combo),
                ("字幕大小", self.export_subtitle_font_size_spin),
                ("目标响度", self.audio_lufs_target_spin),
                ("输出容器", self.output_container_combo),
                ("烧录字幕", self.export_burn_subtitles_checkbox),
                ("导出 SRT", self.export_sidecar_srt_checkbox),
            ],
        )

        self._add_settings_section(
            "常规",
            "管理工作目录、自动保存和应用日志等基础行为。",
            [workspace_group],
        )
        self._add_settings_section(
            "媒体",
            "配置 ffmpeg、默认输出目录和通用媒体编码参数。",
            [media_group],
        )
        self._add_settings_section(
            "转写",
            "配置 Whisper 模型、运行设备、本地缓存和 GPU 检测。",
            [asr_group],
        )
        self._add_settings_section(
            "剪辑",
            "调整语气词、停顿阈值和自动剪辑的策略强度。",
            [cutting_group],
        )
        self._add_settings_section(
            "翻译",
            "管理翻译接口、模型、系统提示词以及代理参数。",
            [translation_group],
        )
        self._add_settings_section(
            "配音",
            "设置 TTS 提供方、默认音色、试听文本和重试策略。",
            [tts_group],
        )
        self._add_settings_section(
            "导出",
            "整理默认导出格式、字幕方式、响度目标和容器格式。",
            [export_group],
        )

        self.settings_nav.currentRowChanged.connect(self._set_current_settings_section)
        if self.settings_nav.count() > 0:
            self.settings_nav.setCurrentRow(0)

    def _build_form_group(self, title: str, rows: list[tuple[str, QWidget]]) -> QGroupBox:
        group = QGroupBox(title)
        form = QFormLayout(group)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        form.setHorizontalSpacing(18)
        form.setVerticalSpacing(10)
        for label, widget in rows:
            form.addRow(label, widget)
        return group

    def _add_settings_section(self, title: str, description: str, groups: list[QWidget]) -> None:
        item = QListWidgetItem(title)
        item.setToolTip(description)
        item.setSizeHint(QSize(0, 44))
        self.settings_nav.addItem(item)
        self.settings_stack.addWidget(self._build_settings_section_page(title, description, groups))

    def _build_settings_section_page(self, title: str, description: str, groups: list[QWidget]) -> QWidget:
        # Each category gets its own scroll area so long forms stay contained
        # and the header/sidebar remain fixed while browsing settings.
        scroll = QScrollArea()
        scroll.setObjectName("settingsSectionScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        container = QWidget()
        container.setObjectName("settingsSectionContainer")
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 6, 0)
        layout.setSpacing(16)
        section_title = QLabel(title)
        section_title.setObjectName("settingsSectionTitle")
        section_text = QLabel(description)
        section_text.setObjectName("settingsSectionText")
        section_text.setWordWrap(True)
        layout.addWidget(section_title)
        layout.addWidget(section_text)
        for group in groups:
            layout.addWidget(group)
        layout.addStretch(1)
        scroll.setWidget(container)
        return scroll

    def _set_current_settings_section(self, index: int) -> None:
        if 0 <= index < self.settings_stack.count():
            self.settings_stack.setCurrentIndex(index)

    def _selected_asr_model_name(self) -> str:
        for model_name, radio in self._asr_model_radios.items():
            if radio.isChecked():
                return model_name
        custom_name = self.asr_custom_model_edit.text().strip()
        return custom_name or "small"

    def _sync_asr_model_editor_state(self) -> None:
        is_custom = self.asr_custom_radio.isChecked()
        self.asr_custom_model_edit.setEnabled(is_custom)

    def _reload_tts_voice_options(self, provider_type: str, selected_voice: str | None = None) -> None:
        options = available_tts_voices(provider_type)
        preferred_voice = (selected_voice or str(self.tts_default_voice_combo.currentData() or "")).strip()
        self.tts_default_voice_combo.blockSignals(True)
        self.tts_default_voice_combo.clear()
        for voice in options:
            self.tts_default_voice_combo.addItem(f"{voice.label} ({voice.style})", voice.voice_id)
        self.tts_default_voice_combo.blockSignals(False)
        if preferred_voice:
            set_combo_value(self.tts_default_voice_combo, preferred_voice)

    def _sync_tts_provider_state(self) -> None:
        provider_type = str(self.tts_provider_combo.currentData() or "").strip()
        self._reload_tts_voice_options(provider_type)
        if provider_type == "kokoro_local":
            if kokoro_local_ready():
                self.tts_hint_label.setText(
                    "已检测到本地离线 Kokoro 英文音色。推荐默认音色：Bella（温柔女声）。"
                )
            else:
                self.tts_hint_label.setText(
                    "未检测到离线 Kokoro 资源，请把 sherpa-onnx 和 kokoro 模型放到程序目录下的 models/tts。"
                )
        else:
            english_voice_names = [
                voice.name for voice in installed_windows_voices() if voice.culture.lower().startswith("en")
            ]
            if english_voice_names:
                self.tts_hint_label.setText(
                    f"当前检测到 {len(english_voice_names)} 个英文系统音色。发布给别的电脑使用时，建议优先改用离线 Kokoro。"
                )
            else:
                self.tts_hint_label.setText(
                    "当前没有检测到英文系统音色。如果要跨电脑稳定使用，建议改用离线 Kokoro。"
                )

    def load_settings(self, settings: AppSettings) -> None:
        self.workspace_dir_edit.setText(settings.workspace.workspace_dir)
        self.temp_dir_edit.setText(settings.workspace.temp_dir)
        self.auto_save_minutes_spin.setValue(settings.workspace.auto_save_minutes)
        self.recent_projects_limit_spin.setValue(settings.workspace.recent_projects_limit)
        set_combo_value(self.log_level_combo, settings.workspace.log_level)

        self.ffmpeg_path_edit.setText(settings.media.ffmpeg_path)
        self.default_output_dir_edit.setText(settings.media.default_output_dir)
        self.overwrite_existing_checkbox.setChecked(settings.media.overwrite_existing)
        self.video_codec_edit.setText(settings.media.video_codec)
        self.audio_codec_edit.setText(settings.media.audio_codec)
        set_combo_value(self.quality_preset_combo, settings.media.quality_preset)

        normalized_asr_model = str(settings.asr.model_name or "").strip()
        selected_radio = self._asr_model_radios.get(normalized_asr_model)
        if selected_radio is not None:
            selected_radio.setChecked(True)
            self.asr_custom_model_edit.clear()
        else:
            self.asr_custom_radio.setChecked(True)
            self.asr_custom_model_edit.setText(normalized_asr_model)
        set_combo_value(self.asr_device_combo, settings.asr.device)
        self.vad_enabled_checkbox.setChecked(settings.asr.vad_enabled)
        self.segment_max_seconds_spin.setValue(settings.asr.segment_max_seconds)
        self.min_confidence_spin.setValue(settings.asr.min_confidence)
        self.asr_cache_dir_edit.setText(settings.asr.model_cache_dir)
        self.asr_local_model_dir_edit.setText(settings.asr.local_model_dir)
        self.asr_local_files_only_checkbox.setChecked(settings.asr.local_files_only)
        self._sync_asr_model_editor_state()

        self.filler_words_edit.setPlainText("\n".join(settings.cutting.filler_words))
        self.pause_threshold_spin.setValue(settings.cutting.pause_threshold_ms)
        self.cut_padding_spin.setValue(settings.cutting.cut_padding_ms)
        self.merge_gap_spin.setValue(settings.cutting.merge_gap_ms)
        set_combo_value(self.cutting_mode_combo, settings.cutting.mode)
        self.preserve_intro_pause_checkbox.setChecked(settings.cutting.preserve_intro_pause)

        set_combo_value(self.translation_provider_combo, settings.translation.provider_type)
        self.translation_base_url_edit.setText(settings.translation.base_url)
        self.translation_api_key_edit.setText(settings.translation.api_key)
        self.translation_model_edit.setText(settings.translation.model)
        self.translation_system_prompt_edit.setPlainText(settings.translation.system_prompt)
        self.translation_timeout_spin.setValue(settings.translation.timeout_sec)
        self.translation_retries_spin.setValue(settings.translation.max_retries)
        self.translation_concurrency_spin.setValue(settings.translation.concurrency)
        self.translation_batch_size_spin.setValue(settings.translation.batch_size)
        self.translation_proxy_enabled_checkbox.setChecked(settings.translation.proxy_enabled)
        self.translation_http_proxy_edit.setText(settings.translation.http_proxy)
        self.translation_https_proxy_edit.setText(settings.translation.https_proxy)
        self.translation_no_proxy_edit.setText(settings.translation.no_proxy)

        set_combo_value(self.tts_provider_combo, settings.tts.provider_type)
        self._reload_tts_voice_options(settings.tts.provider_type, settings.tts.default_voice)
        self.tts_rate_spin.setValue(settings.tts.rate)
        self.tts_volume_spin.setValue(settings.tts.volume)
        self.tts_sample_rate_spin.setValue(settings.tts.sample_rate)
        self.tts_retry_spin.setValue(settings.tts.retry_count)
        self.tts_preview_text_edit.setPlainText(settings.tts.preview_text)
        self._sync_tts_provider_state()

        set_combo_value(self.export_default_type_combo, settings.export.default_export_type)
        set_combo_value(self.export_subtitle_mode_combo, settings.export.subtitle_mode)
        self.export_subtitle_font_size_spin.setValue(settings.export.subtitle_font_size)
        self.export_burn_subtitles_checkbox.setChecked(settings.export.burn_subtitles)
        self.export_sidecar_srt_checkbox.setChecked(settings.export.export_sidecar_srt)
        self.audio_lufs_target_spin.setValue(settings.export.audio_lufs_target)
        set_combo_value(self.output_container_combo, settings.export.output_container)

    def build_settings(self) -> AppSettings:
        raw_filler_words = self.filler_words_edit.toPlainText().replace("\n", ",")
        filler_words = [word.strip() for word in raw_filler_words.split(",") if word.strip()]

        return AppSettings.from_dict(
            {
                "workspace": {
                    "workspace_dir": self.workspace_dir_edit.text().strip(),
                    "temp_dir": self.temp_dir_edit.text().strip(),
                    "auto_save_minutes": self.auto_save_minutes_spin.value(),
                    "recent_projects_limit": self.recent_projects_limit_spin.value(),
                    "log_level": self.log_level_combo.currentData() or self.log_level_combo.currentText(),
                },
                "media": {
                    "ffmpeg_path": self.ffmpeg_path_edit.text().strip(),
                    "default_output_dir": self.default_output_dir_edit.text().strip(),
                    "overwrite_existing": self.overwrite_existing_checkbox.isChecked(),
                    "video_codec": self.video_codec_edit.text().strip(),
                    "audio_codec": self.audio_codec_edit.text().strip(),
                    "quality_preset": self.quality_preset_combo.currentData() or self.quality_preset_combo.currentText(),
                },
                "asr": {
                    "model_name": self._selected_asr_model_name(),
                    "device": self.asr_device_combo.currentData() or self.asr_device_combo.currentText(),
                    "language": "zh",
                    "vad_enabled": self.vad_enabled_checkbox.isChecked(),
                    "segment_max_seconds": self.segment_max_seconds_spin.value(),
                    "min_confidence": self.min_confidence_spin.value(),
                    "model_cache_dir": self.asr_cache_dir_edit.text().strip(),
                    "local_model_dir": self.asr_local_model_dir_edit.text().strip(),
                    "local_files_only": self.asr_local_files_only_checkbox.isChecked(),
                },
                "cutting": {
                    "filler_words": filler_words,
                    "pause_threshold_ms": self.pause_threshold_spin.value(),
                    "cut_padding_ms": self.cut_padding_spin.value(),
                    "merge_gap_ms": self.merge_gap_spin.value(),
                    "mode": self.cutting_mode_combo.currentData() or self.cutting_mode_combo.currentText(),
                    "preserve_intro_pause": self.preserve_intro_pause_checkbox.isChecked(),
                },
                "translation": {
                    "provider_type": self.translation_provider_combo.currentData(),
                    "base_url": self.translation_base_url_edit.text().strip(),
                    "api_key": self.translation_api_key_edit.text().strip(),
                    "model": self.translation_model_edit.text().strip(),
                    "system_prompt": self.translation_system_prompt_edit.toPlainText().strip(),
                    "timeout_sec": self.translation_timeout_spin.value(),
                    "max_retries": self.translation_retries_spin.value(),
                    "concurrency": self.translation_concurrency_spin.value(),
                    "batch_size": self.translation_batch_size_spin.value(),
                    "proxy_enabled": self.translation_proxy_enabled_checkbox.isChecked(),
                    "http_proxy": self.translation_http_proxy_edit.text().strip(),
                    "https_proxy": self.translation_https_proxy_edit.text().strip(),
                    "no_proxy": self.translation_no_proxy_edit.text().strip(),
                },
                "tts": {
                    "provider_type": self.tts_provider_combo.currentData(),
                    "default_voice": self.tts_default_voice_combo.currentData(),
                    "rate": self.tts_rate_spin.value(),
                    "volume": self.tts_volume_spin.value(),
                    "sample_rate": self.tts_sample_rate_spin.value(),
                    "retry_count": self.tts_retry_spin.value(),
                    "preview_text": self.tts_preview_text_edit.toPlainText().strip(),
                },
                "export": {
                    "default_export_type": self.export_default_type_combo.currentData(),
                    "subtitle_mode": self.export_subtitle_mode_combo.currentData(),
                    "subtitle_font_size": self.export_subtitle_font_size_spin.value(),
                    "burn_subtitles": self.export_burn_subtitles_checkbox.isChecked(),
                    "export_sidecar_srt": self.export_sidecar_srt_checkbox.isChecked(),
                    "audio_lufs_target": self.audio_lufs_target_spin.value(),
                    "output_container": self.output_container_combo.currentText(),
                },
            }
        )
