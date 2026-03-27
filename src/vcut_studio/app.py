from __future__ import annotations

import ctypes
import sys

from PySide6.QtCore import QLibraryInfo, QLocale, QTranslator
from PySide6.QtGui import QColor, QIcon, QPalette
from PySide6.QtWidgets import QApplication

from .context import AppContext
from .project_store import ProjectStore
from .runtime_assets import find_app_icon
from .settings import SettingsStore
from .ui.main_window import MainWindow


def _set_windows_app_id() -> None:
    if sys.platform != "win32":
        return
    app_id = "VCutStudio.Desktop"
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)


def create_context() -> AppContext:
    settings_store = SettingsStore()
    settings = settings_store.load()
    project_store = ProjectStore()
    return AppContext(settings_store=settings_store, project_store=project_store, settings=settings)


def _install_qt_translations(app: QApplication) -> None:
    translations_path = QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
    locale = QLocale(QLocale.Language.Chinese, QLocale.Country.China)
    QLocale.setDefault(locale)

    installed_translators: list[QTranslator] = []
    for base_name in ("qtbase", "qt"):
        translator = QTranslator(app)
        if translator.load(locale, base_name, "_", translations_path):
            app.installTranslator(translator)
            installed_translators.append(translator)

    # Keep strong references on the application object so translators stay alive.
    app._qt_translators = installed_translators  # type: ignore[attr-defined]


def run() -> int:
    _set_windows_app_id()
    app = QApplication(sys.argv)
    _install_qt_translations(app)
    app.setStyle("Fusion")
    app.setApplicationName("VCut Studio")
    app.setOrganizationName("VCut Studio")
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor("#161a1f"))
    palette.setColor(QPalette.ColorRole.WindowText, QColor("#e5e7eb"))
    palette.setColor(QPalette.ColorRole.Base, QColor("#11151b"))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor("#18202a"))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor("#11151b"))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor("#f8fafc"))
    palette.setColor(QPalette.ColorRole.Text, QColor("#e5e7eb"))
    palette.setColor(QPalette.ColorRole.Button, QColor("#273445"))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor("#f3f4f6"))
    palette.setColor(QPalette.ColorRole.BrightText, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.Link, QColor("#38bdf8"))
    palette.setColor(QPalette.ColorRole.Highlight, QColor("#0f766e"))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.PlaceholderText, QColor("#7c8798"))
    app.setPalette(palette)
    icon_path = find_app_icon()
    if icon_path is not None:
        app.setWindowIcon(QIcon(str(icon_path)))
    context = create_context()
    window = MainWindow(context)
    window.show()
    return app.exec()
