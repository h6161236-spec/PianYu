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
    below_normal_priority = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
    creationflags = 0
    if create_no_window:
        creationflags |= create_no_window
    if below_normal_priority:
        creationflags |= below_normal_priority
    if creationflags:
        kwargs["creationflags"] = creationflags

    return kwargs
