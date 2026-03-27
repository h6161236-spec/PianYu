# -*- mode: python ; coding: utf-8 -*-
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules


project_root = Path(SPECPATH).resolve().parent
src_root = project_root / "src"

datas = [(str(project_root / "README.md"), ".")]
docs_root = project_root / "docs"
if docs_root.exists():
    for doc_path in docs_root.glob("*.md"):
        datas.append((str(doc_path), "docs"))

runtime_icon = os.environ.get("VCUT_RUNTIME_ICON", "").strip()
if runtime_icon:
    runtime_icon_path = Path(runtime_icon)
    if runtime_icon_path.exists():
        datas.append((str(runtime_icon_path), "."))

binaries: list[tuple[str, str]] = []
hiddenimports: list[str] = []


def _extend_for_package(package_name: str) -> None:
    if importlib.util.find_spec(package_name) is None:
        return
    hiddenimports.extend(collect_submodules(package_name))
    datas.extend(collect_data_files(package_name))
    binaries.extend(collect_dynamic_libs(package_name))


for package_name in (
    "faster_whisper",
    "ctranslate2",
    "tokenizers",
    "sentencepiece",
    "av",
):
    _extend_for_package(package_name)

ffmpeg_dir = os.environ.get("VCUT_BUNDLE_FFMPEG_DIR", "").strip()
if ffmpeg_dir:
    ffmpeg_root = Path(ffmpeg_dir)
    for binary_name in ("ffmpeg.exe", "ffprobe.exe"):
        binary_path = ffmpeg_root / binary_name
        if binary_path.exists():
            binaries.append((str(binary_path), "."))

datas = list(dict.fromkeys(datas))
binaries = list(dict.fromkeys(binaries))
hiddenimports = sorted(set(hiddenimports))


a = Analysis(
    [str(src_root / "vcut_studio_bootstrap.py")],
    pathex=[str(src_root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="VCutStudio",
    icon=os.environ.get("VCUT_APP_ICON", None) or None,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="VCutStudio",
)
