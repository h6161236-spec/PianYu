from __future__ import annotations

import sys

from vcut_studio.main import main
from vcut_studio.ppt_export_runner import main as ppt_export_main


def _dispatch() -> int:
    args = sys.argv[1:]
    if args and args[0] == "--ppt-export-runner":
        sys.argv = [sys.argv[0], *args[1:]]
        return ppt_export_main()
    if len(args) >= 2 and args[0] == "-m" and args[1] == "vcut_studio.ppt_export_runner":
        sys.argv = [sys.argv[0], *args[2:]]
        return ppt_export_main()
    return main()


raise SystemExit(_dispatch())
