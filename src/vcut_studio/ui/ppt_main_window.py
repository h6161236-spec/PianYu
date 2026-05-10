from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import time
from uuid import uuid4

from PySide6.QtCore import (
    QObject,
    QPointF,
    QRect,
    QRectF,
    QSize,
    QProcess,
    QProcessEnvironment,
    QThread,
    QTimer,
    Qt,
    QUrl,
    Signal,
)
from PySide6.QtGui import QCloseEvent, QColor, QFont, QFontMetrics, QIcon, QImageReader, QMouseEvent, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtMultimedia import QAudioInput, QMediaCaptureSession, QMediaFormat, QMediaRecorder
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QColorDialog,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QDoubleSpinBox,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from ..context import AppContext
from ..exporting import ExportPlan
from ..models import ExportJob, Project, SlidePage
from ..ppt_io import import_presentation
from ..ppt_exporting import resolve_ppt_subtitle_mode, resolve_ppt_voiceover_language
from ..project_store import sanitize_filename
from ..providers.tts import (
    KOKORO_LOCAL_PROVIDER,
    MELO_LOCAL_PROVIDER,
    available_tts_voices,
    create_tts_provider,
    tts_provider_choices,
    tts_provider_display_name,
    tts_provider_runtime_status,
)
from ..runtime_assets import find_app_icon
from ..settings import AppSettings
from .ppt_workspace import PptWorkspacePage
from .theme import build_dialog_stylesheet, build_window_stylesheet, theme_mode_choices
from .workers import (
    ControllableWorker,
    PptImportJobResult,
    PptImportWorker,
    PptExportResult,
    PptExportWorker,
    PptScriptDraftJobResult,
    PptScriptGenerationWorker,
    PptScriptTranslationJobResult,
    PptScriptTranslationWorker,
    PptVoiceInputResult,
    PptVoiceInputWorker,
)

try:
    import winsound
except ImportError:
    winsound = None


def _inline_row(*widgets: QWidget) -> QWidget:
    container = QWidget()
    layout = QHBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(6)
    for widget in widgets:
        layout.addWidget(widget)
    return container


def _voice_role_hint(voice_id: str, label: str) -> str:
    normalized = f"{voice_id} {label}".lower()
    if any(token in normalized for token in ("am_eric", "am_liam", "am_puck", "echo")):
        return "清爽男声"
    if any(token in normalized for token in ("am_michael", "bm_daniel", "bm_george", "bm_lewis", "onyx")):
        return "沉稳男声"
    if any(token in normalized for token in ("adam", "fenrir", "fable", "santa")):
        return "通用男声"
    if any(token in normalized for token in ("af_", "bf_", "zf_", "hf_", "if_", "jf_", "pf_")):
        return "女声"
    if any(token in normalized for token in ("am_", "bm_", "zm_", "hm_", "im_", "jm_", "pm_", "em_")):
        return "男声"
    return "音色"


def _voice_style_hint(voice: object) -> str:
    voice_id = str(getattr(voice, "voice_id", "") or "").strip().lower()
    style = str(getattr(voice, "style", "") or "").strip()
    normalized_style = style.lower()
    if style and normalized_style not in {"female", "male", "installed"}:
        return style
    if normalized_style == "installed":
        return "系统已安装，可直接试音"
    if voice_id.startswith("zf_"):
        return "中文女声，建议先试听日期和数字"
    if voice_id.startswith("zm_"):
        return "中文男声，建议先试听日期和数字"
    if voice_id.startswith(("af_", "bf_")):
        return "英文女声，适合解说和培训"
    if voice_id.startswith(("am_", "bm_")):
        return "英文男声，适合教程和旁白"
    return "建议先试听后再使用"


def _accent_zh(accent: str) -> str:
    normalized = str(accent or "").strip().lower()
    mapping = {
        "us": "美式",
        "uk": "英式",
        "au": "澳式",
        "es": "西语",
        "fr": "法语",
        "hi": "印地语",
        "it": "意式",
        "jp": "日语",
        "br": "葡语",
        "zh": "中文",
        "system": "系统",
    }
    return mapping.get(normalized, accent or "未知")


def _voice_display_text(voice: object) -> str:
    voice_id = str(getattr(voice, "voice_id", "") or "")
    label = str(getattr(voice, "label", "") or voice_id)
    accent = _accent_zh(str(getattr(voice, "accent", "") or ""))
    role = _voice_role_hint(voice_id, label)
    style_hint = _voice_style_hint(voice)
    return f"{label} | {role} | {accent} | {style_hint} | {voice_id}"


def _voice_provider_choices(language: str) -> list[tuple[str, str]]:
    choices = list(tts_provider_choices(language))
    normalized_language = str(language or "en").strip().lower()
    if normalized_language != "zh":
        return choices
    seen = {provider_type for _label, provider_type in choices}
    is_kokoro_ready, _status_text = tts_provider_runtime_status(KOKORO_LOCAL_PROVIDER, language="zh")
    if is_kokoro_ready and KOKORO_LOCAL_PROVIDER not in seen:
        choices.insert(0, ("Kokoro 中文音色（推荐，可选男声）", KOKORO_LOCAL_PROVIDER))
    for index, (label, provider_type) in enumerate(choices):
        if provider_type == MELO_LOCAL_PROVIDER:
            choices[index] = ("Melo 中文音色（女声）", provider_type)
    return choices


def _preferred_chinese_male_voice_id() -> str:
    preferred_voice_ids = ("zm_yunxi", "zm_yunjian", "zm_yunyang", "zm_001")
    available_voice_ids = {
        str(getattr(voice, "voice_id", "") or "")
        for voice in available_tts_voices(KOKORO_LOCAL_PROVIDER, language="zh")
    }
    for voice_id in preferred_voice_ids:
        if voice_id in available_voice_ids:
            return voice_id
    for voice_id in available_voice_ids:
        if voice_id.startswith("zm_"):
            return voice_id
    return ""


def _merge_script_text(existing_text: str, appended_text: str) -> str:
    existing = str(existing_text or "").strip()
    addition = str(appended_text or "").strip()
    if not existing:
        return addition
    if not addition:
        return existing
    return f"{existing}\n{addition}"


_SUBTITLE_SAFE_MARGIN_X_PERCENT = 8.0
_SUBTITLE_SAFE_MARGIN_Y_PERCENT = 6.0
_SUBTITLE_FONT_REFERENCE_HEIGHT = 480.0


def _preview_subtitle_font_pixel_size(configured_font_size: int, page_height: float) -> int:
    normalized_size = max(1, int(configured_font_size or 28))
    normalized_height = max(1.0, float(page_height or 0.0))
    scaled_size = normalized_size * (normalized_height / _SUBTITLE_FONT_REFERENCE_HEIGHT)
    return max(1, int(round(scaled_size)))


class SubtitleLayoutPreview(QWidget):
    position_changed = Signal(str, float, float)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(340)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self._preview_pixmap = QPixmap()
        self._subtitle_mode = "bilingual"
        self._font_size = 28
        self._english_color = QColor("#FFFFFF")
        self._chinese_color = QColor("#FFD966")
        self._safe_area_enabled = True
        self._english_position = (50.0, 90.0)
        self._chinese_position = (50.0, 82.0)
        self._dragging_language: str | None = None
        self._drag_offset = QPointF()
        self._subtitle_rects: dict[str, QRectF] = {}
        self._hover_language: str | None = None

    def set_preview_image(self, preview_image_path: str | Path | None) -> None:
        preview_path = Path(preview_image_path) if preview_image_path else None
        if preview_path is not None and preview_path.exists():
            self._preview_pixmap = self._load_preview_pixmap(preview_path)
        else:
            self._preview_pixmap = QPixmap()
        self.update()

    def _load_preview_pixmap(self, preview_path: Path) -> QPixmap:
        try:
            reader = QImageReader(str(preview_path))
            reader.setAutoTransform(True)
            source_size = reader.size()
            if source_size.isValid():
                max_edge = 2200
                width = max(1, source_size.width())
                height = max(1, source_size.height())
                if width > max_edge or height > max_edge:
                    scale = min(max_edge / width, max_edge / height)
                    reader.setScaledSize(
                        QSize(
                            max(1, int(round(width * scale))),
                            max(1, int(round(height * scale))),
                        )
                    )
            image = reader.read()
            if image.isNull():
                return QPixmap()
            return QPixmap.fromImage(image)
        except Exception:
            return QPixmap()

    def set_preview_state(
        self,
        *,
        subtitle_mode: str,
        font_size: int,
        english_color: QColor,
        chinese_color: QColor,
        safe_area_enabled: bool,
        english_position: tuple[float, float],
        chinese_position: tuple[float, float],
    ) -> None:
        self._subtitle_mode = str(subtitle_mode or "bilingual")
        self._font_size = max(10, int(font_size))
        self._english_color = QColor(english_color)
        self._chinese_color = QColor(chinese_color)
        self._safe_area_enabled = bool(safe_area_enabled)
        self._english_position = self._clamp_position(*english_position)
        self._chinese_position = self._clamp_position(*chinese_position)
        self.update()

    def _clamp_position(self, x_percent: float, y_percent: float) -> tuple[float, float]:
        min_x = _SUBTITLE_SAFE_MARGIN_X_PERCENT if self._safe_area_enabled else 4.0
        max_x = 100.0 - min_x
        min_y = _SUBTITLE_SAFE_MARGIN_Y_PERCENT if self._safe_area_enabled else 4.0
        max_y = 100.0 - min_y
        return (
            max(min_x, min(max_x, float(x_percent))),
            max(min_y, min(max_y, float(y_percent))),
        )

    def _page_rect(self) -> QRectF:
        outer = QRectF(self.rect()).adjusted(18.0, 18.0, -18.0, -18.0)
        if outer.width() <= 0 or outer.height() <= 0:
            return QRectF()

        if not self._preview_pixmap.isNull() and self._preview_pixmap.height() > 0:
            aspect_ratio = self._preview_pixmap.width() / self._preview_pixmap.height()
        else:
            aspect_ratio = 16.0 / 9.0

        target_width = outer.width()
        target_height = target_width / aspect_ratio
        if target_height > outer.height():
            target_height = outer.height()
            target_width = target_height * aspect_ratio

        left = outer.left() + (outer.width() - target_width) / 2.0
        top = outer.top() + (outer.height() - target_height) / 2.0
        return QRectF(left, top, target_width, target_height)

    def _subtitle_text(self, language: str) -> str:
        if language == "zh":
            return "这是中文字幕预览，可拖动到想要的位置"
        return "This is the English subtitle preview. Drag it freely."

    def _active_languages(self) -> list[str]:
        if self._subtitle_mode == "zh":
            return ["zh"]
        if self._subtitle_mode == "en":
            return ["en"]
        return ["zh", "en"]

    def _subtitle_rect(
        self,
        painter: QPainter,
        page_rect: QRectF,
        *,
        language: str,
    ) -> QRectF:
        position = self._chinese_position if language == "zh" else self._english_position
        x_percent, y_percent = position
        anchor_x = page_rect.left() + page_rect.width() * (x_percent / 100.0)
        anchor_y = page_rect.top() + page_rect.height() * (y_percent / 100.0)

        font = QFont(self.font())
        font.setPixelSize(_preview_subtitle_font_pixel_size(self._font_size, page_rect.height()))
        font.setBold(language == "zh")
        painter.setFont(font)
        metrics = QFontMetrics(font)
        max_width = max(160.0, page_rect.width() * 0.8)
        text_rect = metrics.boundingRect(
            0,
            0,
            int(max_width),
            2000,
            int(Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap),
            self._subtitle_text(language),
        )
        width = min(max_width, text_rect.width() + 28.0)
        height = text_rect.height() + 18.0
        rect = QRectF(anchor_x - width / 2.0, anchor_y - height / 2.0, width, height)

        constraint_rect = self._safe_rect(page_rect) if self._safe_area_enabled else page_rect
        min_left = constraint_rect.left() + 12.0
        max_left = constraint_rect.right() - width - 12.0
        min_top = constraint_rect.top() + 12.0
        max_top = constraint_rect.bottom() - height - 12.0
        rect.moveLeft(min(max(rect.left(), min_left), max_left))
        rect.moveTop(min(max(rect.top(), min_top), max_top))
        return rect

    def _safe_rect(self, page_rect: QRectF) -> QRectF:
        return page_rect.adjusted(
            page_rect.width() * (_SUBTITLE_SAFE_MARGIN_X_PERCENT / 100.0),
            page_rect.height() * (_SUBTITLE_SAFE_MARGIN_Y_PERCENT / 100.0),
            -page_rect.width() * (_SUBTITLE_SAFE_MARGIN_X_PERCENT / 100.0),
            -page_rect.height() * (_SUBTITLE_SAFE_MARGIN_Y_PERCENT / 100.0),
        )

    def paintEvent(self, event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.fillRect(self.rect(), QColor("#dbe7f5"))

        page_rect = self._page_rect()
        if page_rect.isNull():
            return

        shadow_rect = page_rect.adjusted(0.0, 8.0, 0.0, 8.0)
        shadow_path = QPainterPath()
        shadow_path.addRoundedRect(shadow_rect, 18.0, 18.0)
        painter.fillPath(shadow_path, QColor(42, 56, 86, 40))

        page_path = QPainterPath()
        page_path.addRoundedRect(page_rect, 18.0, 18.0)
        painter.save()
        painter.setClipPath(page_path)
        painter.fillRect(page_rect, QColor("#f8fbff"))
        painter.fillRect(
            QRectF(page_rect.left(), page_rect.top(), page_rect.width(), page_rect.height() * 0.16),
            QColor("#e5edf7"),
        )
        painter.fillRect(
            QRectF(page_rect.left(), page_rect.top() + page_rect.height() * 0.16, page_rect.width(), page_rect.height()),
            QColor("#f8fbff"),
        )
        if not self._preview_pixmap.isNull():
            scaled = self._preview_pixmap.scaled(
                int(page_rect.width() - 32),
                int(page_rect.height() - 56),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            target_rect = QRect(
                int(round(page_rect.left() + (page_rect.width() - scaled.width()) / 2.0)),
                int(round(page_rect.top() + (page_rect.height() - scaled.height()) / 2.0)),
                scaled.width(),
                scaled.height(),
            )
            painter.setOpacity(0.34)
            painter.drawPixmap(target_rect, scaled)
            painter.setOpacity(1.0)

        painter.setPen(QColor("#56718f"))
        painter.setFont(QFont("Arial", 18, QFont.Weight.Bold))
        painter.drawText(
            page_rect.adjusted(26.0, 18.0, -26.0, -26.0),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop),
            "PPT 视频页面预览",
        )
        painter.setFont(QFont("Arial", 12))
        painter.drawText(
            page_rect.adjusted(26.0, 64.0, -26.0, -26.0),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap),
            "字幕会按导出视频的真实页面比例预览。拖动中英文字幕块，即可调整最终烧录位置。",
        )
        painter.restore()

        painter.setPen(QPen(QColor(122, 145, 173, 180), 1.0, Qt.PenStyle.SolidLine))
        painter.drawRoundedRect(page_rect, 18.0, 18.0)
        if self._safe_area_enabled:
            safe_rect = self._safe_rect(page_rect)
            painter.setPen(QPen(QColor(15, 118, 110, 150), 1.0, Qt.PenStyle.DashLine))
            painter.drawRoundedRect(safe_rect, 14.0, 14.0)

        badge_rect = QRectF(page_rect.left() + 16.0, page_rect.top() + 16.0, 140.0, 30.0)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(15, 118, 110, 210))
        painter.drawRoundedRect(badge_rect, 15.0, 15.0)
        painter.setPen(QColor("#ffffff"))
        painter.setFont(QFont("Arial", 10, QFont.Weight.Bold))
        painter.drawText(badge_rect, int(Qt.AlignmentFlag.AlignCenter), "真实页面预览")

        self._subtitle_rects = {}
        for language in self._active_languages():
            rect = self._subtitle_rect(painter, page_rect, language=language)
            self._subtitle_rects[language] = rect

            is_active = language == self._dragging_language or language == self._hover_language
            if is_active:
                painter.setPen(QPen(QColor(45, 212, 191, 220), 2.0))
                painter.setBrush(QColor(15, 23, 42, 110))
            else:
                painter.setPen(QPen(QColor(255, 255, 255, 45), 1.0))
                painter.setBrush(QColor(15, 23, 42, 70))
            painter.drawRoundedRect(rect, 14.0, 14.0)

            font = QFont(self.font())
            font.setPixelSize(_preview_subtitle_font_pixel_size(self._font_size, page_rect.height()))
            font.setBold(language == "zh")
            painter.setFont(font)
            text = self._subtitle_text(language)
            color = self._chinese_color if language == "zh" else self._english_color
            shadow_pen = QColor(0, 0, 0, max(140, color.alpha()))
            for offset_x, offset_y in ((1, 1), (-1, 1), (1, -1), (-1, -1)):
                painter.setPen(shadow_pen)
                painter.drawText(
                    rect.adjusted(offset_x, offset_y, offset_x, offset_y),
                    int(Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap),
                    text,
                )
            painter.setPen(color)
            painter.drawText(
                rect,
                int(Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap),
                text,
            )

    def _language_at(self, position: QPointF) -> str | None:
        for language, rect in reversed(list(self._subtitle_rects.items())):
            if rect.contains(position):
                return language
        return None

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        language = self._language_at(event.position())
        if language is None:
            super().mousePressEvent(event)
            return
        rect = self._subtitle_rects.get(language)
        if rect is None:
            return
        self._dragging_language = language
        self._drag_offset = event.position() - rect.center()
        self.update()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        page_rect = self._page_rect()
        if self._dragging_language and not page_rect.isNull():
            target_center = event.position() - self._drag_offset
            x_percent = ((target_center.x() - page_rect.left()) / page_rect.width()) * 100.0
            y_percent = ((target_center.y() - page_rect.top()) / page_rect.height()) * 100.0
            x_percent, y_percent = self._clamp_position(x_percent, y_percent)
            if self._dragging_language == "zh":
                self._chinese_position = (x_percent, y_percent)
            else:
                self._english_position = (x_percent, y_percent)
            self.position_changed.emit(self._dragging_language, x_percent, y_percent)
            self.update()
            return

        hover_language = self._language_at(event.position())
        if hover_language != self._hover_language:
            self._hover_language = hover_language
            self.setCursor(
                Qt.CursorShape.OpenHandCursor if hover_language is not None else Qt.CursorShape.ArrowCursor
            )
            self.update()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._dragging_language is not None:
            self._dragging_language = None
            self.setCursor(Qt.CursorShape.ArrowCursor)
            self.update()
            return
        super().mouseReleaseEvent(event)


class PptSettingsDialog(QDialog):
    def __init__(
        self,
        settings: AppSettings,
        parent: QWidget | None = None,
        preview_image_path: str | Path | None = None,
    ) -> None:
        super().__init__(parent)
        self._preview_audio_path: Path | None = None
        self._theme_mode = settings.appearance.theme_mode
        self.setWindowTitle("设置")
        self.resize(1180, 780)
        if parent is not None:
            self.setWindowIcon(parent.windowIcon())
        self._apply_dialog_style()

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(14)

        header_card = QFrame()
        header_card.setObjectName("settingsHeaderCard")
        header_layout = QVBoxLayout(header_card)
        header_layout.setContentsMargins(18, 18, 18, 18)
        header_layout.setSpacing(6)
        header_title = QLabel("PPT 项目设置")
        header_title.setObjectName("settingsHeaderTitle")
        header_text = QLabel("左侧选择模块，右侧查看和调整详细配置。这样每一块都能单独展开，不会挤在一起。")
        header_text.setObjectName("settingsHeaderText")
        header_text.setWordWrap(True)
        header_layout.addWidget(header_title)
        header_layout.addWidget(header_text)
        root.addWidget(header_card)

        self.appearance_theme_combo = QComboBox()
        for label, theme_mode in theme_mode_choices():
            self.appearance_theme_combo.addItem(label, theme_mode)
        appearance_group = self._build_form_group(
            "界面外观",
            [
                ("软件颜色", self.appearance_theme_combo),
            ],
        )

        self.translation_base_url_edit = QLineEdit(settings.translation.base_url)
        self.translation_api_key_edit = QLineEdit(settings.translation.api_key)
        self.translation_api_key_edit.setEchoMode(QLineEdit.EchoMode.PasswordEchoOnEdit)
        self.translation_model_edit = QLineEdit(settings.translation.model)
        self.translation_timeout_spin = QSpinBox()
        self.translation_timeout_spin.setRange(10, 600)
        self.translation_timeout_spin.setValue(settings.translation.timeout_sec)
        self.translation_batch_spin = QSpinBox()
        self.translation_batch_spin.setRange(1, 12)
        self.translation_batch_spin.setValue(settings.translation.batch_size)

        translation_group = self._build_form_group(
            "大模型",
            [
                ("模型 Base URL", self.translation_base_url_edit),
                ("模型 API Key", self.translation_api_key_edit),
                ("模型名称", self.translation_model_edit),
                ("超时（秒）", self.translation_timeout_spin),
                ("每批页数", self.translation_batch_spin),
            ],
        )

        self.zh_tts_provider_combo = QComboBox()
        for label, provider_type in _voice_provider_choices("zh"):
            self.zh_tts_provider_combo.addItem(label, provider_type)
        self.zh_tts_status_label = QLabel()
        self.zh_tts_status_label.setWordWrap(True)
        self.zh_tts_voice_combo = QComboBox()
        self.zh_tts_preview_text_edit = QPlainTextEdit()
        self.zh_tts_preview_text_edit.setPlainText(settings.tts.preview_text_for_language("zh"))
        self.zh_tts_preview_text_edit.setMinimumHeight(90)
        self.zh_tts_preview_button = QPushButton("试听当前音色")
        self.zh_tts_stop_button = QPushButton("停止试听")
        zh_tts_group = self._build_form_group(
            "中文配音",
            [
                ("配音引擎", self.zh_tts_provider_combo),
                ("引擎状态", self.zh_tts_status_label),
                ("默认音色", self.zh_tts_voice_combo),
                ("试听文本", self.zh_tts_preview_text_edit),
                ("试听", _inline_row(self.zh_tts_preview_button, self.zh_tts_stop_button)),
            ],
        )

        self.tts_provider_combo = QComboBox()
        for label, provider_type in _voice_provider_choices("en"):
            self.tts_provider_combo.addItem(label, provider_type)
        self.tts_status_label = QLabel()
        self.tts_status_label.setWordWrap(True)
        self.tts_voice_combo = QComboBox()
        self.tts_rate_spin = QDoubleSpinBox()
        self.tts_rate_spin.setRange(0.5, 2.0)
        self.tts_rate_spin.setSingleStep(0.05)
        self.tts_rate_spin.setDecimals(2)
        self.tts_rate_spin.setValue(settings.tts.rate)
        self.tts_preview_text_edit = QPlainTextEdit()
        self.tts_preview_text_edit.setPlainText(settings.tts.preview_text_for_language("en"))
        self.tts_preview_text_edit.setMinimumHeight(90)
        self.tts_preview_button = QPushButton("试听当前音色")
        self.tts_stop_button = QPushButton("停止试听")

        tts_group = self._build_form_group(
            "英文配音",
            [
                ("配音引擎", self.tts_provider_combo),
                ("引擎状态", self.tts_status_label),
                ("默认音色", self.tts_voice_combo),
                ("语速", self.tts_rate_spin),
                ("试听文本", self.tts_preview_text_edit),
                ("试听", _inline_row(self.tts_preview_button, self.tts_stop_button)),
            ],
        )

        self.ffmpeg_path_edit = QLineEdit(settings.media.ffmpeg_path)
        self.output_dir_edit = QLineEdit(settings.media.default_output_dir)
        self.output_dir_browse_button = QPushButton("浏览")
        self.output_container_combo = QComboBox()
        for item in ("mp4", "mov", "mkv"):
            self.output_container_combo.addItem(item, item)
        self.subtitle_mode_combo = QComboBox()
        self.subtitle_mode_combo.addItem("中英双语", "bilingual")
        self.subtitle_mode_combo.addItem("仅中文", "zh")
        self.subtitle_mode_combo.addItem("仅英文", "en")
        self.burn_subtitles_checkbox = QCheckBox("导出时默认烧录字幕")
        self.export_sidecar_checkbox = QCheckBox("同时导出 SRT 字幕文件")

        export_group = self._build_form_group(
            "导出",
            [
                ("FFmpeg 路径", self.ffmpeg_path_edit),
                ("默认导出目录", _inline_row(self.output_dir_edit, self.output_dir_browse_button)),
                ("默认封装格式", self.output_container_combo),
                ("默认字幕模式", self.subtitle_mode_combo),
                ("字幕烧录", self.burn_subtitles_checkbox),
                ("SRT 文件", self.export_sidecar_checkbox),
            ],
        )

        self.subtitle_font_size_spin = QSpinBox()
        self.subtitle_font_size_spin.setRange(5, 72)
        self.subtitle_font_size_spin.setSingleStep(1)
        self.subtitle_font_size_spin.setSuffix(" 号")
        self.subtitle_font_size_spin.setValue(settings.export.subtitle_font_size)
        self.subtitle_font_size_spin.setToolTip("字号会按导出视频分辨率自动缩放，1080P 下建议从 14 到 24 开始调。")
        self.subtitle_english_color_edit = QLineEdit(settings.export.subtitle_english_color)
        self.subtitle_english_color_edit.setMaxLength(9)
        self.subtitle_english_color_button = QPushButton("色盘")
        self.subtitle_chinese_color_edit = QLineEdit(settings.export.subtitle_chinese_color)
        self.subtitle_chinese_color_edit.setMaxLength(9)
        self.subtitle_chinese_color_button = QPushButton("色盘")
        self.subtitle_safe_area_checkbox = QCheckBox("启用小屏/横屏安全区")
        self.subtitle_safe_area_checkbox.setChecked(settings.export.subtitle_safe_area_enabled)
        self.subtitle_english_x_spin = QSpinBox()
        self.subtitle_english_x_spin.setRange(0, 100)
        self.subtitle_english_x_spin.setSuffix("%")
        self.subtitle_english_x_spin.setValue(int(round(settings.export.subtitle_english_x_percent)))
        self.subtitle_english_y_spin = QSpinBox()
        self.subtitle_english_y_spin.setRange(0, 100)
        self.subtitle_english_y_spin.setSuffix("%")
        self.subtitle_english_y_spin.setValue(int(round(settings.export.subtitle_english_y_percent)))
        self.subtitle_chinese_x_spin = QSpinBox()
        self.subtitle_chinese_x_spin.setRange(0, 100)
        self.subtitle_chinese_x_spin.setSuffix("%")
        self.subtitle_chinese_x_spin.setValue(int(round(settings.export.subtitle_chinese_x_percent)))
        self.subtitle_chinese_y_spin = QSpinBox()
        self.subtitle_chinese_y_spin.setRange(0, 100)
        self.subtitle_chinese_y_spin.setSuffix("%")
        self.subtitle_chinese_y_spin.setValue(int(round(settings.export.subtitle_chinese_y_percent)))
        self.subtitle_position_reset_button = QPushButton("恢复默认位置")
        self.subtitle_preview_hint = QLabel(
            "预览区会按真实视频页面比例显示。可直接拖动中英字幕块，也可在下面输入精确位置百分比。"
            " 开启安全区后，字幕会自动限制在更稳妥的可视范围内。"
        )
        self.subtitle_preview_hint.setWordWrap(True)
        self.subtitle_preview_surface = SubtitleLayoutPreview()
        self.subtitle_preview_surface.set_preview_image(preview_image_path)

        english_position_row = _inline_row(
            QLabel("X"),
            self.subtitle_english_x_spin,
            QLabel("Y"),
            self.subtitle_english_y_spin,
        )
        chinese_position_row = _inline_row(
            QLabel("X"),
            self.subtitle_chinese_x_spin,
            QLabel("Y"),
            self.subtitle_chinese_y_spin,
        )

        subtitle_group = self._build_form_group(
            "字幕样式",
            [
                ("字幕字号", self.subtitle_font_size_spin),
                ("英文字幕颜色", _inline_row(self.subtitle_english_color_edit, self.subtitle_english_color_button)),
                ("中文字幕颜色", _inline_row(self.subtitle_chinese_color_edit, self.subtitle_chinese_color_button)),
                ("安全区", self.subtitle_safe_area_checkbox),
                ("英文位置", english_position_row),
                ("中文位置", chinese_position_row),
                ("位置重置", self.subtitle_position_reset_button),
                ("说明", self.subtitle_preview_hint),
                ("实时预览", self.subtitle_preview_surface),
            ],
        )

        body = QHBoxLayout()
        body.setSpacing(14)
        root.addLayout(body, stretch=1)

        sidebar = QFrame()
        sidebar.setObjectName("settingsSidebar")
        sidebar.setFixedWidth(220)
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(14, 14, 14, 14)
        sidebar_layout.setSpacing(10)
        nav_title = QLabel("设置项目")
        nav_title.setObjectName("settingsNavTitle")
        nav_hint = QLabel("点击左侧模块切换右侧详细配置。")
        nav_hint.setObjectName("settingsNavHint")
        nav_hint.setWordWrap(True)
        self.settings_nav = QListWidget()
        self.settings_nav.setObjectName("settingsNav")
        self.settings_nav.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.settings_nav.setSpacing(2)
        self.settings_nav.insertItem(0, "界面")
        for label in ("大模型", "中文配音", "英文配音", "导出", "字幕样式"):
            self.settings_nav.addItem(label)
        sidebar_layout.addWidget(nav_title)
        sidebar_layout.addWidget(nav_hint)
        sidebar_layout.addWidget(self.settings_nav, stretch=1)
        body.addWidget(sidebar)

        content = QFrame()
        content.setObjectName("settingsContent")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(14, 14, 14, 14)
        content_layout.setSpacing(0)
        self.settings_stack = QStackedWidget()
        self.settings_stack.addWidget(
            self._build_section_page(
                "界面",
                "切换软件深色、浅色或者跟随系统外观。",
                [appearance_group],
            )
        )
        self.settings_stack.addWidget(
            self._build_section_page(
                "大模型",
                "配置生成中文稿和英文翻译时要调用的大模型。",
                [translation_group],
            )
        )
        self.settings_stack.addWidget(
            self._build_section_page(
                "中文配音",
                "单独配置中文口播使用的引擎、音色和试听文本。",
                [zh_tts_group],
            )
        )
        self.settings_stack.addWidget(
            self._build_section_page(
                "英文配音",
                "选择英文配音引擎、音色和试听文本。",
                [tts_group],
            )
        )
        self.settings_stack.addWidget(
            self._build_section_page(
                "导出",
                "控制默认导出目录、容器格式和字幕导出方式。",
                [export_group],
            )
        )
        self.settings_stack.addWidget(
            self._build_section_page(
                "字幕样式",
                "调整双语字幕字号、颜色和预览效果。",
                [subtitle_group],
            )
        )
        content_layout.addWidget(self.settings_stack)
        body.addWidget(content, stretch=1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        root.addWidget(buttons)

        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self.settings_nav.currentRowChanged.connect(self.settings_stack.setCurrentIndex)
        self.appearance_theme_combo.currentIndexChanged.connect(lambda _index: self._update_theme_preview())
        self.output_dir_browse_button.clicked.connect(self._browse_output_dir)
        self.zh_tts_provider_combo.currentIndexChanged.connect(
            lambda _index: self._refresh_tts_section("zh")
        )
        self.tts_provider_combo.currentIndexChanged.connect(
            lambda _index: self._refresh_tts_section("en")
        )
        self.zh_tts_preview_button.clicked.connect(lambda: self._preview_selected_voice("zh"))
        self.tts_preview_button.clicked.connect(lambda: self._preview_selected_voice("en"))
        self.tts_stop_button.clicked.connect(self._stop_preview)
        self.zh_tts_stop_button.clicked.connect(self._stop_preview)
        self.subtitle_english_color_button.clicked.connect(
            lambda: self._pick_subtitle_color(self.subtitle_english_color_edit)
        )
        self.subtitle_chinese_color_button.clicked.connect(
            lambda: self._pick_subtitle_color(self.subtitle_chinese_color_edit)
        )
        self.subtitle_font_size_spin.valueChanged.connect(self._refresh_subtitle_preview)
        self.subtitle_english_color_edit.textChanged.connect(self._refresh_subtitle_preview)
        self.subtitle_chinese_color_edit.textChanged.connect(self._refresh_subtitle_preview)
        self.subtitle_mode_combo.currentIndexChanged.connect(self._refresh_subtitle_preview)
        self.subtitle_safe_area_checkbox.toggled.connect(self._refresh_subtitle_preview)
        self.subtitle_english_x_spin.valueChanged.connect(self._refresh_subtitle_preview)
        self.subtitle_english_y_spin.valueChanged.connect(self._refresh_subtitle_preview)
        self.subtitle_chinese_x_spin.valueChanged.connect(self._refresh_subtitle_preview)
        self.subtitle_chinese_y_spin.valueChanged.connect(self._refresh_subtitle_preview)
        self.subtitle_position_reset_button.clicked.connect(self._reset_subtitle_positions)
        self.subtitle_preview_surface.position_changed.connect(self._on_preview_position_changed)

        self._set_combo_data(self.appearance_theme_combo, settings.appearance.theme_mode)
        self._set_combo_data(self.zh_tts_provider_combo, settings.tts.provider_type_for_language("zh"))
        self._set_combo_data(self.tts_provider_combo, settings.tts.provider_type_for_language("en"))
        self._set_combo_data(self.output_container_combo, settings.export.output_container)
        self._set_combo_data(self.subtitle_mode_combo, settings.export.subtitle_mode)
        self.burn_subtitles_checkbox.setChecked(settings.export.burn_subtitles)
        self.export_sidecar_checkbox.setChecked(settings.export.export_sidecar_srt)
        self._refresh_tts_section("zh", preferred_voice_id=settings.tts.default_voice_for_language("zh"))
        self._refresh_tts_section("en", preferred_voice_id=settings.tts.default_voice_for_language("en"))
        self._refresh_subtitle_preview()
        self._update_theme_preview()
        self.settings_nav.setCurrentRow(0)

    def apply_to_settings(self, settings: AppSettings) -> None:
        settings.appearance.theme_mode = str(self.appearance_theme_combo.currentData() or "system")
        settings.translation.base_url = self.translation_base_url_edit.text().strip()
        settings.translation.api_key = self.translation_api_key_edit.text().strip()
        settings.translation.model = self.translation_model_edit.text().strip()
        settings.translation.timeout_sec = self.translation_timeout_spin.value()
        settings.translation.batch_size = self.translation_batch_spin.value()

        settings.tts.set_provider_type_for_language(
            "zh",
            str(self.zh_tts_provider_combo.currentData() or "").strip(),
        )
        settings.tts.set_default_voice_for_language(
            "zh",
            str(self.zh_tts_voice_combo.currentData() or "").strip(),
        )
        settings.tts.set_preview_text_for_language(
            "zh",
            self.zh_tts_preview_text_edit.toPlainText().strip(),
        )
        settings.tts.set_provider_type_for_language(
            "en",
            str(self.tts_provider_combo.currentData() or "").strip(),
        )
        settings.tts.set_default_voice_for_language(
            "en",
            str(self.tts_voice_combo.currentData() or "").strip(),
        )
        settings.tts.rate = self.tts_rate_spin.value()
        settings.tts.set_preview_text_for_language(
            "en",
            self.tts_preview_text_edit.toPlainText().strip(),
        )

        settings.media.ffmpeg_path = self.ffmpeg_path_edit.text().strip() or "ffmpeg"
        settings.media.default_output_dir = self.output_dir_edit.text().strip()
        settings.export.output_container = str(self.output_container_combo.currentData() or "mp4")
        settings.export.subtitle_mode = str(self.subtitle_mode_combo.currentData() or "bilingual")
        settings.export.subtitle_font_size = self.subtitle_font_size_spin.value()
        settings.export.subtitle_english_color = self.subtitle_english_color_edit.text().strip()
        settings.export.subtitle_chinese_color = self.subtitle_chinese_color_edit.text().strip()
        settings.export.subtitle_safe_area_enabled = self.subtitle_safe_area_checkbox.isChecked()
        settings.export.subtitle_english_x_percent = float(self.subtitle_english_x_spin.value())
        settings.export.subtitle_english_y_percent = float(self.subtitle_english_y_spin.value())
        settings.export.subtitle_chinese_x_percent = float(self.subtitle_chinese_x_spin.value())
        settings.export.subtitle_chinese_y_percent = float(self.subtitle_chinese_y_spin.value())
        settings.export.burn_subtitles = self.burn_subtitles_checkbox.isChecked()
        settings.export.export_sidecar_srt = self.export_sidecar_checkbox.isChecked()

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

    def _build_section_page(self, title: str, description: str, groups: list[QWidget]) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(12)

        title_label = QLabel(title)
        title_label.setObjectName("settingsSectionTitle")
        desc_label = QLabel(description)
        desc_label.setObjectName("settingsSectionText")
        desc_label.setWordWrap(True)
        layout.addWidget(title_label)
        layout.addWidget(desc_label)
        for group in groups:
            layout.addWidget(group)
        layout.addStretch(1)

        scroll.setWidget(container)
        return scroll

    def _apply_dialog_style(self) -> None:
        self.setStyleSheet(build_dialog_stylesheet(self._theme_mode, app=QApplication.instance()))

    def _update_theme_preview(self) -> None:
        self._theme_mode = str(self.appearance_theme_combo.currentData() or "system")
        self._apply_dialog_style()

    def _browse_output_dir(self) -> None:
        selected_dir = QFileDialog.getExistingDirectory(
            self,
            "选择输出目录",
            self.output_dir_edit.text().strip(),
        )
        if selected_dir:
            self.output_dir_edit.setText(selected_dir)

    def _refresh_tts_section(self, language: str, preferred_voice_id: str | None = None) -> None:
        if language == "zh":
            provider_combo = self.zh_tts_provider_combo
            status_label = self.zh_tts_status_label
            voice_combo = self.zh_tts_voice_combo
        else:
            provider_combo = self.tts_provider_combo
            status_label = self.tts_status_label
            voice_combo = self.tts_voice_combo
        provider_type = str(provider_combo.currentData() or "").strip()
        is_ready, status_text = tts_provider_runtime_status(provider_type, language=language)
        status_prefix = "可用" if is_ready else "不可用"
        status_label.setText(
            f"{provider_combo.currentText().strip() or tts_provider_display_name(provider_type)} | {status_prefix} | {status_text}"
        )

        voice_combo.blockSignals(True)
        try:
            voice_combo.clear()
            for voice in available_tts_voices(provider_type, language=language):
                voice_combo.addItem(
                    _voice_display_text(voice),
                    voice.voice_id,
                )
            self._set_combo_data(
                voice_combo,
                preferred_voice_id or "",
            )
        finally:
            voice_combo.blockSignals(False)

    def _preview_selected_voice(self, language: str) -> None:
        if language == "zh":
            provider_combo = self.zh_tts_provider_combo
            voice_combo = self.zh_tts_voice_combo
            preview_edit = self.zh_tts_preview_text_edit
        else:
            provider_combo = self.tts_provider_combo
            voice_combo = self.tts_voice_combo
            preview_edit = self.tts_preview_text_edit
        provider_type = str(provider_combo.currentData() or "").strip()
        voice_id = str(voice_combo.currentData() or "").strip()
        preview_text = preview_edit.toPlainText().strip()
        if not voice_id:
            QMessageBox.information(self, "未选择音色", "请先选择一个音色。")
            return
        if not preview_text:
            QMessageBox.information(self, "缺少试听文本", "请先输入或生成可试听的文本。")
            return
        if winsound is None:
            QMessageBox.information(self, "当前环境不支持", "当前系统环境不支持直接播放试听音频。")
            return

        preview_settings = AppSettings()
        preview_settings.tts.set_provider_type_for_language(language, provider_type)
        preview_settings.tts.set_default_voice_for_language(language, voice_id)
        preview_settings.tts.set_preview_text_for_language(language, preview_text)
        preview_settings.tts.provider_type = provider_type
        preview_settings.tts.default_voice = voice_id
        preview_settings.tts.rate = self.tts_rate_spin.value()
        preview_settings.tts.preview_text = preview_text

        try:
            provider = create_tts_provider(preview_settings.tts)
            preview_dir = Path(tempfile.gettempdir()) / "vcut_ppt_voice_preview"
            preview_dir.mkdir(parents=True, exist_ok=True)
            preview_path = preview_dir / f"{language}_{sanitize_filename(voice_id or 'voice')}.wav"
            provider.synthesize_segment(preview_text, voice_id, preview_path)
            self._preview_audio_path = preview_path
            winsound.PlaySound(str(preview_path), winsound.SND_FILENAME | winsound.SND_ASYNC)
        except Exception as exc:
            QMessageBox.critical(self, "试听失败", str(exc))

    def _stop_preview(self) -> None:
        if winsound is None:
            return
        winsound.PlaySound(None, 0)

    def _pick_subtitle_color(self, target_edit: QLineEdit) -> None:
        current = QColor(target_edit.text().strip() or "#FFFFFF")
        if not current.isValid():
            current = QColor("#FFFFFF")
        color = QColorDialog.getColor(
            current,
            self,
            "选择字幕颜色",
            QColorDialog.ColorDialogOption.ShowAlphaChannel,
        )
        if not color.isValid():
            return
        if color.alpha() >= 255:
            target_edit.setText(f"#{color.red():02X}{color.green():02X}{color.blue():02X}")
        else:
            target_edit.setText(
                f"#{color.alpha():02X}{color.red():02X}{color.green():02X}{color.blue():02X}"
            )

    def _subtitle_preview_color(self, value: str, fallback: str) -> QColor:
        color = QColor(value.strip() or fallback)
        if color.isValid():
            return color
        return QColor(fallback)

    def _subtitle_preview_css(self, color: QColor) -> str:
        return f"rgba({color.red()}, {color.green()}, {color.blue()}, {color.alpha()})"

    def _subtitle_position_values(self, language: str) -> tuple[float, float]:
        if language == "zh":
            return (
                float(self.subtitle_chinese_x_spin.value()),
                float(self.subtitle_chinese_y_spin.value()),
            )
        return (
            float(self.subtitle_english_x_spin.value()),
            float(self.subtitle_english_y_spin.value()),
        )

    def _position_bounds(self) -> tuple[float, float, float, float]:
        if self.subtitle_safe_area_checkbox.isChecked():
            return (
                _SUBTITLE_SAFE_MARGIN_X_PERCENT,
                100.0 - _SUBTITLE_SAFE_MARGIN_X_PERCENT,
                _SUBTITLE_SAFE_MARGIN_Y_PERCENT,
                100.0 - _SUBTITLE_SAFE_MARGIN_Y_PERCENT,
            )
        return (4.0, 96.0, 4.0, 96.0)

    def _set_subtitle_position_values(self, language: str, x_percent: float, y_percent: float) -> None:
        target_x_spin = self.subtitle_chinese_x_spin if language == "zh" else self.subtitle_english_x_spin
        target_y_spin = self.subtitle_chinese_y_spin if language == "zh" else self.subtitle_english_y_spin
        min_x, max_x, min_y, max_y = self._position_bounds()
        for spin, value in (
            (target_x_spin, int(round(max(min_x, min(max_x, x_percent))))),
            (target_y_spin, int(round(max(min_y, min(max_y, y_percent))))),
        ):
            spin.blockSignals(True)
            try:
                spin.setValue(value)
            finally:
                spin.blockSignals(False)

    def _on_preview_position_changed(self, language: str, x_percent: float, y_percent: float) -> None:
        self._set_subtitle_position_values(language, x_percent, y_percent)
        self._refresh_subtitle_preview()

    def _reset_subtitle_positions(self) -> None:
        self._set_subtitle_position_values("en", 50.0, 90.0)
        self._set_subtitle_position_values("zh", 50.0, 82.0)
        self._refresh_subtitle_preview()

    def _refresh_subtitle_preview(self) -> None:
        font_size = max(10, int(self.subtitle_font_size_spin.value()))
        zh_color = self._subtitle_preview_color(self.subtitle_chinese_color_edit.text(), "#FFD966")
        en_color = self._subtitle_preview_color(self.subtitle_english_color_edit.text(), "#FFFFFF")
        mode = str(self.subtitle_mode_combo.currentData() or "bilingual")
        self._set_subtitle_position_values("en", *self._subtitle_position_values("en"))
        self._set_subtitle_position_values("zh", *self._subtitle_position_values("zh"))
        self.subtitle_preview_surface.set_preview_state(
            subtitle_mode=mode,
            font_size=font_size,
            english_color=en_color,
            chinese_color=zh_color,
            safe_area_enabled=self.subtitle_safe_area_checkbox.isChecked(),
            english_position=self._subtitle_position_values("en"),
            chinese_position=self._subtitle_position_values("zh"),
        )

    @staticmethod
    def _set_combo_data(combo: QComboBox, data: str) -> None:
        index = combo.findData(data)
        if index >= 0:
            combo.setCurrentIndex(index)
        elif combo.count() > 0:
            combo.setCurrentIndex(0)


class PptExportDialog(QDialog):
    def __init__(self, settings: AppSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._theme_mode = settings.appearance.theme_mode
        self.setWindowTitle("导出视频")
        self.resize(560, 260)
        if parent is not None:
            self.setWindowIcon(parent.windowIcon())
        self.setStyleSheet(build_dialog_stylesheet(self._theme_mode, app=QApplication.instance()))

        layout = QVBoxLayout(self)
        intro = QLabel("导出时会按每页英文稿生成语音，并按真实语音时长自动翻页。")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        form = QFormLayout()
        layout.addLayout(form)

        self.output_dir_edit = QLineEdit(settings.media.default_output_dir)
        self.output_dir_browse_button = QPushButton("浏览")
        self.container_combo = QComboBox()
        self.subtitle_mode_combo = QComboBox()
        self.burn_subtitles_checkbox = QCheckBox("烧录字幕到视频")
        self.sidecar_checkbox = QCheckBox("导出 SRT 字幕文件")

        for item in ("mp4", "mov", "mkv"):
            self.container_combo.addItem(item, item)
        self.subtitle_mode_combo.addItem("中英双语", "bilingual")
        self.subtitle_mode_combo.addItem("仅中文", "zh")
        self.subtitle_mode_combo.addItem("仅英文", "en")

        form.addRow("导出目录", _inline_row(self.output_dir_edit, self.output_dir_browse_button))
        form.addRow("封装格式", self.container_combo)
        form.addRow("字幕模式", self.subtitle_mode_combo)
        form.addRow("", self.burn_subtitles_checkbox)
        form.addRow("", self.sidecar_checkbox)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        layout.addWidget(buttons)

        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self.output_dir_browse_button.clicked.connect(self._browse_output_dir)

        self._set_combo_data(self.container_combo, settings.export.output_container)
        self._set_combo_data(self.subtitle_mode_combo, settings.export.subtitle_mode or "bilingual")
        self.burn_subtitles_checkbox.setChecked(settings.export.burn_subtitles)
        self.sidecar_checkbox.setChecked(settings.export.export_sidecar_srt)

    def export_options(self) -> dict[str, object]:
        return {
            "output_dir": self.output_dir_edit.text().strip(),
            "output_container": str(self.container_combo.currentData() or "mp4"),
            "subtitle_mode": str(self.subtitle_mode_combo.currentData() or "bilingual"),
            "burn_subtitles": self.burn_subtitles_checkbox.isChecked(),
            "export_sidecar_srt": self.sidecar_checkbox.isChecked(),
        }

    def _browse_output_dir(self) -> None:
        selected_dir = QFileDialog.getExistingDirectory(
            self,
            "选择导出目录",
            self.output_dir_edit.text().strip(),
        )
        if selected_dir:
            self.output_dir_edit.setText(selected_dir)

    @staticmethod
    def _set_combo_data(combo: QComboBox, data: str) -> None:
        index = combo.findData(data)
        if index >= 0:
            combo.setCurrentIndex(index)
        elif combo.count() > 0:
            combo.setCurrentIndex(0)


class PptGuidedExportDialog(QDialog):
    STEP_COUNT = 5

    def __init__(
        self,
        settings: AppSettings,
        parent: QWidget | None = None,
        preview_image_path: str | Path | None = None,
    ) -> None:
        super().__init__(parent)
        self._theme_mode = settings.appearance.theme_mode
        self._preferred_provider_by_language = {
            "zh": settings.tts.provider_type_for_language("zh"),
            "en": settings.tts.provider_type_for_language("en"),
        }
        self._preferred_voice_by_language = {
            "zh": settings.tts.default_voice_for_language("zh"),
            "en": settings.tts.default_voice_for_language("en"),
        }
        zh_kokoro_ready, _status_text = tts_provider_runtime_status(KOKORO_LOCAL_PROVIDER, language="zh")
        if zh_kokoro_ready and self._preferred_provider_by_language.get("zh") == MELO_LOCAL_PROVIDER:
            self._preferred_provider_by_language["zh"] = KOKORO_LOCAL_PROVIDER
        if self._preferred_provider_by_language.get("zh") == KOKORO_LOCAL_PROVIDER and (
            not self._preferred_voice_by_language.get("zh")
            or self._preferred_voice_by_language.get("zh") == "melo_zh_female"
        ):
            preferred_male_voice_id = _preferred_chinese_male_voice_id()
            if preferred_male_voice_id:
                self._preferred_voice_by_language["zh"] = preferred_male_voice_id
        self.setWindowTitle("一键导出")
        self.resize(900, 760)
        if parent is not None:
            self.setWindowIcon(parent.windowIcon())
        self.setStyleSheet(build_dialog_stylesheet(self._theme_mode, app=QApplication.instance()))

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 18, 18, 18)
        root.setSpacing(14)

        self.step_progress_bar = QProgressBar()
        self.step_progress_bar.setRange(1, self.STEP_COUNT)
        self.step_progress_bar.setValue(1)
        self.step_progress_bar.setFormat("步骤 1 / 5")
        self.step_title_label = QLabel("第一步：选择口播语言")
        self.step_title_label.setObjectName("taskProgressTitle")
        self.step_hint_label = QLabel("按顺序完成选择后，最后一步点击“马上开始”就会自动执行任务。")
        self.step_hint_label.setObjectName("taskProgressHint")
        self.step_hint_label.setWordWrap(True)
        root.addWidget(self.step_progress_bar)
        root.addWidget(self.step_title_label)
        root.addWidget(self.step_hint_label)

        self.stack = QStackedWidget()
        root.addWidget(self.stack, stretch=1)

        self.voiceover_language_combo = QComboBox()
        self.voiceover_language_combo.addItem("中文口播", "zh")
        self.voiceover_language_combo.addItem("英文口播", "en")
        self.stack.addWidget(
            self._build_step_page(
                "口播语言",
                "中文口播会直接使用中文稿，不会额外翻译；英文口播会在需要时自动补齐英文稿。",
                self._build_form_group("语言", [("口播语言", self.voiceover_language_combo)]),
            )
        )

        self.bilingual_subtitles_checkbox = QCheckBox("开启双文字幕")
        self.bilingual_subtitles_checkbox.setChecked(str(settings.export.subtitle_mode or "") == "bilingual")
        subtitle_mode_hint = QLabel(
            "不开启时会跟随口播语言显示单语字幕；开启后会自动补齐另一种语言，并导出双语字幕。"
        )
        subtitle_mode_hint.setWordWrap(True)
        subtitle_mode_panel = QWidget()
        subtitle_mode_layout = QVBoxLayout(subtitle_mode_panel)
        subtitle_mode_layout.setContentsMargins(0, 0, 0, 0)
        subtitle_mode_layout.setSpacing(10)
        subtitle_mode_layout.addWidget(self.bilingual_subtitles_checkbox)
        subtitle_mode_layout.addWidget(subtitle_mode_hint)
        subtitle_mode_layout.addStretch(1)
        self.stack.addWidget(
            self._build_step_page(
                "字幕方案",
                "这里决定导出时是单语字幕还是双语字幕。",
                subtitle_mode_panel,
            )
        )

        self.voice_provider_combo = QComboBox()
        self.voice_status_label = QLabel()
        self.voice_status_label.setWordWrap(True)
        self.voice_combo = QComboBox()
        self.stack.addWidget(
            self._build_step_page(
                "音色",
                "音色会先按口播语言筛选，只给你看当前语言可用的声音。",
                self._build_form_group(
                    "声音",
                    [
                        ("配音引擎", self.voice_provider_combo),
                        ("引擎状态", self.voice_status_label),
                        ("音色", self.voice_combo),
                    ],
                ),
            )
        )

        self.subtitle_font_size_spin = QSpinBox()
        self.subtitle_font_size_spin.setRange(5, 72)
        self.subtitle_font_size_spin.setValue(settings.export.subtitle_font_size)
        self.subtitle_font_size_spin.setSuffix(" 号")
        self.subtitle_english_color_edit = QLineEdit(settings.export.subtitle_english_color)
        self.subtitle_chinese_color_edit = QLineEdit(settings.export.subtitle_chinese_color)
        self.subtitle_english_color_button = QPushButton("颜色")
        self.subtitle_chinese_color_button = QPushButton("颜色")
        self.subtitle_safe_area_checkbox = QCheckBox("启用安全区")
        self.subtitle_safe_area_checkbox.setChecked(settings.export.subtitle_safe_area_enabled)
        self.subtitle_english_x_spin = QSpinBox()
        self.subtitle_english_x_spin.setRange(0, 100)
        self.subtitle_english_x_spin.setSuffix("%")
        self.subtitle_english_x_spin.setValue(int(round(settings.export.subtitle_english_x_percent)))
        self.subtitle_english_y_spin = QSpinBox()
        self.subtitle_english_y_spin.setRange(0, 100)
        self.subtitle_english_y_spin.setSuffix("%")
        self.subtitle_english_y_spin.setValue(int(round(settings.export.subtitle_english_y_percent)))
        self.subtitle_chinese_x_spin = QSpinBox()
        self.subtitle_chinese_x_spin.setRange(0, 100)
        self.subtitle_chinese_x_spin.setSuffix("%")
        self.subtitle_chinese_x_spin.setValue(int(round(settings.export.subtitle_chinese_x_percent)))
        self.subtitle_chinese_y_spin = QSpinBox()
        self.subtitle_chinese_y_spin.setRange(0, 100)
        self.subtitle_chinese_y_spin.setSuffix("%")
        self.subtitle_chinese_y_spin.setValue(int(round(settings.export.subtitle_chinese_y_percent)))
        self.subtitle_position_reset_button = QPushButton("恢复默认位置")
        self.subtitle_preview_surface = SubtitleLayoutPreview()
        self.subtitle_preview_surface.set_preview_image(preview_image_path)
        subtitle_style_group = self._build_form_group(
            "字幕样式",
            [
                ("字号", self.subtitle_font_size_spin),
                ("英文颜色", _inline_row(self.subtitle_english_color_edit, self.subtitle_english_color_button)),
                ("中文颜色", _inline_row(self.subtitle_chinese_color_edit, self.subtitle_chinese_color_button)),
                ("安全区", self.subtitle_safe_area_checkbox),
                ("英文位置", _inline_row(QLabel("X"), self.subtitle_english_x_spin, QLabel("Y"), self.subtitle_english_y_spin)),
                ("中文位置", _inline_row(QLabel("X"), self.subtitle_chinese_x_spin, QLabel("Y"), self.subtitle_chinese_y_spin)),
                ("位置重置", self.subtitle_position_reset_button),
                ("实时预览", self.subtitle_preview_surface),
            ],
        )
        self.stack.addWidget(
            self._build_step_page(
                "字幕样式",
                "这里沿用了设置里的预览能力，可以直接看颜色、大小和位置。",
                subtitle_style_group,
            )
        )

        self.output_dir_edit = QLineEdit(settings.media.default_output_dir)
        self.output_dir_browse_button = QPushButton("浏览")
        self.container_combo = QComboBox()
        for item in ("mp4", "mov", "mkv"):
            self.container_combo.addItem(item, item)
        self.burn_subtitles_checkbox = QCheckBox("烧录字幕到视频")
        self.burn_subtitles_checkbox.setChecked(settings.export.burn_subtitles)
        self.sidecar_checkbox = QCheckBox("同时导出 SRT 文件")
        self.sidecar_checkbox.setChecked(settings.export.export_sidecar_srt)
        self.summary_label = QLabel()
        self.summary_label.setObjectName("taskProgressHint")
        self.summary_label.setWordWrap(True)
        self.stack.addWidget(
            self._build_step_page(
                "确认开始",
                "确认这次导出的语言、字幕和音色后，点击下方按钮开始执行。",
                self._build_form_group(
                    "导出",
                    [
                        ("输出目录", _inline_row(self.output_dir_edit, self.output_dir_browse_button)),
                        ("封装格式", self.container_combo),
                        ("字幕烧录", self.burn_subtitles_checkbox),
                        ("SRT 文件", self.sidecar_checkbox),
                        ("本次配置", self.summary_label),
                    ],
                ),
            )
        )

        action_row = QHBoxLayout()
        self.back_button = QPushButton("上一步")
        self.next_button = QPushButton("下一步")
        self.start_button = QPushButton("马上开始")
        self.cancel_button = QPushButton("取消")
        action_row.addStretch(1)
        action_row.addWidget(self.back_button)
        action_row.addWidget(self.next_button)
        action_row.addWidget(self.start_button)
        action_row.addWidget(self.cancel_button)
        root.addLayout(action_row)

        self._set_combo_data(self.container_combo, settings.export.output_container)
        self.voiceover_language_combo.currentIndexChanged.connect(lambda _index: self._on_language_changed())
        self.voice_provider_combo.currentIndexChanged.connect(lambda _index: self._refresh_voice_options())
        self.voice_combo.currentIndexChanged.connect(lambda _index: self._on_voice_selection_changed())
        self.bilingual_subtitles_checkbox.toggled.connect(lambda _checked: self._refresh_subtitle_preview())
        self.output_dir_browse_button.clicked.connect(self._browse_output_dir)
        self.back_button.clicked.connect(self._go_previous_step)
        self.next_button.clicked.connect(self._go_next_step)
        self.start_button.clicked.connect(self.accept)
        self.cancel_button.clicked.connect(self.reject)
        self.subtitle_english_color_button.clicked.connect(
            lambda: self._pick_subtitle_color(self.subtitle_english_color_edit)
        )
        self.subtitle_chinese_color_button.clicked.connect(
            lambda: self._pick_subtitle_color(self.subtitle_chinese_color_edit)
        )
        self.subtitle_font_size_spin.valueChanged.connect(self._refresh_subtitle_preview)
        self.subtitle_english_color_edit.textChanged.connect(self._refresh_subtitle_preview)
        self.subtitle_chinese_color_edit.textChanged.connect(self._refresh_subtitle_preview)
        self.subtitle_safe_area_checkbox.toggled.connect(self._refresh_subtitle_preview)
        self.subtitle_english_x_spin.valueChanged.connect(self._refresh_subtitle_preview)
        self.subtitle_english_y_spin.valueChanged.connect(self._refresh_subtitle_preview)
        self.subtitle_chinese_x_spin.valueChanged.connect(self._refresh_subtitle_preview)
        self.subtitle_chinese_y_spin.valueChanged.connect(self._refresh_subtitle_preview)
        self.subtitle_position_reset_button.clicked.connect(self._reset_subtitle_positions)
        self.subtitle_preview_surface.position_changed.connect(self._on_preview_position_changed)

        self._on_language_changed()
        self._refresh_subtitle_preview()
        self._update_step_state()

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

    def _build_step_page(self, title: str, description: str, body_widget: QWidget) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        title_label = QLabel(title)
        title_label.setObjectName("settingsSectionTitle")
        desc_label = QLabel(description)
        desc_label.setObjectName("settingsSectionText")
        desc_label.setWordWrap(True)
        layout.addWidget(title_label)
        layout.addWidget(desc_label)
        layout.addWidget(body_widget)
        layout.addStretch(1)
        return container

    def _selected_voiceover_language(self) -> str:
        return str(self.voiceover_language_combo.currentData() or "zh")

    def _preferred_provider_for_current_language(self) -> str:
        return self._preferred_provider_by_language.get(
            self._selected_voiceover_language(),
            "",
        )

    def _preferred_voice_for_current_language(self) -> str:
        return self._preferred_voice_by_language.get(
            self._selected_voiceover_language(),
            "",
        )

    def _effective_subtitle_mode(self) -> str:
        if self.bilingual_subtitles_checkbox.isChecked():
            return "bilingual"
        return self._selected_voiceover_language()

    def _on_language_changed(self) -> None:
        language = self._selected_voiceover_language()
        current_provider = self._preferred_provider_by_language.get(language, "")
        self.voice_provider_combo.blockSignals(True)
        try:
            self.voice_provider_combo.clear()
            for label, provider_type in _voice_provider_choices(language):
                self.voice_provider_combo.addItem(label, provider_type)
            self._set_combo_data(self.voice_provider_combo, current_provider)
        finally:
            self.voice_provider_combo.blockSignals(False)
        self._refresh_voice_options()
        self._refresh_subtitle_preview()

    def _refresh_voice_options(self) -> None:
        provider_type = str(self.voice_provider_combo.currentData() or "").strip()
        language = self._selected_voiceover_language()
        self._preferred_provider_by_language[language] = provider_type
        is_ready, status_text = tts_provider_runtime_status(provider_type, language=language)
        status_prefix = "可用" if is_ready else "不可用"
        self.voice_status_label.setText(
            f"{self.voice_provider_combo.currentText().strip() or tts_provider_display_name(provider_type)} | {status_prefix} | {status_text}"
        )
        self.voice_combo.blockSignals(True)
        try:
            self.voice_combo.clear()
            for voice in available_tts_voices(provider_type, language=language):
                self.voice_combo.addItem(_voice_display_text(voice), voice.voice_id)
            self._set_combo_data(
                self.voice_combo,
                self._preferred_voice_by_language.get(language, ""),
            )
        finally:
            self.voice_combo.blockSignals(False)
        self._preferred_voice_by_language[language] = str(self.voice_combo.currentData() or "")
        self._refresh_summary()

    def _on_voice_selection_changed(self) -> None:
        language = self._selected_voiceover_language()
        self._preferred_voice_by_language[language] = str(self.voice_combo.currentData() or "")
        self._refresh_summary()

    def _refresh_summary(self) -> None:
        language_text = "中文口播" if self._selected_voiceover_language() == "zh" else "英文口播"
        subtitle_mode = self._effective_subtitle_mode()
        if subtitle_mode == "bilingual":
            subtitle_text = "中英双语字幕"
        elif subtitle_mode == "zh":
            subtitle_text = "中文字幕"
        else:
            subtitle_text = "英文字幕"
        voice_text = self.voice_combo.currentText().strip() or "未选择"
        self.summary_label.setText(
            f"口播：{language_text}\n字幕：{subtitle_text}\n音色：{voice_text}"
        )

    def _update_step_state(self) -> None:
        step_index = self.stack.currentIndex()
        self.step_progress_bar.setValue(step_index + 1)
        self.step_progress_bar.setFormat(f"步骤 {step_index + 1} / {self.STEP_COUNT}")
        titles = [
            "第一步：选择口播语言",
            "第二步：选择字幕方案",
            "第三步：选择音色",
            "第四步：调整字幕样式",
            "第五步：确认并开始",
        ]
        self.step_title_label.setText(titles[step_index])
        self.back_button.setEnabled(step_index > 0)
        self.next_button.setVisible(step_index < self.STEP_COUNT - 1)
        self.start_button.setVisible(step_index == self.STEP_COUNT - 1)
        self._refresh_summary()

    def _go_previous_step(self) -> None:
        self.stack.setCurrentIndex(max(0, self.stack.currentIndex() - 1))
        self._update_step_state()

    def _go_next_step(self) -> None:
        self.stack.setCurrentIndex(min(self.STEP_COUNT - 1, self.stack.currentIndex() + 1))
        self._update_step_state()

    def _browse_output_dir(self) -> None:
        selected_dir = QFileDialog.getExistingDirectory(
            self,
            "选择导出目录",
            self.output_dir_edit.text().strip(),
        )
        if selected_dir:
            self.output_dir_edit.setText(selected_dir)

    def _subtitle_preview_color(self, value: str, fallback: str) -> QColor:
        color = QColor(value.strip() or fallback)
        if color.isValid():
            return color
        return QColor(fallback)

    def _subtitle_position_values(self, language: str) -> tuple[float, float]:
        if language == "zh":
            return (
                float(self.subtitle_chinese_x_spin.value()),
                float(self.subtitle_chinese_y_spin.value()),
            )
        return (
            float(self.subtitle_english_x_spin.value()),
            float(self.subtitle_english_y_spin.value()),
        )

    def _position_bounds(self) -> tuple[float, float, float, float]:
        if self.subtitle_safe_area_checkbox.isChecked():
            return (
                _SUBTITLE_SAFE_MARGIN_X_PERCENT,
                100.0 - _SUBTITLE_SAFE_MARGIN_X_PERCENT,
                _SUBTITLE_SAFE_MARGIN_Y_PERCENT,
                100.0 - _SUBTITLE_SAFE_MARGIN_Y_PERCENT,
            )
        return (4.0, 96.0, 4.0, 96.0)

    def _set_subtitle_position_values(self, language: str, x_percent: float, y_percent: float) -> None:
        target_x_spin = self.subtitle_chinese_x_spin if language == "zh" else self.subtitle_english_x_spin
        target_y_spin = self.subtitle_chinese_y_spin if language == "zh" else self.subtitle_english_y_spin
        min_x, max_x, min_y, max_y = self._position_bounds()
        for spin, value in (
            (target_x_spin, int(round(max(min_x, min(max_x, x_percent))))),
            (target_y_spin, int(round(max(min_y, min(max_y, y_percent))))),
        ):
            spin.blockSignals(True)
            try:
                spin.setValue(value)
            finally:
                spin.blockSignals(False)

    def _on_preview_position_changed(self, language: str, x_percent: float, y_percent: float) -> None:
        self._set_subtitle_position_values(language, x_percent, y_percent)
        self._refresh_subtitle_preview()

    def _reset_subtitle_positions(self) -> None:
        self._set_subtitle_position_values("en", 50.0, 90.0)
        self._set_subtitle_position_values("zh", 50.0, 82.0)
        self._refresh_subtitle_preview()

    def _pick_subtitle_color(self, target_edit: QLineEdit) -> None:
        current = QColor(target_edit.text().strip() or "#FFFFFF")
        if not current.isValid():
            current = QColor("#FFFFFF")
        color = QColorDialog.getColor(
            current,
            self,
            "选择字幕颜色",
            QColorDialog.ColorDialogOption.ShowAlphaChannel,
        )
        if not color.isValid():
            return
        if color.alpha() >= 255:
            target_edit.setText(f"#{color.red():02X}{color.green():02X}{color.blue():02X}")
        else:
            target_edit.setText(
                f"#{color.alpha():02X}{color.red():02X}{color.green():02X}{color.blue():02X}"
            )

    def _refresh_subtitle_preview(self) -> None:
        self._set_subtitle_position_values("en", *self._subtitle_position_values("en"))
        self._set_subtitle_position_values("zh", *self._subtitle_position_values("zh"))
        self.subtitle_preview_surface.set_preview_state(
            subtitle_mode=self._effective_subtitle_mode(),
            font_size=max(10, int(self.subtitle_font_size_spin.value())),
            english_color=self._subtitle_preview_color(self.subtitle_english_color_edit.text(), "#FFFFFF"),
            chinese_color=self._subtitle_preview_color(self.subtitle_chinese_color_edit.text(), "#FFD966"),
            safe_area_enabled=self.subtitle_safe_area_checkbox.isChecked(),
            english_position=self._subtitle_position_values("en"),
            chinese_position=self._subtitle_position_values("zh"),
        )
        self._refresh_summary()

    def export_options(self) -> dict[str, object]:
        return {
            "voiceover_language": self._selected_voiceover_language(),
            "selected_provider_type": str(self.voice_provider_combo.currentData() or ""),
            "selected_voice_id": str(self.voice_combo.currentData() or ""),
            "output_dir": self.output_dir_edit.text().strip(),
            "output_container": str(self.container_combo.currentData() or "mp4"),
            "subtitle_mode": self._effective_subtitle_mode(),
            "burn_subtitles": self.burn_subtitles_checkbox.isChecked(),
            "export_sidecar_srt": self.sidecar_checkbox.isChecked(),
            "subtitle_font_size": int(self.subtitle_font_size_spin.value()),
            "subtitle_english_color": self.subtitle_english_color_edit.text().strip(),
            "subtitle_chinese_color": self.subtitle_chinese_color_edit.text().strip(),
            "subtitle_safe_area_enabled": self.subtitle_safe_area_checkbox.isChecked(),
            "subtitle_english_x_percent": float(self.subtitle_english_x_spin.value()),
            "subtitle_english_y_percent": float(self.subtitle_english_y_spin.value()),
            "subtitle_chinese_x_percent": float(self.subtitle_chinese_x_spin.value()),
            "subtitle_chinese_y_percent": float(self.subtitle_chinese_y_spin.value()),
        }

    @staticmethod
    def _set_combo_data(combo: QComboBox, data: str) -> None:
        index = combo.findData(data)
        if index >= 0:
            combo.setCurrentIndex(index)
        elif combo.count() > 0:
            combo.setCurrentIndex(0)


class PptExportProcessController(QObject):
    progress = Signal(str)
    progress_value = Signal(int, str)
    finished = Signal(object)
    cancelled = Signal(str)
    error = Signal(str)

    def __init__(
        self,
        project: Project,
        settings: AppSettings,
        *,
        output_dir: str,
        output_container: str,
        burn_subtitles: bool,
        export_sidecar_srt: bool,
        subtitle_mode: str,
        voiceover_language: str,
        selected_provider_type: str,
        selected_voice_id: str,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.task_label = "PPT 口播导出"
        self._project = Project.from_dict(project.to_dict())
        self._settings = AppSettings.from_dict(settings.to_dict())
        self._output_dir = str(output_dir)
        self._output_container = str(output_container or "mp4")
        self._burn_subtitles = bool(burn_subtitles)
        self._export_sidecar_srt = bool(export_sidecar_srt)
        self._subtitle_mode = str(subtitle_mode or "bilingual")
        self._voiceover_language = str(voiceover_language or "zh").strip().lower()
        self._selected_provider_type = str(selected_provider_type or "").strip()
        self._selected_voice_id = str(selected_voice_id or "").strip()
        self._process = QProcess(self)
        self._process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self._process.readyReadStandardOutput.connect(self._on_ready_read)
        self._process.finished.connect(self._on_finished)
        self._process.errorOccurred.connect(self._on_error_occurred)
        self._stdout_buffer = ""
        self._stdout_lines: list[str] = []
        self._temp_dir: tempfile.TemporaryDirectory[str] | None = None
        self._result_path: Path | None = None
        self._cancel_requested = False
        self._completed = False
        self._last_progress_emit_at = 0.0
        self._last_progress_value: int | None = None
        self._last_progress_message = ""
        self._start_timeout_timer = QTimer(self)
        self._start_timeout_timer.setSingleShot(True)
        self._start_timeout_timer.setInterval(3500)
        self._start_timeout_timer.timeout.connect(self._on_start_timeout)
        self._process.started.connect(self._on_started)

    def supports_pause(self) -> bool:
        return False

    def supports_cancel(self) -> bool:
        return True

    def is_paused(self) -> bool:
        return False

    def request_pause(self) -> None:
        raise RuntimeError("当前一键导出暂不支持暂停。")

    def request_resume(self) -> None:
        raise RuntimeError("当前一键导出暂不支持继续。")

    def request_cancel(self) -> None:
        self._cancel_requested = True
        if self._process.state() != QProcess.ProcessState.NotRunning:
            self._process.terminate()
            QTimer.singleShot(2500, self._force_kill_if_needed)

    def start(self) -> None:
        self._settings.tts.provider_type = self._settings.tts.provider_type_for_language(
            self._voiceover_language
        )
        self._settings.tts.default_voice = self._settings.tts.default_voice_for_language(
            self._voiceover_language
        )
        if self._selected_provider_type:
            self._settings.tts.provider_type = self._selected_provider_type
        if self._selected_voice_id:
            self._settings.tts.default_voice = self._selected_voice_id

        self._temp_dir = tempfile.TemporaryDirectory(prefix="vcut_ppt_export_job_")
        temp_dir = Path(self._temp_dir.name)
        project_path = temp_dir / "project.vcutproj"
        settings_path = temp_dir / "settings.json"
        self._result_path = temp_dir / "result.json"
        project_path.write_text(
            json.dumps(self._project.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        settings_path.write_text(
            json.dumps(self._settings.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        src_root = str(Path(__file__).resolve().parents[2])
        repo_root = str(Path(__file__).resolve().parents[3])
        env = QProcessEnvironment.systemEnvironment()
        existing_pythonpath = env.value("PYTHONPATH", "").strip()
        env.insert("PYTHONPATH", src_root if not existing_pythonpath else src_root + os.pathsep + existing_pythonpath)
        env.insert("PYTHONIOENCODING", "utf-8")
        self._process.setProcessEnvironment(env)
        self._process.setWorkingDirectory(repo_root)

        runner_args = [
            "--project",
            str(project_path),
            "--settings",
            str(settings_path),
            "--result",
            str(self._result_path),
            "--output-dir",
            self._output_dir,
            "--container",
            self._output_container,
            "--subtitle-mode",
            self._subtitle_mode,
            "--voiceover-language",
            self._voiceover_language,
            "--selected-voice-id",
            self._selected_voice_id or self._settings.tts.default_voice,
        ]
        if getattr(sys, "frozen", False):
            arguments = ["--ppt-export-runner", *runner_args]
        else:
            arguments = ["-u", "-m", "vcut_studio.ppt_export_runner", *runner_args]
        if self._burn_subtitles:
            arguments.append("--burn-subtitles")
        if self._export_sidecar_srt:
            arguments.append("--export-sidecar-srt")

        self.progress.emit("正在启动 PPT 口播导出任务...")
        self.progress_value.emit(1, "正在启动 PPT 口播导出任务...")
        self._start_timeout_timer.start()
        self._process.start(sys.executable, arguments)
        if False:
            self._emit_error_once("PPT 导出进程启动失败。")

    def _force_kill_if_needed(self) -> None:
        if self._process.state() != QProcess.ProcessState.NotRunning:
            self._process.kill()

    def _on_started(self) -> None:
        self._start_timeout_timer.stop()

    def _on_start_timeout(self) -> None:
        if self._completed or self._cancel_requested:
            return
        if self._process.state() == QProcess.ProcessState.Starting:
            self._emit_error_once("PPT export process failed to start.")

    def _on_ready_read(self) -> None:
        chunk = bytes(self._process.readAllStandardOutput()).decode("utf-8", "replace")
        if not chunk:
            return
        self._stdout_buffer += chunk
        while "\n" in self._stdout_buffer:
            line, self._stdout_buffer = self._stdout_buffer.split("\n", 1)
            self._handle_output_line(line.strip())

    def _handle_output_line(self, line: str) -> None:
        if not line:
            return
        self._stdout_lines.append(line)
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            return
        if payload.get("type") != "progress":
            return
        value = int(payload.get("value", 0))
        message = str(payload.get("message", "")).strip()
        self._emit_progress_update(
            46 + int(max(0, min(value, 100)) * 54 / 100),
            message,
        )

    def _on_error_occurred(self, _error: QProcess.ProcessError) -> None:
        if self._completed:
            return
        if self._process.state() == QProcess.ProcessState.NotRunning and not self._cancel_requested:
            self._emit_error_once("PPT 导出进程异常退出。")

    def _on_finished(self, exit_code: int, _exit_status: QProcess.ExitStatus) -> None:
        if self._stdout_buffer.strip():
            self._handle_output_line(self._stdout_buffer.strip())
        self._stdout_buffer = ""
        if self._completed:
            self._cleanup_temp_dir()
            return
        self._completed = True
        if self._cancel_requested:
            self.cancelled.emit("已终止导出任务。")
            self._cleanup_temp_dir()
            return
        if exit_code != 0:
            stdout_tail = "\n".join(self._stdout_lines[-12:])
            self.error.emit(stdout_tail or "PPT 导出子进程执行失败。")
            self._cleanup_temp_dir()
            return
        if self._result_path is None or not self._result_path.exists():
            self.error.emit("PPT 导出子进程没有生成结果文件。")
            self._cleanup_temp_dir()
            return

        try:
            result_data = json.loads(self._result_path.read_text(encoding="utf-8"))
            output_path = Path(str(result_data.get("output_path", "") or ""))
            if not str(output_path):
                raise RuntimeError("PPT 导出结果缺少输出路径。")
            project_snapshot = Project.from_dict(result_data.get("project_snapshot", {}))
        except Exception as exc:
            self.error.emit(str(exc))
            self._cleanup_temp_dir()
            return

        self.finished.emit(
            PptExportResult(
                plan=ExportPlan(cut_ranges=[], keep_ranges=[], output_path=output_path),
                project_snapshot=project_snapshot,
            )
        )
        self._cleanup_temp_dir()

    def _emit_error_once(self, message: str) -> None:
        if self._completed:
            return
        self._completed = True
        self._start_timeout_timer.stop()
        self.error.emit(message)
        self._cleanup_temp_dir()

    def _cleanup_temp_dir(self) -> None:
        self._start_timeout_timer.stop()
        if self._temp_dir is None:
            return
        self._temp_dir.cleanup()
        self._temp_dir = None

    def _emit_progress_update(self, value: int, message: str) -> None:
        clamped_value = max(0, min(int(value), 100))
        normalized_message = str(message or "")
        now = time.monotonic()

        should_emit = False
        if self._last_progress_value is None:
            should_emit = True
        elif clamped_value in {0, 100}:
            should_emit = True
        elif clamped_value != self._last_progress_value and abs(clamped_value - self._last_progress_value) >= 2:
            should_emit = True
        elif normalized_message != self._last_progress_message and now - self._last_progress_emit_at >= 0.15:
            should_emit = True
        elif now - self._last_progress_emit_at >= 0.4:
            should_emit = True

        if not should_emit:
            return

        self._last_progress_emit_at = now
        self._last_progress_value = clamped_value
        self._last_progress_message = normalized_message
        self.progress.emit(normalized_message)
        self.progress_value.emit(clamped_value, normalized_message)


class PptMainWindow(QMainWindow):
    def __init__(self, context: AppContext) -> None:
        super().__init__()
        self.context = context
        self.import_busy = False
        self.translation_busy = False
        self.export_busy = False
        self.voice_input_busy = False
        self.voice_recording_active = False
        self.import_thread: QThread | None = None
        self.import_worker: PptImportWorker | None = None
        self.translation_thread: QThread | None = None
        self.translation_worker: PptScriptGenerationWorker | PptScriptTranslationWorker | None = None
        self.export_thread: QThread | None = None
        self.export_worker: PptExportWorker | PptExportProcessController | None = None
        self.voice_input_thread: QThread | None = None
        self.voice_input_worker: PptVoiceInputWorker | None = None
        self._guided_export_pending_options: dict[str, object] | None = None
        self._active_background_worker: object | None = None
        self._voice_input_target_slide_index = 0
        self._voice_input_pending_output_path: Path | None = None
        self._voice_input_should_transcribe = False
        self._pending_import_project: Project | None = None
        self._pending_import_ppt_path: Path | None = None
        self._voice_capture_session: QMediaCaptureSession | None = None
        self._voice_audio_input: QAudioInput | None = None
        self._voice_recorder: QMediaRecorder | None = None
        self._task_heartbeat_phase = 0
        self._task_heartbeat_timer = QTimer(self)
        self._task_heartbeat_timer.setInterval(180)
        self._task_heartbeat_timer.timeout.connect(self._on_task_heartbeat)
        self._deferred_autosave_timer = QTimer(self)
        self._deferred_autosave_timer.setSingleShot(True)
        self._deferred_autosave_timer.setInterval(1200)
        self._deferred_autosave_timer.timeout.connect(self._autosave_project_silently)

        self.resize(1560, 940)
        self._apply_window_icon()
        self.setWindowTitle("片语 | PPT配音")

        self.workspace = PptWorkspacePage()
        self.setCentralWidget(self.workspace)
        self.setStatusBar(QStatusBar())
        self._build_status_controls()
        self._setup_voice_input_recorder()
        self._create_menu()
        self._connect_signals()
        self._apply_theme()
        self.refresh_views()

    def _apply_window_icon(self) -> None:
        icon_path = find_app_icon()
        if icon_path is not None:
            self.setWindowIcon(QIcon(str(icon_path)))

    def _apply_theme(self) -> None:
        self.setStyleSheet(
            build_window_stylesheet(
                self.context.settings.appearance.theme_mode,
                app=QApplication.instance(),
            )
        )

    def _build_status_controls(self) -> None:
        container = QWidget(self)
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.background_task_label = QLabel("后台任务：空闲")
        self.pause_task_button = QPushButton("暂停")
        self.resume_task_button = QPushButton("继续")
        self.cancel_task_button = QPushButton("终止")

        layout.addWidget(self.background_task_label)
        layout.addWidget(self.pause_task_button)
        layout.addWidget(self.resume_task_button)
        layout.addWidget(self.cancel_task_button)

        self.statusBar().addPermanentWidget(container, 1)

    def _setup_voice_input_recorder(self) -> None:
        self._voice_capture_session = QMediaCaptureSession(self)
        self._voice_audio_input = QAudioInput(self)
        self._voice_recorder = QMediaRecorder(self)

        self._voice_capture_session.setAudioInput(self._voice_audio_input)
        self._voice_capture_session.setRecorder(self._voice_recorder)

        media_format = QMediaFormat()
        media_format.setFileFormat(QMediaFormat.FileFormat.Wave)
        media_format.setAudioCodec(QMediaFormat.AudioCodec.Wave)
        self._voice_recorder.setMediaFormat(media_format)
        self._voice_recorder.setAudioSampleRate(16000)
        self._voice_recorder.setAudioBitRate(256000)
        self._voice_recorder.recorderStateChanged.connect(self._on_voice_recorder_state_changed)
        self._voice_recorder.errorOccurred.connect(lambda *_args: self._on_voice_recorder_error())

    def _voice_input_output_dir(self) -> Path:
        output_dir = Path(self.context.settings.workspace.temp_dir) / "ppt_voice_input"
        output_dir.mkdir(parents=True, exist_ok=True)
        return output_dir

    def _create_menu(self) -> None:
        file_menu = self.menuBar().addMenu("文件")
        file_menu.addAction("新建项目", self.new_project)
        file_menu.addAction("导入 PPT", self.import_ppt)
        file_menu.addAction("打开项目", self.open_project)
        file_menu.addAction("保存项目", self.save_project)
        file_menu.addAction("项目另存为", self.save_project_as)
        file_menu.addSeparator()
        file_menu.addAction("导出视频", self.show_export_dialog)
        file_menu.addAction("一键导出", self.show_guided_export_dialog)
        file_menu.addSeparator()
        file_menu.addAction("退出", self.close)

        tools_menu = self.menuBar().addMenu("工具")
        tools_menu.addAction("整套生成中文稿", self.generate_all_scripts)
        tools_menu.addAction("整套翻译英文稿", self.translate_all_scripts)
        tools_menu.addSeparator()
        tools_menu.addAction("设置", self.show_settings_dialog)

    def _connect_signals(self) -> None:
        self.workspace.slide_selection_changed.connect(
            lambda _slide_index: self._refresh_interactive_state()
        )
        self.workspace.chinese_script_edited.connect(self.update_slide_chinese_script)
        self.workspace.english_script_edited.connect(self.update_slide_english_script)
        self.workspace.generate_all_requested.connect(self.generate_all_scripts)
        self.workspace.generate_current_requested.connect(self.generate_current_script)
        self.workspace.translate_all_requested.connect(self.translate_all_scripts)
        self.workspace.translate_current_requested.connect(self.translate_current_script)
        self.workspace.export_requested.connect(self.show_export_dialog)
        self.workspace.quick_export_requested.connect(self.show_guided_export_dialog)
        self.workspace.save_requested.connect(self.save_project)
        self.workspace.chinese_voice_input_pressed.connect(self.start_chinese_voice_input)
        self.workspace.chinese_voice_input_released.connect(self.stop_chinese_voice_input)
        self.pause_task_button.clicked.connect(self.pause_active_task)
        self.resume_task_button.clicked.connect(self.resume_active_task)
        self.cancel_task_button.clicked.connect(self.cancel_active_task)

    def refresh_views(self, *, reload_project: bool = True) -> None:
        if reload_project:
            self.workspace.load_project(self.context.current_project)
        self._refresh_interactive_state()
        self._update_window_title()

    def _refresh_interactive_state(self) -> None:
        running = self._background_job_in_progress()
        self.workspace.set_running(running)
        if running:
            if not self._task_heartbeat_timer.isActive():
                self._task_heartbeat_phase = 0
                self._task_heartbeat_timer.start()
        elif self._task_heartbeat_timer.isActive():
            self._task_heartbeat_timer.stop()
            self._task_heartbeat_phase = 0
        if self.voice_recording_active:
            self.background_task_label.setText("后台任务：语音录音中")
        elif self.voice_input_busy and self._active_background_worker is None:
            self.background_task_label.setText("后台任务：中文语音转文字")
        elif self._active_background_worker is None:
            self.background_task_label.setText("后台任务：空闲")
        supports_pause = getattr(self._active_background_worker, "supports_pause", None)
        supports_cancel = getattr(self._active_background_worker, "supports_cancel", None)
        self.pause_task_button.setEnabled(
            bool(self._active_background_worker and callable(supports_pause) and supports_pause())
            and not self._active_worker_paused()
        )
        self.resume_task_button.setEnabled(
            bool(self._active_background_worker and callable(supports_pause) and supports_pause())
            and self._active_worker_paused()
        )
        self.cancel_task_button.setEnabled(
            bool(self._active_background_worker and callable(supports_cancel) and supports_cancel())
        )

    def _update_window_title(self) -> None:
        project = self.context.current_project
        dirty_suffix = " *" if self.context.project_dirty else ""
        self.setWindowTitle(f"片语 | {project.name or '未命名演示'}{dirty_suffix}")

    def _slide_by_index(self, slide_index: int) -> SlidePage | None:
        for slide in self.context.current_project.slides:
            if slide.slide_index == slide_index:
                return slide
        return None

    def _apply_export_project_snapshot(self, project_snapshot: Project | None) -> None:
        if project_snapshot is None:
            return
        current_project = self.context.current_project
        current_project.deck_summary = project_snapshot.deck_summary
        snapshot_by_index = {slide.slide_index: slide for slide in project_snapshot.slides}
        for slide in current_project.slides:
            snapshot_slide = snapshot_by_index.get(slide.slide_index)
            if snapshot_slide is None:
                continue
            slide.zh_script = snapshot_slide.zh_script
            slide.en_script = snapshot_slide.en_script
            slide.estimated_duration_ms = snapshot_slide.estimated_duration_ms
            slide.actual_tts_duration_ms = snapshot_slide.actual_tts_duration_ms
            slide.translation_status = snapshot_slide.translation_status
            slide.tts_status = snapshot_slide.tts_status
            slide.voice_id = snapshot_slide.voice_id
            slide.tts_audio_path = snapshot_slide.tts_audio_path

    def _invalidate_slide_audio(self, slide: SlidePage) -> None:
        slide.tts_status = "pending"
        slide.actual_tts_duration_ms = 0
        slide.tts_audio_path = None

    def _mark_project_dirty(self) -> None:
        self.context.mark_project_dirty()
        self._update_window_title()

    def _autosave_project_silently(self) -> None:
        project_path = self.context.current_project_path
        if project_path is None:
            return
        try:
            self.context.project_store.save(self.context.current_project, project_path)
        except Exception:
            return
        self.context.mark_project_saved(project_path)
        self._update_window_title()

    def _schedule_project_autosave(self) -> None:
        if self.context.current_project_path is None:
            return
        self._deferred_autosave_timer.start()

    def _slides_with_existing_content(self, slide_indices: list[int], field_name: str) -> list[int]:
        existing: list[int] = []
        for slide_index in slide_indices:
            slide = self._slide_by_index(slide_index)
            if slide is None:
                continue
            if str(getattr(slide, field_name, "") or "").strip():
                existing.append(slide_index)
        return existing

    def _confirm_replace_existing_content(
        self,
        slide_indices: list[int],
        *,
        field_name: str,
        content_label: str,
    ) -> bool:
        existing = self._slides_with_existing_content(slide_indices, field_name)
        if not existing:
            return True
        preview = ", ".join(str(item) for item in existing[:12])
        message = (
            f"以下页面已经有{content_label}：{preview}"
            + (" ..." if len(existing) > 12 else "")
            + f"\n\n重新生成会直接替换现有{content_label}，是否继续？"
        )
        reply = QMessageBox.question(
            self,
            f"已存在{content_label}",
            message,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return reply == QMessageBox.StandardButton.Yes

    def _background_job_in_progress(self) -> bool:
        return self.import_busy or self.translation_busy or self.export_busy or self.voice_input_busy

    def _active_worker_paused(self) -> bool:
        is_paused = getattr(self._active_background_worker, "is_paused", None)
        return bool(callable(is_paused) and is_paused())

    def _set_active_background_worker(self, worker: object | None) -> None:
        self._active_background_worker = worker
        if worker is not None:
            self.background_task_label.setText(f"后台任务：{getattr(worker, 'task_label', '后台任务')}")
            self._refresh_interactive_state()
            return
        if worker is None:
            self.background_task_label.setText("后台任务：空闲")
        else:
            self.background_task_label.setText(f"后台任务：{worker.task_label}")
        self._refresh_interactive_state()

    def _set_task_progress(self, progress: int, message: str) -> None:
        clamped = max(0, min(int(progress), 100))
        self.workspace.set_progress(clamped, message)
        self.statusBar().showMessage(message, 0)

    def _clear_task_progress(self) -> None:
        self.workspace.set_running(False)
        self._refresh_interactive_state()

    def _show_task_started(self, message: str) -> None:
        self.statusBar().showMessage(message, 0)
        self.workspace.set_running(True)
        self._set_task_progress(1, message)

    def _on_task_heartbeat(self) -> None:
        if not self._background_job_in_progress():
            self._task_heartbeat_timer.stop()
            self._task_heartbeat_phase = 0
            return
        self._task_heartbeat_phase = (self._task_heartbeat_phase + 1) % 4
        dots = "." * self._task_heartbeat_phase
        base_text = self.background_task_label.text().split("…", 1)[0].split("...", 1)[0].rstrip(". ")
        self.background_task_label.setText(f"{base_text}{dots}")

    def _ensure_translation_configured(self) -> bool:
        missing: list[str] = []
        if not self.context.settings.translation.base_url.strip():
            missing.append("Base URL")
        if not self.context.settings.translation.api_key.strip():
            missing.append("API Key")
        if not self.context.settings.translation.model.strip():
            missing.append("模型名称")
        if not missing:
            return True
        QMessageBox.information(
            self,
            "请先配置大模型",
            "在生成或翻译口播稿之前，请先在设置中补全：\n- " + "\n- ".join(missing),
        )
        return False

    def _ensure_export_ready(self) -> bool:
        project = self.context.current_project
        if not project.slides:
            QMessageBox.information(self, "无法导出视频", "请先导入 PPT。")
            return False
        return True

    def _ensure_guided_export_ready(self) -> bool:
        if self._background_job_in_progress():
            QMessageBox.information(self, "后台任务进行中", "请等待当前后台任务结束后再继续。")
            return False
        if not self.context.current_project.slides:
            QMessageBox.information(self, "无法开始导出", "请先导入 PPT。")
            return False
        return True

    def _guided_export_missing_scripts(self, options: dict[str, object]) -> tuple[list[int], list[int]]:
        project = self.context.current_project
        voiceover_language = resolve_ppt_voiceover_language(
            project,
            str(options.get("voiceover_language", "zh") or "zh").strip().lower(),
        )
        subtitle_mode = resolve_ppt_subtitle_mode(
            project,
            str(options.get("subtitle_mode", "bilingual") or "bilingual").strip().lower(),
        )
        need_english = voiceover_language == "en" or subtitle_mode in {"en", "bilingual"}
        need_chinese = voiceover_language == "zh" or subtitle_mode in {"zh", "bilingual"} or need_english
        slides = project.slides
        missing_chinese = [slide.slide_index for slide in slides if need_chinese and not slide.zh_script.strip()]
        missing_english = [slide.slide_index for slide in slides if need_english and not slide.en_script.strip()]
        return (missing_chinese, missing_english)

    def _cancel_guided_export_pipeline(self) -> None:
        self._guided_export_pending_options = None

    def _continue_guided_export_pipeline(self) -> None:
        if self._guided_export_pending_options is None:
            return
        if self._background_job_in_progress():
            return
        options = dict(self._guided_export_pending_options)
        missing_chinese, missing_english = self._guided_export_missing_scripts(options)
        if missing_chinese:
            self._start_generation_job(missing_chinese, "一键导出：正在补全中文口播稿...")
            return
        if missing_english:
            self._start_translation_job(missing_english, "一键导出：正在补全英文口播稿...")
            return
        self._guided_export_pending_options = None
        options["prepare_scripts"] = False
        self._start_export_job(options)

    def _start_guided_export_pipeline(self, options: dict[str, object]) -> None:
        missing_chinese, missing_english = self._guided_export_missing_scripts(options)
        if (missing_chinese or missing_english) and not self._ensure_translation_configured():
            return
        self._guided_export_pending_options = dict(options)
        self._continue_guided_export_pipeline()

    def _confirm_can_replace_project(self) -> bool:
        if self.voice_recording_active:
            QMessageBox.information(self, "正在录音", "请先松开话筒，结束当前录音后再继续。")
            return False
        if self._background_job_in_progress():
            QMessageBox.information(self, "后台任务进行中", "请等当前后台任务结束后再继续。")
            return False
        if not self.context.project_dirty:
            return True
        reply = QMessageBox.question(
            self,
            "当前项目尚未保存",
            "当前项目有未保存修改。继续前是否先保存？",
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

    def new_project(self) -> None:
        if not self._confirm_can_replace_project():
            return
        self.context.set_project(Project.new("未命名演示", project_kind="ppt"))
        self.refresh_views()
        self.statusBar().showMessage("已新建空白 PPT 项目。", 5000)

    def import_ppt(self) -> None:
        if not self._confirm_can_replace_project():
            return
        selected_path, _ = QFileDialog.getOpenFileName(
            self,
            "导入 PPT",
            "",
            "PowerPoint (*.pptx *.ppt)",
        )
        if not selected_path:
            return
        self._import_ppt_from_path(Path(selected_path))

    def _import_preview_dir(self, project: Project) -> Path:
        return (
            Path(self.context.settings.workspace.workspace_dir)
            / "ppt_previews"
            / f"{sanitize_filename(project.name)}_{project.project_id[:8]}"
        )

    def _import_ppt_from_path(self, ppt_path: Path) -> None:
        if self._background_job_in_progress():
            QMessageBox.information(self, "后台任务进行中", "请等待当前后台任务结束后再继续。")
            return
        project = Project.new(ppt_path.stem, project_kind="ppt")
        preview_dir = self._import_preview_dir(project)
        worker = PptImportWorker(ppt_path, preview_dir)
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._on_import_progress)
        worker.progress_value.connect(self._on_import_progress_value)
        worker.finished.connect(self._on_import_finished)
        worker.error.connect(self._on_import_error)
        worker.finished.connect(thread.quit)
        worker.error.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        worker.error.connect(worker.deleteLater)
        thread.finished.connect(self._cleanup_import_job)
        thread.finished.connect(thread.deleteLater)

        self.import_busy = True
        self.import_thread = thread
        self.import_worker = worker
        self._pending_import_project = project
        self._pending_import_ppt_path = Path(ppt_path)
        self._set_active_background_worker(worker)
        self._show_task_started(f"正在启动 PPT 导入：{ppt_path.name}")
        thread.start()

    def _on_import_progress(self, message: str) -> None:
        self.statusBar().showMessage(message, 0)

    def _on_import_progress_value(self, progress: int, message: str) -> None:
        self._set_task_progress(progress, message)

    def _on_import_finished(self, result: PptImportJobResult) -> None:
        project = self._pending_import_project
        ppt_path = self._pending_import_ppt_path
        self.import_busy = False
        self._set_active_background_worker(None)
        if project is None or ppt_path is None:
            self._clear_task_progress()
            return
        project.source_ppt_path = str(ppt_path)
        project.rendered_slides_dir = str(result.import_result.rendered_slides_dir)
        project.deck_summary = result.import_result.deck_summary
        project.slides = result.import_result.slides
        self.context.set_project(project)
        self.context.mark_project_dirty()
        self.refresh_views()
        self._clear_task_progress()
        self.statusBar().showMessage(f"已导入 PPT：{ppt_path.name}，共 {len(project.slides)} 页。", 8000)

    def _on_import_error(self, message: str) -> None:
        self.import_busy = False
        self._pending_import_project = None
        self._pending_import_ppt_path = None
        self._set_active_background_worker(None)
        self._clear_task_progress()
        QMessageBox.critical(self, "导入 PPT 失败", message)
        self.statusBar().showMessage("导入 PPT 失败。", 8000)

    def _cleanup_import_job(self) -> None:
        self.import_thread = None
        self.import_worker = None
        self._pending_import_project = None
        self._pending_import_ppt_path = None

    def open_project(self) -> None:
        if not self._confirm_can_replace_project():
            return
        selected_path, _ = QFileDialog.getOpenFileName(
            self,
            "打开项目",
            "",
            "片语项目 (*.vcutproj)",
        )
        if not selected_path:
            return
        project_path = Path(selected_path)
        try:
            project = self.context.project_store.load(project_path)
        except Exception as exc:
            QMessageBox.critical(self, "打开项目失败", str(exc))
            return
        if project.project_kind != "ppt":
            QMessageBox.information(self, "项目类型不匹配", "当前窗口只支持 PPT 项目。")
            return

        previews_rebuilt = self._ensure_project_previews(project)
        self.context.set_project(project, project_path)
        if previews_rebuilt:
            self.context.mark_project_dirty()
        self.refresh_views()
        self.statusBar().showMessage(f"已打开项目：{project_path.name}", 6000)

    def _ensure_project_previews(self, project: Project) -> bool:
        if not project.source_ppt_path:
            return False
        if project.slides and all(
            slide.preview_image_path and Path(slide.preview_image_path).exists()
            for slide in project.slides
        ):
            return False

        preview_dir = Path(project.rendered_slides_dir or "").expanduser()
        if not str(preview_dir).strip():
            preview_dir = (
                Path(self.context.settings.workspace.workspace_dir)
                / "ppt_previews"
                / f"{sanitize_filename(project.name)}_{project.project_id[:8]}"
            )
        try:
            import_result = import_presentation(project.source_ppt_path, preview_dir)
        except Exception:
            return False

        previous_slides = {slide.slide_index: slide for slide in project.slides}
        merged_slides: list[SlidePage] = []
        for imported_slide in import_result.slides:
            previous_slide = previous_slides.get(imported_slide.slide_index)
            if previous_slide is not None:
                imported_slide.slide_id = previous_slide.slide_id or imported_slide.slide_id
                imported_slide.zh_script = previous_slide.zh_script
                imported_slide.en_script = previous_slide.en_script
                imported_slide.estimated_duration_ms = (
                    previous_slide.estimated_duration_ms or imported_slide.estimated_duration_ms
                )
                imported_slide.actual_tts_duration_ms = previous_slide.actual_tts_duration_ms
                imported_slide.translation_status = previous_slide.translation_status
                imported_slide.tts_status = previous_slide.tts_status
                imported_slide.voice_id = previous_slide.voice_id
                imported_slide.tts_audio_path = previous_slide.tts_audio_path
            merged_slides.append(imported_slide)

        project.rendered_slides_dir = str(import_result.rendered_slides_dir)
        project.deck_summary = project.deck_summary or import_result.deck_summary
        project.slides = merged_slides
        return True

    def save_project(self) -> bool:
        if self.context.current_project_path is None:
            return self.save_project_as()
        return self._save_project_to_path(self.context.current_project_path)

    def save_project_as(self) -> bool:
        default_path = self.context.project_store.default_path(
            self.context.settings.workspace.workspace_dir,
            self.context.current_project.name or "presentation",
        )
        selected_path, _ = QFileDialog.getSaveFileName(
            self,
            "项目另存为",
            str(self.context.current_project_path or default_path),
            "片语项目 (*.vcutproj)",
        )
        if not selected_path:
            return False
        project_path = Path(selected_path)
        if project_path.suffix.lower() != self.context.project_store.extension:
            project_path = project_path.with_suffix(self.context.project_store.extension)
        return self._save_project_to_path(project_path)

    def _save_project_to_path(self, project_path: Path) -> bool:
        try:
            saved_path = self.context.project_store.save(self.context.current_project, project_path)
        except Exception as exc:
            QMessageBox.critical(self, "保存项目失败", str(exc))
            return False
        self.context.mark_project_saved(saved_path)
        self._update_window_title()
        self.statusBar().showMessage(f"项目已保存：{saved_path}", 6000)
        return True

    def show_settings_dialog(self) -> None:
        preview_image_path = ""
        current_slide = self.workspace.current_slide()
        if current_slide is not None:
            preview_image_path = current_slide.preview_image_path
        elif self.context.current_project.slides:
            preview_image_path = self.context.current_project.slides[0].preview_image_path
        dialog = PptSettingsDialog(self.context.settings, self, preview_image_path=preview_image_path)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        dialog.apply_to_settings(self.context.settings)
        self.context.settings_store.save(self.context.settings)
        self._apply_theme()
        self.statusBar().showMessage("设置已保存。", 5000)

    def update_slide_chinese_script(self, slide_index: int, text: str) -> None:
        slide = self._slide_by_index(slide_index)
        if slide is None:
            return
        if slide.zh_script == text:
            return
        slide.zh_script = text
        slide.translation_status = "needs_review" if slide.en_script.strip() else "pending"
        self._mark_project_dirty()

    def update_slide_english_script(self, slide_index: int, text: str) -> None:
        slide = self._slide_by_index(slide_index)
        if slide is None:
            return
        if slide.en_script == text:
            return
        slide.en_script = text
        slide.translation_status = "manual" if text.strip() else "pending"
        self._invalidate_slide_audio(slide)
        self._mark_project_dirty()

    def start_chinese_voice_input(self) -> None:
        if self.voice_recording_active:
            return
        if self._background_job_in_progress():
            QMessageBox.information(self, "后台任务进行中", "请先等待当前任务完成，再使用语音输入。")
            return
        slide = self.workspace.current_slide()
        if slide is None:
            QMessageBox.information(self, "没有选中页面", "请先选择一个 PPT 页面，再按住话筒输入。")
            return
        if self._voice_recorder is None:
            QMessageBox.critical(self, "语音输入不可用", "当前环境没有成功初始化录音器。")
            return

        output_path = self._voice_input_output_dir() / f"slide_{slide.slide_index:03d}_{uuid4().hex[:8]}.wav"
        self._voice_input_target_slide_index = slide.slide_index
        self._voice_input_pending_output_path = output_path
        self._voice_input_should_transcribe = True
        self.voice_recording_active = True
        self.workspace.set_chinese_voice_input_state(
            recording=True,
            message=f"正在为第 {slide.slide_index} 页录音，松开后自动转文字。",
        )
        self._refresh_interactive_state()
        self.statusBar().showMessage("正在录音，松开后自动转成中文。", 0)

        self._voice_recorder.setOutputLocation(QUrl.fromLocalFile(str(output_path)))
        self._voice_recorder.record()

    def stop_chinese_voice_input(self) -> None:
        if not self.voice_recording_active or self._voice_recorder is None:
            return
        self.voice_recording_active = False
        self.workspace.set_chinese_voice_input_state(message="正在结束录音...")
        self._refresh_interactive_state()
        self._voice_recorder.stop()

    def _on_voice_recorder_state_changed(self, state: QMediaRecorder.RecorderState) -> None:
        if state == QMediaRecorder.RecorderState.RecordingState:
            return
        if state != QMediaRecorder.RecorderState.StoppedState:
            return
        if not self._voice_input_should_transcribe:
            self.workspace.set_chinese_voice_input_state()
            self._cleanup_voice_input_file()
            self._refresh_interactive_state()
            return

        self._voice_input_should_transcribe = False
        audio_path = self._voice_input_pending_output_path
        if audio_path is None or not audio_path.exists() or audio_path.stat().st_size <= 0:
            self.workspace.set_chinese_voice_input_state(message="录音太短或没有录到声音，请再试一次。")
            self._cleanup_voice_input_file()
            self._refresh_interactive_state()
            self.statusBar().showMessage("录音失败或内容过短。", 6000)
            return
        self._start_voice_input_transcription(audio_path)

    def _on_voice_recorder_error(self) -> None:
        self.voice_recording_active = False
        self._voice_input_should_transcribe = False
        self.workspace.set_chinese_voice_input_state(message="录音失败，请检查麦克风权限或设备。")
        self._refresh_interactive_state()
        error_text = ""
        if self._voice_recorder is not None:
            error_text = self._voice_recorder.errorString().strip()
        if not error_text:
            error_text = "录音失败，请检查麦克风权限或设备。"
        self._cleanup_voice_input_file()
        QMessageBox.critical(self, "语音输入失败", error_text)

    def _start_voice_input_transcription(self, audio_path: Path) -> None:
        worker = PptVoiceInputWorker(audio_path, self.context.settings.asr)
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._on_voice_input_progress)
        worker.progress_value.connect(self._on_voice_input_progress_value)
        worker.finished.connect(self._on_voice_input_finished)
        worker.error.connect(self._on_voice_input_error)
        worker.finished.connect(thread.quit)
        worker.error.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        worker.error.connect(worker.deleteLater)
        thread.finished.connect(self._cleanup_voice_input_job)
        thread.finished.connect(thread.deleteLater)

        self.voice_input_busy = True
        self.voice_input_thread = thread
        self.voice_input_worker = worker
        self._set_active_background_worker(worker)
        self.workspace.set_chinese_voice_input_state(
            transcribing=True,
            message="正在把语音转成中文，请稍候。",
        )
        self.statusBar().showMessage("正在识别语音并写入中文稿。", 0)
        thread.start()

    def _on_voice_input_progress(self, message: str) -> None:
        self.statusBar().showMessage(message, 0)

    def _on_voice_input_progress_value(self, progress: int, message: str) -> None:
        self._set_task_progress(progress, message)

    def _on_voice_input_finished(self, result: PptVoiceInputResult) -> None:
        self.voice_input_busy = False
        self._set_active_background_worker(None)

        slide = self._slide_by_index(self._voice_input_target_slide_index)
        if slide is not None:
            if self.workspace.selected_slide_index() == slide.slide_index:
                self.workspace.insert_chinese_script_text(result.text)
            else:
                slide.zh_script = _merge_script_text(slide.zh_script, result.text)
                slide.translation_status = "needs_review" if slide.en_script.strip() else "pending"
                self._mark_project_dirty()

        self.workspace.set_chinese_voice_input_state(message="识别完成，已写入中文稿。")
        self._clear_task_progress()
        self._cleanup_voice_input_file()
        self.statusBar().showMessage("语音输入完成。", 6000)

    def _on_voice_input_error(self, message: str) -> None:
        self.voice_input_busy = False
        self._set_active_background_worker(None)
        self.workspace.set_chinese_voice_input_state(message="语音识别失败，请再试一次。")
        self._refresh_interactive_state()
        self._clear_task_progress()
        self._cleanup_voice_input_file()
        QMessageBox.critical(self, "语音识别失败", message)
        self.statusBar().showMessage("语音识别失败。", 8000)

    def _cleanup_voice_input_job(self) -> None:
        self.voice_input_thread = None
        self.voice_input_worker = None

    def _cleanup_voice_input_file(self) -> None:
        audio_path = self._voice_input_pending_output_path
        self._voice_input_pending_output_path = None
        if audio_path is None:
            return
        try:
            audio_path.unlink(missing_ok=True)
        except Exception:
            pass

    def generate_all_scripts(self) -> None:
        slide_indices = [slide.slide_index for slide in self.context.current_project.slides]
        if not self._confirm_replace_existing_content(
            slide_indices,
            field_name="zh_script",
            content_label="中文口播稿",
        ):
            return
        self._start_generation_job(slide_indices, "正在生成整套中文口播稿...")

    def generate_current_script(self) -> None:
        slide = self.workspace.current_slide()
        if slide is None:
            QMessageBox.information(self, "没有选中页面", "请先选中一个 PPT 页面。")
            return
        if not self._confirm_replace_existing_content(
            [slide.slide_index],
            field_name="zh_script",
            content_label="中文口播稿",
        ):
            return
        self._start_generation_job([slide.slide_index], f"正在生成第 {slide.slide_index} 页中文口播稿...")

    def translate_all_scripts(self) -> None:
        slide_indices = [
            slide.slide_index
            for slide in self.context.current_project.slides
            if slide.zh_script.strip()
        ]
        if not self._confirm_replace_existing_content(
            slide_indices,
            field_name="en_script",
            content_label="英文口播稿",
        ):
            return
        self._start_translation_job(slide_indices, "正在翻译整套英文口播稿...")

    def translate_current_script(self) -> None:
        slide = self.workspace.current_slide()
        if slide is None:
            QMessageBox.information(self, "没有选中页面", "请先选中一个 PPT 页面。")
            return
        if not self._confirm_replace_existing_content(
            [slide.slide_index],
            field_name="en_script",
            content_label="英文口播稿",
        ):
            return
        self._start_translation_job([slide.slide_index], f"正在翻译第 {slide.slide_index} 页英文口播稿...")

    def _start_generation_job(self, slide_indices: list[int], queued_message: str) -> None:
        if self._background_job_in_progress():
            QMessageBox.information(self, "后台任务进行中", "请等当前后台任务结束后再继续。")
            return
        if not self.context.current_project.slides:
            QMessageBox.information(self, "没有可生成的内容", "请先导入 PPT。")
            return
        if not slide_indices:
            QMessageBox.information(self, "没有可生成的页面", "当前没有可生成中文稿的页面。")
            return
        if not self._ensure_translation_configured():
            return

        worker = PptScriptGenerationWorker(self.context.current_project, self.context.settings, slide_indices)
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._on_translation_progress)
        worker.progress_value.connect(self._on_translation_progress_value)
        worker.finished.connect(self._on_generation_finished)
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

        self.translation_busy = True
        self.translation_thread = thread
        self.translation_worker = worker
        self._set_active_background_worker(worker)
        self._show_task_started(queued_message)
        thread.start()

    def _start_translation_job(self, slide_indices: list[int], queued_message: str) -> None:
        if self._background_job_in_progress():
            QMessageBox.information(self, "后台任务进行中", "请等当前后台任务结束后再继续。")
            return
        if not slide_indices:
            QMessageBox.information(self, "没有可翻译的页面", "请先生成或填写中文口播稿。")
            return
        if not self._ensure_translation_configured():
            return

        worker = PptScriptTranslationWorker(self.context.current_project, self.context.settings, slide_indices)
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._on_translation_progress)
        worker.progress_value.connect(self._on_translation_progress_value)
        worker.item_translated.connect(self._on_translation_item_translated)
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

        self.translation_busy = True
        self.translation_thread = thread
        self.translation_worker = worker
        self._set_active_background_worker(worker)
        self._show_task_started(queued_message)
        thread.start()

    def show_export_dialog(self) -> None:
        if self._background_job_in_progress():
            QMessageBox.information(self, "后台任务进行中", "请等待当前后台任务结束后再继续。")
            return
        if not self._ensure_export_ready():
            return
        dialog = PptExportDialog(self.context.settings, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        options = dialog.export_options()
        voiceover_language = resolve_ppt_voiceover_language(
            self.context.current_project,
            str(options.get("voiceover_language") or "en").strip().lower(),
        )
        options["voiceover_language"] = voiceover_language
        options["subtitle_mode"] = resolve_ppt_subtitle_mode(
            self.context.current_project,
            str(options.get("subtitle_mode") or "bilingual"),
        )
        options["selected_provider_type"] = str(
            options.get("selected_provider_type")
            or self.context.settings.tts.provider_type_for_language(voiceover_language)
            or ""
        )
        options["selected_voice_id"] = str(
            options.get("selected_voice_id")
            or self.context.settings.tts.default_voice_for_language(voiceover_language)
            or ""
        )
        self.context.settings.media.default_output_dir = str(
            options["output_dir"] or self.context.settings.media.default_output_dir
        )
        self.context.settings.export.output_container = str(options["output_container"])
        self.context.settings.export.subtitle_mode = str(options["subtitle_mode"])
        self.context.settings.export.burn_subtitles = bool(options["burn_subtitles"])
        self.context.settings.export.export_sidecar_srt = bool(options["export_sidecar_srt"])
        options["prepare_scripts"] = False
        self.context.settings_store.save(self.context.settings)
        self._start_export_job(options)

    def show_guided_export_dialog(self) -> None:
        if not self._ensure_guided_export_ready():
            return
        preview_image_path = ""
        current_slide = self.workspace.current_slide()
        if current_slide is not None:
            preview_image_path = current_slide.preview_image_path
        elif self.context.current_project.slides:
            preview_image_path = self.context.current_project.slides[0].preview_image_path
        dialog = PptGuidedExportDialog(
            self.context.settings,
            self,
            preview_image_path=preview_image_path,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        options = dialog.export_options()
        voiceover_language = str(options["voiceover_language"] or "zh")
        self.context.settings.media.default_output_dir = str(
            options["output_dir"] or self.context.settings.media.default_output_dir
        )
        self.context.settings.tts.set_provider_type_for_language(
            voiceover_language,
            str(options["selected_provider_type"] or ""),
        )
        self.context.settings.tts.set_default_voice_for_language(
            voiceover_language,
            str(options["selected_voice_id"] or ""),
        )
        self.context.settings.export.output_container = str(options["output_container"])
        self.context.settings.export.subtitle_mode = str(options["subtitle_mode"])
        self.context.settings.export.burn_subtitles = bool(options["burn_subtitles"])
        self.context.settings.export.export_sidecar_srt = bool(options["export_sidecar_srt"])
        self.context.settings.export.subtitle_font_size = int(options["subtitle_font_size"])
        self.context.settings.export.subtitle_english_color = str(options["subtitle_english_color"])
        self.context.settings.export.subtitle_chinese_color = str(options["subtitle_chinese_color"])
        self.context.settings.export.subtitle_safe_area_enabled = bool(options["subtitle_safe_area_enabled"])
        self.context.settings.export.subtitle_english_x_percent = float(options["subtitle_english_x_percent"])
        self.context.settings.export.subtitle_english_y_percent = float(options["subtitle_english_y_percent"])
        self.context.settings.export.subtitle_chinese_x_percent = float(options["subtitle_chinese_x_percent"])
        self.context.settings.export.subtitle_chinese_y_percent = float(options["subtitle_chinese_y_percent"])
        self.context.settings_store.save(self.context.settings)
        self._start_guided_export_pipeline(options)

    def _start_export_job(self, options: dict[str, object]) -> None:
        prepare_scripts = bool(options.get("prepare_scripts", False))
        voiceover_language = resolve_ppt_voiceover_language(
            self.context.current_project,
            str(options.get("voiceover_language") or "en").strip().lower(),
        )
        subtitle_mode = resolve_ppt_subtitle_mode(
            self.context.current_project,
            str(options.get("subtitle_mode") or "bilingual"),
        )
        selected_provider_type = str(
            options.get("selected_provider_type")
            or self.context.settings.tts.provider_type_for_language(voiceover_language)
            or ""
        )
        selected_voice_id = str(
            options.get("selected_voice_id")
            or self.context.settings.tts.default_voice_for_language(voiceover_language)
            or ""
        )
        if not prepare_scripts:
            worker = PptExportProcessController(
                project=self.context.current_project,
                settings=self.context.settings,
                output_dir=str(options["output_dir"] or self.context.settings.media.default_output_dir),
                output_container=str(options["output_container"] or "mp4"),
                burn_subtitles=bool(options["burn_subtitles"]),
                export_sidecar_srt=bool(options["export_sidecar_srt"]),
                subtitle_mode=subtitle_mode,
                voiceover_language=voiceover_language,
                selected_provider_type=selected_provider_type,
                selected_voice_id=selected_voice_id,
                parent=self,
            )
            worker.progress.connect(self._on_export_progress)
            worker.progress_value.connect(self._on_export_progress_value)
            worker.finished.connect(self._on_export_finished)
            worker.cancelled.connect(self._on_export_cancelled)
            worker.error.connect(self._on_export_error)
            worker.finished.connect(lambda _result: self._cleanup_export_job())
            worker.cancelled.connect(lambda _message: self._cleanup_export_job())
            worker.error.connect(lambda _message: self._cleanup_export_job())
            worker.finished.connect(worker.deleteLater)
            worker.cancelled.connect(worker.deleteLater)
            worker.error.connect(worker.deleteLater)

            self.export_busy = True
            self.export_thread = None
            self.export_worker = worker
            self._set_active_background_worker(worker)
            self._show_task_started("正在启动 PPT 口播导出任务...")
            worker.start()
            return

        worker = PptExportWorker(
            project=self.context.current_project,
            settings=self.context.settings,
            output_dir=str(options["output_dir"] or self.context.settings.media.default_output_dir),
            output_container=str(options["output_container"] or "mp4"),
            burn_subtitles=bool(options["burn_subtitles"]),
            export_sidecar_srt=bool(options["export_sidecar_srt"]),
            subtitle_mode=subtitle_mode,
            voiceover_language=voiceover_language,
            selected_provider_type=selected_provider_type,
            selected_voice_id=selected_voice_id,
            prepare_scripts=prepare_scripts,
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

        self.export_busy = True
        self.export_thread = thread
        self.export_worker = worker
        self._set_active_background_worker(worker)
        self._show_task_started("正在启动 PPT 口播导出任务...")
        thread.start()

    def _on_translation_progress(self, message: str) -> None:
        self.statusBar().showMessage(message, 0)

    def _on_translation_progress_value(self, progress: int, message: str) -> None:
        self._set_task_progress(progress, message)

    def _on_generation_finished(self, result: PptScriptDraftJobResult) -> None:
        project = self.context.current_project
        project.deck_summary = result.deck_summary.strip()
        for item in result.items:
            slide = self._slide_by_index(item.slide_index)
            if slide is None:
                continue
            slide.zh_script = item.zh_script
            slide.estimated_duration_ms = item.estimated_duration_ms
            slide.translation_status = "pending"
        self.translation_busy = False
        self._set_active_background_worker(None)
        self._mark_project_dirty()
        self.refresh_views()
        self._clear_task_progress()
        self.statusBar().showMessage(f"中文口播稿已生成，共更新 {len(result.items)} 页。", 8000)
        if self._guided_export_pending_options is not None:
            QTimer.singleShot(0, self._continue_guided_export_pipeline)

    def _on_translation_item_translated(self, item: PptScriptTranslationItem) -> None:
        slide = self._slide_by_index(item.slide_index)
        if slide is None:
            return
        slide.en_script = item.en_script
        slide.translation_status = "translated"
        self._invalidate_slide_audio(slide)
        self.workspace.update_slide_scripts(
            item.slide_index,
            en_script=item.en_script,
            translation_status="translated",
            refresh_summary=False,
        )
        if not self.context.project_dirty:
            self._mark_project_dirty()
        self._schedule_project_autosave()

    def _on_translation_finished(self, result: PptScriptTranslationJobResult) -> None:
        project = self.context.current_project
        if result.deck_summary.strip():
            project.deck_summary = result.deck_summary.strip()
        for item in result.items:
            slide = self._slide_by_index(item.slide_index)
            if slide is None:
                continue
            slide.en_script = item.en_script
            slide.translation_status = "translated"
            self._invalidate_slide_audio(slide)
        self.translation_busy = False
        self._set_active_background_worker(None)
        self._mark_project_dirty()
        self._autosave_project_silently()
        self.refresh_views()
        self._clear_task_progress()
        self.statusBar().showMessage(f"英文口播稿已生成，共更新 {len(result.items)} 页。", 8000)
        if self._guided_export_pending_options is not None:
            QTimer.singleShot(0, self._continue_guided_export_pipeline)

    def _on_translation_error(self, message: str) -> None:
        self._cancel_guided_export_pipeline()
        self.translation_busy = False
        self._set_active_background_worker(None)
        self._refresh_interactive_state()
        self._clear_task_progress()
        QMessageBox.critical(self, "口播稿任务失败", message)
        self.statusBar().showMessage("口播稿任务失败。", 8000)

    def _on_translation_cancelled(self, message: str) -> None:
        self._cancel_guided_export_pipeline()
        self.translation_busy = False
        self._set_active_background_worker(None)
        self._refresh_interactive_state()
        self._clear_task_progress()
        self.statusBar().showMessage(message or "已终止当前口播稿任务。", 8000)

    def _cleanup_translation_job(self) -> None:
        self.translation_thread = None
        self.translation_worker = None

    def _on_export_progress(self, message: str) -> None:
        self.statusBar().showMessage(message, 0)

    def _on_export_progress_value(self, progress: int, message: str) -> None:
        self._set_task_progress(progress, message)

    def _on_export_finished(self, result: PptExportResult) -> None:
        self._cancel_guided_export_pipeline()
        self.export_busy = False
        self._set_active_background_worker(None)
        self._apply_export_project_snapshot(result.project_snapshot)
        self.context.current_project.exports.append(
            ExportJob(
                export_id=uuid4().hex,
                export_type="ppt_voiceover",
                output_path=str(result.plan.output_path),
                status="completed",
            )
        )
        self._mark_project_dirty()
        self._autosave_project_silently()
        self.refresh_views()
        self._clear_task_progress()
        QMessageBox.information(
            self,
            "导出完成",
            f"视频已导出完成。\n\n输出文件：\n{result.plan.output_path}",
        )
        self.statusBar().showMessage(f"导出完成：{result.plan.output_path}", 10000)

    def _on_export_error(self, message: str) -> None:
        self._cancel_guided_export_pipeline()
        self.export_busy = False
        self._set_active_background_worker(None)
        self._refresh_interactive_state()
        self._clear_task_progress()
        QMessageBox.critical(self, "导出失败", message)
        self.statusBar().showMessage("导出失败。", 8000)

    def _on_export_cancelled(self, message: str) -> None:
        self._cancel_guided_export_pipeline()
        self.export_busy = False
        self._set_active_background_worker(None)
        self._refresh_interactive_state()
        self._clear_task_progress()
        self.statusBar().showMessage(message or "已终止导出任务。", 8000)

    def _cleanup_export_job(self) -> None:
        self.export_thread = None
        self.export_worker = None

    def pause_active_task(self) -> None:
        if self._active_background_worker is None:
            return
        try:
            self._active_background_worker.request_pause()
        except Exception as exc:
            QMessageBox.warning(self, "暂停失败", str(exc))
            return
        self.statusBar().showMessage("后台任务已暂停。", 5000)
        self._refresh_interactive_state()

    def resume_active_task(self) -> None:
        if self._active_background_worker is None:
            return
        try:
            self._active_background_worker.request_resume()
        except Exception as exc:
            QMessageBox.warning(self, "继续失败", str(exc))
            return
        self.statusBar().showMessage("后台任务已继续。", 5000)
        self._refresh_interactive_state()

    def cancel_active_task(self) -> None:
        if self._active_background_worker is None:
            return
        try:
            self._active_background_worker.request_cancel()
        except Exception as exc:
            QMessageBox.warning(self, "终止失败", str(exc))
            return
        self.statusBar().showMessage("已请求终止后台任务。", 5000)
        self._refresh_interactive_state()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.voice_recording_active:
            QMessageBox.information(self, "正在录音", "请先松开话筒，结束当前录音后再关闭程序。")
            event.ignore()
            return
        if self._background_job_in_progress():
            QMessageBox.information(self, "后台任务进行中", "请等当前后台任务结束后再关闭程序。")
            event.ignore()
            return
        if not self._confirm_can_replace_project():
            event.ignore()
            return
        super().closeEvent(event)
