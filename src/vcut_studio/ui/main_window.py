from __future__ import annotations

import importlib.util
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QThread, QTimer, QUrl
from PySide6.QtGui import QCloseEvent, QIcon
from PySide6.QtMultimedia import QAudioOutput, QMediaDevices, QMediaPlayer
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QStatusBar,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..context import AppContext
from ..asr import collect_asr_runtime_diagnostics
from ..cutting import generate_cut_suggestions
from ..models import ExportJob, Project, Segment
from ..media import (
    MediaProbeError,
    build_preview_proxy_media,
    extract_audio_waveform,
    preview_proxy_required,
    probe_media,
    resolve_ffmpeg_path,
)
from ..ppt_io import import_presentation
from ..providers.translation import TranslationRequest
from ..providers.tts import tts_provider_display_name, tts_provider_runtime_status
from ..runtime_assets import find_app_icon
from ..settings import AppSettings
from ..subtitles import SubtitleCueData, read_subtitle_file, write_subtitle_file
from .dubbing_logic import apply_voice_to_segments, pick_voice_preview_text
from .ppt_workspace import PptWorkspacePage
from .pages import DubbingPage, ExportPage, SettingsPage, TimelinePage, TranslationPage
from .workers import (
    AnalysisResult,
    AnalysisWorker,
    ControllableWorker,
    DubbingJobResult,
    DubbingWorker,
    ExportResult,
    ExportWorker,
    PptScriptDraftJobResult,
    PptScriptGenerationWorker,
    PptScriptTranslationJobResult,
    PptScriptTranslationWorker,
    SourceProofreadWorker,
    TranslationJobResult,
    TranslationTestWorker,
    TranslationWorker,
    VoicePreviewResult,
    VoicePreviewWorker,
)

try:  # pragma: no cover - Windows-only runtime path
    import winsound
except ImportError:  # pragma: no cover - non-Windows runtime path
    winsound = None


class MainWindow(QMainWindow):
    def __init__(self, context: AppContext) -> None:
        super().__init__()
        self.context = context
        self.resize(1500, 960)
        self._apply_window_icon()
        self._apply_workspace_style()

        self.timeline_page = TimelinePage()
        self.translation_page = TranslationPage()
        self.dubbing_page = DubbingPage()
        self.ppt_workspace_page = PptWorkspacePage()
        self.export_page = ExportPage()
        self.settings_page = SettingsPage()
        self.analysis_busy = False
        self.translation_busy = False
        self.dubbing_busy = False
        self.export_busy = False
        self.analysis_thread: QThread | None = None
        self.analysis_worker: AnalysisWorker | None = None
        self.translation_thread: QThread | None = None
        self.translation_worker: (
            TranslationWorker
            | TranslationTestWorker
            | SourceProofreadWorker
            | PptScriptGenerationWorker
            | PptScriptTranslationWorker
            | None
        ) = None
        self.dubbing_thread: QThread | None = None
        self.dubbing_worker: DubbingWorker | VoicePreviewWorker | None = None
        self.export_thread: QThread | None = None
        self.export_worker: ExportWorker | None = None
        self._active_dubbing_previous_statuses: dict[int, str] = {}
        self._last_proofread_snapshot: dict[int, dict[str, object]] = {}
        self._last_proofread_project_id: str | None = None
        self._pending_preview_source_message = ""
        self._active_background_worker: ControllableWorker | None = None
        self._preview_source_path = ""
        self._preview_proxy_cache: dict[str, str] = {}
        self._preview_proxy_failures: set[str] = set()
        self._waveform_cache: dict[str, list[float]] = {}
        self._autosave_timer = QTimer(self)
        self._autosave_timer.timeout.connect(self._auto_save_project_snapshot)

        self.timeline_page.attach_workspace_panels(
            self.translation_page,
            self.dubbing_page,
        )
        self.workspace_stack = QStackedWidget()
        self.workspace_stack.addWidget(self.timeline_page)
        self.workspace_stack.addWidget(self.ppt_workspace_page)
        self.setCentralWidget(self.workspace_stack)
        self.setStatusBar(QStatusBar())
        self._build_status_controls()

        self.settings_dialog = QDialog(self)
        self.settings_dialog.setWindowTitle("设置")
        self.settings_dialog.resize(1080, 760)
        self.settings_dialog.setWindowIcon(self.windowIcon())
        settings_layout = QVBoxLayout(self.settings_dialog)
        settings_layout.setContentsMargins(0, 0, 0, 0)
        settings_layout.addWidget(self.settings_page)

        self.export_dialog = QDialog(self)
        self.export_dialog.setWindowTitle("导出视频")
        self.export_dialog.resize(840, 520)
        self.export_dialog.setWindowIcon(self.windowIcon())
        export_layout = QVBoxLayout(self.export_dialog)
        export_layout.setContentsMargins(12, 12, 12, 12)
        export_layout.addWidget(self.export_page)

        self.preview_audio_output = QAudioOutput(self)
        self.preview_audio_output.setVolume(1.0)
        self.preview_audio_output.setMuted(False)
        self.preview_player = QMediaPlayer(self)
        self.preview_player.setAudioOutput(self.preview_audio_output)
        self.preview_player.setVideoOutput(self.timeline_page.video_widget)
        self.media_devices = QMediaDevices(self)
        self.media_devices.audioOutputsChanged.connect(self._sync_preview_audio_device)
        self._sync_preview_audio_device()
        self.timeline_page.set_preview_audio_state(100, False)

        self._create_menu()
        self._connect_signals()
        self.settings_page.load_settings(self.context.settings)
        self._configure_autosave_timer()
        self.refresh_views()

    def _apply_window_icon(self) -> None:
        icon_path = find_app_icon()
        if icon_path is not None:
            self.setWindowIcon(QIcon(str(icon_path)))

    def _apply_workspace_style(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow, QDialog, QMessageBox {
                background: #161a1f;
                color: #f3f4f6;
            }
            QMenuBar, QMenu, QStatusBar, QToolTip {
                background: #11151b;
                color: #f3f4f6;
                border: 1px solid #2b3440;
            }
            QTabWidget::pane {
                border: 1px solid #2c3440;
                border-radius: 10px;
                background: #1c222b;
                top: -1px;
            }
            QTabBar::tab {
                background: #11151b;
                color: #cbd5e1;
                border: 1px solid #2c3440;
                border-bottom: none;
                border-top-left-radius: 8px;
                border-top-right-radius: 8px;
                padding: 8px 16px;
                margin-right: 6px;
            }
            QTabBar::tab:selected {
                background: #1c222b;
                color: #f8fafc;
            }
            QTabBar::tab:hover:!selected {
                background: #18202a;
            }
            QWidget {
                color: #e5e7eb;
            }
            QLabel {
                color: #e5e7eb;
            }
            QFrame#settingsHeaderCard,
            QFrame#settingsSidebar,
            QFrame#settingsContent {
                background: #1c222b;
                border: 1px solid #2c3440;
                border-radius: 12px;
            }
            QLabel#settingsHeaderTitle,
            QLabel#settingsSectionTitle {
                color: #f8fafc;
                font-size: 18px;
                font-weight: 700;
            }
            QLabel#settingsHeaderText,
            QLabel#settingsNavHint,
            QLabel#settingsSectionText {
                color: #94a3b8;
            }
            QLabel#settingsNavTitle {
                color: #f8fafc;
                font-weight: 700;
            }
            QGroupBox {
                border: 1px solid #2c3440;
                border-radius: 10px;
                margin-top: 12px;
                padding-top: 10px;
                background: #1c222b;
                font-weight: 600;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 12px;
                padding: 0 6px;
                color: #f8fafc;
            }
            QPushButton {
                background: #273445;
                border: 1px solid #344255;
                border-radius: 8px;
                padding: 6px 12px;
                color: #f8fafc;
            }
            QPushButton:hover {
                background: #304259;
            }
            QPushButton:disabled {
                background: #202734;
                color: #6b7280;
                border-color: #2a3240;
            }
            QLineEdit, QPlainTextEdit, QTextEdit, QComboBox, QSpinBox, QDoubleSpinBox {
                background: #11151b;
                border: 1px solid #2f3946;
                border-radius: 6px;
                padding: 4px 6px;
                selection-background-color: #0f766e;
                selection-color: #ffffff;
            }
            QComboBox::drop-down {
                border: none;
                width: 22px;
            }
            QAbstractItemView, QListView, QTableWidget, QTableView, QTreeView {
                background: #11151b;
                alternate-background-color: #18202a;
                border: 1px solid #2f3946;
                gridline-color: #27313d;
                selection-background-color: #0f766e;
                selection-color: #ffffff;
            }
            QListWidget#settingsNav {
                background: transparent;
                border: none;
                outline: none;
                padding: 2px 0;
            }
            QListWidget#settingsNav::item {
                border-radius: 8px;
                padding: 10px 12px;
                margin: 2px 0;
                color: #cbd5e1;
            }
            QListWidget#settingsNav::item:selected {
                background: #0f766e;
                color: #ffffff;
            }
            QListWidget#settingsNav::item:hover:!selected {
                background: #243140;
                color: #f8fafc;
            }
            QTableWidget {
                color: #e5e7eb;
            }
            QHeaderView::section {
                background: #202734;
                border: none;
                padding: 6px;
            }
            QScrollArea {
                border: none;
            }
            QScrollBar:vertical, QScrollBar:horizontal {
                background: #11151b;
                border: none;
                margin: 0;
            }
            QScrollBar::handle:vertical, QScrollBar::handle:horizontal {
                background: #334155;
                border-radius: 6px;
                min-height: 28px;
                min-width: 28px;
            }
            QScrollBar::add-line, QScrollBar::sub-line,
            QScrollBar::add-page, QScrollBar::sub-page {
                background: none;
                border: none;
            }
            QProgressBar {
                border: 1px solid #2f3946;
                border-radius: 8px;
                text-align: center;
                background: #11151b;
            }
            QProgressBar::chunk {
                background: #0ea5a4;
                border-radius: 6px;
            }
            QSlider::groove:horizontal {
                height: 6px;
                background: #243140;
                border-radius: 3px;
            }
            QSlider::handle:horizontal {
                width: 14px;
                margin: -5px 0;
                border-radius: 7px;
                background: #7dd3fc;
            }
            QCheckBox::indicator {
                width: 16px;
                height: 16px;
                border-radius: 4px;
                border: 1px solid #3b4756;
                background: #11151b;
            }
            QCheckBox::indicator:checked {
                background: #0f766e;
                border-color: #14b8a6;
            }
            QSplitter::handle {
                background: #11151b;
            }
            QVideoWidget {
                background: #090c10;
                border-radius: 10px;
            }
            """
        )

    def _build_status_controls(self) -> None:
        control_container = QWidget(self)
        control_layout = QHBoxLayout(control_container)
        control_layout.setContentsMargins(0, 0, 0, 0)
        control_layout.setSpacing(6)

        self.background_task_label = QLabel("后台任务：空闲")
        self.background_progress_bar = QProgressBar()
        self.background_progress_bar.setRange(0, 100)
        self.background_progress_bar.setValue(0)
        self.background_progress_bar.setFormat("等待开始")
        self.background_progress_bar.setVisible(False)
        self.background_progress_bar.setMinimumWidth(280)
        self.pause_task_button = QPushButton("暂停")
        self.resume_task_button = QPushButton("继续")
        self.cancel_task_button = QPushButton("终止")

        self.pause_task_button.clicked.connect(self.pause_active_task)
        self.resume_task_button.clicked.connect(self.resume_active_task)
        self.cancel_task_button.clicked.connect(self.cancel_active_task)

        control_layout.addWidget(self.background_task_label)
        control_layout.addWidget(self.background_progress_bar)
        control_layout.addWidget(self.pause_task_button)
        control_layout.addWidget(self.resume_task_button)
        control_layout.addWidget(self.cancel_task_button)

        self.statusBar().addPermanentWidget(control_container)

    def _create_menu(self) -> None:
        file_menu = self.menuBar().addMenu("文件")
        file_menu.addAction("新建项目", self.new_project)
        file_menu.addAction("导入视频", self.import_video)
        file_menu.addAction("打开项目", self.open_project)
        file_menu.addAction("保存项目", self.save_project)
        file_menu.addAction("项目另存为", self.save_project_as)
        file_menu.addSeparator()
        file_menu.addAction("导出视频", self.show_export_dialog)
        file_menu.addSeparator()
        file_menu.addAction("退出", self.close)

        tools_menu = self.menuBar().addMenu("工具")
        tools_menu.addAction("扫描依赖", self.scan_dependencies)
        tools_menu.addAction("识别字幕 / 分段", self.analyze_video)
        tools_menu.addAction("生成删除建议", self.generate_cut_suggestions_for_project)
        tools_menu.addSeparator()
        tools_menu.addAction("打开设置", self.show_settings_dialog)

    def _connect_signals(self) -> None:
        self.timeline_page.analyze_button.clicked.connect(self.analyze_video)
        self.timeline_page.generate_suggestions_requested.connect(self.generate_cut_suggestions_for_project)
        self.timeline_page.accept_selected_button.clicked.connect(self.accept_selected_suggestions)
        self.timeline_page.clear_selected_button.clicked.connect(self.clear_selected_suggestions)
        self.timeline_page.accept_all_button.clicked.connect(self.accept_all_suggestions)
        self.timeline_page.reject_all_button.clicked.connect(self.clear_suggestion_acceptance)
        self.timeline_page.suggestion_acceptance_changed.connect(self.set_suggestion_acceptance)
        self.timeline_page.suggestion_selection_changed.connect(self.locate_selected_suggestion)
        self.timeline_page.suggestion_range_apply_requested.connect(self.update_suggestion_range)
        self.timeline_page.segment_selection_changed.connect(self.locate_selected_segment)
        self.timeline_page.seek_requested.connect(self.seek_preview)
        self.timeline_page.play_requested.connect(self.play_preview)
        self.timeline_page.pause_requested.connect(self.pause_preview)
        self.timeline_page.stop_requested.connect(self.stop_preview)
        self.timeline_page.preview_volume_changed.connect(self.set_preview_volume)
        self.timeline_page.preview_mute_toggled.connect(self.set_preview_muted)
        self.translation_page.chinese_text_edited.connect(self.update_segment_chinese_text)
        self.translation_page.english_text_edited.connect(self.update_segment_english_text)
        self.translation_page.selection_changed.connect(self._refresh_interactive_state)
        self.translation_page.time_double_clicked.connect(self.preview_translation_time)
        self.translation_page.translate_all_button.clicked.connect(self.translate_all_segments)
        self.translation_page.translate_selected_button.clicked.connect(self.translate_selected_segments)
        self.translation_page.proofread_source_requested.connect(self.proofread_source_segments)
        self.translation_page.undo_proofread_requested.connect(self.undo_last_source_proofread)
        self.translation_page.import_source_subtitle_requested.connect(self.import_source_subtitles)
        self.translation_page.import_translation_subtitle_requested.connect(self.import_translation_subtitles)
        self.translation_page.export_subtitle_requested.connect(self.export_translation_subtitles)
        self.translation_page.save_requested.connect(self.save_project)
        self.translation_page.test_translation_button.clicked.connect(self.test_translation_config)
        self.dubbing_page.selection_changed.connect(self._refresh_interactive_state)
        self.dubbing_page.check_state_changed.connect(self.set_dub_row_checked)
        self.dubbing_page.apply_voice_button.clicked.connect(self.apply_voice_to_selected_segments)
        self.dubbing_page.generate_button.clicked.connect(self.generate_selected_dubs)
        self.dubbing_page.preview_button.clicked.connect(self.preview_selected_voice)
        self.dubbing_page.stop_button.clicked.connect(self.stop_dub_playback)
        self.dubbing_page.check_selected_button.clicked.connect(self.check_selected_dub_rows)
        self.dubbing_page.uncheck_selected_button.clicked.connect(self.uncheck_selected_dub_rows)
        self.dubbing_page.check_all_button.clicked.connect(self.check_all_dub_rows)
        self.dubbing_page.uncheck_all_button.clicked.connect(self.uncheck_all_dub_rows)
        self.dubbing_page.play_generated_requested.connect(self.play_generated_dub)
        self.export_page.output_dir_browse_requested.connect(self.browse_export_output_dir)
        self.export_page.export_button.clicked.connect(self.start_export)
        self.settings_page.top_save_button.clicked.connect(self.save_settings)
        self.settings_page.top_reset_button.clicked.connect(self.reset_settings)
        self.settings_page.asr_runtime_check_requested.connect(self.show_asr_runtime_diagnostics)
        self.settings_page.asr_cache_dir_browse_requested.connect(self.browse_asr_cache_dir)
        self.settings_page.asr_local_model_dir_browse_requested.connect(self.browse_asr_local_model_dir)
        self.preview_player.positionChanged.connect(self._on_preview_position_changed)
        self.preview_player.durationChanged.connect(self._on_preview_duration_changed)
        self.preview_player.playbackStateChanged.connect(self._on_preview_playback_state_changed)
        self.preview_player.errorOccurred.connect(self._on_preview_error)

    def show_settings_dialog(self) -> None:
        self.settings_page.load_settings(self.context.settings)
        self.settings_dialog.show()
        self.settings_dialog.raise_()
        self.settings_dialog.activateWindow()

    def show_export_dialog(self) -> None:
        self.export_page.load_project(self.context.current_project, self.context.settings)
        self._refresh_interactive_state()
        self.export_dialog.show()
        self.export_dialog.raise_()
        self.export_dialog.activateWindow()

    def show_asr_runtime_diagnostics(self) -> None:
        requested_device = str(
            self.settings_page.asr_device_combo.currentData() or self.settings_page.asr_device_combo.currentText()
        )
        diagnostics = collect_asr_runtime_diagnostics(requested_device)

        nvidia_text = "未安装或不可用"
        if diagnostics.nvidia_smi_available:
            nvidia_text = "；".join(diagnostics.nvidia_gpu_names) if diagnostics.nvidia_gpu_names else "可用，但未读取到显卡名称"

        if diagnostics.gpu_ready:
            recommendation = "GPU 环境可用，可以选择 CUDA 或自动。"
        elif diagnostics.cuda_device_count <= 0:
            recommendation = "当前未检测到可用 CUDA 设备，建议先使用 CPU。"
        elif diagnostics.missing_cuda_libraries:
            recommendation = (
                "检测到显卡，但缺少 CUDA 运行库："
                + ", ".join(diagnostics.missing_cuda_libraries)
                + "。当前建议使用 CPU，或安装对应 CUDA 12 / cuDNN 9 运行库。"
            )
        else:
            recommendation = "CUDA 环境状态未知，建议先用 CPU 做识别。"

        message = "\n".join(
            [
                f"当前设备设置：{diagnostics.requested_device}",
                f"模型缓存目录：{self.settings_page.asr_cache_dir_edit.text().strip() or '默认缓存目录'}",
                f"本地模型目录：{self.settings_page.asr_local_model_dir_edit.text().strip() or '未指定'}",
                f"仅本地加载：{'是' if self.settings_page.asr_local_files_only_checkbox.isChecked() else '否'}",
                f"faster-whisper：{diagnostics.faster_whisper_version}",
                f"ctranslate2：{diagnostics.ctranslate2_version}",
                f"nvidia-smi：{nvidia_text}",
                f"CUDA 设备数：{diagnostics.cuda_device_count}",
                "cuBLAS/cuDNN："
                + (
                    "已就绪"
                    if not diagnostics.missing_cuda_libraries
                    else "缺少 " + ", ".join(diagnostics.missing_cuda_libraries)
                ),
                "",
                recommendation,
            ]
        )
        QMessageBox.information(self, "GPU 环境检测", message)
        self.statusBar().showMessage("GPU 环境检测完成。", 5000)

    def browse_asr_cache_dir(self) -> None:
        initial_dir = (
            self.settings_page.asr_cache_dir_edit.text().strip()
            or self.context.settings.asr.model_cache_dir
            or self.context.settings.workspace.workspace_dir
        )
        directory = QFileDialog.getExistingDirectory(self, "选择 ASR 模型缓存目录", initial_dir)
        if directory:
            self.settings_page.asr_cache_dir_edit.setText(directory)

    def browse_asr_local_model_dir(self) -> None:
        initial_dir = (
            self.settings_page.asr_local_model_dir_edit.text().strip()
            or self.context.settings.asr.local_model_dir
            or self.context.settings.asr.model_cache_dir
            or self.context.settings.workspace.workspace_dir
        )
        directory = QFileDialog.getExistingDirectory(self, "选择本地 ASR 模型目录", initial_dir)
        if directory:
            self.settings_page.asr_local_model_dir_edit.setText(directory)

    def browse_export_output_dir(self) -> None:
        initial_dir = (
            self.export_page.output_dir_edit.text().strip()
            or self.context.settings.media.default_output_dir
            or self.context.settings.workspace.workspace_dir
        )
        directory = QFileDialog.getExistingDirectory(self, "选择导出目录", initial_dir)
        if directory:
            self.export_page.output_dir_edit.setText(directory)

    def _configure_autosave_timer(self) -> None:
        interval_minutes = max(1, int(self.context.settings.workspace.auto_save_minutes or 1))
        self._autosave_timer.start(interval_minutes * 60 * 1000)

    def _update_window_title(self) -> None:
        project_name = self.context.current_project.name or "未命名项目"
        dirty_suffix = " *" if self.context.project_dirty else ""
        self.setWindowTitle(f"片语 - {project_name}{dirty_suffix}")

    def _set_task_progress(self, progress: int, message: str, *, pump_events: bool = False) -> None:
        clamped = max(0, min(int(progress), 100))
        self.background_progress_bar.setVisible(True)
        self.background_progress_bar.setValue(clamped)
        self.background_progress_bar.setFormat(f"{clamped}% | {message}")
        self.statusBar().showMessage(message, 0)
        if pump_events:
            QApplication.processEvents()

    def _clear_task_progress(self) -> None:
        self.background_progress_bar.setValue(0)
        self.background_progress_bar.setFormat("等待开始")
        self.background_progress_bar.setVisible(False)

    def _notify_task_finished(self, title: str, message: str) -> None:
        QApplication.alert(self)
        if winsound is not None:
            try:
                winsound.MessageBeep(winsound.MB_ICONASTERISK)
            except Exception:
                pass
        QMessageBox.information(self, title, message)

    def _mark_project_dirty(self) -> None:
        self.context.mark_project_dirty()
        self._update_window_title()

    def _mark_project_saved(self, path: Path | None = None) -> None:
        self.context.mark_project_saved(path)
        self._update_window_title()

    def _clear_proofread_snapshot(self) -> None:
        self._last_proofread_snapshot = {}
        self._last_proofread_project_id = None

    def _has_proofread_undo_available(self) -> bool:
        return (
            bool(self._last_proofread_snapshot)
            and self._last_proofread_project_id == self.context.current_project.project_id
        )

    def _segment_matches_proofread_snapshot(self, segment: Segment, snapshot: dict[str, object]) -> bool:
        return (
            segment.zh_text == str(snapshot.get("zh_text", ""))
            and segment.en_text == str(snapshot.get("en_text", ""))
            and segment.translation_status == str(snapshot.get("translation_status", "pending"))
            and segment.tts_status == str(snapshot.get("tts_status", "pending"))
            and segment.dub_audio_path == snapshot.get("dub_audio_path")
            and segment.dub_text == str(snapshot.get("dub_text", ""))
            and segment.dub_voice_id == snapshot.get("dub_voice_id")
            and segment.dub_rate == snapshot.get("dub_rate")
        )

    def _proofread_snapshot_has_live_changes(self) -> bool:
        if not self._has_proofread_undo_available():
            return False
        for row_index, snapshot in self._last_proofread_snapshot.items():
            if not (0 <= row_index < len(self.context.current_project.segments)):
                continue
            segment = self.context.current_project.segments[row_index]
            if not self._segment_matches_proofread_snapshot(segment, snapshot):
                return True
        return False

    def _clear_proofread_snapshot_if_rows_touched(self, rows: list[int] | set[int]) -> None:
        if not self._has_proofread_undo_available():
            return
        if any(row in self._last_proofread_snapshot for row in rows):
            self._clear_proofread_snapshot()

    def _save_project_to_path(self, target_path: str | Path, success_message: str) -> bool:
        try:
            saved_path = self.context.project_store.save(self.context.current_project, target_path)
        except Exception as exc:
            QMessageBox.critical(self, "保存项目失败", str(exc))
            return False
        self._mark_project_saved(saved_path)
        self.refresh_views()
        self.statusBar().showMessage(f"{success_message}：{saved_path}", 5000)
        return True

    def _confirm_project_switch(self, action_text: str) -> bool:
        if not self.context.project_dirty:
            return True
        reply = QMessageBox.question(
            self,
            "项目尚未保存",
            f"当前项目有未保存修改。{action_text}前要先保存吗？",
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Save,
        )
        if reply == QMessageBox.StandardButton.Cancel:
            return False
        if reply == QMessageBox.StandardButton.Save:
            return self.save_project()
        return True

    def _auto_save_project_snapshot(
        self,
        *,
        status_message: str | None = None,
        error_title: str | None = None,
        allow_during_background: bool = False,
    ) -> Path | None:
        if (self._background_job_in_progress() and not allow_during_background) or not self.context.project_dirty:
            return None
        project = self.context.current_project
        target_path = self.context.current_project_path
        if target_path is None:
            target_path = self.context.project_store.autosave_path(
                self.context.settings.workspace.workspace_dir,
                project.name,
                project.project_id,
            )
        try:
            saved_path = self.context.project_store.save(project, target_path)
        except Exception as exc:
            if error_title:
                QMessageBox.critical(self, error_title, str(exc))
            return None
        if self.context.current_project_path is not None:
            self._mark_project_saved(saved_path)
        if status_message:
            self.statusBar().showMessage(f"{status_message}：{saved_path}", 5000)
        return saved_path

    def _set_active_background_worker(self, worker: ControllableWorker | None) -> None:
        self._active_background_worker = worker

    def _current_background_task_label(self) -> str:
        if self._active_background_worker is None:
            return ""
        return self._active_background_worker.task_label

    def pause_active_task(self) -> None:
        worker = self._active_background_worker
        if worker is None:
            self.statusBar().showMessage("当前没有可暂停的后台任务。", 4000)
            return
        try:
            worker.request_pause()
        except Exception as exc:
            QMessageBox.warning(self, "暂停失败", str(exc))
            return
        self._refresh_interactive_state()
        self.statusBar().showMessage(f"{worker.task_label}已暂停。", 5000)

    def resume_active_task(self) -> None:
        worker = self._active_background_worker
        if worker is None:
            self.statusBar().showMessage("当前没有可继续的后台任务。", 4000)
            return
        try:
            worker.request_resume()
        except Exception as exc:
            QMessageBox.warning(self, "继续失败", str(exc))
            return
        self._refresh_interactive_state()
        self.statusBar().showMessage(f"{worker.task_label}已继续。", 5000)

    def cancel_active_task(self) -> None:
        worker = self._active_background_worker
        if worker is None:
            self.statusBar().showMessage("当前没有可终止的后台任务。", 4000)
            return
        try:
            worker.request_cancel()
        except Exception as exc:
            QMessageBox.warning(self, "终止失败", str(exc))
            return
        self._refresh_interactive_state()
        self.statusBar().showMessage(f"正在终止{worker.task_label}...", 0)

    def refresh_views(self) -> None:
        project = self.context.current_project
        self._update_window_title()
        self.timeline_page.load_project(project)
        self._sync_timeline_preview(project)
        self.translation_page.load_project(project, self.context.settings)
        self.dubbing_page.load_project(project, self.context.settings)
        self.export_page.load_project(project, self.context.settings)
        self._refresh_interactive_state()

    def _sync_timeline_preview(self, project: Project) -> None:
        video_path = str(project.video_path or "").strip()
        duration_ms = int(project.source_duration_ms or project.media_info.duration_ms or 0)
        self.timeline_page.set_preview_enabled(bool(video_path))
        self.timeline_page.set_waveform_data(duration_ms, [])
        self.timeline_page.set_focus_range(None, None)

        if not video_path:
            self.preview_player.stop()
            self.preview_player.setSource(QUrl())
            self._preview_source_path = ""
            self.timeline_page.set_preview_position(0, 0)
            return

        preview_source_path = self._resolve_preview_source_path(project)
        if preview_source_path != self._preview_source_path:
            self.preview_player.stop()
            self.preview_player.setSource(QUrl.fromLocalFile(preview_source_path))
            self._preview_source_path = preview_source_path

        waveform_points = self._waveform_cache.get(video_path)
        if waveform_points is None and project.media_info.has_audio:
            try:
                waveform_points = extract_audio_waveform(
                    video_path,
                    self.context.settings.media.ffmpeg_path,
                )
            except Exception:
                waveform_points = []
            self._waveform_cache[video_path] = waveform_points

        self.timeline_page.set_waveform_data(duration_ms, waveform_points or [])
        self.timeline_page.set_preview_position(self.preview_player.position(), duration_ms)

    def _sync_preview_audio_device(self) -> None:
        try:
            device = QMediaDevices.defaultAudioOutput()
            if not device.isNull():
                self.preview_audio_output.setDevice(device)
        except Exception:
            pass
        self.timeline_page.set_preview_audio_state(
            round(self.preview_audio_output.volume() * 100),
            self.preview_audio_output.isMuted(),
        )

    def set_preview_volume(self, value: int) -> None:
        clamped = max(0, min(int(value), 100))
        self.preview_audio_output.setVolume(clamped / 100.0)
        if clamped > 0 and self.preview_audio_output.isMuted():
            self.preview_audio_output.setMuted(False)
        self.timeline_page.set_preview_audio_state(clamped, self.preview_audio_output.isMuted())

    def set_preview_muted(self, muted: bool) -> None:
        self.preview_audio_output.setMuted(bool(muted))
        self.timeline_page.set_preview_audio_state(
            round(self.preview_audio_output.volume() * 100),
            self.preview_audio_output.isMuted(),
        )

    def _resolve_preview_source_path(self, project: Project) -> str:
        source_path = str(project.video_path or "").strip()
        if not source_path:
            return ""
        cached_proxy_path = self._preview_proxy_cache.get(source_path)
        if cached_proxy_path and Path(cached_proxy_path).exists():
            return cached_proxy_path
        if source_path in self._preview_proxy_failures:
            return source_path
        if not preview_proxy_required(source_path, project.media_info):
            return source_path

        try:
            preview_path = build_preview_proxy_media(
                source_path,
                project.media_info,
                self.context.settings.media.ffmpeg_path,
                Path(self.context.settings.workspace.temp_dir) / "preview_media",
            )
        except Exception as exc:
            self._preview_proxy_failures.add(source_path)
            self.statusBar().showMessage(f"监看预览兼容处理失败，已继续使用原视频：{exc}", 8000)
            return source_path

        self._preview_proxy_cache[source_path] = str(preview_path)
        self._preview_proxy_failures.discard(source_path)
        self.statusBar().showMessage(
            f"已为视频监看切换到兼容预览源：{Path(preview_path).name}",
            5000,
        )
        return str(preview_path)

    def play_preview(self) -> None:
        if not self.context.current_project.video_path:
            self.statusBar().showMessage("请先导入视频。", 4000)
            return
        self.preview_player.play()

    def pause_preview(self) -> None:
        if self.preview_player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.preview_player.pause()
        else:
            self.preview_player.play()

    def stop_preview(self) -> None:
        self.preview_player.stop()
        self.timeline_page.set_preview_position(0, self.preview_player.duration())

    def seek_preview(self, position_ms: int) -> None:
        if not self.context.current_project.video_path:
            return
        self.preview_player.setPosition(max(0, int(position_ms)))

    def _on_preview_position_changed(self, position_ms: int) -> None:
        self.timeline_page.set_preview_position(position_ms, self.preview_player.duration())

    def _on_preview_duration_changed(self, duration_ms: int) -> None:
        self.timeline_page.set_preview_position(self.preview_player.position(), duration_ms)

    def _on_preview_playback_state_changed(self, state: QMediaPlayer.PlaybackState) -> None:
        self.timeline_page.set_preview_playing(state == QMediaPlayer.PlaybackState.PlayingState)

    def _on_preview_error(self, _error: object, error_text: str) -> None:
        if error_text:
            self.statusBar().showMessage(f"视频监看不可用：{error_text}", 8000)

    def _dependency_rows(self) -> list[tuple[str, str, str]]:
        ffmpeg_target = self.context.settings.media.ffmpeg_path or "ffmpeg"
        ffmpeg_path = resolve_ffmpeg_path(ffmpeg_target)
        ffmpeg_status = "已就绪" if ffmpeg_path else "缺失"
        ffmpeg_detail = ffmpeg_path or f"无法在 PATH 中找到“{ffmpeg_target}”"

        asr_available = importlib.util.find_spec("faster_whisper") is not None
        asr_status = "已就绪" if asr_available else "缺失"
        asr_detail = f"{self.context.settings.asr.model_name} / {self.context.settings.asr.device}"

        translation_ready = all(
            [
                self.context.settings.translation.base_url,
                self.context.settings.translation.model,
                self.context.settings.translation.api_key,
            ]
        )
        translation_status = "已配置" if translation_ready else "未配置"
        translation_detail = (
            f"{self.context.settings.translation.provider_type} / {self.context.settings.translation.model}"
        )

        tts_ready, tts_runtime_detail = tts_provider_runtime_status(self.context.settings.tts.provider_type)
        tts_status = "已就绪" if tts_ready else "缺失"
        tts_detail = (
            f"{tts_provider_display_name(self.context.settings.tts.provider_type)} / "
            f"默认音色：{self.context.settings.tts.default_voice or '未设置'} / {tts_runtime_detail}"
        )
        return [
            ("ffmpeg", ffmpeg_status, ffmpeg_detail),
            ("中文 ASR", asr_status, asr_detail),
            ("翻译模型", translation_status, translation_detail),
            ("英文配音", tts_status, tts_detail),
        ]

    def new_project(self) -> None:
        if not self._require_idle_background("新建项目"):
            return
        if not self._confirm_project_switch("新建项目"):
            return
        project = Project.new("未命名项目")
        project.analysis_completed = False
        self.context.set_project(project)
        self._clear_proofread_snapshot()
        self._clear_task_progress()
        self.refresh_views()
        self.statusBar().showMessage("已新建空白项目。", 5000)

    def import_video(self) -> None:
        if not self._require_idle_background("导入其他视频"):
            return
        if not self._confirm_project_switch("导入视频"):
            return
        workspace_dir = self.context.settings.workspace.workspace_dir
        filename, _ = QFileDialog.getOpenFileName(
            self,
            "导入视频",
            workspace_dir,
            "视频文件 (*.mp4 *.mov *.mkv *.avi *.m4v *.webm);;所有文件 (*.*)",
        )
        if not filename:
            return

        self._set_task_progress(15, "正在探测视频信息...", pump_events=True)
        try:
            probe = probe_media(filename, self.context.settings.media.ffmpeg_path)
        except MediaProbeError as exc:
            self._clear_task_progress()
            QMessageBox.critical(self, "导入视频失败", str(exc))
            return

        project = self.context.current_project
        project.name = Path(filename).stem
        project.video_path = filename
        project.audio_path = ""
        project.source_duration_ms = probe.media_info.duration_ms
        project.analysis_completed = False
        project.media_info = probe.media_info
        project.segments.clear()
        project.cut_suggestions.clear()
        project.exports.clear()
        self._clear_proofread_snapshot()
        self._mark_project_dirty()
        self._set_task_progress(55, "正在准备视频监看预览...", pump_events=True)
        self._resolve_preview_source_path(project)
        self._set_task_progress(65, "正在载入视频监看和音轨...", pump_events=True)
        self.refresh_views()
        self._set_task_progress(100, "视频导入完成。", pump_events=True)
        self._clear_task_progress()
        self.timeline_page.setFocus()

        if not probe.media_info.has_audio:
            QMessageBox.warning(
                self,
                "导入视频不含音频",
                "所选视频似乎没有音频流。在你换成包含人声的媒体之前，ASR 很可能无法正常工作。",
            )
        self.statusBar().showMessage(
            f"已导入视频：{filename}（{probe.media_info.duration_ms / 1000:.2f}s）",
            6000,
        )

    def open_project(self) -> None:
        if not self._require_idle_background("打开其他项目"):
            return
        if not self._confirm_project_switch("打开其他项目"):
            return
        workspace_dir = self.context.settings.workspace.workspace_dir
        filename, _ = QFileDialog.getOpenFileName(
            self,
            "打开项目",
            workspace_dir,
            "片语项目 (*.vcutproj)",
        )
        if not filename:
            return
        try:
            project = self.context.project_store.load(filename)
        except Exception as exc:
            QMessageBox.critical(self, "打开项目失败", str(exc))
            return
        self.context.set_project(project, Path(filename))
        self._clear_proofread_snapshot()
        self.refresh_views()
        self.statusBar().showMessage(f"已打开项目：{filename}", 5000)

    def save_project(self) -> bool:
        if not self._require_idle_background("保存项目"):
            return False
        if self.context.current_project_path is None:
            return self.save_project_as()
        return self._save_project_to_path(self.context.current_project_path, "已保存项目")

    def save_project_as(self) -> bool:
        if not self._require_idle_background("保存项目"):
            return False
        workspace_dir = self.context.settings.workspace.workspace_dir
        suggested_path = self.context.project_store.default_path(workspace_dir, self.context.current_project.name)
        filename, _ = QFileDialog.getSaveFileName(
            self,
            "项目另存为",
            str(suggested_path),
            "片语项目 (*.vcutproj)",
        )
        if not filename:
            return False
        if not filename.endswith(".vcutproj"):
            filename = f"{filename}.vcutproj"
        return self._save_project_to_path(filename, "已保存项目")

    def analyze_video(self) -> None:
        if self._background_job_in_progress():
            QMessageBox.information(
                self,  # noqa: COM812
                "后台任务进行中",
                "请等待当前后台任务完成。",
            )
            return
        project = self.context.current_project
        if not project.video_path:
            QMessageBox.information(
                self,
                "没有源视频",
                "请先导入源视频，再执行中文 ASR 分析。",
            )
            return

        worker = AnalysisWorker(
            media_path=project.video_path,
            asr_settings=self.context.settings.asr,
            default_voice=self.context.settings.tts.default_voice,
        )
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._on_analysis_progress)
        worker.progress_value.connect(self._on_analysis_progress_value)
        worker.finished.connect(self._on_analysis_finished)
        worker.cancelled.connect(self._on_analysis_cancelled)
        worker.error.connect(self._on_analysis_error)
        worker.finished.connect(thread.quit)
        worker.cancelled.connect(thread.quit)
        worker.error.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        worker.cancelled.connect(worker.deleteLater)
        worker.error.connect(worker.deleteLater)
        thread.finished.connect(self._cleanup_analysis_job)
        thread.finished.connect(thread.deleteLater)

        self.analysis_thread = thread
        self.analysis_worker = worker
        self._set_active_background_worker(worker)
        self.analysis_busy = True
        self._refresh_interactive_state()
        self.statusBar().showMessage("已加入字幕识别队列...", 0)
        thread.start()

    def start_export(self) -> None:
        if self._background_job_in_progress():
            QMessageBox.information(
                self,
                "后台任务进行中",
                "请等待当前后台任务完成。",
            )
            return

        project = self.context.current_project
        if not project.video_path:
            QMessageBox.information(
                self,
                "没有源视频",
                "请先导入源视频，再开始导出。",
            )
            return

        output_dir = self.export_page.output_dir_edit.text().strip() or self.context.settings.media.default_output_dir
        export_type = str(self.export_page.export_type_combo.currentData() or "clean_zh")
        subtitle_mode = str(self.export_page.subtitle_mode_combo.currentData() or "en")
        output_container = self.export_page.output_container_combo.currentText().strip() or "mp4"
        if self.context.project_dirty:
            snapshot_path = self._auto_save_project_snapshot(
                status_message="导出前已保存工程快照",
                error_title="导出前保存工程失败",
            )
            if snapshot_path is None:
                return
        self.context.settings.export.subtitle_mode = subtitle_mode
        job_settings = AppSettings.from_dict(self.context.settings.to_dict())
        if export_type in {"en_dub", "en_dub_subtitle"}:
            job_settings = self._build_dubbing_job_settings()

        worker = ExportWorker(
            project=project,
            settings=job_settings,
            export_type=export_type,
            output_dir=output_dir,
            output_container=output_container,
            burn_subtitles=self.export_page.burn_subtitles_checkbox.isChecked(),
            export_sidecar_srt=self.export_page.sidecar_srt_checkbox.isChecked(),
            subtitle_mode=subtitle_mode,
        )
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._on_export_progress)
        worker.progress_value.connect(self._on_export_progress_value)
        worker.finished.connect(self._on_export_finished)
        worker.cancelled.connect(self._on_export_cancelled)
        worker.error.connect(self._on_export_error)
        worker.finished.connect(thread.quit)
        worker.cancelled.connect(thread.quit)
        worker.error.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        worker.cancelled.connect(worker.deleteLater)
        worker.error.connect(worker.deleteLater)
        thread.finished.connect(self._cleanup_export_job)
        thread.finished.connect(thread.deleteLater)

        self.export_thread = thread
        self.export_worker = worker
        self._set_active_background_worker(worker)
        self.export_busy = True
        self._refresh_interactive_state()
        self.statusBar().showMessage("已加入导出队列...", 0)
        thread.start()

    def apply_voice_to_selected_segments(self) -> None:
        if not self._require_idle_background("调整配音音色"):
            return
        selected_rows = self._active_dubbing_rows()
        if not selected_rows:
            QMessageBox.information(
                self,
                "没有选中配音行",
                "请先勾选片段，或先在配音表中选中一行或多行。",
            )
            return
        voice_id = self.dubbing_page.current_voice_id()
        if not voice_id:
            QMessageBox.information(
                self,
                "没有可用音色",
                "当前没有选中的英文音色。",
            )
            return

        updated_count = apply_voice_to_segments(
            self.context.current_project.segments,
            selected_rows,
            voice_id,
            invalidate_segment_dub=self._invalidate_segment_dub,
        )

        if updated_count:
            self._mark_project_dirty()
        self.refresh_views()
        if updated_count:
            self.statusBar().showMessage(f"已为 {updated_count} 个片段应用当前音色。", 5000)
        else:
            self.statusBar().showMessage("所选片段已经使用当前音色。", 4000)

    def generate_selected_dubs(self) -> None:
        if self._background_job_in_progress():
            QMessageBox.information(
                self,
                "后台任务进行中",
                "请等待当前后台任务完成。",
            )
            return
        selected_rows = self._active_dubbing_rows()
        if not selected_rows:
            QMessageBox.information(
                self,
                "没有选中配音行",
                "请先勾选片段，或先在配音表中选中一行或多行。",
            )
            return

        active_rows: list[int] = []
        for row_index in selected_rows:
            if 0 <= row_index < len(self.context.current_project.segments):
                segment = self.context.current_project.segments[row_index]
                if segment.en_text.strip():
                    active_rows.append(row_index)
        if not active_rows:
            QMessageBox.information(
                self,
                "没有可生成内容",
                "所选片段中没有可用于配音的英文文本。",
            )
            return

        selected_voice = self.dubbing_page.current_voice_id()
        if not selected_voice:
            QMessageBox.information(
                self,
                "没有可用音色",
                "当前没有选中的英文音色。",
            )
            return
        updated_voice_count = apply_voice_to_segments(
            self.context.current_project.segments,
            active_rows,
            selected_voice,
            invalidate_segment_dub=None,
        )
        if updated_voice_count:
            self._mark_project_dirty()

        self._active_dubbing_previous_statuses = {
            row_index: self.context.current_project.segments[row_index].tts_status
            for row_index in active_rows
        }
        selected_rate = self.dubbing_page.rate_spin.value()
        job_settings = self._build_dubbing_job_settings(
            selected_voice=selected_voice,
            rate=selected_rate,
        )
        for row_index in active_rows:
            self.context.current_project.segments[row_index].tts_status = "generating"
        self.refresh_views()

        worker = DubbingWorker(
            project=self.context.current_project,
            settings=job_settings,
            row_indices=active_rows,
        )
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._on_dubbing_progress)
        worker.progress_value.connect(self._on_dubbing_progress_value)
        worker.finished.connect(self._on_dubbing_finished)
        worker.cancelled.connect(self._on_dubbing_cancelled)
        worker.error.connect(self._on_dubbing_error)
        worker.finished.connect(thread.quit)
        worker.cancelled.connect(thread.quit)
        worker.error.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        worker.cancelled.connect(worker.deleteLater)
        worker.error.connect(worker.deleteLater)
        thread.finished.connect(self._cleanup_dubbing_job)
        thread.finished.connect(thread.deleteLater)

        self.dubbing_thread = thread
        self.dubbing_worker = worker
        self._set_active_background_worker(worker)
        self.dubbing_busy = True
        self._refresh_interactive_state()
        self.statusBar().showMessage("已自动应用当前音色，并加入配音生成队列...", 0)
        thread.start()

    def preview_selected_voice(self) -> None:
        if self._background_job_in_progress():
            QMessageBox.information(
                self,
                "后台任务进行中",
                "请等待当前后台任务完成。",
            )
            return
        voice_id = self.dubbing_page.current_voice_id()
        if not voice_id:
            QMessageBox.information(
                self,
                "没有可用音色",
                "当前没有选中的英文音色。",
            )
            return

        try:
            preview_text, preview_source_message = pick_voice_preview_text(
                self.context.current_project.segments,
                self.dubbing_page.selected_rows(),
                self.context.settings.tts.preview_text,
            )
        except ValueError as exc:
            QMessageBox.information(self, "没有可试听文本", str(exc))
            return

        self._pending_preview_source_message = preview_source_message
        worker = VoicePreviewWorker(
            settings=self._build_dubbing_job_settings(),
            voice_id=voice_id,
            preview_text=preview_text,
        )
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._on_dubbing_progress)
        worker.finished.connect(self._on_dubbing_preview_ready)
        worker.cancelled.connect(self._on_dubbing_cancelled)
        worker.error.connect(self._on_dubbing_error)
        worker.finished.connect(thread.quit)
        worker.cancelled.connect(thread.quit)
        worker.error.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        worker.cancelled.connect(worker.deleteLater)
        worker.error.connect(worker.deleteLater)
        thread.finished.connect(self._cleanup_dubbing_job)
        thread.finished.connect(thread.deleteLater)

        self.dubbing_thread = thread
        self.dubbing_worker = worker
        self._set_active_background_worker(worker)
        self.dubbing_busy = True
        self._refresh_interactive_state()
        self.statusBar().showMessage(f"{preview_source_message} 已加入音色试听队列...", 0)
        thread.start()

    def stop_dub_playback(self) -> None:
        if winsound is None:
            QMessageBox.information(
                self,
                "当前环境不支持播放",
                "当前系统环境不支持内置音频播放。",
            )
            return
        self._stop_audio_playback()
        self.statusBar().showMessage("已停止当前音频播放。", 4000)

    def play_generated_dub(self, row: int) -> None:
        if self._background_job_in_progress():
            self.statusBar().showMessage("请先等待当前后台任务完成。", 4000)
            return
        if not (0 <= row < len(self.context.current_project.segments)):
            return
        segment = self.context.current_project.segments[row]
        audio_path = str(segment.dub_audio_path or "").strip()
        if not audio_path:
            QMessageBox.information(
                self,
                "没有已生成配音",
                "请先为该片段生成配音，再进行试听。",
            )
            return
        if not Path(audio_path).exists():
            QMessageBox.warning(
                self,
                "配音文件不存在",
                "记录中的配音文件已经不存在，将把该片段状态重置为待处理。",
            )
            self._invalidate_segment_dub(segment)
            self.refresh_views()
            return
        self._play_audio_file(audio_path)
        self.statusBar().showMessage(f"正在播放第 {row + 1} 个片段的配音。", 5000)

    def translate_all_segments(self) -> None:
        if not self.context.current_project.segments:
            QMessageBox.information(
                self,
                "没有转写分段",
                "请先执行 ASR 分析，再翻译片段。",
            )
            return
        row_requests = self._translation_requests_for_rows(
            range(len(self.context.current_project.segments)),
            skip_completed=True,
        )
        if not row_requests:
            QMessageBox.information(
                self,
                "无需继续翻译",
                "当前全部片段都已有英文结果。你可以直接导出，或手动选中需要重译的片段。",
            )
            return
        self._start_translation_job(row_requests, "已加入全部片段翻译队列...")

    def translate_selected_segments(self) -> None:
        selected_rows = self.translation_page.selected_rows()
        if not selected_rows:
            QMessageBox.information(
                self,
                "没有选中翻译行",
                "请先在翻译表中选中一行或多行。",
            )
            return
        row_requests = self._translation_requests_for_rows(selected_rows)
        self._start_translation_job(row_requests, "已加入所选片段翻译队列...")

    def proofread_source_segments(self) -> None:
        if self._background_job_in_progress():
            QMessageBox.information(
                self,
                "后台任务进行中",
                "请等待当前后台任务完成。",
            )
            return
        if not self.context.current_project.segments:
            QMessageBox.information(
                self,
                "没有中文字幕可校正",
                "请先执行 ASR 分析或导入中文字幕，再进行文稿校正。",
            )
            return
        selected_rows = self.translation_page.selected_rows()
        target_rows = selected_rows if selected_rows else list(range(len(self.context.current_project.segments)))
        row_requests = self._translation_requests_for_rows(target_rows)
        if not row_requests:
            QMessageBox.information(
                self,
                "没有可校正内容",
                "当前选中范围里没有可校正的中文字幕。",
            )
            return

        self._clear_proofread_snapshot()
        self._last_proofread_snapshot = {
            row_index: {
                "zh_text": self.context.current_project.segments[row_index].zh_text,
                "en_text": self.context.current_project.segments[row_index].en_text,
                "translation_status": self.context.current_project.segments[row_index].translation_status,
                "tts_status": self.context.current_project.segments[row_index].tts_status,
                "dub_audio_path": self.context.current_project.segments[row_index].dub_audio_path,
                "dub_text": self.context.current_project.segments[row_index].dub_text,
                "dub_voice_id": self.context.current_project.segments[row_index].dub_voice_id,
                "dub_rate": self.context.current_project.segments[row_index].dub_rate,
            }
            for row_index, _request_item in row_requests
            if 0 <= row_index < len(self.context.current_project.segments)
        }
        self._last_proofread_project_id = self.context.current_project.project_id
        self._refresh_interactive_state()
        queued_message = (
            "已加入所选中文字幕校正队列..."
            if selected_rows
            else "已加入全部中文字幕校正队列..."
        )
        self._start_source_proofread_job(row_requests, queued_message)

    def undo_last_source_proofread(self) -> None:
        if self._background_job_in_progress():
            QMessageBox.information(
                self,
                "后台任务进行中",
                "请等待当前后台任务完成。",
            )
            return
        if not self._proofread_snapshot_has_live_changes():
            QMessageBox.information(
                self,
                "没有可恢复内容",
                "当前没有可恢复的上一次文稿校正结果。",
            )
            return

        restored_count = 0
        for row_index, snapshot in self._last_proofread_snapshot.items():
            if not (0 <= row_index < len(self.context.current_project.segments)):
                continue
            segment = self.context.current_project.segments[row_index]
            if self._segment_matches_proofread_snapshot(segment, snapshot):
                continue
            segment.zh_text = str(snapshot.get("zh_text", ""))
            segment.en_text = str(snapshot.get("en_text", ""))
            segment.translation_status = str(snapshot.get("translation_status", "pending"))
            segment.tts_status = str(snapshot.get("tts_status", "pending"))
            segment.dub_audio_path = snapshot.get("dub_audio_path")
            segment.dub_text = str(snapshot.get("dub_text", ""))
            segment.dub_voice_id = snapshot.get("dub_voice_id")
            segment.dub_rate = snapshot.get("dub_rate")
            self.translation_page.update_row_texts(
                row_index,
                zh_text=segment.zh_text,
                en_text=segment.en_text,
            )
            restored_count += 1

        self._clear_proofread_snapshot()
        if restored_count:
            self._mark_project_dirty()
            self._auto_save_project_snapshot()
            self.refresh_views()
            self.translation_page.translation_table.setFocus()
        self._refresh_interactive_state()
        self.statusBar().showMessage(f"已恢复 {restored_count} 条文稿到校正前状态。", 5000)

    def import_source_subtitles(self) -> None:
        if self._background_job_in_progress():
            QMessageBox.information(
                self,
                "后台任务进行中",
                "请等待当前后台任务完成。",
            )
            return

        subtitle_path = self._choose_subtitle_file("导入中文字幕")
        if not subtitle_path:
            return

        try:
            cues = read_subtitle_file(subtitle_path)
        except Exception as exc:
            QMessageBox.critical(self, "字幕导入失败", str(exc))
            return
        if not cues:
            QMessageBox.information(self, "没有可导入内容", "字幕文件里没有可用的字幕条目。")
            return

        imported_count = self._replace_project_segments_from_cues(cues)
        self._mark_project_dirty()
        self.timeline_page.show_workspace_tab("翻译")
        self.refresh_views()
        self.translation_page.translation_table.setFocus()
        self.statusBar().showMessage(
            f"已导入 {imported_count} 条中文字幕，并重建翻译分段。",
            6000,
        )

    def import_translation_subtitles(self) -> None:
        if self._background_job_in_progress():
            QMessageBox.information(
                self,
                "后台任务进行中",
                "请等待当前后台任务完成。",
            )
            return
        if not self.context.current_project.segments:
            QMessageBox.information(
                self,
                "还没有分段",
                "请先导入视频并识别字幕，或先导入中文字幕，再导入英文字幕。",
            )
            return

        subtitle_path = self._choose_subtitle_file("导入英文字幕")
        if not subtitle_path:
            return

        try:
            cues = read_subtitle_file(subtitle_path)
        except Exception as exc:
            QMessageBox.critical(self, "字幕导入失败", str(exc))
            return
        if not cues:
            QMessageBox.information(self, "没有可导入内容", "字幕文件里没有可用的字幕条目。")
            return

        updated_count = self._apply_translation_cues_to_segments(cues)
        if updated_count:
            self._mark_project_dirty()
        self.timeline_page.show_workspace_tab("翻译")
        self.refresh_views()
        self.translation_page.translation_table.setFocus()
        self.statusBar().showMessage(
            f"已导入 {updated_count} 条英文字幕到翻译表。",
            6000,
        )

    def export_translation_subtitles(self) -> None:
        if self._background_job_in_progress():
            QMessageBox.information(
                self,
                "后台任务进行中",
                "请等待当前后台任务完成。",
            )
            return
        if not self.context.current_project.segments:
            QMessageBox.information(
                self,
                "没有可导出内容",
                "当前还没有可导出的分段字幕。",
            )
            return

        output_dir = self.context.settings.media.default_output_dir or self.context.settings.workspace.workspace_dir
        default_name = f"{self.context.current_project.name or 'vcut_subtitles'}.srt"
        output_path, _ = QFileDialog.getSaveFileName(
            self,
            "导出当前字幕",
            str(Path(output_dir) / default_name),
            "SRT 字幕 (*.srt);;WebVTT 字幕 (*.vtt)",
        )
        if not output_path:
            return

        prefer_english = any(segment.en_text.strip() for segment in self.context.current_project.segments)
        cues: list[SubtitleCueData] = []
        for cue_index, segment in enumerate(self.context.current_project.segments, start=1):
            text = segment.en_text.strip() if prefer_english else segment.zh_text.strip()
            if not text:
                text = segment.zh_text.strip() if prefer_english else segment.en_text.strip()
            if not text:
                continue
            cues.append(
                SubtitleCueData(
                    index=cue_index,
                    start_ms=segment.start_ms,
                    end_ms=segment.end_ms,
                    text=text,
                )
            )
        if not cues:
            QMessageBox.information(
                self,
                "没有可导出内容",
                "当前分段里还没有可导出的字幕文本。",
            )
            return

        try:
            saved_path = write_subtitle_file(cues, output_path)
        except Exception as exc:
            QMessageBox.critical(self, "字幕导出失败", str(exc))
            return

        self.statusBar().showMessage(f"字幕已导出：{saved_path}", 6000)

    def test_translation_config(self) -> None:
        if self._background_job_in_progress():
            QMessageBox.information(
                self,
                "后台任务进行中",
                "请等待当前后台任务完成。",
            )
            return

        worker = TranslationTestWorker(self.context.settings)
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._on_translation_progress)
        worker.finished.connect(self._on_translation_test_finished)
        worker.error.connect(self._on_translation_error)
        worker.finished.connect(thread.quit)
        worker.error.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        worker.error.connect(worker.deleteLater)
        thread.finished.connect(self._cleanup_translation_job)
        thread.finished.connect(thread.deleteLater)

        self.translation_thread = thread
        self.translation_worker = worker
        self._set_active_background_worker(worker)
        self.translation_busy = True
        self._refresh_interactive_state()
        self.statusBar().showMessage("已加入翻译配置测试队列...", 0)
        thread.start()

    def save_settings(self) -> None:
        if self._background_job_in_progress():
            QMessageBox.information(
                self,
                "后台任务进行中",
                "请等待当前后台任务完成后再保存设置。",
            )
            return
        previous_tts_signature = self._tts_cache_signature(self.context.settings)
        try:
            new_settings = self.settings_page.build_settings()
            settings_path = self.context.settings_store.save(new_settings)
        except Exception as exc:
            QMessageBox.critical(self, "保存设置失败", str(exc))
            return
        self.context.settings = new_settings
        self._configure_autosave_timer()
        if self._tts_cache_signature(new_settings) != previous_tts_signature:
            self._invalidate_all_dubs()
        self.refresh_views()
        self.statusBar().showMessage(f"已保存设置：{settings_path}", 5000)

    def reset_settings(self) -> None:
        if self._background_job_in_progress():
            QMessageBox.information(
                self,
                "后台任务进行中",
                "请等待当前后台任务完成后再恢复默认设置。",
            )
            return
        self.context.settings = AppSettings()
        self._configure_autosave_timer()
        self._invalidate_all_dubs()
        self.settings_page.load_settings(self.context.settings)
        self.refresh_views()
        self.statusBar().showMessage("已将设置表单恢复为默认值。", 5000)

    def scan_dependencies(self) -> None:
        rows = self._dependency_rows()
        summary = "\n".join(f"{name}：{status} | {detail}" for name, status, detail in rows)
        QMessageBox.information(self, "依赖状态", summary)
        self.statusBar().showMessage("依赖扫描完成。", 5000)

    def generate_cut_suggestions_for_project(self) -> None:
        if self._background_job_in_progress():
            self.statusBar().showMessage("请先等待当前后台任务完成。", 4000)
            return
        project = self.context.current_project
        if not project.segments and not project.analysis_completed:
            QMessageBox.information(
                self,
                "还没有识别分段",
                "请先执行“识别字幕 / 分段”，再生成删除建议。",
            )
            return

        total_duration_ms = int(
            project.source_duration_ms
            or project.media_info.duration_ms
            or max((segment.end_ms for segment in project.segments), default=0)
        )
        project.cut_suggestions = generate_cut_suggestions(
            project.segments,
            self.context.settings.cutting,
            total_duration_ms=total_duration_ms,
        )
        self._mark_project_dirty()
        self.refresh_views()
        self.timeline_page.show_workspace_tab("建议删除")
        self.statusBar().showMessage(
            f"已生成 {len(project.cut_suggestions)} 条删除建议。翻译功能不受影响，可以单独继续。",
            6000,
        )

    def accept_selected_suggestions(self) -> None:
        if self._background_job_in_progress():
            self.statusBar().showMessage("请先等待当前后台任务完成。", 4000)
            return
        selected_rows = self.timeline_page.selected_suggestion_rows()
        if not selected_rows:
            self.statusBar().showMessage("请先选中要勾选的删除建议。", 4000)
            return
        self._set_suggestion_rows_acceptance(selected_rows, True)
        self.statusBar().showMessage(f"已勾选 {len(selected_rows)} 条删除建议。", 5000)

    def clear_selected_suggestions(self) -> None:
        if self._background_job_in_progress():
            self.statusBar().showMessage("请先等待当前后台任务完成。", 4000)
            return
        selected_rows = self.timeline_page.selected_suggestion_rows()
        if not selected_rows:
            self.statusBar().showMessage("请先选中要取消的删除建议。", 4000)
            return
        self._set_suggestion_rows_acceptance(selected_rows, False)
        self.statusBar().showMessage(f"已取消 {len(selected_rows)} 条删除建议的勾选。", 5000)

    def accept_all_suggestions(self) -> None:
        if self._background_job_in_progress():
            self.statusBar().showMessage("请先等待当前后台任务完成。", 4000)
            return
        if not self.context.current_project.cut_suggestions:
            self.statusBar().showMessage("当前没有可接受的剪辑建议。", 4000)
            return
        self._set_suggestion_rows_acceptance(
            list(range(len(self.context.current_project.cut_suggestions))),
            True,
        )
        self.statusBar().showMessage(
            f"已接受 {len(self.context.current_project.cut_suggestions)} 条剪辑建议。",
            5000,
        )

    def clear_suggestion_acceptance(self) -> None:
        if self._background_job_in_progress():
            self.statusBar().showMessage("请先等待当前后台任务完成。", 4000)
            return
        if not self.context.current_project.cut_suggestions:
            self.statusBar().showMessage("当前没有可清空的剪辑建议。", 4000)
            return
        self._set_suggestion_rows_acceptance(
            list(range(len(self.context.current_project.cut_suggestions))),
            False,
        )
        self.statusBar().showMessage("已清空剪辑建议的接受标记。", 5000)

    def set_suggestion_acceptance(self, row: int, accepted: bool) -> None:
        if self._background_job_in_progress():
            self.statusBar().showMessage("请先等待当前后台任务完成。", 4000)
            return
        suggestions = self.context.current_project.cut_suggestions
        if row < 0 or row >= len(suggestions):
            return
        suggestions[row].accepted = accepted
        self._mark_project_dirty()
        self.refresh_views()
        state = "已接受" if accepted else "已取消"
        self.statusBar().showMessage(f"第 {row + 1} 条建议{state}。", 4000)

    def _set_suggestion_rows_acceptance(self, rows: list[int], accepted: bool) -> None:
        suggestions = self.context.current_project.cut_suggestions
        changed = False
        for row in rows:
            if 0 <= row < len(suggestions) and suggestions[row].accepted != accepted:
                suggestions[row].accepted = accepted
                changed = True
        if changed:
            self._mark_project_dirty()
            self.refresh_views()

    def update_suggestion_range(self, row: int, start_ms: int, end_ms: int) -> None:
        if self._background_job_in_progress():
            self.statusBar().showMessage("请先等待当前后台任务完成。", 4000)
            return
        suggestions = self.context.current_project.cut_suggestions
        if not (0 <= row < len(suggestions)):
            return
        total_duration_ms = max(
            0,
            int(
                self.context.current_project.source_duration_ms
                or self.context.current_project.media_info.duration_ms
                or 0
            ),
        )
        if total_duration_ms > 0:
            start_ms = min(max(0, start_ms), total_duration_ms)
            end_ms = min(max(0, end_ms), total_duration_ms)
        if end_ms <= start_ms:
            QMessageBox.information(self, "范围时间无效", "结束时间必须大于开始时间。")
            return
        suggestion = suggestions[row]
        suggestion.start_ms = start_ms
        suggestion.end_ms = end_ms
        self._mark_project_dirty()
        self.refresh_views()
        self.timeline_page.show_workspace_tab("建议删除")
        self.timeline_page.set_selected_suggestion_range(row, start_ms, end_ms)
        self.timeline_page.set_focus_range(start_ms, end_ms)
        self.seek_preview(start_ms)
        self.statusBar().showMessage(f"已更新第 {row + 1} 条建议的范围时间。", 5000)

    def locate_selected_suggestion(self, rows: list[int]) -> None:
        if not rows:
            selected_segments = self.timeline_page.selected_segment_rows()
            if selected_segments:
                self.locate_selected_segment(selected_segments)
            else:
                self.timeline_page.set_focus_range(None, None)
                self.timeline_page.clear_selected_suggestion_range()
            return
        suggestions = self.context.current_project.cut_suggestions
        row = rows[0]
        if not (0 <= row < len(suggestions)):
            return
        suggestion = suggestions[row]
        self.timeline_page.set_selected_suggestion_range(row, suggestion.start_ms, suggestion.end_ms)
        self.timeline_page.set_focus_range(suggestion.start_ms, suggestion.end_ms)
        self.seek_preview(suggestion.start_ms)

    def locate_selected_segment(self, rows: list[int]) -> None:
        if not rows:
            if not self.timeline_page.selected_suggestion_rows():
                self.timeline_page.set_focus_range(None, None)
            return
        segments = self.context.current_project.segments
        row = rows[0]
        if not (0 <= row < len(segments)):
            return
        segment = segments[row]
        self.timeline_page.set_focus_range(segment.start_ms, segment.end_ms)
        self.seek_preview(segment.start_ms)

    def preview_translation_time(self, row: int, column: int) -> None:
        segments = self.context.current_project.segments
        if not (0 <= row < len(segments)):
            return
        if not self.context.current_project.video_path:
            self.statusBar().showMessage("当前项目还没有源视频，无法跳转预览。", 4000)
            return
        segment = segments[row]
        position_ms = segment.start_ms if column == 1 else segment.end_ms
        self.timeline_page.set_focus_range(segment.start_ms, segment.end_ms)
        self.seek_preview(position_ms)
        self.statusBar().showMessage(
            f"已跳转到第 {row + 1} 个分段的{'开始' if column == 1 else '结束'}时间。",
            3000,
        )

    def update_segment_english_text(self, row: int, text: str) -> None:
        segments = self.context.current_project.segments
        if row < 0 or row >= len(segments):
            return
        segments[row].en_text = text.strip()
        segments[row].translation_status = "edited" if text.strip() else "pending"
        self._invalidate_segment_dub(segments[row])
        self._clear_proofread_snapshot_if_rows_touched([row])
        self._mark_project_dirty()
        self.timeline_page.load_project(self.context.current_project)
        self.dubbing_page.load_project(self.context.current_project, self.context.settings)
        self.export_page.load_project(self.context.current_project, self.context.settings)
        self._refresh_interactive_state()
        self.statusBar().showMessage(f"已更新第 {row + 1} 个分段的英文文本。", 3000)

    def update_segment_chinese_text(self, row: int, text: str) -> None:
        segments = self.context.current_project.segments
        if row < 0 or row >= len(segments):
            return
        normalized_text = text.strip()
        segment = segments[row]
        previous_text = segment.zh_text.strip()
        if normalized_text == previous_text:
            return

        segment.zh_text = normalized_text
        segment.translation_status = "pending"
        segment.en_text = ""
        self._invalidate_segment_dub(segment)
        self._clear_proofread_snapshot_if_rows_touched([row])
        self._mark_project_dirty()

        self.translation_page.clear_english_text(row)

        self.timeline_page.load_project(self.context.current_project)
        self.dubbing_page.load_project(self.context.current_project, self.context.settings)
        self.export_page.load_project(self.context.current_project, self.context.settings)
        self._refresh_interactive_state()
        self.statusBar().showMessage(f"已更新第 {row + 1} 个分段的中文文本，并清空旧翻译。", 4000)

    def _choose_subtitle_file(self, title: str) -> str:
        workspace_dir = self.context.settings.workspace.workspace_dir
        filename, _ = QFileDialog.getOpenFileName(
            self,
            title,
            workspace_dir,
            "字幕文件 (*.srt *.vtt);;SRT 字幕 (*.srt);;WebVTT 字幕 (*.vtt);;所有文件 (*.*)",
        )
        return filename

    def _replace_project_segments_from_cues(self, cues: list[SubtitleCueData]) -> int:
        project = self.context.current_project
        existing_segments = list(project.segments)
        default_voice = self.context.settings.tts.default_voice or None
        rebuilt_segments: list[Segment] = []
        for cue_index, cue in enumerate(sorted(cues, key=lambda item: (item.start_ms, item.end_ms))):
            previous_segment = existing_segments[cue_index] if cue_index < len(existing_segments) else None
            rebuilt_segments.append(
                Segment(
                    segment_id=previous_segment.segment_id if previous_segment is not None else uuid4().hex,
                    start_ms=cue.start_ms,
                    end_ms=cue.end_ms,
                    zh_text=cue.text.strip(),
                    en_text="",
                    keep=True,
                    dub_selected=previous_segment.dub_selected if previous_segment is not None else False,
                    translation_status="pending",
                    tts_status="pending",
                    voice_id=(
                        previous_segment.voice_id
                        if previous_segment is not None and previous_segment.voice_id
                        else default_voice
                    ),
                )
            )
        project.segments = rebuilt_segments
        project.cut_suggestions = []
        self._clear_proofread_snapshot()

        latest_end_ms = max((cue.end_ms for cue in cues), default=0)
        if not project.video_path and latest_end_ms > 0:
            project.source_duration_ms = max(project.source_duration_ms, latest_end_ms)
            if project.media_info.duration_ms <= 0:
                project.media_info.duration_ms = project.source_duration_ms
        return len(rebuilt_segments)

    def _apply_translation_cues_to_segments(self, cues: list[SubtitleCueData]) -> int:
        segments = self.context.current_project.segments
        segment_rows = sorted(
            range(len(segments)),
            key=lambda row_index: (segments[row_index].start_ms, segments[row_index].end_ms),
        )
        ordered_cues = sorted(cues, key=lambda item: (item.start_ms, item.end_ms))
        updated_count = 0
        updated_rows: set[int] = set()

        for row_index, cue in zip(segment_rows, ordered_cues):
            segment = segments[row_index]
            new_text = cue.text.strip()
            if segment.en_text.strip() == new_text and segment.translation_status == "edited":
                continue
            segment.en_text = new_text
            segment.translation_status = "edited" if new_text else "pending"
            self._invalidate_segment_dub(segment)
            updated_count += 1
            updated_rows.add(row_index)

        if updated_rows:
            self._clear_proofread_snapshot_if_rows_touched(updated_rows)

        return updated_count

    def _active_dubbing_rows(self) -> list[int]:
        checked_rows = self.dubbing_page.checked_rows()
        if checked_rows:
            return checked_rows
        return self.dubbing_page.selected_rows()

    def set_dub_row_checked(self, row: int, checked: bool) -> None:
        segments = self.context.current_project.segments
        if row < 0 or row >= len(segments):
            return
        if segments[row].dub_selected == checked:
            return
        segments[row].dub_selected = checked
        self._mark_project_dirty()
        self.refresh_views()

    def _set_dub_rows_checked(self, rows: list[int], checked: bool) -> int:
        segments = self.context.current_project.segments
        changed_count = 0
        for row in rows:
            if 0 <= row < len(segments) and segments[row].dub_selected != checked:
                segments[row].dub_selected = checked
                changed_count += 1
        if changed_count:
            self._mark_project_dirty()
            self.refresh_views()
        return changed_count

    def check_selected_dub_rows(self) -> None:
        changed_count = self._set_dub_rows_checked(self.dubbing_page.selected_rows(), True)
        if changed_count:
            self.statusBar().showMessage(f"已勾选 {changed_count} 个配音片段。", 4000)

    def uncheck_selected_dub_rows(self) -> None:
        changed_count = self._set_dub_rows_checked(self.dubbing_page.selected_rows(), False)
        if changed_count:
            self.statusBar().showMessage(f"已取消 {changed_count} 个配音片段的勾选。", 4000)

    def check_all_dub_rows(self) -> None:
        changed_count = self._set_dub_rows_checked(
            list(range(len(self.context.current_project.segments))),
            True,
        )
        if changed_count:
            self.statusBar().showMessage(f"已全部勾选 {changed_count} 个配音片段。", 4000)

    def uncheck_all_dub_rows(self) -> None:
        changed_count = self._set_dub_rows_checked(
            list(range(len(self.context.current_project.segments))),
            False,
        )
        if changed_count:
            self.statusBar().showMessage("已清空配音片段勾选。", 4000)

    def _build_dubbing_job_settings(
        self,
        selected_voice: str | None = None,
        rate: float | None = None,
    ) -> AppSettings:
        selected_voice = (
            selected_voice
            if selected_voice is not None
            else self.dubbing_page.current_voice_id()
        )
        if selected_voice:
            self.context.settings.tts.default_voice = selected_voice
        self.context.settings.tts.rate = (
            self.dubbing_page.rate_spin.value()
            if rate is None
            else float(rate)
        )
        return AppSettings.from_dict(self.context.settings.to_dict())

    def _tts_cache_signature(self, settings: AppSettings) -> tuple[str, float]:
        return (
            settings.tts.provider_type,
            round(float(settings.tts.rate), 3),
        )

    def _invalidate_segment_dub(self, segment: object) -> None:
        setattr(segment, "tts_status", "pending")
        setattr(segment, "dub_audio_path", None)
        setattr(segment, "dub_text", "")
        setattr(segment, "dub_voice_id", None)
        setattr(segment, "dub_rate", None)

    def _invalidate_all_dubs(self) -> None:
        for segment in self.context.current_project.segments:
            self._invalidate_segment_dub(segment)

    def _play_audio_file(self, path: str) -> None:
        if winsound is None:
            QMessageBox.information(
                self,
                "当前环境不支持播放",
                "当前系统环境不支持内置音频播放。",
            )
            return
        winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)

    def _stop_audio_playback(self) -> None:
        if winsound is None:
            return
        winsound.PlaySound(None, 0)

    def _analysis_in_progress(self) -> bool:
        return self.analysis_busy

    def _translation_in_progress(self) -> bool:
        return self.translation_busy

    def _dubbing_in_progress(self) -> bool:
        return self.dubbing_busy

    def _export_in_progress(self) -> bool:
        return self.export_busy

    def _background_job_in_progress(self) -> bool:
        return (
            self._analysis_in_progress()
            or self._translation_in_progress()
            or self._dubbing_in_progress()
            or self._export_in_progress()
        )

    def _require_idle_background(self, action_text: str) -> bool:
        if not self._background_job_in_progress():
            return True
        QMessageBox.information(
            self,
            "后台任务进行中",
            f"请等待当前后台任务完成后再{action_text}。",
        )
        return False

    def _refresh_interactive_state(self) -> None:
        background_busy = self._background_job_in_progress()
        project = self.context.current_project
        active_worker = self._active_background_worker if background_busy else None

        if active_worker is None:
            self.background_task_label.setText("后台任务：空闲")
            self.pause_task_button.setEnabled(False)
            self.resume_task_button.setEnabled(False)
            self.cancel_task_button.setEnabled(False)
        else:
            if active_worker.is_cancel_requested():
                state_text = "正在终止"
            elif active_worker.is_paused():
                state_text = "已暂停"
            else:
                state_text = "运行中"
            self.background_task_label.setText(
                f"后台任务：{active_worker.task_label}（{state_text}）"
            )
            self.pause_task_button.setEnabled(
                active_worker.supports_pause()
                and not active_worker.is_paused()
                and not active_worker.is_cancel_requested()
            )
            self.resume_task_button.setEnabled(
                active_worker.supports_pause()
                and active_worker.is_paused()
                and not active_worker.is_cancel_requested()
            )
            self.cancel_task_button.setEnabled(
                active_worker.supports_cancel()
                and not active_worker.is_cancel_requested()
            )

        self.settings_page.top_save_button.setEnabled(not background_busy)
        self.settings_page.top_reset_button.setEnabled(not background_busy)
        self.settings_page.asr_runtime_check_button.setEnabled(not background_busy)

        self.timeline_page.set_analysis_running(self._analysis_in_progress())
        self.timeline_page.set_preview_enabled(bool(project.video_path))
        if not self._analysis_in_progress():
            has_segments = bool(project.segments)
            has_suggestions = bool(project.cut_suggestions)
            selected_suggestion_rows = self.timeline_page.selected_suggestion_rows()
            self.timeline_page.analyze_button.setEnabled(bool(project.video_path) and not background_busy)
            self.timeline_page.generate_suggestions_button.setEnabled(has_segments and not background_busy)
            self.timeline_page.accept_selected_button.setEnabled(
                bool(selected_suggestion_rows) and not background_busy
            )
            self.timeline_page.clear_selected_button.setEnabled(
                bool(selected_suggestion_rows) and not background_busy
            )
            self.timeline_page.accept_all_button.setEnabled(has_suggestions and not background_busy)
            self.timeline_page.reject_all_button.setEnabled(has_suggestions and not background_busy)
            has_single_suggestion = len(selected_suggestion_rows) == 1
            self.timeline_page.suggestion_start_time_edit.setEnabled(has_single_suggestion and not background_busy)
            self.timeline_page.suggestion_end_time_edit.setEnabled(has_single_suggestion and not background_busy)
            self.timeline_page.use_current_as_start_button.setEnabled(has_single_suggestion and not background_busy)
            self.timeline_page.use_current_as_end_button.setEnabled(has_single_suggestion and not background_busy)
            self.timeline_page.apply_suggestion_range_button.setEnabled(has_single_suggestion and not background_busy)

        self.translation_page.set_translation_running(self._translation_in_progress())
        self.translation_page.set_proofread_undo_available(
            self._proofread_snapshot_has_live_changes() and not background_busy
        )
        if not self._translation_in_progress():
            has_segments = bool(project.segments)
            self.translation_page.translate_all_button.setEnabled(has_segments and not background_busy)
            selected_rows = self.translation_page.selected_rows()
            self.translation_page.translate_selected_button.setEnabled(bool(selected_rows) and not background_busy)
            self.translation_page.proofread_source_button.setEnabled(has_segments and not background_busy)
            self.translation_page.import_source_subtitle_button.setEnabled(not background_busy)
            self.translation_page.import_translation_subtitle_button.setEnabled(has_segments and not background_busy)
            self.translation_page.export_subtitle_button.setEnabled(has_segments and not background_busy)
            self.translation_page.save_button.setEnabled(not background_busy)
            self.translation_page.test_translation_button.setEnabled(not background_busy)

        self.dubbing_page.set_dubbing_running(self._dubbing_in_progress())
        self.dubbing_page.stop_button.setEnabled(winsound is not None)
        if not self._dubbing_in_progress():
            selected_rows = self._active_dubbing_rows()
            selected_table_rows = self.dubbing_page.selected_rows()
            has_segments = bool(project.segments)
            self.dubbing_page.apply_voice_button.setEnabled(bool(selected_rows) and not background_busy)
            self.dubbing_page.generate_button.setEnabled(bool(selected_rows) and not background_busy)
            self.dubbing_page.preview_button.setEnabled(not background_busy)
            self.dubbing_page.check_selected_button.setEnabled(bool(selected_table_rows) and not background_busy)
            self.dubbing_page.uncheck_selected_button.setEnabled(bool(selected_table_rows) and not background_busy)
            self.dubbing_page.check_all_button.setEnabled(has_segments and not background_busy)
            self.dubbing_page.uncheck_all_button.setEnabled(has_segments and not background_busy)

        self.export_page.set_export_running(self._export_in_progress())
        if not self._export_in_progress():
            self.export_page.export_button.setEnabled(
                bool(project.video_path)
                and not self._analysis_in_progress()
                and not self._translation_in_progress()
                and not self._dubbing_in_progress()
            )

    def _on_analysis_progress(self, message: str) -> None:
        self.statusBar().showMessage(message, 0)

    def _on_analysis_progress_value(self, progress: int, message: str) -> None:
        self.timeline_page.set_analysis_progress(progress, message)
        self._set_task_progress(progress, message)

    def _on_analysis_finished(self, result: AnalysisResult) -> None:
        project = self.context.current_project
        project.segments = result.segments
        project.analysis_completed = True
        project.cut_suggestions = []
        self._clear_proofread_snapshot()
        self._mark_project_dirty()
        self.analysis_busy = False
        self._set_active_background_worker(None)
        self._refresh_interactive_state()
        self.refresh_views()
        self.timeline_page.show_workspace_tab("识别")
        self.timeline_page.segments_table.setFocus()
        self._clear_task_progress()
        if result.summary.segment_count > 0:
            message = (
                f"字幕识别已完成：共生成 {result.summary.segment_count} 个分段。\n"
                f"本次视频平均置信度参考值：{result.summary.average_confidence:.2f}\n"
            )
            if result.summary.filtered_segment_count > 0:
                message += f"已按最低置信度过滤 {result.summary.filtered_segment_count} 个低质量分段。\n"
            message += "参考：0.85 以上通常较稳，0.70-0.85 建议抽查，低于 0.70 建议重试或提高阈值。"
        else:
            message = (
                "字幕识别已完成，但没有检测到可用语音分段。\n"
                f"本次视频平均置信度参考值：{result.summary.average_confidence:.2f}\n"
                "如果视频前后是长时间空白，现在可以直接生成无声/无人声删除建议。"
            )
        self.statusBar().showMessage(message, 8000)
        self._notify_task_finished("字幕识别完成", message)

    def _on_analysis_error(self, message: str) -> None:
        self.analysis_busy = False
        self._set_active_background_worker(None)
        self._refresh_interactive_state()
        self._clear_task_progress()
        QMessageBox.critical(self, "字幕识别失败", message)
        self.statusBar().showMessage("字幕识别失败。", 8000)

    def _on_analysis_cancelled(self, message: str) -> None:
        self.analysis_busy = False
        self._set_active_background_worker(None)
        self._refresh_interactive_state()
        self._clear_task_progress()
        self.statusBar().showMessage(message or "已终止字幕识别任务。", 8000)

    def _cleanup_analysis_job(self) -> None:
        self.analysis_thread = None
        self.analysis_worker = None

    def _on_export_progress(self, message: str) -> None:
        self.statusBar().showMessage(message, 0)

    def _on_export_progress_value(self, progress: int, message: str) -> None:
        self.export_page.set_export_progress(progress, message)
        self._set_task_progress(progress, message)

    def _on_translation_progress(self, message: str) -> None:
        self.statusBar().showMessage(message, 0)

    def _on_translation_progress_value(self, progress: int, message: str) -> None:
        self.translation_page.set_translation_progress(progress, message)
        self._set_task_progress(progress, message)

    def _on_dubbing_progress(self, message: str) -> None:
        self.statusBar().showMessage(message, 0)

    def _on_dubbing_progress_value(self, progress: int, message: str) -> None:
        self.dubbing_page.set_dubbing_progress(progress, message)
        self._set_task_progress(progress, message)

    def _on_export_finished(self, result: ExportResult) -> None:
        self.export_busy = False
        self._set_active_background_worker(None)
        self.context.current_project.exports.append(
            ExportJob(
                export_id=uuid4().hex,
                export_type=result.export_type,
                output_path=str(result.plan.output_path),
                status="completed",
            )
        )
        self._mark_project_dirty()
        self._refresh_interactive_state()
        self.refresh_views()
        self.export_page.export_button.setFocus()
        self._clear_task_progress()
        QMessageBox.information(
            self,
            "导出完成",
            f"视频已经导出完成。\n\n输出文件：\n{result.plan.output_path}",
        )
        self.statusBar().showMessage(
            f"导出完成：{result.plan.output_path}",
            10000,
        )

    def _on_export_error(self, message: str) -> None:
        self.export_busy = False
        self._set_active_background_worker(None)
        self._refresh_interactive_state()
        self._clear_task_progress()
        QMessageBox.critical(self, "导出失败", message)
        self.statusBar().showMessage("导出失败。", 8000)

    def _on_export_cancelled(self, message: str) -> None:
        self.export_busy = False
        self._set_active_background_worker(None)
        self._refresh_interactive_state()
        self._clear_task_progress()
        self.statusBar().showMessage(message or "已终止导出任务。", 8000)

    def _on_dubbing_finished(self, result: DubbingJobResult) -> None:
        updated_count = 0
        updated_rows: set[int] = set()
        for item in result.items:
            if 0 <= item.row_index < len(self.context.current_project.segments):
                segment = self.context.current_project.segments[item.row_index]
                segment.voice_id = item.voice_id
                segment.tts_status = "completed"
                segment.dub_audio_path = item.output_path
                segment.dub_text = item.text
                segment.dub_voice_id = item.voice_id
                segment.dub_rate = item.rate
                updated_count += 1
                updated_rows.add(item.row_index)
        for row_index, previous_status in self._active_dubbing_previous_statuses.items():
            if row_index in updated_rows:
                continue
            if 0 <= row_index < len(self.context.current_project.segments):
                self.context.current_project.segments[row_index].tts_status = previous_status
        self._active_dubbing_previous_statuses = {}
        if updated_count:
            self._mark_project_dirty()
        self.dubbing_busy = False
        self._set_active_background_worker(None)
        self._refresh_interactive_state()
        self.refresh_views()
        self.dubbing_page.dubbing_table.setFocus()
        self._clear_task_progress()
        self.statusBar().showMessage(f"配音生成完成：已更新 {updated_count} 个片段。", 8000)
        self._notify_task_finished(
            "配音生成完成",
            f"英文配音已经生成完成。\n\n已更新片段数：{updated_count}",
        )

    def _on_dubbing_preview_ready(self, result: VoicePreviewResult) -> None:
        self.dubbing_busy = False
        self._set_active_background_worker(None)
        self._refresh_interactive_state()
        self._clear_task_progress()
        self._play_audio_file(result.output_path)
        preview_source_message = self._pending_preview_source_message
        self._pending_preview_source_message = ""
        if preview_source_message:
            self.statusBar().showMessage(f"{preview_source_message} 音色试听已开始播放。", 6000)
        else:
            self.statusBar().showMessage("音色试听已开始播放。", 6000)

    def _on_dubbing_error(self, message: str) -> None:
        for row_index, previous_status in self._active_dubbing_previous_statuses.items():
            if 0 <= row_index < len(self.context.current_project.segments):
                self.context.current_project.segments[row_index].tts_status = previous_status
        self._active_dubbing_previous_statuses = {}
        self._pending_preview_source_message = ""
        self.dubbing_busy = False
        self._set_active_background_worker(None)
        self._refresh_interactive_state()
        self.refresh_views()
        self._clear_task_progress()
        QMessageBox.critical(self, "配音失败", message)
        self.statusBar().showMessage("配音任务失败。", 8000)

    def _on_dubbing_cancelled(self, message: str) -> None:
        for row_index, previous_status in self._active_dubbing_previous_statuses.items():
            if 0 <= row_index < len(self.context.current_project.segments):
                self.context.current_project.segments[row_index].tts_status = previous_status
        self._active_dubbing_previous_statuses = {}
        self._pending_preview_source_message = ""
        self.dubbing_busy = False
        self._set_active_background_worker(None)
        self._refresh_interactive_state()
        self.refresh_views()
        self._clear_task_progress()
        self.statusBar().showMessage(message or "已终止配音任务。", 8000)

    def _on_translation_finished(self, result: TranslationJobResult) -> None:
        self.translation_busy = False
        self._set_active_background_worker(None)
        self._refresh_interactive_state()
        self.refresh_views()
        self.translation_page.translation_table.setFocus()
        self._clear_task_progress()
        self.statusBar().showMessage(
            f"翻译完成：已更新 {len(result.items)} 个分段。",
            8000,
        )
        self._notify_task_finished(
            "翻译完成",
            f"英文翻译已经完成。\n\n已更新分段数：{len(result.items)}",
        )

    def _on_translation_test_finished(self, sample: str) -> None:
        self.translation_busy = False
        self._set_active_background_worker(None)
        self._refresh_interactive_state()
        self._clear_task_progress()
        QMessageBox.information(
            self,
            "翻译配置正常",
            f"连接测试成功。\n\n示例翻译：\n{sample}",
        )
        self.statusBar().showMessage("翻译提供方连接测试成功。", 8000)

    def _on_translation_error(self, message: str) -> None:
        self.translation_busy = False
        self._set_active_background_worker(None)
        self._refresh_interactive_state()
        self._clear_task_progress()
        QMessageBox.critical(self, "翻译失败", message)
        self.statusBar().showMessage("翻译任务失败。", 8000)

    def _on_translation_cancelled(self, message: str) -> None:
        self.translation_busy = False
        self._set_active_background_worker(None)
        self._refresh_interactive_state()
        self._clear_task_progress()
        self.statusBar().showMessage(message or "已终止翻译任务。", 8000)

    def _cleanup_export_job(self) -> None:
        self.export_thread = None
        self.export_worker = None

    def _cleanup_dubbing_job(self) -> None:
        self.dubbing_thread = None
        self.dubbing_worker = None

    def _cleanup_translation_job(self) -> None:
        self.translation_thread = None
        self.translation_worker = None

    def _translation_requests_for_rows(
        self,
        rows: range | list[int],
        *,
        skip_completed: bool = False,
    ) -> list[tuple[int, TranslationRequest]]:
        requests: list[tuple[int, TranslationRequest]] = []
        for row_index in rows:
            if not (0 <= row_index < len(self.context.current_project.segments)):
                continue
            segment = self.context.current_project.segments[row_index]
            source_text = segment.zh_text.strip()
            if not source_text:
                continue
            if skip_completed and segment.en_text.strip():
                continue
            requests.append(
                (
                    row_index,
                    TranslationRequest(
                        segment_id=segment.segment_id,
                        source_text=source_text,
                    ),
                )
            )
        return requests

    def _start_translation_job(
        self,
        row_requests: list[tuple[int, TranslationRequest]],
        queued_message: str,
    ) -> None:
        if self._background_job_in_progress():
            QMessageBox.information(
                self,
                "后台任务进行中",
                "请等待当前后台任务完成。",
            )
            return
        if not row_requests:
            QMessageBox.information(
                self,
                "没有可翻译内容",
                "所选行中没有可翻译的中文文本。",
            )
            return

        worker = TranslationWorker(self.context.settings, row_requests)
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._on_translation_progress)
        worker.progress_value.connect(self._on_translation_progress_value)
        worker.partial_results.connect(self._on_translation_partial_results)
        worker.finished.connect(self._on_translation_finished)
        worker.cancelled.connect(self._on_translation_cancelled)
        worker.error.connect(self._on_translation_error)
        worker.finished.connect(thread.quit)
        worker.cancelled.connect(thread.quit)
        worker.error.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        worker.cancelled.connect(worker.deleteLater)
        worker.error.connect(worker.deleteLater)
        thread.finished.connect(self._cleanup_translation_job)
        thread.finished.connect(thread.deleteLater)

        self.translation_thread = thread
        self.translation_worker = worker
        self._set_active_background_worker(worker)
        self.translation_busy = True
        self._refresh_interactive_state()
        self.statusBar().showMessage(queued_message, 0)
        thread.start()

    def _start_source_proofread_job(
        self,
        row_requests: list[tuple[int, TranslationRequest]],
        queued_message: str,
    ) -> None:
        if self._background_job_in_progress():
            QMessageBox.information(
                self,
                "后台任务进行中",
                "请等待当前后台任务完成。",
            )
            return
        if not row_requests:
            QMessageBox.information(
                self,
                "没有可校正内容",
                "当前没有可校正的中文字幕。",
            )
            return

        worker = SourceProofreadWorker(self.context.settings, row_requests)
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._on_translation_progress)
        worker.progress_value.connect(self._on_translation_progress_value)
        worker.partial_results.connect(self._on_source_proofread_partial_results)
        worker.finished.connect(self._on_source_proofread_finished)
        worker.cancelled.connect(self._on_translation_cancelled)
        worker.error.connect(self._on_translation_error)
        worker.finished.connect(thread.quit)
        worker.cancelled.connect(thread.quit)
        worker.error.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        worker.cancelled.connect(worker.deleteLater)
        worker.error.connect(worker.deleteLater)
        thread.finished.connect(self._cleanup_translation_job)
        thread.finished.connect(thread.deleteLater)

        self.translation_thread = thread
        self.translation_worker = worker
        self._set_active_background_worker(worker)
        self.translation_busy = True
        self._refresh_interactive_state()
        self.statusBar().showMessage(queued_message, 0)
        thread.start()

    def _apply_source_corrections(self, items: list[tuple[int, str]]) -> int:
        changed_count = 0
        for row_index, corrected_text in items:
            if not (0 <= row_index < len(self.context.current_project.segments)):
                continue
            normalized_text = corrected_text.strip()
            segment = self.context.current_project.segments[row_index]
            if segment.zh_text.strip() == normalized_text:
                continue
            segment.zh_text = normalized_text
            segment.en_text = ""
            segment.translation_status = "pending"
            self._invalidate_segment_dub(segment)
            self.translation_page.update_row_texts(row_index, zh_text=normalized_text, en_text="")
            changed_count += 1
        if changed_count:
            self._mark_project_dirty()
            self._auto_save_project_snapshot(allow_during_background=True)
            self.refresh_views()
        return changed_count

    def _apply_translation_batch(self, items: list[tuple[int, object]]) -> int:
        updated_count = 0
        updated_rows: set[int] = set()
        for row_index, translation_result in items:
            if not (0 <= row_index < len(self.context.current_project.segments)):
                continue
            translated_text = str(getattr(translation_result, "translated_text", "")).strip()
            if not translated_text:
                continue
            segment = self.context.current_project.segments[row_index]
            if segment.en_text.strip() == translated_text and segment.translation_status == "translated":
                continue
            segment.en_text = translated_text
            segment.translation_status = "translated"
            self._invalidate_segment_dub(segment)
            self.translation_page.update_row_texts(row_index, en_text=translated_text)
            updated_count += 1
            updated_rows.add(row_index)
        if updated_count:
            self._clear_proofread_snapshot_if_rows_touched(updated_rows)
            self._mark_project_dirty()
            self._auto_save_project_snapshot(allow_during_background=True)
            self.refresh_views()
        return updated_count

    def _on_source_proofread_partial_results(self, items: object) -> None:
        changed_count = self._apply_source_corrections(list(items))
        if changed_count:
            self.statusBar().showMessage(f"已保存 {changed_count} 条文稿校正结果。", 3000)

    def _on_source_proofread_finished(self, corrected_count: int) -> None:
        self.translation_busy = False
        self._set_active_background_worker(None)
        self._refresh_interactive_state()
        self.refresh_views()
        self.translation_page.translation_table.setFocus()
        self._clear_task_progress()
        self.statusBar().showMessage(
            f"中文字幕校正完成：已处理 {corrected_count} 个分段。",
            8000,
        )
        self._notify_task_finished(
            "中文字幕校正完成",
            f"中文字幕校正已经完成。\n\n已处理分段数：{corrected_count}",
        )

    def _on_translation_partial_results(self, items: object) -> None:
        updated_count = self._apply_translation_batch(list(items))
        if updated_count:
            self.statusBar().showMessage(f"已保存 {updated_count} 条翻译结果。", 3000)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._background_job_in_progress():
            QMessageBox.information(
                self,
                "后台任务进行中",
                "请等待当前后台任务完成后再关闭应用。",
            )
            event.ignore()
            return
        if self.context.project_dirty:
            reply = QMessageBox.question(
                self,
                "项目尚未保存",
                "当前项目有未保存修改。关闭前要先保存吗？",
                QMessageBox.StandardButton.Save
                | QMessageBox.StandardButton.Discard
                | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Save,
            )
            if reply == QMessageBox.StandardButton.Cancel:
                event.ignore()
                return
            if reply == QMessageBox.StandardButton.Save and not self.save_project():
                event.ignore()
                return
        super().closeEvent(event)
