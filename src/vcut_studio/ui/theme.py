from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QGuiApplication, QPalette
from PySide6.QtWidgets import QApplication


THEME_SYSTEM = "system"
THEME_DARK = "dark"
THEME_LIGHT = "light"


@dataclass(frozen=True, slots=True)
class ThemeColors:
    mode: str
    window: str
    window_text: str
    base: str
    alternate_base: str
    tooltip_base: str
    tooltip_text: str
    text: str
    button: str
    button_hover: str
    button_disabled: str
    button_text: str
    bright_text: str
    link: str
    highlight: str
    highlighted_text: str
    placeholder_text: str
    border: str
    panel: str
    panel_alt: str
    muted_text: str
    scrollbar_handle: str
    selection_hover: str
    progress_chunk: str
    video_surface: str


_DARK_THEME = ThemeColors(
    mode=THEME_DARK,
    window="#161a1f",
    window_text="#e5e7eb",
    base="#11151b",
    alternate_base="#18202a",
    tooltip_base="#11151b",
    tooltip_text="#f8fafc",
    text="#e5e7eb",
    button="#273445",
    button_hover="#304259",
    button_disabled="#202734",
    button_text="#f3f4f6",
    bright_text="#ffffff",
    link="#38bdf8",
    highlight="#0f766e",
    highlighted_text="#ffffff",
    placeholder_text="#7c8798",
    border="#2f3946",
    panel="#1c222b",
    panel_alt="#202734",
    muted_text="#94a3b8",
    scrollbar_handle="#334155",
    selection_hover="#243140",
    progress_chunk="#0ea5a4",
    video_surface="#090c10",
)

_LIGHT_THEME = ThemeColors(
    mode=THEME_LIGHT,
    window="#f3f6fb",
    window_text="#0f172a",
    base="#ffffff",
    alternate_base="#eef2f8",
    tooltip_base="#ffffff",
    tooltip_text="#0f172a",
    text="#0f172a",
    button="#e2e8f0",
    button_hover="#d8e0ea",
    button_disabled="#e5e7eb",
    button_text="#0f172a",
    bright_text="#ffffff",
    link="#2563eb",
    highlight="#0f766e",
    highlighted_text="#ffffff",
    placeholder_text="#64748b",
    border="#cbd5e1",
    panel="#ffffff",
    panel_alt="#f8fafc",
    muted_text="#475569",
    scrollbar_handle="#94a3b8",
    selection_hover="#dbe7f5",
    progress_chunk="#0f766e",
    video_surface="#dbe3ef",
)


def theme_mode_choices() -> list[tuple[str, str]]:
    return [
        ("跟随系统", THEME_SYSTEM),
        ("深色", THEME_DARK),
        ("浅色", THEME_LIGHT),
    ]


def resolve_theme_mode(theme_mode: str, app: QApplication | None = None) -> str:
    normalized = str(theme_mode or THEME_SYSTEM).strip().lower()
    if normalized in {THEME_DARK, THEME_LIGHT}:
        return normalized

    qt_app = app or QApplication.instance()
    if qt_app is not None:
        try:
            color_scheme = qt_app.styleHints().colorScheme()
            if color_scheme == Qt.ColorScheme.Light:
                return THEME_LIGHT
        except Exception:
            pass
    return THEME_DARK


def theme_colors(theme_mode: str, app: QApplication | None = None) -> ThemeColors:
    resolved = resolve_theme_mode(theme_mode, app=app)
    return _LIGHT_THEME if resolved == THEME_LIGHT else _DARK_THEME


def build_palette(colors: ThemeColors) -> QPalette:
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(colors.window))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(colors.window_text))
    palette.setColor(QPalette.ColorRole.Base, QColor(colors.base))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(colors.alternate_base))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(colors.tooltip_base))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor(colors.tooltip_text))
    palette.setColor(QPalette.ColorRole.Text, QColor(colors.text))
    palette.setColor(QPalette.ColorRole.Button, QColor(colors.button))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(colors.button_text))
    palette.setColor(QPalette.ColorRole.BrightText, QColor(colors.bright_text))
    palette.setColor(QPalette.ColorRole.Link, QColor(colors.link))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(colors.highlight))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(colors.highlighted_text))
    palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(colors.placeholder_text))
    return palette


def apply_application_theme(app: QApplication, theme_mode: str) -> ThemeColors:
    colors = theme_colors(theme_mode, app=app)
    app.setPalette(build_palette(colors))
    app.setProperty("theme_mode", str(theme_mode or THEME_SYSTEM).strip().lower())
    app.setProperty("resolved_theme_mode", colors.mode)
    return colors


def _stylesheet(colors: ThemeColors) -> str:
    return f"""
        QMainWindow, QDialog, QMessageBox {{
            background: {colors.window};
            color: {colors.text};
        }}
        QWidget {{
            color: {colors.text};
        }}
        QStackedWidget, QStackedWidget > QWidget,
        QWidget#qt_scrollarea_viewport, QDialogButtonBox {{
            background: transparent;
        }}
        QLabel {{
            color: {colors.text};
        }}
        QLabel#pptPreviewLabel {{
            background: {colors.video_surface};
            border-radius: 10px;
            border: 1px solid {colors.border};
        }}
        QMenuBar, QMenu, QStatusBar, QToolTip {{
            background: {colors.base};
            color: {colors.text};
            border: 1px solid {colors.border};
        }}
        QFrame#settingsHeaderCard,
        QFrame#settingsSidebar,
        QFrame#settingsContent,
        QFrame#taskProgressCard {{
            background: {colors.panel};
            border: 1px solid {colors.border};
            border-radius: 12px;
        }}
        QLabel#settingsHeaderTitle,
        QLabel#settingsSectionTitle,
        QLabel#taskProgressTitle {{
            color: {colors.window_text};
            font-size: 18px;
            font-weight: 700;
        }}
        QLabel#settingsHeaderText,
        QLabel#settingsNavHint,
        QLabel#settingsSectionText,
        QLabel#taskProgressHint {{
            color: {colors.muted_text};
        }}
        QLabel#settingsNavTitle,
        QLabel#taskProgressStage {{
            color: {colors.window_text};
            font-weight: 700;
        }}
        QGroupBox {{
            border: 1px solid {colors.border};
            border-radius: 10px;
            margin-top: 12px;
            padding-top: 10px;
            background: {colors.panel};
            font-weight: 600;
        }}
        QGroupBox::title {{
            subcontrol-origin: margin;
            left: 12px;
            padding: 0 6px;
            color: {colors.window_text};
        }}
        QPushButton {{
            background: {colors.button};
            border: 1px solid {colors.border};
            border-radius: 8px;
            padding: 6px 12px;
            color: {colors.button_text};
        }}
        QPushButton:hover {{
            background: {colors.button_hover};
        }}
        QPushButton:disabled {{
            background: {colors.button_disabled};
            color: {colors.placeholder_text};
            border-color: {colors.border};
        }}
        QLineEdit, QPlainTextEdit, QTextEdit, QComboBox, QSpinBox, QDoubleSpinBox {{
            background: {colors.base};
            border: 1px solid {colors.border};
            border-radius: 6px;
            padding: 4px 6px;
            selection-background-color: {colors.highlight};
            selection-color: {colors.highlighted_text};
        }}
        QComboBox::drop-down {{
            border: none;
            width: 22px;
        }}
        QListWidget#settingsNav {{
            background: transparent;
            border: none;
            outline: none;
            padding: 2px 0;
        }}
        QListWidget#settingsNav::item {{
            border-radius: 8px;
            padding: 10px 12px;
            margin: 2px 0;
            color: {colors.muted_text};
        }}
        QListWidget#settingsNav::item:selected {{
            background: {colors.highlight};
            color: {colors.highlighted_text};
        }}
        QListWidget#settingsNav::item:hover:!selected {{
            background: {colors.selection_hover};
            color: {colors.window_text};
        }}
        QAbstractItemView, QListView, QTableWidget, QTableView, QTreeView {{
            background: {colors.base};
            alternate-background-color: {colors.alternate_base};
            border: 1px solid {colors.border};
            gridline-color: {colors.border};
            selection-background-color: {colors.highlight};
            selection-color: {colors.highlighted_text};
        }}
        QHeaderView::section {{
            background: {colors.panel_alt};
            border: none;
            padding: 6px;
        }}
        QScrollArea {{
            border: none;
            background: transparent;
        }}
        QScrollArea > QWidget > QWidget {{
            background: transparent;
        }}
        QScrollBar:vertical, QScrollBar:horizontal {{
            background: {colors.base};
            border: none;
            margin: 0;
        }}
        QScrollBar::handle:vertical, QScrollBar::handle:horizontal {{
            background: {colors.scrollbar_handle};
            border-radius: 6px;
            min-height: 28px;
            min-width: 28px;
        }}
        QScrollBar::add-line, QScrollBar::sub-line,
        QScrollBar::add-page, QScrollBar::sub-page {{
            background: none;
            border: none;
        }}
        QProgressBar {{
            border: 1px solid {colors.border};
            border-radius: 8px;
            text-align: center;
            background: {colors.base};
        }}
        QProgressBar::chunk {{
            background: {colors.progress_chunk};
            border-radius: 6px;
        }}
        QCheckBox::indicator {{
            width: 16px;
            height: 16px;
            border-radius: 4px;
            border: 1px solid {colors.border};
            background: {colors.base};
        }}
        QCheckBox::indicator:checked {{
            background: {colors.highlight};
            border-color: {colors.highlight};
        }}
        QVideoWidget {{
            background: {colors.video_surface};
            border-radius: 10px;
        }}
    """


def build_window_stylesheet(theme_mode: str, app: QApplication | None = None) -> str:
    return _stylesheet(theme_colors(theme_mode, app=app))


def build_dialog_stylesheet(theme_mode: str, app: QApplication | None = None) -> str:
    return _stylesheet(theme_colors(theme_mode, app=app))
