from __future__ import annotations

import ctypes
import sys

from PySide6.QtCore import QLibraryInfo, QLocale, QTranslator
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from .context import AppContext
from .project_store import ProjectStore
from .runtime_assets import find_app_icon
from .settings import SettingsStore
from .ui.ppt_main_window import PptMainWindow
from .ui.theme import apply_application_theme


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
    context = create_context()
    app = QApplication(sys.argv)
    _install_qt_translations(app)
    app.setStyle("Fusion")
    app.setApplicationName("片语")
    app.setOrganizationName("片语")
    apply_application_theme(app, context.settings.appearance.theme_mode)
    icon_path = find_app_icon()
    if icon_path is not None:
        app.setWindowIcon(QIcon(str(icon_path)))
    window = PptMainWindow(context)
    window.show()
    return app.exec()
