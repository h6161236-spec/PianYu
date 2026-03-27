from __future__ import annotations

import subprocess
import sys
from typing import Any


def subprocess_windowless_kwargs() -> dict[str, Any]:
    if sys.platform != "win32":
        return {}

    kwargs: dict[str, Any] = {}

    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = 0
    kwargs["startupinfo"] = startupinfo

    create_no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if create_no_window:
        kwargs["creationflags"] = create_no_window

    return kwargs
